"""Tests for the NOAA tides server.

    python3 servers/tides/tests/test_agent.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import PushWatcher, TaskStore  # noqa: E402

from agent import (  # noqa: E402
    CARD_SKILLS,
    NoaaTidesAgent,
    parse,
    station_name_from_text,
)
from data import (  # noqa: E402
    DATASET,
    FLOODLEVELS_PATH,
    STATIONS_DATASET,
    STATIONS_PATH,
    NoaaTidesClient,
    convert,
    haversine_km,
    to_feet,
)

DATING = "2026-09-22T12:00:00Z"

OBSERVATION = {
    "station": "8518750",
    "name": "The Battery",
    "latitude": 40.7006,
    "longitude": -74.0142,
    "time": "2026-09-22 13:00",
    "value": 2.484,
    "units": "english",
    "unit_label": "ft",
    "sigma": 0.144,
    "flags": "1,0,0,0",
    "quality": "p",
    "quality_label": "preliminary",
    "datum": "MLLW",
    "dataset": DATASET,
}

EVENTS = [
    {"time": "2026-09-22 11:54", "value": 1.3, "units": "english", "unit_label": "ft", "type": "low"},
    {"time": "2026-09-22 18:05", "value": 4.841, "units": "english", "unit_label": "ft", "type": "high"},
    {"time": "2026-09-23 00:31", "value": 0.776, "units": "english", "unit_label": "ft", "type": "low"},
    {"time": "2026-09-23 06:38", "value": 4.452, "units": "english", "unit_label": "ft", "type": "high"},
]

LEVELS = {
    "nos_minor": 10.19, "nos_moderate": 11.12, "nos_major": 12.39,
    "nws_minor": 10.49, "nws_moderate": 11.74, "nws_major": 13.24,
    "action": 10.29, "units": "english",
}

NEW_YORK = {
    "id": "8518750", "name": "NEW YORK (The Battery)", "state": "NY", "lat": 40.7006, "lng": -74.0142,
    "tideType": "", "timezonecorr": -5, "affiliations": "",
}
BATTERY_CREEK = {
    "id": "8668092", "name": "Battery Creek, 4 mi. above entrance", "state": "SC", "lat": 32.4133,
    "lng": -80.7, "tideType": "", "timezonecorr": -5, "affiliations": "",
}
SEATTLE = {
    "id": "9447130", "name": "SEATTLE", "state": "WA", "lat": 47.6026, "lng": -122.3393,
    "tideType": "", "timezonecorr": -8, "affiliations": "",
}


def _no_network(*args, **kwargs):
    raise AssertionError("FakeTides must not touch the network")


class FakeTides(NoaaTidesClient):
    """Same interface as NoaaTidesClient, no network. Derived helpers stay real."""

    def __init__(self, observation=OBSERVATION, events=EVENTS, matches=(NEW_YORK,),
                 levels=LEVELS) -> None:
        super().__init__(fetch=_no_network)
        self.observation = observation
        self.events = list(events)
        self.matches = list(matches)
        self.levels = dict(levels)
        self.reads = []

    def latest_observation(self, station):
        self.reads.append(("observation", station))
        return dict(self.observation) if self.observation else None

    def predictions(self, station, days=2, interval="hilo"):
        self.reads.append(("predictions", station, days))
        return [dict(event) for event in self.events]

    def stations(self, name=None, state=None, point=None, radius_km=50.0, limit=10):
        self.reads.append(("stations", name, state, point))
        rows = list(self.matches)
        if name:
            rows = [row for row in rows if name.lower() in (row.get("name") or "").lower()]
        if state:
            rows = [row for row in rows if (row.get("state") or "").upper() == state.upper()]
        return rows[:limit]

    def flood_levels(self, station):
        self.reads.append(("levels", station))
        return dict(self.levels)


def fetch_from(payloads, calls=None):
    """A JsonApiClient `fetch` that dispatches on path, so data.py runs for real."""

    def fetch(url, params, headers):
        if calls is not None:
            calls.append((url, dict(params)))
        if url.endswith("/stations.json"):
            return payloads["stations"]
        if url.endswith("/datagetter"):
            product = params.get("product")
            key = f"datagetter:{product}"
            payload = payloads[key]
            return payload() if callable(payload) else payload
        if "floodlevels" in url:
            return payloads["floodlevels"]
        if "/stations/" in url:
            return payloads["station"]
        raise AssertionError(f"unexpected url {url}")

    return fetch


def datagetter(rows, key="data"):
    return {key: rows}


class ValidationTests(unittest.TestCase):
    def test_check_station(self):
        self.assertEqual(NoaaTidesClient.check_station(" 8518750 "), "8518750")
        for bad in ("851875", "85187500", "Battery", ""):
            with self.assertRaises(ValueError):
                NoaaTidesClient.check_station(bad)

    def test_check_days_point_threshold(self):
        self.assertEqual(NoaaTidesClient.check_days("3"), 3)
        for bad in ("0", "8", "two"):
            with self.assertRaises(ValueError):
                NoaaTidesClient.check_days(bad)
        self.assertEqual(NoaaTidesClient.check_point("40.7006, -74.0142"), (40.7006, -74.0142))
        with self.assertRaises(ValueError):
            NoaaTidesClient.check_point("91.0,-74.0")
        self.assertEqual(NoaaTidesClient.check_threshold("6.25"), 6.25)
        with self.assertRaises(ValueError):
            NoaaTidesClient.check_threshold("deep")

    def test_check_stage_units_limit(self):
        self.assertEqual(NoaaTidesClient.check_flood_stage("Minor"), "minor")
        with self.assertRaises(ValueError):
            NoaaTidesClient.check_flood_stage("catastrophic")
        self.assertEqual(NoaaTidesClient.check_units("METRIC"), "metric")
        with self.assertRaises(ValueError):
            NoaaTidesClient.check_units("fathoms")
        self.assertEqual(NoaaTidesClient.check_limit("5"), 5)
        with self.assertRaises(ValueError):
            NoaaTidesClient.check_limit("0")

    def test_unit_helpers(self):
        self.assertEqual(to_feet(1.0, "english"), 1.0)
        self.assertAlmostEqual(to_feet(1.0, "metric"), 3.28084, places=5)
        self.assertAlmostEqual(convert(3.28084, "english", "metric"), 1.0, places=5)
        self.assertAlmostEqual(convert(1.0, "metric", "english"), 3.28084, places=5)
        self.assertEqual(convert(2.5, "english", "english"), 2.5)

    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(40.7006, -74.0142, 40.7006, -74.0142), 0.0, places=6)
        self.assertAlmostEqual(haversine_km(40.7006, -74.0142, 47.6026, -122.3393), 3862, delta=25)


class ReaderTests(unittest.TestCase):
    def test_predictions_maps_type_and_errors(self):
        calls = []
        payload = {"predictions": [{"t": "2026-09-22 18:05", "v": "4.841", "type": "H"},
                                   {"t": "2026-09-23 00:31", "v": "0.776", "type": "L"}]}
        client = NoaaTidesClient(fetch=fetch_from({"datagetter:predictions": payload}, calls))
        events = client.predictions("8518750", days=2)
        self.assertEqual([event["type"] for event in events], ["high", "low"])
        self.assertEqual(events[0]["time"], "2026-09-22 18:05")
        url, params = calls[0]
        self.assertTrue(url.endswith("/api/prod/datagetter"))
        self.assertEqual(params["application"], "a2a-tides")
        self.assertEqual(params["time_zone"], "lst_ldt")
        self.assertEqual(params["interval"], "hilo")

    def test_datagetter_error_body_is_upstream_error(self):
        from a2a_kit import UpstreamError

        payload = {"error": {"message": "No Predictions data was found."}}
        client = NoaaTidesClient(fetch=fetch_from({"datagetter:predictions": payload}))
        with self.assertRaises(UpstreamError):
            client.predictions("8518750")

    def test_latest_observation(self):
        rows = [{"t": "2026-09-22 13:00", "v": "2.484", "s": "0.144", "f": "1,0,0,0", "q": "p"}]
        payload = {"metadata": {"id": "8518750", "name": "The Battery", "lat": "40.7006", "lon": "-74.0142"},
                   "data": rows}
        client = NoaaTidesClient(fetch=fetch_from({"datagetter:water_level": payload}))
        observation = client.latest_observation("8518750")
        self.assertEqual(observation["value"], 2.484)
        self.assertEqual(observation["name"], "The Battery")
        self.assertEqual(observation["quality_label"], "preliminary")
        self.assertEqual(observation["latitude"], 40.7006)

    def test_upcoming_tides_drops_events_before_the_station_clock(self):
        payload = {"predictions": [
            {"t": "2026-09-22 11:54", "v": "1.3", "type": "L"},
            {"t": "2026-09-22 18:05", "v": "4.841", "type": "H"},
        ]}
        water = {"metadata": {"id": "8518750", "name": "The Battery"}, "data": [{"t": "2026-09-22 13:00", "v": "2.484"}]}
        client = NoaaTidesClient(fetch=fetch_from({"datagetter:predictions": payload,
                                                  "datagetter:water_level": water}))
        table = client.upcoming_tides("8518750", days=2)
        self.assertEqual([event["type"] for event in table["events"]], ["high"])
        self.assertEqual(table["place"], "The Battery (8518750)")

    def test_flood_levels_and_stage_lookup(self):
        payload = {"nos_minor": 10.19, "nos_moderate": 11.12, "nos_major": 12.39,
                   "nws_minor": 10.49, "nws_moderate": 11.74, "nws_major": 13.24, "action": 10.29}
        client = NoaaTidesClient(fetch=fetch_from({"floodlevels": payload}))
        self.assertEqual(client.flood_levels("8518750")["nos_minor"], 10.19)
        self.assertEqual(client.threshold_for_stage("8518750", "moderate"), 11.12)
        self.assertEqual(client.threshold_for_stage("8518750", "moderate", "metric"), 3.389)
        self.assertEqual(client.flood_stage_for_value("8518750", 2.484), "normal")
        self.assertEqual(client.flood_stage_for_value("8518750", 11.5), "moderate flooding")
        self.assertEqual(client.flood_stage_for_value("8518750", 13.0), "major flooding")

    def test_station_search_filters_and_sorts_by_distance(self):
        payload = {"stations": [NEW_YORK, BATTERY_CREEK, SEATTLE],
                   "count": 3}
        calls = []
        client = NoaaTidesClient(fetch=fetch_from({"stations": payload}, calls))
        self.assertEqual([row["id"] for row in client.stations(name="Battery")], ["8518750", "8668092"])
        self.assertEqual([row["id"] for row in client.stations(state="WA")], ["9447130"])
        near = client.stations(point="40.7006,-74.0142", limit=3)
        self.assertEqual(near[0]["id"], "8518750")
        self.assertLess(near[0]["distance_km"], near[1]["distance_km"])
        self.assertEqual(client.nearest_station("40.7006,-74.0142")["id"], "8518750")
        # The 2 MB catalogue is fetched once and then served from cache.
        self.assertEqual(len([call for call in calls if call[0].endswith(STATIONS_PATH)]), 1)
        self.assertEqual(client.stations(point="47.6,-122.3")[0]["id"], "9447130")


class ParseTests(unittest.TestCase):
    def test_default_is_predictions(self):
        request = parse({"parts": [{"kind": "text", "text": "what are the tides today?"}]})
        self.assertEqual(request["skill"], "tide-predictions")

    def test_station_point_and_days(self):
        request = parse({"parts": [{"kind": "text", "text": "tide table for the next 3 days near 40.7006,-74.0142"}]})
        self.assertEqual(request["skill"], "tide-predictions")
        self.assertEqual(request["params"]["point"], "40.7006,-74.0142")
        self.assertEqual(request["params"]["days"], "3")

    def test_station_id_and_next(self):
        request = parse({"parts": [{"kind": "text", "text": "when is the next high tide at 8518750?"}]})
        self.assertEqual(request["skill"], "tide-next")
        self.assertEqual(request["params"]["station"], "8518750")

    def test_water_level_wording(self):
        request = parse({"parts": [{"kind": "text", "text": "how high is the water at 8518750 right now?"}]})
        self.assertEqual(request["skill"], "water-level")
        self.assertEqual(request["params"]["station"], "8518750")

    def test_station_search(self):
        request = parse({"parts": [{"kind": "text", "text": "which tide stations are near 40.7,-74.0?"}]})
        self.assertEqual(request["skill"], "stations")
        self.assertEqual(request["params"]["point"], "40.7,-74.0")

    def test_watch_with_threshold(self):
        request = parse({"parts": [{"kind": "text", "text": "notify me when 8518750 goes above 6 ft"}]})
        self.assertEqual(request["skill"], "tide-watch")
        self.assertEqual(request["params"]["station"], "8518750")
        self.assertEqual(request["params"]["threshold"], "6")
        self.assertEqual(request["params"]["units"], "english")

    def test_watch_with_flood_stage(self):
        request = parse({"parts": [{"kind": "text", "text": "tell me when the water hits minor flood stage near 40.7006,-74.0142"}]})
        self.assertEqual(request["skill"], "tide-watch")
        self.assertEqual(request["params"]["flood_stage"], "minor")
        self.assertEqual(request["params"]["point"], "40.7006,-74.0142")

    def test_metric_hint(self):
        request = parse({"parts": [{"kind": "text", "text": "tides at 8518750 in meters"}]})
        self.assertEqual(request["params"]["units"], "metric")

    def test_explicit_json_wins(self):
        request = parse({"parts": [{"kind": "data", "data": {"skill": "water-level", "station": "9447130"}}]})
        self.assertEqual(request["skill"], "water-level")
        self.assertEqual(request["params"]["station"], "9447130")
        self.assertTrue(request["explicit"])

    def test_help_and_empty(self):
        self.assertEqual(parse({"parts": [{"kind": "text", "text": ""}]})["skill"], "help")
        self.assertEqual(parse({"parts": [{"kind": "text", "text": "what can you do?"}]})["skill"], "help")
        self.assertEqual(parse({"parts": [{"kind": "text", "text": "hello there"}]})["skill"], "tide-predictions")

    def test_name_extraction_is_conservative(self):
        self.assertEqual(station_name_from_text("tides at The Battery today?", None), "The Battery")
        self.assertIsNone(station_name_from_text("tides for the next 3 days", None))
        self.assertIsNone(station_name_from_text("tides at 8518750", "8518750"))


class SkillTests(unittest.TestCase):
    def run_skill(self, skill, client=None, **params):
        agent = NoaaTidesAgent(client=client or FakeTides())
        request = {"skill": skill, "params": params, "missing": agent.missing(skill, params)}
        return agent.run(request)

    def test_predictions_cite_dataset_and_datum(self):
        result = self.run_skill("tide-predictions", station="8518750", days=2)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn(DATASET, result["message"])
        self.assertIn("MLLW", result["message"])
        self.assertIn("The Battery", result["message"])
        self.assertIn("2026-09-22 18:05", result["message"])
        artifact = result["artifact"]
        self.assertEqual(artifact["station"], "8518750")
        self.assertEqual(len(artifact["events"]), 3)
        self.assertEqual(artifact["dataset"], DATASET)

    def test_next_tide(self):
        result = self.run_skill("tide-next", station="8518750")
        self.assertIn("Next high tide: 2026-09-22 18:05 at 4.841 ft", result["message"])
        self.assertIn("Next low tide: 2026-09-23 00:31 at 0.776 ft", result["message"])
        self.assertEqual(result["artifact"]["next_high"]["time"], "2026-09-22 18:05")

    def test_water_level_reports_stage_and_quality(self):
        result = self.run_skill("water-level", station="8518750")
        self.assertIn("2.484 ft over MLLW", result["message"])
        self.assertIn("normal", result["message"])
        self.assertIn("minor flood stage here is 10.19 ft", result["message"])
        self.assertIn("preliminary", result["message"])
        self.assertEqual(result["artifact"]["flood_stage"], "normal")

    def test_water_level_when_flooding(self):
        result = self.run_skill("water-level", client=FakeTides(observation={**OBSERVATION, "value": 11.6}),
                                station="8518750")
        self.assertIn("moderate flooding", result["message"])

    def test_stations_lists_matches(self):
        result = self.run_skill("stations", client=FakeTides(matches=(NEW_YORK, BATTERY_CREEK)),
                                station_name="Battery", limit=10)
        self.assertIn("8518750", result["message"])
        self.assertIn("8668092", result["message"])
        self.assertEqual(result["artifact"]["dataset"], STATIONS_DATASET)

    def test_stations_requires_a_place(self):
        agent = NoaaTidesAgent(client=FakeTides())
        self.assertEqual(agent.missing("stations", {}), ["place"])
        self.assertIn("state code", agent.input_prompt("stations", ["place"]))

    def test_tide_skills_require_a_place(self):
        agent = NoaaTidesAgent(client=FakeTides())
        self.assertEqual(agent.missing("tide-next", {}), ["station"])
        result = agent.run({"skill": "tide-next", "params": {}, "missing": ["station"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("seven-digit NOAA station id", result["message"])

    def test_ambiguous_name_asks_which_station(self):
        result = self.run_skill("tide-predictions", client=FakeTides(matches=(NEW_YORK, BATTERY_CREEK)),
                                station_name="Battery")
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("matches more than one station", result["message"])
        self.assertIn("8518750", result["message"])

    def test_unknown_name_is_reported_not_guessed(self):
        result = self.run_skill("tide-predictions", client=FakeTides(matches=()), station_name="Atlantis")
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("No NOAA tide station matches", result["message"])

    def test_point_resolves_to_nearest_station(self):
        result = self.run_skill("tide-next", client=FakeTides(matches=(SEATTLE,)), point="47.6,-122.3")
        self.assertIn("9447130", result["message"])

    def test_point_without_a_station_nearby(self):
        result = self.run_skill("tide-next", client=FakeTides(matches=()), point="0.0,0.0", radius_km=25)
        self.assertIn("No NOAA tide station within 25 km", result["message"])

    def test_watch_defaults_to_minor_flood_stage(self):
        client = FakeTides()
        result = self.run_skill("tide-watch", client=client, station="8518750")
        watch = result["watch"]
        self.assertEqual(watch["kind"], "tide-watch")
        self.assertEqual(watch["threshold"], 10.19)
        self.assertEqual(watch["threshold_ft"], 10.19)
        self.assertEqual(watch["flood_stage"], "minor")
        self.assertFalse(watch["observed"]["above"])
        self.assertIn("below", result["message"])
        self.assertIn("pushNotificationConfig", result["message"])

    def test_watch_with_numeric_threshold_above_the_water(self):
        result = self.run_skill("tide-watch", client=FakeTides(), station="8518750", threshold="2")
        self.assertTrue(result["watch"]["observed"]["above"])
        self.assertIn("at or above", result["message"])

    def test_watch_with_numeric_threshold_below_the_water(self):
        result = self.run_skill("tide-watch", client=FakeTides(), station="8518750", threshold="6")
        self.assertFalse(result["watch"]["observed"]["above"])
        self.assertIn("below", result["message"])

    def test_watch_without_flood_levels_asks_for_a_number(self):
        result = self.run_skill("tide-watch", client=FakeTides(levels={}), station="8518750")
        self.assertIsNone(result["watch"])
        self.assertIn("tell me a height", result["message"])

    def test_watch_applies_the_threshold_in_metric(self):
        levels = {"nos_minor": 3.1, "units": "english"}
        result = self.run_skill("tide-watch", client=FakeTides(levels=levels), station="8518750", units="metric")
        self.assertEqual(result["watch"]["threshold"], 0.945)
        self.assertEqual(result["watch"]["threshold_ft"], 3.1)

    def test_help_does_not_touch_the_feed(self):
        result = self.run_skill("help")
        self.assertIn("NOAA", result["message"])
        self.assertIsNone(result["artifact"])

    def test_card_skills_and_watch_kinds(self):
        agent = NoaaTidesAgent(client=FakeTides())
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["tide-predictions", "tide-next", "water-level", "stations", "tide-watch"])
        self.assertEqual(agent.watch_kinds, ("tide-watch",))
        self.assertEqual(agent.env_prefix, "NOAA_TIDES")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.watch = {
            "kind": "tide-watch",
            "dataset": DATASET,
            "station": "8518750",
            "station_name": "The Battery",
            "threshold": 10.19,
            "threshold_ft": 10.19,
            "threshold_source": "NOAA's minor flood stage for this station",
            "units": "english",
            "flood_stage": "minor",
            "observed": {"station": "8518750", "threshold_ft": 10.19, "above": False},
        }
        self.task = {
            "kind": "task",
            "id": "watched",
            "contextId": "ctx",
            "status": {"state": "completed", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {"watch": self.watch, "last_observed": dict(self.watch["observed"])},
        }
        self.store.save_task(self.task)
        self.store.set_push_config("watched", {"id": "cfg", "url": "https://example.com/hook", "token": "tok"})

    def tearDown(self):
        self.store.close()

    def test_probe_is_stable_while_the_water_is_safely_below(self):
        client = FakeTides()
        agent = NoaaTidesAgent(client=client)
        first = agent.probe_watch(self.watch)
        client.observation = {**OBSERVATION, "value": 2.9}  # value moved, still below
        second = agent.probe_watch(self.watch)
        self.assertEqual(first, second)
        self.assertFalse(first["above"])

    def test_probe_flips_only_when_the_water_crosses(self):
        agent = NoaaTidesAgent(client=FakeTides(observation={**OBSERVATION, "value": 10.5}))
        self.assertTrue(agent.probe_watch(self.watch)["above"])

    def test_probe_ignores_incomplete_watches(self):
        agent = NoaaTidesAgent(client=FakeTides())
        self.assertIsNone(agent.probe_watch({"kind": "tide-watch", "station": "8518750"}))

    def test_describe_crossing_and_receding(self):
        agent = NoaaTidesAgent(client=FakeTides(observation={**OBSERVATION, "value": 10.5}))
        rising = agent.describe_watch_change(self.watch, {"above": False}, {"above": True})
        self.assertIn("at or above", rising)
        self.assertIn("minor", rising)
        self.assertIn("10.5", rising)
        falling = agent.describe_watch_change(self.watch, {"above": True}, {"above": False})
        self.assertIn("dropped back below", falling)

    def test_watcher_posts_once_on_the_crossing(self):
        sent = []

        def post(url, payload, headers):
            sent.append((url, json.loads(payload), headers))
            return 200

        agent = NoaaTidesAgent(client=FakeTides(observation={**OBSERVATION, "value": 10.6}))
        watcher = PushWatcher(self.store, agent, interval=5)
        watcher._post = post
        self.assertEqual(watcher.tick(), 1)
        url, event, headers = sent[0]
        self.assertEqual(url, "https://example.com/hook")
        self.assertEqual(headers["X-A2A-Notification-Token"], "tok")
        self.assertEqual(event["status"]["state"], "working")
        self.assertIn("at or above", event["status"]["message"]["parts"][0]["text"])
        self.assertEqual(event["metadata"]["observed"]["above"], True)
        self.assertEqual(watcher.tick(), 0)  # crossing already reported
        self.assertEqual(
            self.store.get_task("watched")["metadata"]["last_observed"]["above"], True
        )


if __name__ == "__main__":
    unittest.main()
