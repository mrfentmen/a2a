"""Unit tests for a2a_kit: store, protocol lifecycle, push notifications, Socrata client.

    python3 tests/test_kit.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from a2a_kit import (  # noqa: E402
    A2AError,
    A2AHandler,
    PushWatcher,
    SkillAgent,
    SocrataClient,
    TaskStore,
    UpstreamError,
    soql_escape,
)

DATING = "2026-09-22T00:00:00Z"


class DummyAgent(SkillAgent):
    name = "dummy"
    card_name = "Dummy Agent"
    card_description = "test agent"
    env_prefix = "DUMMY"
    datasets = ("fake-0000",)
    card_skills = [{"id": "watch-thing", "name": "Watch", "description": "watch a thing", "tags": ["test"]}]
    watch_kinds = ("watch-thing",)

    def __init__(self) -> None:
        self.observed = {"last": ""}

    def parse(self, message: dict) -> dict:
        text = " ".join(part.get("text", "") for part in message["parts"] if part.get("kind") == "text")
        data = {}
        for part in message["parts"]:
            if part.get("kind") == "data":
                data.update(part["data"])
        if data.get("skill"):
            return {"skill": data["skill"], "params": data, "explicit": True}
        if "watch" in text.lower():
            return {"skill": "watch-thing", "params": {}, "explicit": True}
        # A bare answer carries no skill name; real agents also map it to their own
        # parameter names ("thing" here) so an input-required exchange can continue.
        return {
            "skill": "echo",
            "params": {"value": text.strip(), "thing": text.strip()},
            "explicit": False,
        }

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == "watch-thing":
            return [] if params.get("thing") else ["thing"]
        return [] if params.get("value") else ["value"]

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return "Which thing should I watch?"

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {"final_state": "input-required", "message": self.input_prompt(request["skill"], request["missing"]),
                    "artifact": None, "watch": None}
        if request["skill"] == "watch-thing":
            return {
                "final_state": "completed",
                "message": f"Watching {request['params'].get('thing')}.",
                "artifact": {"dataset": "fake-0000", "watching": request["params"]},
                "watch": {"kind": "watch-thing", "thing": request["params"].get("thing"), "observed": self.observed.copy()},
            }
        return {"final_state": "completed", "message": f"echo: {request['params'].get('value')}",
                "artifact": {"dataset": "fake-0000", "echo": request["params"].get("value")}, "watch": None}

    def probe_watch(self, watch: dict):
        return self.observed

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        return f"{watch.get('thing')} moved from {previous} to {observed}"


def message(text: str, **extra) -> dict:
    payload = {
        "kind": "message",
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    payload.update(extra)
    return payload


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_task_round_trip(self):
        task = {
            "kind": "task",
            "id": "t1",
            "contextId": "c1",
            "status": {"state": "working", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {},
        }
        self.store.save_task(task)
        self.assertEqual(self.store.get_task("t1")["status"]["state"], "working")
        self.assertEqual(self.store.count_tasks(), 1)
        task["status"]["state"] = "completed"
        self.store.save_task(task)
        self.assertEqual(self.store.get_task("t1")["status"]["state"], "completed")
        self.assertEqual(self.store.count_tasks(), 1)

    def test_push_config_crud(self):
        self.store.set_push_config("t1", {"id": "cfg", "url": "https://example.com/hook", "token": "tok"})
        self.assertEqual(self.store.get_push_config("t1", "cfg")["token"], "tok")
        self.assertEqual(len(self.store.list_push_configs("t1")), 1)
        self.assertEqual(self.store.count_push_configs(), 1)
        self.assertTrue(self.store.delete_push_config("t1", "cfg"))
        self.assertEqual(self.store.list_push_configs("t1"), [])

    def test_tasks_with_push_configs(self):
        task = {
            "kind": "task",
            "id": "t2",
            "contextId": "c2",
            "status": {"state": "completed", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {},
        }
        self.store.save_task(task)
        self.store.set_push_config("t2", {"id": "cfg", "url": "https://example.com/hook"})
        self.assertEqual([t["id"] for t in self.store.tasks_with_push_configs()], ["t2"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.agent = DummyAgent()
        self.handler = A2AHandler(self.store, self.agent)

    def tearDown(self):
        self.store.close()

    def test_echo_completes_with_artifact(self):
        task = self.handler.handle("message/send", {"message": message("hello world")})
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["artifacts"][-1]["parts"][0]["data"]["echo"], "hello world")
        self.assertEqual(task["artifacts"][-1]["name"], "echo")

    def test_input_required_then_bare_follow_up_keeps_skill(self):
        first = self.handler.handle("message/send", {"message": message("please watch something")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = self.handler.handle(
            "message/send",
            {"message": message("sensor-7", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["id"], first["id"])
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["metadata"]["skill"], "watch-thing")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["watching"]["thing"], "sensor-7")

    def test_dynamic_agent_failure_becomes_failed_task(self):
        class Boom(DummyAgent):
            def run(self, request):
                raise UpstreamError("upstream down")

        handler = A2AHandler(self.store, Boom())
        task = handler.handle("message/send", {"message": message("hello")})
        self.assertEqual(task["status"]["state"], "failed")
        self.assertIn("unreachable", task["status"]["message"]["parts"][0]["text"])

    def test_unknown_method(self):
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("nope/nothing", {})
        self.assertEqual(ctx.exception.code, -32601)

    def test_missing_task(self):
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("tasks/get", {"id": "missing"})
        self.assertEqual(ctx.exception.code, -32001)

    def test_context_mismatch(self):
        task = self.handler.handle("message/send", {"message": message("watch it", contextId="ctx-a")})
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle(
                "message/send",
                {"message": message("thing", taskId=task["id"], contextId="ctx-b")},
            )
        self.assertEqual(ctx.exception.code, -32602)

    def test_message_to_terminal_task(self):
        task = self.handler.handle("message/send", {"message": message("hello")})
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("message/send", {"message": message("more", taskId=task["id"])})
        self.assertEqual(ctx.exception.code, -32004)

    def test_cancel(self):
        record = {
            "kind": "task",
            "id": "manual",
            "contextId": "ctx",
            "status": {"state": "working", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {},
        }
        self.store.save_task(record)
        self.assertEqual(self.handler.handle("tasks/cancel", {"id": "manual"})["status"]["state"], "canceled")
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("tasks/cancel", {"id": "manual"})
        self.assertEqual(ctx.exception.code, -32002)

    def test_bad_message_parts(self):
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("message/send", {"message": {"kind": "message", "role": "user", "messageId": "m"}})
        self.assertEqual(ctx.exception.code, -32602)
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle(
                "message/send",
                {"message": {"kind": "message", "role": "user", "messageId": "m", "parts": [{"kind": "video"}]}},
            )
        self.assertEqual(ctx.exception.code, -32005)

    def test_history_length(self):
        task = self.handler.handle("message/send", {"message": message("hello")})
        self.assertNotIn("history", self.handler.handle("tasks/get", {"id": task["id"], "historyLength": 0}))
        self.assertEqual(len(self.handler.handle("tasks/get", {"id": task["id"], "historyLength": 1})["history"]), 1)

    def test_return_immediately_then_poll(self):
        task = self.handler.handle(
            "message/send", {"message": message("hello"), "configuration": {"returnImmediately": True}}
        )
        self.assertIn(task["status"]["state"], ("submitted", "working", "completed"))
        deadline = time.time() + 5
        current = task
        while time.time() < deadline:
            current = self.handler.handle("tasks/get", {"id": task["id"]})
            if current["status"]["state"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(current["status"]["state"], "completed")

    def test_stream_event_sequence(self):
        events = list(self.handler.handle("message/stream", {"message": message("hello")}))
        self.assertEqual([event["kind"] for event in events],
                         ["task", "status-update", "artifact-update", "status-update"])
        self.assertEqual(events[1]["status"]["state"], "working")
        self.assertFalse(events[1]["final"])
        self.assertTrue(events[-1]["final"])
        self.assertEqual(events[-1]["status"]["state"], "completed")

    def test_stream_input_required_is_final(self):
        events = list(self.handler.handle("message/stream", {"message": message("watch")}))
        self.assertTrue(events[-1]["final"])
        self.assertEqual(events[-1]["status"]["state"], "input-required")

    def test_push_config_lifecycle_and_validation(self):
        task = self.handler.handle("message/send", {"message": message("hello")})
        created = self.handler.handle(
            "tasks/pushNotificationConfig/set",
            {"taskId": task["id"], "pushNotificationConfig": {"url": "https://example.com/hook", "token": "tok"}},
        )["pushNotificationConfig"]
        self.assertEqual(
            self.handler.handle(
                "tasks/pushNotificationConfig/get",
                {"taskId": task["id"], "pushNotificationConfigId": created["id"]},
            )["pushNotificationConfig"]["token"],
            "tok",
        )
        self.assertEqual(len(self.handler.handle("tasks/pushNotificationConfig/list", {"taskId": task["id"]})["configs"]), 1)
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle(
                "tasks/pushNotificationConfig/set",
                {"taskId": task["id"], "pushNotificationConfig": {"url": "ftp://example.com/hook"}},
            )
        self.assertEqual(ctx.exception.code, -32602)
        self.handler.handle(
            "tasks/pushNotificationConfig/delete",
            {"taskId": task["id"], "pushNotificationConfigId": created["id"]},
        )
        self.assertEqual(self.handler.handle("tasks/pushNotificationConfig/list", {"taskId": task["id"]})["configs"], [])

    def test_private_webhook_gating(self):
        task = self.handler.handle("message/send", {"message": message("hello")})
        config = {"taskId": task["id"], "pushNotificationConfig": {"url": "http://127.0.0.1:9999/hook"}}
        with self.assertRaises(A2AError) as ctx:
            self.handler.handle("tasks/pushNotificationConfig/set", config)
        self.assertEqual(ctx.exception.code, -32602)
        with mock.patch.dict(os.environ, {"DUMMY_ALLOW_PRIVATE_WEBHOOKS": "1"}):
            self.handler.handle("tasks/pushNotificationConfig/set", config)
        self.assertEqual(len(self.handler.handle("tasks/pushNotificationConfig/list", {"taskId": task["id"]})["configs"]), 1)


def make_receiver(state):
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            state["events"].append(json.loads(self.rfile.read(length).decode()))
            state["tokens"].append(self.headers.get("X-A2A-Notification-Token"))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Receiver


class PushWatcherTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.agent = DummyAgent()
        self.state = {"events": [], "tokens": []}
        self.receiver = ThreadingHTTPServer(("127.0.0.1", 0), make_receiver(self.state))
        threading.Thread(target=self.receiver.serve_forever, daemon=True).start()
        self.port = self.receiver.server_address[1]

        self.task = {
            "kind": "task",
            "id": "watched",
            "contextId": "ctx",
            "status": {"state": "completed", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {"watch": {"kind": "watch-thing", "thing": "sensor-7"}, "last_observed": {"last": ""}},
        }
        self.store.save_task(self.task)
        self.store.set_push_config(
            "watched", {"id": "cfg", "url": f"http://127.0.0.1:{self.port}/hook", "token": "tok-1"}
        )

    def tearDown(self):
        self.receiver.shutdown()
        self.receiver.server_close()
        self.store.close()

    def test_fires_on_change_and_updates_state(self):
        self.agent.observed = {"last": "2026-09-22T01:00:00Z"}
        watcher = PushWatcher(self.store, self.agent, interval=5)
        self.assertEqual(watcher.tick(), 1)
        event = self.state["events"][0]
        self.assertEqual(event["kind"], "status-update")
        self.assertEqual(event["taskId"], "watched")
        self.assertFalse(event["final"])
        self.assertEqual(event["metadata"]["observed"], {"last": "2026-09-22T01:00:00Z"})
        self.assertEqual(self.state["tokens"][0], "tok-1")
        self.assertEqual(self.store.get_task("watched")["metadata"]["last_observed"], {"last": "2026-09-22T01:00:00Z"})
        self.assertEqual(watcher.tick(), 0)  # nothing new the second time

    def test_retries_then_gives_up(self):
        self.agent.observed = {"last": "2026-09-22T01:00:00Z"}
        watcher = PushWatcher(self.store, self.agent, interval=5)
        attempts = {"n": 0}

        def failing_post(url, payload, headers):
            attempts["n"] += 1
            return 500

        watcher._post = failing_post
        with mock.patch("a2a_kit.push.time.sleep"):
            self.assertEqual(watcher.tick(), 0)
        self.assertEqual(attempts["n"], 3)

    def test_unreadable_watch_is_skipped(self):
        class Silent(DummyAgent):
            def probe_watch(self, watch):
                return None

        watcher = PushWatcher(self.store, Silent(), interval=5)
        self.assertEqual(watcher.tick(), 0)
        self.assertEqual(self.state["events"], [])


class SocrataTests(unittest.TestCase):
    class Client(SocrataClient):
        env_prefix = "TEST"
        dataset = "abcd-1234"

        def __init__(self, responses):
            self.responses = responses
            self.calls = []
            super().__init__(fetch=self._fetch, base_url="https://example.org")

        def _fetch(self, url, params, headers):
            self.calls.append((url, dict(params)))
            body = self.responses.get("views" if "/api/views/" in url else "rows", [])
            if isinstance(body, Exception):
                raise body
            return body

    def test_soql_escape(self):
        self.assertEqual(soql_escape("PADDY'S"), "PADDY''S")

    def test_rows_cached(self):
        client = self.Client({"rows": [{"a": 1}]})
        self.assertEqual(client.rows("abcd-1234", {"$limit": "1"}), [{"a": 1}])
        client.rows("abcd-1234", {"$limit": "1"})
        self.assertEqual(len(client.calls), 1)

    def test_error_envelope(self):
        client = self.Client({"rows": {"error": "boom"}})
        with self.assertRaises(UpstreamError):
            client.rows("abcd-1234", {"$limit": "1"})

    def test_transport_error_wrapped(self):
        client = self.Client({"rows": UpstreamError("down")})
        with self.assertRaises(UpstreamError):
            client.rows("abcd-1234", {"$limit": "1"})

    def test_count_and_freshness(self):
        client = self.Client({"rows": [{"count": "42"}], "views": {"rowsUpdatedAt": 1790000000}})
        self.assertEqual(client.count("abcd-1234"), 42)
        freshness = client.dataset_freshness()
        self.assertTrue(freshness.endswith("Z"))
        self.assertEqual(freshness, client.dataset_freshness())  # cached

    def test_pick_keeps_order_and_stamps_dataset(self):
        picked = SocrataClient.pick({"b": 1, "a": 2}, ("a", "b"), "ds")
        self.assertEqual(list(picked), ["a", "b", "dataset"])


if __name__ == "__main__":
    unittest.main()
