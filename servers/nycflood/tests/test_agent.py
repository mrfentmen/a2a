"""Tests for the NYC street-flooding (FloodNet) server.

    python3 servers/nycflood/tests/test_agent.py
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import A2AHandler, PushWatcher, TaskStore  # noqa: E402

from agent import CARD_SKILLS, FloodAgent, parse  # noqa: E402
from data import FloodClient  # noqa: E402

SENSOR = {
    "sensor_id": "BK-richardson-st-n-11th-st-1x59w1",
    "sensor_name": "BK - Richardson St/N 11th St",
    "street_name": "North 11th Street",
    "borough": "Brooklyn",
    "zipcode": "11211",
    "latitude": "40.718566",
    "longitude": "-73.952866",
    "dataset": "kb2e-tjy3",
}

EVENT = {
    "sensor_id": "BK-richardson-st-n-11th-st-1x59w1",
    "sensor_name": "BK - Richardson St/N 11th St",
    "flood_start_time": "2026-09-08T00:14:50.000",
    "flood_end_time": "2026-09-08T02:10:50.000",
    "max_depth_inches": "2.36",
    "duration_mins": "116",
    "dataset": "aq7i-eu5q",
}


class FakeFlood(FloodClient):
    """Same interface as the real client, no network."""

    def __init__(self, events=None, sensors=None):
        self.events = list(events if events is not None else [EVENT])
        self.sensor_rows = list(sensors if sensors is not None else [SENSOR])

    def sensors(self, zip_code=None, borough=None, limit=25):
        return self.sensor_rows[:limit]

    def sensor(self, sensor_id):
        for row in self.sensor_rows:
            if row["sensor_id"] == sensor_id:
                return row
        return None

    def recent_events(self, sensor_ids=None, hours=72, limit=20):
        return self.events[:limit]

    def latest_event(self, sensor_ids=None):
        return self.events[0] if self.events else None

    def freshness(self, dataset=None):
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


class ClientValidationTests(unittest.TestCase):
    def test_check_zip(self):
        self.assertEqual(FloodClient.check_zip("11211"), "11211")
        with self.assertRaises(ValueError):
            FloodClient.check_zip("1234")

    def test_check_borough(self):
        self.assertEqual(FloodClient.check_borough("queens"), "Queens")
        with self.assertRaises(ValueError):
            FloodClient.check_borough("Jersey City")

    def test_check_sensor_id(self):
        self.assertEqual(
            FloodClient.check_sensor_id("BK-richardson-st-n-11th-st-1x59w1"),
            "BK-richardson-st-n-11th-st-1x59w1",
        )
        with self.assertRaises(ValueError):
            FloodClient.check_sensor_id("BK richardson")


class ParseTests(unittest.TestCase):
    def test_sensor_words_mean_sensor_listing(self):
        parsed = parse(message("Which flood sensors cover 11211?"))
        self.assertEqual(parsed["skill"], "flood-sensors")
        self.assertEqual(parsed["params"]["zip_code"], "11211")
        self.assertTrue(parsed["explicit"])

    def test_borough_detected(self):
        parsed = parse(message("how many sensors are in Queens?"))
        self.assertEqual(parsed["params"]["borough"], "Queens")

    def test_watch_words_mean_watch(self):
        parsed = parse(message("tell me when 11211 floods again"))
        self.assertEqual(parsed["skill"], "flood-watch")
        self.assertTrue(parsed["explicit"])

    def test_window_parsed_from_text(self):
        parsed = parse(message("which streets flooded in the last 30 days?"))
        self.assertEqual(parsed["skill"], "flood-recent")
        self.assertEqual(parsed["params"]["hours"], 720)
        self.assertFalse(parsed["explicit"])

    def test_sensor_id_detected(self):
        parsed = parse(message("watch sensor BK-richardson-st-n-11th-st-1x59w1"))
        self.assertEqual(parsed["skill"], "flood-watch")
        self.assertEqual(parsed["params"]["sensor_id"], "BK-richardson-st-n-11th-st-1x59w1")

    def test_data_part_wins(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "flood-recent", "zip_code": "11211", "hours": 6}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "flood-recent")
        self.assertEqual(parsed["params"]["hours"], 6)


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["flood-recent", "flood-sensors", "flood-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = FloodAgent(FakeFlood())
        self.assertEqual(agent.missing("flood-recent", {}), [])
        self.assertEqual(agent.missing("flood-sensors", {}), ["location"])
        self.assertEqual(agent.missing("flood-sensors", {"zip_code": "11211"}), [])
        self.assertEqual(agent.missing("flood-watch", {}), ["location"])
        self.assertEqual(agent.missing("flood-watch", {"sensor_id": "BK-x-1"}), [])

    def test_recent_when_no_events(self):
        agent = FloodAgent(FakeFlood(events=[]))
        result = agent.run({"skill": "flood-recent", "params": {"hours": 24}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("No street-flooding events", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_recent_when_events(self):
        agent = FloodAgent(FakeFlood())
        result = agent.run({"skill": "flood-recent", "params": {"hours": 72}, "missing": []})
        self.assertIn("1 street-flooding event", result["message"])
        self.assertEqual(result["artifact"]["events"][0]["max_depth_inches"], "2.36")

    def test_recent_with_no_sensors_in_place(self):
        agent = FloodAgent(FakeFlood(sensors=[]))
        result = agent.run({"skill": "flood-recent", "params": {"zip_code": "10001"}, "missing": []})
        self.assertIn("No FloodNet sensors", result["message"])
        self.assertEqual(result["artifact"]["sensor_count"], 0)

    def test_sensors_listing(self):
        agent = FloodAgent(FakeFlood())
        result = agent.run({"skill": "flood-sensors", "params": {"zip_code": "11211"}, "missing": []})
        self.assertEqual(result["artifact"]["count"], 1)
        self.assertIn("Richardson", result["message"])

    def test_watch_unknown_sensor(self):
        agent = FloodAgent(FakeFlood())
        result = agent.run({"skill": "flood-watch", "params": {"sensor_id": "BK-nope-1"}, "missing": []})
        self.assertFalse(result["artifact"]["found"])
        self.assertIsNone(result["watch"])

    def test_watch_sets_observation(self):
        agent = FloodAgent(FakeFlood())
        result = agent.run({"skill": "flood-watch", "params": {"zip_code": "11211"}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "flood-watch")
        self.assertEqual(watch["sensor_ids"], [SENSOR["sensor_id"]])
        self.assertEqual(watch["observed"]["last_start"], EVENT["flood_start_time"])

    def test_watch_with_no_events_yet(self):
        agent = FloodAgent(FakeFlood(events=[]))
        result = agent.run({"skill": "flood-watch", "params": {"zip_code": "11211"}, "missing": []})
        self.assertEqual(result["watch"]["observed"]["last_start"], "")
        self.assertIn("No flood event", result["message"])

    def test_probe_and_describe(self):
        agent = FloodAgent(FakeFlood())
        observed = agent.probe_watch({"sensor_ids": [SENSOR["sensor_id"]]})
        self.assertEqual(observed["max_depth_inches"], "2.36")
        self.assertIsNone(agent.probe_watch({"sensor_ids": []}))
        text = agent.describe_watch_change({}, {"last_start": ""}, observed)
        self.assertIn("New street-flooding event", text)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, FloodAgent(FakeFlood()))
        task = handler.handle("message/send", {"message": message("which flood sensors cover 11211?")})
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["artifacts"][-1]["parts"][0]["data"]["dataset"], "kb2e-tjy3")

    def test_watch_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, FloodAgent(FakeFlood()))
        first = handler.handle("message/send", {"message": message("watch for flooding")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": message("11211", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["metadata"]["watch"]["kind"], "flood-watch")
        self.assertEqual(follow["metadata"]["last_observed"]["last_start"], EVENT["flood_start_time"])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_watcher_fires_when_a_new_event_appears(self):
        client = FakeFlood(events=[])
        agent = FloodAgent(client)
        task = A2AHandler(self.store, agent).handle("message/send", {"message": message("watch 11211")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(
            self.store, agent, interval=5,
            http_post=lambda url, payload, headers: posted.append(payload) or 200,
        )
        self.assertEqual(watcher.tick(), 0)  # no event yet

        client.events = [EVENT]
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(len(posted), 1)
        self.assertEqual(
            self.store.get_task(task["id"])["metadata"]["last_observed"]["last_start"],
            EVENT["flood_start_time"],
        )
        self.assertEqual(watcher.tick(), 0)  # same event, nothing new


if __name__ == "__main__":
    unittest.main()
