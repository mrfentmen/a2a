"""Tests for the wildfire server.

    python3 servers/fire/tests/test_agent.py
"""

from __future__ import annotations

import json
import re
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
    WildfireAgent,
    acres_from_text,
    name_from_text,
    parse,
    place_from_text,
    point_from_text,
    radius_from_text,
    state_from_text,
)
from data import (  # noqa: E402
    DATASET,
    WildfireClient,
    escape_literal,
    haversine_miles,
    to_iso,
)

#: Shapes copied from the live NIFC layer on 2026-09-22.
FEATURES = [
    {"attributes": {"IncidentName": "Plaskett", "IncidentSize": 29993, "PercentContained": 97,
                    "FireDiscoveryDateTime": 1756225560000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Monterey", "FireCause": "Undetermined",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Complex Incident Management Team",
                    "UniqueFireIdentifier": "2026-CAOSC-002993", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -121.7175, "y": 36.2159}},
    {"attributes": {"IncidentName": "Timber", "IncidentSize": 25436, "PercentContained": 59,
                    "FireDiscoveryDateTime": 1754700900000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Monterey", "FireCause": "Undetermined",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Type 2 Team",
                    "UniqueFireIdentifier": "2026-CAOSC-002543", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -121.9, "y": 36.4}},
    {"attributes": {"IncidentName": "DOME", "IncidentSize": 3420, "PercentContained": 15,
                    "FireDiscoveryDateTime": 1757950080000, "IncidentTypeCategory": "WF",
                    "POOState": "US-CA", "POOCounty": "Mariposa", "FireCause": "Human",
                    "GACC": "OSCC", "IncidentManagementOrganization": "Type 3 Team",
                    "UniqueFireIdentifier": "2026-CAOSC-000342", "ModifiedOnDateTime_dt": 1758491324000},
     "geometry": {"x": -119.8, "y": 37.5}},
]

WILLOW = {
    "attributes": {"IncidentName": "Willow", "IncidentSize": 7389, "PercentContained": 80,
                   "FireDiscoveryDateTime": 1751142420000, "IncidentTypeCategory": "WF",
                   "POOState": "US-CO", "POOCounty": "Lake", "FireCause": "Natural",
                   "GACC": "RMCC", "IncidentManagementOrganization": "Type 3 Team",
                   "UniqueFireIdentifier": "2026-COGCC-000738", "ModifiedOnDateTime_dt": 1758491324000},
    "geometry": {"x": -106.3, "y": 39.2},
}


class FakeLayer(WildfireClient):
    """A WildfireClient whose transport is a canned layer, so data.py runs for real."""

    def __init__(self, features=None):
        super().__init__()
        self.features = list(features if features is not None else FEATURES + [WILLOW])
        self.queries: list[dict] = []

    def get_json(self, url, params=None, ttl=None):
        """Apply the same filters the real ArcGIS layer would, so data.py runs for real."""
        params = dict(params or {})
        self.queries.append(params)
        rows = list(self.features)
        where = str(params.get("where") or "1=1")

        match = re.search(r"POOState = '([^']+)'", where)
        if match:
            rows = [row for row in rows if row["attributes"]["POOState"] == match.group(1)]
        match = re.search(r"IncidentSize >= ([\d.]+)", where)
        if match:
            rows = [row for row in rows if row["attributes"]["IncidentSize"] >= float(match.group(1))]
        match = re.search(r"PercentContained <= ([\d.]+)", where)
        if match:
            rows = [row for row in rows if row["attributes"]["PercentContained"] <= float(match.group(1))]
        match = re.search(r"UPPER\(IncidentName\) LIKE '([^']+)'", where)
        if match:
            needle = match.group(1).strip("%").upper()
            rows = [row for row in rows if needle in row["attributes"]["IncidentName"].upper()]

        if params.get("geometry"):
            lon, lat = (float(part) for part in params["geometry"].split(","))
            radius = float(params.get("distance") or 0)
            rows = [row for row in rows
                    if haversine_miles(lat, lon, row["geometry"]["y"], row["geometry"]["x"]) <= radius]

        if "IncidentSize DESC" in str(params.get("orderByFields") or ""):
            rows.sort(key=lambda row: -row["attributes"]["IncidentSize"])
        if params.get("resultRecordCount"):
            rows = rows[: int(params["resultRecordCount"])]
        return {"features": rows}


class ClientTests(unittest.TestCase):
    def test_escape_literal_doubles_quotes(self):
        self.assertEqual(escape_literal("O'Brien"), "'O''Brien'")
        self.assertEqual(escape_literal("CA"), "'CA'")

    def test_to_iso_converts_epoch_milliseconds(self):
        self.assertEqual(to_iso(1756225560000), "2025-08-26T16:26:00Z")
        self.assertIsNone(to_iso(None))
        self.assertIsNone(to_iso(""))
        self.assertIsNone(to_iso("not a date"))

    def test_haversine_is_zero_at_the_same_point_and_about_69_miles_per_degree(self):
        self.assertEqual(haversine_miles(39.74, -104.99, 39.74, -104.99), 0.0)
        one_degree = haversine_miles(39.0, -105.0, 40.0, -105.0)
        self.assertTrue(68.5 <= one_degree <= 69.5, one_degree)

    def test_check_state_accepts_codes_names_and_the_us_prefix(self):
        self.assertEqual(WildfireClient.check_state("US-CA"), "CA")
        self.assertEqual(WildfireClient.check_state("ca"), "CA")
        self.assertEqual(WildfireClient.check_state("Oregon"), "OR")
        with self.assertRaises(ValueError):
            WildfireClient.check_state("XX")

    def test_check_point_and_city(self):
        self.assertEqual(WildfireClient.check_point("39.74, -104.99"), (39.74, -104.99))
        with self.assertRaises(ValueError):
            WildfireClient.check_point("39.74")
        self.assertEqual(WildfireClient.city("Denver"), (39.74, -104.99, "Denver"))
        self.assertIsNone(WildfireClient.city("Atlantis"))

    def test_incidents_builds_a_filtered_sorted_query(self):
        layer = FakeLayer()
        result = layer.incidents(state="CA", min_acres=5000, limit=5)
        query = layer.queries[-1]
        self.assertEqual(query["where"], "POOState = 'US-CA' AND IncidentSize >= 5000.0")
        self.assertEqual(query["orderByFields"], "IncidentSize DESC")
        self.assertEqual(query["resultRecordCount"], "5")
        self.assertEqual([row["name"] for row in result["incidents"]], ["Plaskett", "Timber"])
        self.assertEqual(result["incidents"][0]["state"], "CA")
        self.assertEqual(result["incidents"][0]["latitude"], 36.2159)
        self.assertEqual(result["incidents"][0]["type_name"], "wildfire")
        self.assertEqual(result["acres"], 55429.0)
        self.assertEqual(result["dataset"], DATASET)

    def test_incidents_containment_filter(self):
        layer = FakeLayer()
        result = layer.incidents(contained_below=50, limit=10)
        self.assertEqual([row["name"] for row in result["incidents"]], ["DOME"])

    def test_near_sorts_by_distance_and_reports_miles(self):
        layer = FakeLayer()
        result = layer.near(39.74, -104.99, radius_miles=150, limit=10)
        self.assertEqual([row["name"] for row in result["incidents"]], ["Willow"])
        self.assertTrue(50 <= result["incidents"][0]["distance_miles"] <= 100,
                        result["incidents"][0]["distance_miles"])
        self.assertEqual(result["origin"], {"latitude": 39.74, "longitude": -104.99})

    def test_summary_groups_by_state(self):
        layer = FakeLayer()
        national = layer.summary()
        self.assertEqual(national["count"], 4)
        self.assertEqual(national["by_state"][0]["state"], "CA")
        self.assertEqual(national["by_state"][0]["count"], 3)
        self.assertEqual(national["uncontained"], 1)  # only DOME is under half contained

        state = layer.summary(state="CA")
        self.assertEqual(state["count"], 3)
        self.assertEqual(state["scope"], "CA")

    def test_lookup_wildcards_and_escapes(self):
        layer = FakeLayer()
        result = layer.lookup("tim")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["incidents"][0]["name"], "Timber")
        self.assertIn("UPPER(IncidentName) LIKE '%TIM%'", layer.queries[-1]["where"])

    def test_lookup_rejects_a_too_short_name(self):
        with self.assertRaises(ValueError):
            FakeLayer().lookup("x")

    def test_an_arcgis_error_payload_is_raised_as_a_value_error(self):
        client = WildfireClient()
        client.get_json = lambda url, params=None, ttl=None: {"error": {"message": "Invalid where clause"}}
        with self.assertRaises(ValueError):
            client.incidents()


class ParseTests(unittest.TestCase):
    def test_active_by_default(self):
        parsed = parse(_message("what wildfires are burning right now?"))
        self.assertEqual(parsed["skill"], "fire-active")
        self.assertFalse(parsed["explicit"])

    def test_state_from_text(self):
        self.assertEqual(parse(_message("what fires are burning in CA?"))["params"]["state"], "CA")
        self.assertEqual(parse(_message("fires in Oregon?"))["params"]["state"], "OR")
        self.assertIsNone(state_from_text("is it in or out"))
        self.assertIsNone(state_from_text("ok thanks"))

    def test_acres_and_containment_filters(self):
        parsed = parse(_message("show me wildfires over 10,000 acres in CA"))
        # The 10,000 is a size, so this is a filtered list and not a "near 10,000" lookup.
        self.assertEqual(parsed["skill"], "fire-active")
        self.assertEqual(parsed["params"]["min_acres"], 10000.0)
        self.assertEqual(parsed["params"]["state"], "CA")
        self.assertNotIn("point", parsed["params"])
        self.assertEqual(acres_from_text("over 1,500 acres"), 1500.0)
        self.assertIsNone(acres_from_text("no numbers here"))
        uncontained = parse(_message("any uncontained fires in OR?"))
        self.assertEqual(uncontained["params"]["contained_below"], 50)

    def test_a_size_phrase_is_never_read_as_a_point(self):
        self.assertEqual(point_from_text("any fires near 39.74,-104.99?"), "39.74,-104.99")
        self.assertIsNone(point_from_text("over 5,000 acres in Idaho"))
        self.assertIsNone(point_from_text("12,345 acres burned"))
        self.assertIsNone(point_from_text("nothing here"))

    def test_near_by_an_unknown_place_is_refused_by_name(self):
        parsed = parse(_message("any fires near Gotham?"))
        self.assertEqual(parsed["skill"], "fire-near")
        self.assertEqual(parsed["params"]["place"], "gotham")
        # "near me" names nobody, so it stays a place-less near read.
        self.assertNotIn("place", parse(_message("is anything burning near me?"))["params"])

    def test_near_by_point_and_city(self):
        by_point = parse(_message("any fires near 39.74,-104.99?"))
        self.assertEqual(by_point["skill"], "fire-near")
        self.assertEqual(by_point["params"]["point"], "39.74,-104.99")
        by_city = parse(_message("fires within 50 miles of Denver?"))
        self.assertEqual(by_city["params"]["place"], "denver")
        self.assertEqual(by_city["params"]["radius_miles"], 50.0)
        self.assertEqual(radius_from_text("within 200 miles"), 200.0)
        self.assertEqual(place_from_text("near Missoula tonight"), "missoula")

    def test_summary_and_lookup(self):
        self.assertEqual(parse(_message("how many wildfires are burning nationwide?"))["skill"], "fire-summary")
        lookup = parse(_message("tell me about the Timber fire"))
        self.assertEqual(lookup["skill"], "fire-lookup")
        self.assertEqual(lookup["params"]["name"], "Timber")
        self.assertEqual(name_from_text("details on the Plaskett fire"), "Plaskett")
        self.assertIsNone(name_from_text("how many fires are there"))
        # "the" in front of "fire" is an article, not an incident name.
        self.assertIsNone(name_from_text("find the fire"))
        self.assertIsNone(name_from_text("any big fire"))

    def test_watch_with_a_default_threshold(self):
        parsed = parse(_message("tell me when a new large fire starts in Oregon"))
        self.assertEqual(parsed["skill"], "fire-watch")
        self.assertEqual(parsed["params"]["state"], "OR")
        self.assertEqual(parsed["params"]["min_acres"], 1000.0)
        explicit = parse(_message("notify me about fires over 5000 acres in CA"))
        self.assertEqual(explicit["params"]["min_acres"], 5000.0)

    def test_data_part_wins(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "fire-active", "state": "OR", "limit": 3}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "fire-active")
        self.assertEqual(parsed["params"]["state"], "OR")
        self.assertEqual(parsed["params"]["limit"], 3)


def _message(text, **extra):
    payload = {
        "kind": "message",
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    payload.update(extra)
    return payload


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["fire-active", "fire-near", "fire-summary", "fire-lookup", "fire-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = WildfireAgent(FakeLayer())
        self.assertEqual(agent.missing("fire-active", {}), [])
        self.assertEqual(agent.missing("fire-near", {}), ["location"])
        self.assertEqual(agent.missing("fire-near", {"point": "39.74,-104.99"}), [])

    def test_active_lists_fires_with_containment(self):
        result = WildfireAgent(FakeLayer()).run({"skill": "fire-active", "params": {"state": "CA"}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("Plaskett", result["message"])
        self.assertIn("97% contained", result["message"])
        self.assertIn(DATASET, result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET)
        self.assertIsNone(result["watch"])

    def test_active_when_nothing_is_burning(self):
        result = WildfireAgent(FakeLayer(features=[])).run({"skill": "fire-active", "params": {}, "missing": []})
        self.assertIn("No active wildfire matches", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_near_reports_distance_and_the_not_an_evacuation_notice(self):
        result = WildfireAgent(FakeLayer()).run(
            {"skill": "fire-near", "params": {"place": "denver", "radius_miles": 150}, "missing": []}
        )
        self.assertIn("within 150 miles of Denver", result["message"])
        self.assertIn("miles away", result["message"])
        self.assertIn("not an evacuation notice", result["message"])

    def test_near_for_an_unknown_place_is_honest(self):
        result = WildfireAgent(FakeLayer()).run(
            {"skill": "fire-near", "params": {"place": "atlantis"}, "missing": []}
        )
        self.assertIn("will not guess", result["message"])
        self.assertEqual(result["artifact"]["known"], False)

    def test_summary_national_and_per_state(self):
        agent = WildfireAgent(FakeLayer())
        national = agent.run({"skill": "fire-summary", "params": {}, "missing": []})
        self.assertIn("active wildfire(s) on the interagency list", national["message"])
        self.assertIn("Still under half contained", national["message"])
        self.assertEqual(national["artifact"]["by_state"][0]["state"], "CA")
        state = agent.run({"skill": "fire-summary", "params": {"state": "CA"}, "missing": []})
        self.assertIn("for CA", state["message"])

    def test_lookup_by_name(self):
        result = WildfireAgent(FakeLayer()).run({"skill": "fire-lookup", "params": {"name": "Timber"}, "missing": []})
        self.assertIn("Timber", result["message"])
        self.assertIn("25,436 acres", result["message"])
        self.assertIn("coordination center: OSCC", result["message"])

    def test_lookup_without_a_name_asks(self):
        result = WildfireAgent(FakeLayer()).run({"skill": "fire-lookup", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "input-required")

    def test_lookup_when_nothing_matches(self):
        result = WildfireAgent(FakeLayer()).run({"skill": "fire-lookup", "params": {"name": "ZZZ"}, "missing": []})
        self.assertIn("No active incident whose name contains", result["message"])

    def test_watch_records_the_observation(self):
        result = WildfireAgent(FakeLayer()).run(
            {"skill": "fire-watch", "params": {"state": "CA", "min_acres": 5000}, "missing": []}
        )
        watch = result["watch"]
        self.assertEqual(watch["kind"], "fire-watch")
        self.assertEqual(watch["state"], "CA")
        self.assertEqual(watch["min_acres"], 5000.0)
        self.assertEqual(watch["observed"]["count"], 2)
        self.assertEqual(watch["observed"]["names"]["2026-CAOSC-002993"], "Plaskett")
        self.assertNotIn("observed", result["artifact"]["watching"])
        self.assertIn("Point a pushNotificationConfig", result["message"])

    def test_missing_input_prompts(self):
        result = WildfireAgent(FakeLayer()).run({"skill": "fire-near", "params": {}, "missing": ["location"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which place", result["message"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, WildfireAgent(FakeLayer()))
        task = handler.handle("message/send", {"message": _message("what fires are burning in CA?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], DATASET)
        self.assertEqual(data["incidents"][0]["state"], "CA")

    def test_near_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, WildfireAgent(FakeLayer()))
        first = handler.handle("message/send", {"message": _message("what fires are near me?")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": _message("Denver", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["place"], "Denver")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    @staticmethod
    def _posted_text(payload) -> str:
        body = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
        if isinstance(body, str):
            body = json.loads(body)
        return body["status"]["message"]["parts"][0]["text"]

    def test_watcher_fires_when_a_new_fire_appears_and_when_one_leaves(self):
        layer = FakeLayer(features=[FEATURES[0]])
        agent = WildfireAgent(layer)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when a new large fire starts in CA")}
        )
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)  # nothing new yet

        layer.features = [FEATURES[0], FEATURES[1]]
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("New wildfire at or above 1,000 acres in CA: Timber", self._posted_text(posted[0]))
        self.assertEqual(watcher.tick(), 0)  # the same set, nothing new

        layer.features = [FEATURES[0]]
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("left the active list", self._posted_text(posted[1]))


if __name__ == "__main__":
    unittest.main()
