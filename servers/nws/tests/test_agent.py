"""Tests for the NWS weather-alerts server.

    python3 servers/nws/tests/test_agent.py
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

from agent import (  # noqa: E402
    CARD_SKILLS,
    NWSAlertsAgent,
    parse,
    state_from_text,
)
from data import NWSAlertsClient  # noqa: E402

ALERT = {
    "id": "urn:oid:2.49.0.1.840.0.abc123",
    "areaDesc": "New York (Manhattan); Bronx",
    "event": "Hurricane Local Statement",
    "severity": "Severe",
    "certainty": "Likely",
    "urgency": "Expected",
    "onset": "2026-09-22T18:00:00-04:00",
    "ends": "2026-09-23T06:00:00-04:00",
    "headline": "Hurricane Local Statement issued September 22 at 5:20PM EDT",
    "instruction": "Monitor NOAA Weather Radio.",
    "senderName": "NWS New York NY",
    "messageType": "Update",
    "category": "Met",
    "response": "Prepare",
    "dataset": "api.weather.gov/alerts/active",
}

QUIET_ALERT = {
    "id": "urn:oid:2.49.0.1.840.0.def456",
    "areaDesc": "Coastal Waters",
    "event": "Small Craft Advisory",
    "severity": "Minor",
    "onset": "2026-09-22T10:00:00-04:00",
    "headline": "Small Craft Advisory until 6PM",
    "dataset": "api.weather.gov/alerts/active",
}

COUNTS = {"total": 386, "land": 361, "marine": 25, "areas": {"NY": 4, "TX": 22, "CA": 9}, "regions": {}}


class FakeNWS(NWSAlertsClient):
    """Same interface as the real client, no network."""

    def __init__(self, alerts=None, counts=None):
        self._alerts = list(alerts if alerts is not None else [ALERT, QUIET_ALERT])
        self._counts = dict(counts or COUNTS)
        self.calls: list[dict] = []

    def active_alerts(self, area=None, zone=None, point=None, severity=None, event_contains=None, limit=20):
        self.calls.append(
            {"area": area, "zone": zone, "point": point, "severity": severity,
             "event_contains": event_contains, "limit": limit}
        )
        rows = list(self._alerts)
        if severity:
            rows = [row for row in rows if row.get("severity") == severity]
        if event_contains:
            rows = [row for row in rows if event_contains.lower() in (row.get("event") or "").lower()]
        return rows[:limit]

    def counts(self):
        return self._counts

    def freshness(self):
        return "2026-09-22T12:00:00Z"


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
    def test_check_area(self):
        self.assertEqual(NWSAlertsClient.check_area("ny"), "NY")
        with self.assertRaises(ValueError):
            NWSAlertsClient.check_area("New York")

    def test_check_zone(self):
        self.assertEqual(NWSAlertsClient.check_zone("nyz072"), "NYZ072")
        with self.assertRaises(ValueError):
            NWSAlertsClient.check_zone("NYZ07")

    def test_check_point(self):
        self.assertEqual(NWSAlertsClient.check_point("40.7128, -74.0060"), "40.7128,-74.0060")
        with self.assertRaises(ValueError):
            NWSAlertsClient.check_point("40.7128")
        with self.assertRaises(ValueError):
            NWSAlertsClient.check_point("91.0,-74.0")

    def test_place_label(self):
        self.assertEqual(NWSAlertsClient.place_label("NY"), "New York")
        self.assertEqual(NWSAlertsClient.place_label(zone="NYZ072"), "zone NYZ072")
        self.assertEqual(NWSAlertsClient.place_label(), "the United States")


class StateParsingTests(unittest.TestCase):
    def test_uppercase_code(self):
        self.assertEqual(state_from_text("alerts in NY?"), "NY")

    def test_lowercase_after_preposition(self):
        self.assertEqual(state_from_text("any alerts for nm right now"), "NM")

    def test_bare_lowercase_word_is_not_a_state(self):
        self.assertIsNone(state_from_text("ok thanks, all good"))
        self.assertIsNone(state_from_text("is it in or out"))

    def test_full_name(self):
        self.assertEqual(state_from_text("how about California"), "CA")


class ParseTests(unittest.TestCase):
    def test_active_by_default(self):
        parsed = parse(message("Any weather alerts in NY right now?"))
        self.assertEqual(parsed["skill"], "alerts-active")
        self.assertEqual(parsed["params"]["area"], "NY")
        self.assertFalse(parsed["explicit"])

    def test_summary_words(self):
        parsed = parse(message("how many weather alerts are active nationwide?"))
        self.assertEqual(parsed["skill"], "alerts-summary")
        self.assertTrue(parsed["explicit"])

    def test_watch_words(self):
        parsed = parse(message("tell me when a hurricane warning is issued for Florida"))
        self.assertEqual(parsed["skill"], "alerts-watch")
        self.assertEqual(parsed["params"]["area"], "FL")
        self.assertEqual(parsed["params"]["event_contains"], "Hurricane")

    def test_zone_and_point_from_text(self):
        zoned = parse(message("anything active for NYZ072?"))
        self.assertEqual(zoned["params"]["zone"], "NYZ072")
        pointed = parse(message("alerts near 40.7128,-74.0060"))
        self.assertEqual(pointed["params"]["point"], "40.7128,-74.0060")

    def test_severity_from_text(self):
        parsed = parse(message("any severe alerts in TX?"))
        self.assertEqual(parsed["params"]["severity"], "Severe")

    def test_data_part_wins(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "alerts-active", "state": "WA", "limit": 3}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "alerts-active")
        self.assertEqual(parsed["params"]["area"], "WA")
        self.assertEqual(parsed["params"]["limit"], 3)


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["alerts-active", "alerts-summary", "alerts-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = NWSAlertsAgent(FakeNWS())
        self.assertEqual(agent.missing("alerts-active", {}), [])
        self.assertEqual(agent.missing("alerts-summary", {}), [])
        self.assertEqual(agent.missing("alerts-watch", {}), ["location"])
        self.assertEqual(agent.missing("alerts-watch", {"area": "NY"}), [])
        self.assertEqual(agent.missing("alerts-watch", {"zone": "NYZ072"}), [])
        self.assertEqual(agent.missing("alerts-watch", {"point": "40.7,-74.0"}), [])

    def test_active_lists_alerts(self):
        agent = NWSAlertsAgent(FakeNWS())
        result = agent.run({"skill": "alerts-active", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("2 active NWS alert(s)", result["message"])
        self.assertIn("api.weather.gov", result["message"])
        self.assertEqual(result["artifact"]["dataset"], "api.weather.gov/alerts/active")
        self.assertTrue(result["artifact"]["freshness"])
        self.assertIsNone(result["watch"])

    def test_active_when_quiet(self):
        agent = NWSAlertsAgent(FakeNWS(alerts=[]))
        result = agent.run({"skill": "alerts-active", "params": {"area": "NY"}, "missing": []})
        self.assertIn("No active NWS alerts", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_active_passes_filters_through(self):
        client = FakeNWS()
        agent = NWSAlertsAgent(client)
        agent.run({"skill": "alerts-active", "params": {"area": "NY", "severity": "Severe", "limit": 5}, "missing": []})
        self.assertEqual(client.calls[0]["area"], "NY")
        self.assertEqual(client.calls[0]["severity"], "Severe")
        self.assertEqual(client.calls[0]["limit"], 5)

    def test_summary_national(self):
        agent = NWSAlertsAgent(FakeNWS())
        result = agent.run({"skill": "alerts-summary", "params": {}, "missing": []})
        self.assertIn("386 active NWS alerts nationwide", result["message"])
        self.assertEqual(result["artifact"]["counts"]["total"], 386)

    def test_summary_for_one_state(self):
        agent = NWSAlertsAgent(FakeNWS())
        result = agent.run({"skill": "alerts-summary", "params": {"area": "NY"}, "missing": []})
        self.assertIn("New York has 4", result["message"])
        self.assertEqual(result["artifact"]["area_count"], 4)

    def test_watch_records_observation(self):
        agent = NWSAlertsAgent(FakeNWS())
        result = agent.run({"skill": "alerts-watch", "params": {"area": "NY"}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "alerts-watch")
        self.assertEqual(watch["area"], "NY")
        self.assertEqual(watch["observed"]["count"], 2)
        self.assertEqual(watch["observed"]["newest_event"], "Hurricane Local Statement")
        self.assertNotIn("observed", result["artifact"]["watching"])

    def test_watch_when_quiet(self):
        agent = NWSAlertsAgent(FakeNWS(alerts=[]))
        result = agent.run({"skill": "alerts-watch", "params": {"area": "NY"}, "missing": []})
        self.assertEqual(result["watch"]["observed"]["count"], 0)
        self.assertIn("Nothing active", result["message"])

    def test_missing_input_prompts(self):
        agent = NWSAlertsAgent(FakeNWS())
        result = agent.run({"skill": "alerts-watch", "params": {}, "missing": ["location"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which place", result["message"])

    def test_probe_and_describe(self):
        agent = NWSAlertsAgent(FakeNWS())
        self.assertIsNone(agent.probe_watch({}))
        observed = agent.probe_watch({"area": "NY"})
        self.assertEqual(observed["count"], 2)
        grown = agent.describe_watch_change({"place": "New York"}, {"count": 0}, observed)
        self.assertIn("New NWS alert for New York", grown)
        calmed = agent.describe_watch_change({"place": "New York"}, {"count": 5}, {"count": 1})
        self.assertIn("clearing", calmed)
        changed = agent.describe_watch_change({"place": "New York"}, {"count": 2}, observed)
        self.assertIn("changed", changed)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, NWSAlertsAgent(FakeNWS()))
        task = handler.handle("message/send", {"message": message("any severe weather alerts in NY?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], "api.weather.gov/alerts/active")
        self.assertEqual(data["place"], "New York")

    def test_watch_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, NWSAlertsAgent(FakeNWS()))
        first = handler.handle("message/send", {"message": message("watch for weather alerts")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": message("NY", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["metadata"]["watch"]["kind"], "alerts-watch")
        self.assertEqual(follow["metadata"]["watch"]["area"], "NY")
        self.assertEqual(follow["metadata"]["last_observed"]["count"], 2)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_watcher_fires_when_a_new_alert_appears(self):
        client = FakeNWS(alerts=[])
        agent = NWSAlertsAgent(client)
        task = A2AHandler(self.store, agent).handle("message/send", {"message": message("watch for alerts in NY")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(
            self.store, agent, interval=5,
            http_post=lambda url, payload, headers: posted.append(payload) or 200,
        )
        self.assertEqual(watcher.tick(), 0)  # still quiet

        client._alerts = [ALERT]
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(len(posted), 1)
        self.assertEqual(self.store.get_task(task["id"])["metadata"]["last_observed"]["count"], 1)
        self.assertEqual(watcher.tick(), 0)  # same alert, nothing new


if __name__ == "__main__":
    unittest.main()
