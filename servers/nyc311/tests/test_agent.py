"""Tests for the NYC 311 server: dataset client, skill parsing, protocol, HTTP.

    python3 servers/nyc311/tests/test_agent.py
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import A2AError, A2AHandler, AgentServerApp, TaskStore, UpstreamError, make_handler  # noqa: E402

from agent import NYC311Agent, parse  # noqa: E402
from data import NYC311Client  # noqa: E402

ROW = {
    "unique_key": "12345678",
    "complaint_type": "Street Condition",
    "descriptor": "Pothole",
    "status": "Closed",
    "created_date": "2026-09-01T00:00:00.000",
    "closed_date": "2026-09-10T00:00:00.000",
    "agency_name": "DOT",
    "street_name": "OCEAN AVENUE",
    "incident_zip": "11235",
    "borough": "BROOKLYN",
    "latitude": "40.5",
    "longitude": "-73.9",
    "resolution_description": "Repaired",
    "extra_field": "not returned",
}


class FakeFetch:
    def __init__(self, rows=None, views=None):
        self.rows = rows if rows is not None else [ROW]
        self.views = views if views is not None else {"rowsUpdatedAt": 1790000000}
        self.calls = []

    def __call__(self, url, params, headers):
        self.calls.append((url, dict(params)))
        if "/api/views/" in url:
            return self.views
        where = params.get("$where", "")
        if "unique_key='99999999'" in where:
            return []
        return self.rows


class FakeClient(NYC311Client):
    """Same interface as the real client, no network."""

    def __init__(self, status: str = "Closed"):
        self.status = status
        self.lookups = 0

    def get_complaint(self, unique_key):
        self.lookups += 1
        if str(unique_key) != "12345678":
            return None
        return {**ROW, "status": self.status, "dataset": "erm2-nwe9"}

    def recent_complaints(self, **kwargs):
        return [{**ROW, "dataset": "erm2-nwe9"}]

    def freshness(self):
        return "2026-09-22T00:00:00Z"


def message(text, **extra):
    payload = {
        "kind": "message",
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    payload.update(extra)
    return payload


class DataClientTests(unittest.TestCase):
    def test_get_complaint(self):
        fetch = FakeFetch()
        client = NYC311Client(fetch=fetch)
        row = client.get_complaint("12345678")
        self.assertEqual(row["unique_key"], "12345678")
        self.assertEqual(row["dataset"], "erm2-nwe9")
        self.assertNotIn("extra_field", row)
        self.assertEqual(fetch.calls[0][1]["$where"], "unique_key='12345678'")

    def test_get_complaint_missing_and_invalid(self):
        client = NYC311Client(fetch=FakeFetch())
        self.assertIsNone(client.get_complaint("99999999"))
        with self.assertRaises(ValueError):
            client.get_complaint("abc")

    def test_complaint_cache(self):
        fetch = FakeFetch()
        client = NYC311Client(fetch=fetch)
        client.get_complaint("12345678")
        client.get_complaint("12345678")
        self.assertEqual(len(fetch.calls), 1)

    def test_recent_complaints_query(self):
        fetch = FakeFetch()
        client = NYC311Client(fetch=fetch)
        rows = client.recent_complaints(zip_code="11235", days=14, limit=5)
        self.assertEqual(len(rows), 1)
        where = fetch.calls[0][1]["$where"]
        self.assertIn("incident_zip='11235'", where)
        self.assertIn("created_date >", where)
        self.assertEqual(fetch.calls[0][1]["$order"], "created_date DESC")
        self.assertEqual(fetch.calls[0][1]["$limit"], "5")

    def test_recent_complaints_validation(self):
        client = NYC311Client(fetch=FakeFetch())
        with self.assertRaises(ValueError):
            client.recent_complaints()
        with self.assertRaises(ValueError):
            client.recent_complaints(zip_code="1234")

    def test_street_name_escaped(self):
        fetch = FakeFetch()
        client = NYC311Client(fetch=fetch)
        client.recent_complaints(street="PADDY'S LANE")
        self.assertIn("PADDY''S LANE", fetch.calls[0][1]["$where"])

    def test_freshness(self):
        client = NYC311Client(fetch=FakeFetch())
        self.assertTrue(client.freshness().endswith("Z"))

    def test_error_envelope(self):
        client = NYC311Client(fetch=FakeFetch(rows={"error": "boom"}))
        with self.assertRaises(UpstreamError):
            client.get_complaint("12345678")


class ParseTests(unittest.TestCase):
    def test_id_means_status(self):
        parsed = parse(message("Did complaint 12345678 get fixed?"))
        self.assertEqual(parsed["skill"], "complaint-status")
        self.assertEqual(parsed["params"]["unique_key"], "12345678")
        self.assertTrue(parsed["explicit"])

    def test_zip_means_near(self):
        parsed = parse(message("what is happening in 11235 lately?"))
        self.assertEqual(parsed["skill"], "complaints-near")
        self.assertEqual(parsed["params"]["zip_code"], "11235")

    def test_data_part_selects_skill_and_is_explicit(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "complaints-near", "zip_code": "10001"}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "complaints-near")
        self.assertTrue(parsed["explicit"])

    def test_bare_text_is_not_explicit(self):
        parsed = parse(message("hello there"))
        self.assertFalse(parsed["explicit"])
        self.assertEqual(parsed["skill"], "complaint-status")

    def test_heat_keyword(self):
        parsed = parse(message("heat complaints in 11235"))
        self.assertEqual(parsed["params"]["complaint_type"], "HEAT/HOT WATER")


class AgentTests(unittest.TestCase):
    def test_input_prompt_when_missing(self):
        agent = NYC311Agent(FakeClient())
        result = agent.run({"skill": "complaint-status", "params": {}, "missing": ["unique_key"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("unique key", result["message"])

    def test_watch_probe(self):
        client = FakeClient(status="Closed")
        agent = NYC311Agent(client)
        self.assertEqual(agent.probe_watch({"unique_key": "12345678"}), "Closed")
        self.assertEqual(agent.probe_watch({}), None)

    def test_describe_change(self):
        agent = NYC311Agent(FakeClient())
        text = agent.describe_watch_change({"unique_key": "12345678"}, "Open", "Closed")
        self.assertIn("12345678", text)
        self.assertIn("Closed", text)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.client = FakeClient()
        self.agent = NYC311Agent(self.client)
        self.handler = A2AHandler(self.store, self.agent)

    def tearDown(self):
        self.store.close()

    def test_lookup_completes_with_artifact(self):
        task = self.handler.handle("message/send", {"message": message("Did complaint 12345678 get fixed?")})
        self.assertEqual(task["status"]["state"], "completed")
        artifact = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(artifact["complaint"]["unique_key"], "12345678")
        self.assertEqual(artifact["dataset"], "erm2-nwe9")
        self.assertTrue(artifact["freshness"])

    def test_input_required_then_follow_up(self):
        first = self.handler.handle("message/send", {"message": message("hello there")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = self.handler.handle(
            "message/send",
            {"message": message("12345678", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["id"], first["id"])
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(len(follow["history"]), 4)

    def test_unknown_complaint_is_completed_not_found(self):
        task = self.handler.handle("message/send", {"message": message("complaint 99999999")})
        self.assertEqual(task["status"]["state"], "completed")
        artifact = task["artifacts"][-1]["parts"][0]["data"]
        self.assertFalse(artifact["found"])

    def test_upstream_error_fails_the_task(self):
        class Broken(FakeClient):
            def get_complaint(self, key):
                raise UpstreamError("socrata is down")

        handler = A2AHandler(self.store, NYC311Agent(Broken()))
        task = handler.handle("message/send", {"message": message("complaint 12345678")})
        self.assertEqual(task["status"]["state"], "failed")

    def test_cancel_and_get_task(self):
        task = self.handler.handle("message/send", {"message": message("complaint 12345678")})
        fetched = self.handler.handle("tasks/get", {"id": task["id"]})
        self.assertEqual(fetched["id"], task["id"])
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("tasks/cancel", {"id": task["id"]})
        self.assertEqual(ctx.exception.code, -32002)

    def test_stream_sequence(self):
        events = list(self.handler.handle("message/stream", {"message": message("complaint 12345678")}))
        self.assertEqual([event["kind"] for event in events],
                         ["task", "status-update", "artifact-update", "status-update"])
        self.assertTrue(events[-1]["final"])

    def test_push_config_round_trip(self):
        task = self.handler.handle("message/send", {"message": message("complaint 12345678")})
        created = self.handler.handle(
            "tasks/pushNotificationConfig/set",
            {"taskId": task["id"], "pushNotificationConfig": {"url": "https://example.com/hook"}},
        )
        config_id = created["pushNotificationConfig"]["id"]
        listed = self.handler.handle("tasks/pushNotificationConfig/list", {"taskId": task["id"]})
        self.assertEqual(len(listed["configs"]), 1)
        self.handler.handle(
            "tasks/pushNotificationConfig/delete",
            {"taskId": task["id"], "pushNotificationConfigId": config_id},
        )
        self.assertEqual(self.handler.handle("tasks/pushNotificationConfig/list", {"taskId": task["id"]})["configs"], [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = AgentServerApp(
            NYC311Agent(FakeClient()),
            "http://127.0.0.1:1",
            "test",
            db_path=":memory:",
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.stop()

    def post(self, payload):
        req = request.Request(
            self.base + "/", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        with request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def get(self, path):
        with request.urlopen(self.base + path, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_card_and_health(self):
        status, card = self.get("/.well-known/agent-card.json")
        self.assertEqual(status, 200)
        self.assertEqual(card["protocolVersion"], "0.3.0")
        self.assertEqual([skill["id"] for skill in card["skills"]], ["complaint-status", "complaints-near"])
        status, health = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["agent"], "nyc311")
        self.assertEqual(health["datasets"], ["erm2-nwe9"])

    def test_legacy_card_path_and_404(self):
        self.assertEqual(self.get("/.well-known/agent.json")[0], 200)
        with self.assertRaises(HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_method(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {}})
        self.assertEqual(response["error"]["code"], -32601)

    def test_invalid_json(self):
        req = request.Request(self.base + "/", data=b"{not json", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body["error"]["code"], -32700)

    def test_input_required_over_http(self):
        response = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": message("hello there")}}
        )
        self.assertEqual(response["result"]["status"]["state"], "input-required")

    def test_sse_stream(self):
        payload = {"jsonrpc": "2.0", "id": 3, "method": "message/stream",
                   "params": {"message": message("complaint 12345678")}}
        req = request.Request(
            self.base + "/", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        with request.urlopen(req, timeout=15) as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
            body = resp.read().decode()
        events = [json.loads(line[6:])["result"] for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["kind"], "task")
        self.assertTrue(events[-1]["final"])
        self.assertEqual(events[-1]["status"]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
