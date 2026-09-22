"""Tests for the USGS earthquake server.

    python3 servers/quakes/tests/test_agent.py
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
    CITY_POINTS,
    USGSQuakeAgent,
    detect_city,
    magnitude_from_text,
    parse,
    window_from_text,
)
from data import USGSQuakeClient  # noqa: E402

FEATURE = {
    "type": "Feature",
    "id": "us7000abcd",
    "properties": {
        "mag": 4.6,
        "place": "10 km NE of Somewhere, Japan",
        "time": 1790036000000,
        "updated": 1790036600000,
        "tsunami": 0,
        "alert": "green",
        "sig": 326,
        "felt": 12,
        "type": "earthquake",
        "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
    },
    "geometry": {"type": "Point", "coordinates": [139.7, 35.7, 42.5]},
}
QUAKE = {
    "id": "us7000abcd",
    "magnitude": 4.6,
    "place": "10 km NE of Somewhere, Japan",
    "time": "2026-09-22T00:13:20Z",
    "updated": "2026-09-22T00:23:20Z",
    "depth_km": 42.5,
    "latitude": 35.7,
    "longitude": 139.7,
    "tsunami": 0,
    "alert": "green",
    "significance": 326,
    "felt": 12,
    "type": "earthquake",
    "detail_url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
}
SECOND_QUAKE = dict(QUAKE, id="us7000efgh", time="2026-09-22T01:00:00Z", place="20 km S of Elsewhere", magnitude=5.1)


class FakeQuake(USGSQuakeClient):
    """Same interface as the real client, no network."""

    def __init__(self, quakes=None, counts=None):
        self._quakes = list(quakes if quakes is not None else [QUAKE])
        self._counts = dict(counts or {"last_24h_m2.5": 132, "last_24h_m4.5": 7, "last_7d_m6.0": 4})
        self.calls: list[dict] = []

    def recent_quakes(self, min_magnitude=2.5, hours=24.0, limit=10, order="time"):
        self.calls.append({"kind": "recent", "min_magnitude": min_magnitude, "hours": hours, "limit": limit})
        rows = [row for row in self._quakes if row["magnitude"] >= min_magnitude]
        return rows[:limit]

    def quakes_near(self, point, radius_km=100.0, min_magnitude=1.0, hours=720.0, limit=10):
        self.calls.append({"kind": "near", "point": point, "radius_km": radius_km,
                           "min_magnitude": min_magnitude, "hours": hours, "limit": limit})
        rows = [row for row in self._quakes if row["magnitude"] >= min_magnitude]
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


class ClientTests(unittest.TestCase):
    def test_check_point(self):
        self.assertEqual(USGSQuakeClient.check_point("35.68, 139.69"), (35.68, 139.69))
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_point("35.68")
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_point("95.0,139.0")

    def test_check_magnitude(self):
        self.assertEqual(USGSQuakeClient.check_magnitude("4.55"), 4.5)
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_magnitude("nope")
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_magnitude("12")

    def test_check_hours_and_radius_and_limit(self):
        self.assertEqual(USGSQuakeClient.check_hours("24"), 24.0)
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_hours("0.1")
        self.assertEqual(USGSQuakeClient.check_radius_km("300"), 300.0)
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_radius_km("0")
        self.assertEqual(USGSQuakeClient.check_limit("5"), 5)
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_limit("1000")

    def test_check_order_and_id(self):
        self.assertEqual(USGSQuakeClient.check_order("Magnitude"), "magnitude")
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_order("random")
        self.assertEqual(USGSQuakeClient.check_id(" us7000abcd "), "us7000abcd")
        with self.assertRaises(ValueError):
            USGSQuakeClient.check_id("!!")

    def test_query_builds_params_and_maps_features(self):
        seen = {}

        def fetch(url, params, headers):
            seen["url"] = url
            seen["params"] = params
            return {"type": "FeatureCollection", "features": [FEATURE]}

        client = USGSQuakeClient(fetch=fetch)
        rows = client.recent_quakes(min_magnitude=4.5, hours=24, limit=10)
        self.assertTrue(seen["url"].endswith("/fdsnws/event/1/query"))
        self.assertEqual(seen["params"]["minmagnitude"], "4.5")
        self.assertEqual(seen["params"]["orderby"], "time")
        self.assertIn("starttime", seen["params"])
        self.assertEqual(rows[0]["id"], "us7000abcd")
        self.assertEqual(rows[0]["magnitude"], 4.6)
        self.assertEqual(rows[0]["depth_km"], 42.5)
        self.assertEqual(rows[0]["time"], "2026-09-22T00:13:20Z")
        self.assertEqual(rows[0]["updated"], "2026-09-22T00:23:20Z")

    def test_count_reads_count_endpoint(self):
        client = USGSQuakeClient(fetch=lambda url, params, headers: {"count": 132, "maxAllowed": 20000})
        self.assertEqual(client.count(2.5, 24), 132)

    def test_bad_payload_raises(self):
        client = USGSQuakeClient(fetch=lambda url, params, headers: {"nope": 1})
        with self.assertRaises(Exception):
            client.recent_quakes()


class TextParsingTests(unittest.TestCase):
    def test_magnitude_variants(self):
        self.assertEqual(magnitude_from_text("any quakes above m4.5?"), 4.5)
        self.assertEqual(magnitude_from_text("magnitude 6 or higher"), 6.0)
        self.assertEqual(magnitude_from_text("big ones 5.0+"), 5.0)
        self.assertIsNone(magnitude_from_text("any earthquakes lately?"))

    def test_window_variants(self):
        self.assertEqual(window_from_text("in the last 3 days"), 72.0)
        self.assertEqual(window_from_text("past week"), 168.0)
        self.assertEqual(window_from_text("last month"), 720.0)
        self.assertIsNone(window_from_text("recently"))

    def test_detect_city_prefers_longest_name(self):
        self.assertEqual(detect_city("quakes near new york please"), "new york")
        self.assertEqual(detect_city("anything near tokyo?"), "tokyo")
        self.assertIsNone(detect_city("anywhere at all"))


class ParseTests(unittest.TestCase):
    def test_recent_by_default(self):
        parsed = parse(message("any earthquakes above magnitude 4.5 in the last 24 hours?"))
        self.assertEqual(parsed["skill"], "quakes-recent")
        self.assertEqual(parsed["params"]["min_magnitude"], 4.5)
        self.assertEqual(parsed["params"]["hours"], 24)

    def test_near_from_point(self):
        parsed = parse(message("quakes within 200 km of 35.68,139.69"))
        self.assertEqual(parsed["skill"], "quakes-near")
        self.assertEqual(parsed["params"]["point"], "35.68,139.69")
        self.assertEqual(parsed["params"]["radius_km"], "200")

    def test_near_from_city(self):
        parsed = parse(message("anything shaking near Lima this month?"))
        self.assertEqual(parsed["skill"], "quakes-near")
        self.assertEqual(parsed["params"]["place"], "lima")
        self.assertEqual(parsed["params"]["point"], f"{CITY_POINTS['lima'][0]},{CITY_POINTS['lima'][1]}")
        self.assertEqual(parsed["params"]["hours"], 720)

    def test_summary(self):
        parsed = parse(message("how many earthquakes today?"))
        self.assertEqual(parsed["skill"], "quakes-summary")
        self.assertTrue(parsed["explicit"])

    def test_watch(self):
        parsed = parse(message("tell me when there is a quake near Tokyo"))
        self.assertEqual(parsed["skill"], "quakes-watch")
        self.assertEqual(parsed["params"]["point"], "35.68,139.69")
        self.assertNotIn("min_magnitude", parsed["params"])

    def test_watch_global_magnitude(self):
        parsed = parse(message("notify me about magnitude 5.5+ quakes"))
        self.assertEqual(parsed["skill"], "quakes-watch")
        self.assertEqual(parsed["params"]["min_magnitude"], 5.5)
        self.assertNotIn("point", parsed["params"])

    def test_days_data_param_becomes_hours(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "quakes-near", "point": "35.68,139.69", "days": 30}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["params"]["hours"], 720.0)
        self.assertEqual(parsed["skill"], "quakes-near")


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["quakes-recent", "quakes-near", "quakes-summary", "quakes-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = USGSQuakeAgent(FakeQuake())
        self.assertEqual(agent.missing("quakes-recent", {}), [])
        self.assertEqual(agent.missing("quakes-summary", {}), [])
        self.assertEqual(agent.missing("quakes-near", {}), ["location"])
        self.assertEqual(agent.missing("quakes-near", {"point": "1,1"}), [])
        self.assertEqual(agent.missing("quakes-watch", {}), ["location or magnitude"])
        self.assertEqual(agent.missing("quakes-watch", {"point": "1,1"}), [])
        self.assertEqual(agent.missing("quakes-watch", {"min_magnitude": 5}), [])

    def test_recent_lists_quakes(self):
        agent = USGSQuakeAgent(FakeQuake())
        result = agent.run({"skill": "quakes-recent", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("M2.5+", result["message"])
        self.assertIn("earthquake.usgs.gov", result["artifact"]["dataset"])
        self.assertTrue(result["artifact"]["freshness"])
        self.assertIsNone(result["watch"])

    def test_recent_when_quiet(self):
        agent = USGSQuakeAgent(FakeQuake(quakes=[]))
        result = agent.run({"skill": "quakes-recent", "params": {"min_magnitude": 7, "hours": 48}, "missing": []})
        self.assertIn("No M7.0+ earthquakes", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_near_labels_the_place(self):
        agent = USGSQuakeAgent(FakeQuake())
        result = agent.run(
            {"skill": "quakes-near", "params": {"point": "35.68,139.69", "place": "tokyo", "radius_km": 300}, "missing": []}
        )
        self.assertIn("within 300 km of tokyo", result["message"])
        self.assertEqual(result["artifact"]["radius_km"], 300.0)

    def test_summary_reports_counts(self):
        agent = USGSQuakeAgent(FakeQuake())
        result = agent.run({"skill": "quakes-summary", "params": {}, "missing": []})
        self.assertIn("132 earthquakes at M2.5+", result["message"])
        self.assertIn("4 at M6.0+", result["message"])
        self.assertEqual(result["artifact"]["counts"]["last_24h_m4.5"], 7)

    def test_watch_records_observation(self):
        agent = USGSQuakeAgent(FakeQuake())
        result = agent.run({"skill": "quakes-watch", "params": {"min_magnitude": 4.0}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "quakes-watch")
        self.assertEqual(watch["min_magnitude"], 4.0)
        self.assertEqual(watch["observed"]["newest_id"], "us7000abcd")
        self.assertEqual(watch["observed"]["ids"], ["us7000abcd"])
        self.assertNotIn("observed", result["artifact"]["watching"])

    def test_watch_quiet(self):
        agent = USGSQuakeAgent(FakeQuake(quakes=[]))
        result = agent.run({"skill": "quakes-watch", "params": {"min_magnitude": 6.0}, "missing": []})
        self.assertEqual(result["watch"]["observed"]["count"], 0)
        self.assertIn("Nothing matching", result["message"])

    def test_input_required_prompt(self):
        agent = USGSQuakeAgent(FakeQuake())
        result = agent.run({"skill": "quakes-near", "params": {}, "missing": ["location"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Where?", result["message"])

    def test_probe_and_describe(self):
        agent = USGSQuakeAgent(FakeQuake())
        observed = agent.probe_watch({"min_magnitude": 4.0, "window_hours": 168})
        self.assertEqual(observed["count"], 1)
        text = agent.describe_watch_change({"place": "worldwide"}, {"newest_time": ""}, observed)
        self.assertIn("New earthquake for worldwide", text)
        quieter = agent.describe_watch_change({"place": "worldwide"}, {"newest_time": observed["newest_time"], "count": 5}, {"newest_time": observed["newest_time"], "count": 1})
        self.assertIn("leaving the window", quieter)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, USGSQuakeAgent(FakeQuake()))
        task = handler.handle("message/send", {"message": message("how many earthquakes today?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], "earthquake.usgs.gov/fdsnws/event/1")
        self.assertEqual(data["counts"]["last_24h_m2.5"], 132)

    def test_near_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, USGSQuakeAgent(FakeQuake()))
        first = handler.handle("message/send", {"message": message("any quakes near me?")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": message("35.68,139.69", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        data = follow["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["point"], "35.68,139.69")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_watcher_fires_when_a_new_quake_appears(self):
        client = FakeQuake()
        agent = USGSQuakeAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": message("tell me when there is a quake near Tokyo")}
        )
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(
            self.store, agent, interval=5,
            http_post=lambda url, payload, headers: posted.append(payload) or 200,
        )
        self.assertEqual(watcher.tick(), 0)  # nothing new

        client._quakes = [SECOND_QUAKE] + client._quakes
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(len(posted), 1)
        self.assertEqual(self.store.get_task(task["id"])["metadata"]["last_observed"]["newest_id"], "us7000efgh")
        self.assertEqual(watcher.tick(), 0)  # same set


if __name__ == "__main__":
    unittest.main()
