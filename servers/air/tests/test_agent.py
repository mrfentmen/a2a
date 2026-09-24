"""Tests for the air-quality server.

    python3 servers/air/tests/test_agent.py
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import A2AHandler, PushWatcher, TaskStore, UpstreamError  # noqa: E402

from agent import (  # noqa: E402
    CARD_SKILLS,
    AirQualityAgent,
    hours_from_text,
    parse,
    place_from_text,
    places_from_text,
    resolve_places,
    threshold_from_text,
    unknown_place_from_text,
)
from data import (  # noqa: E402
    CURRENT_FIELDS,
    DATASET,
    AirQualityClient,
    aqi_band,
    current_hour_utc,
    round_or_none,
)

#: Value shapes copied from live Open-Meteo reads on 2026-09-23 (Delhi AQI 162, New York 39).
UNITS = {
    "time": "iso8601",
    "interval": "seconds",
    "us_aqi": "USAQI",
    "pm2_5": "µg/m³",
    "pm10": "µg/m³",
    "ozone": "µg/m³",
    "nitrogen_dioxide": "µg/m³",
    "sulphur_dioxide": "µg/m³",
    "carbon_monoxide": "µg/m³",
    "uv_index": "",
    "alder_pollen": "grains/m³",
    "birch_pollen": "grains/m³",
    "grass_pollen": "grains/m³",
    "mugwort_pollen": "grains/m³",
    "olive_pollen": "grains/m³",
    "ragweed_pollen": "grains/m³",
}

PROFILES = {
    28.61: {"us_aqi": 162, "pm2_5": 58.3, "pm10": 102.4, "ozone": 58.0, "nitrogen_dioxide": 25.2,
            "sulphur_dioxide": 24.9, "carbon_monoxide": 616.0, "uv_index": 8.6},
    39.74: {"us_aqi": 50, "pm2_5": 13.4, "pm10": 20.0, "ozone": 90.0, "uv_index": 6.1,
            "grass_pollen": 12.0},
    33.45: {"us_aqi": 51, "pm2_5": 10.6},
}
DEFAULT_VALUES = {"us_aqi": 42, "pm2_5": 9.1, "ozone": 60.0}


class _FakeResponse:
    """The slice of an http.client response that the transport actually touches."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _current(values: dict) -> dict:
    payload = {field: values.get(field) for field in CURRENT_FIELDS}
    payload["time"] = "2026-09-24T00:00"
    payload["interval"] = 3600
    return payload


class FakeAir(AirQualityClient):
    """An AirQualityClient whose transport is a canned model grid, so data.py runs for real."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []
        #: set this to override every location's values (the watch tests do)
        self.values: dict | None = None

    def profile(self, latitude: float) -> dict:
        if self.values is not None:
            return dict(self.values)
        return {**DEFAULT_VALUES, **PROFILES.get(round(latitude, 2), {})}

    @staticmethod
    def _one(latitude: str, longitude: str, values: dict) -> dict:
        return {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "elevation": 1600.0,
            "current_units": UNITS,
            "current": _current(values),
        }

    @staticmethod
    def _hourly(days: int) -> dict:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        times = [(midnight + timedelta(hours=index)).strftime("%Y-%m-%dT%H:00")
                 for index in range(days * 24)]
        aqi = [40 + (index % 5) * 20 for index in range(days * 24)]
        return {
            "latitude": 39.74,
            "longitude": -104.99,
            "hourly_units": {"time": "iso8601", "us_aqi": "USAQI", "pm2_5": "µg/m³"},
            "hourly": {"time": times, "us_aqi": aqi,
                       "pm2_5": [round(value / 2, 1) for value in aqi],
                       "pm10": list(aqi), "ozone": [50 for _ in aqi]},
        }

    def _http_get(self, url, params, headers):
        params = dict(params or {})
        self.calls.append(params)
        latitudes = str(params.get("latitude") or "").split(",")
        longitudes = str(params.get("longitude") or "").split(",")
        if params.get("hourly"):
            return self._hourly(int(params.get("forecast_days") or "1"))
        if len(latitudes) > 1:
            return [self._one(lat, lon, self.profile(float(lat)))
                    for lat, lon in zip(latitudes, longitudes)]
        return self._one(latitudes[0], longitudes[0], self.profile(float(latitudes[0])))


class ClientTests(unittest.TestCase):
    def test_aqi_bands_follow_the_epa_breakpoints(self):
        self.assertEqual(aqi_band(0)[0], "Good")
        self.assertEqual(aqi_band(50)[0], "Good")
        self.assertEqual(aqi_band(51)[0], "Moderate")
        self.assertEqual(aqi_band(100)[0], "Moderate")
        self.assertEqual(aqi_band(101)[0], "Unhealthy for Sensitive Groups")
        self.assertEqual(aqi_band(150)[0], "Unhealthy for Sensitive Groups")
        self.assertEqual(aqi_band(151)[0], "Unhealthy")
        self.assertEqual(aqi_band(200)[0], "Unhealthy")
        self.assertEqual(aqi_band(201)[0], "Very Unhealthy")
        self.assertEqual(aqi_band(301)[0], "Hazardous")
        self.assertEqual(aqi_band(900)[0], "Hazardous")
        self.assertEqual(aqi_band(None)[0], "unavailable")
        self.assertTrue(aqi_band(162)[1])

    def test_round_or_none_keeps_nulls_out_of_the_maths(self):
        self.assertEqual(round_or_none(58.32), 58.3)
        self.assertIsNone(round_or_none(None))
        self.assertIsNone(round_or_none(""))

    def test_check_point_lat_lon(self):
        self.assertEqual(AirQualityClient.check_point("39.74, -104.99"), (39.74, -104.99))
        with self.assertRaises(ValueError):
            AirQualityClient.check_point("39.74")
        with self.assertRaises(ValueError):
            AirQualityClient.check_lat(999)
        with self.assertRaises(ValueError):
            AirQualityClient.check_lon(181)
        with self.assertRaises(ValueError):
            AirQualityClient.check_lat("north")

    def test_check_hours_check_threshold_bounds(self):
        self.assertEqual(AirQualityClient.check_hours("48"), 48)
        with self.assertRaises(ValueError):
            AirQualityClient.check_hours(0)
        with self.assertRaises(ValueError):
            AirQualityClient.check_hours(73)
        self.assertEqual(AirQualityClient.check_threshold("150"), 150.0)
        with self.assertRaises(ValueError):
            AirQualityClient.check_threshold(501)
        with self.assertRaises(ValueError):
            AirQualityClient.check_threshold("bad")

    def test_city_lookup_is_exact_then_fuzzy(self):
        self.assertEqual(AirQualityClient.city("Delhi"), (28.61, 77.21, "Delhi"))
        self.assertEqual(AirQualityClient.city("denver"), (39.74, -104.99, "Denver"))
        self.assertEqual(AirQualityClient.city("  new   york ")[2], "New York")
        self.assertIsNone(AirQualityClient.city("Atlantis"))
        self.assertIsNone(AirQualityClient.city(""))

    def test_now_returns_pollutants_units_and_a_category(self):
        read = FakeAir().now(28.61, 77.21)
        self.assertEqual(read["dataset"], DATASET)
        self.assertEqual(read["aqi"], 162.0)
        self.assertEqual(read["category"], "Unhealthy")
        self.assertEqual(read["pollutants"]["pm2_5"], 58.3)
        self.assertEqual(read["units"]["pm2_5"], "µg/m³")
        self.assertIsNone(read["pollen"])  # Delhi publishes no pollen
        self.assertTrue(read["guidance"])

    def test_now_reports_pollen_when_the_grid_has_it(self):
        read = FakeAir().now(39.74, -104.99)
        self.assertEqual(read["pollen"]["grass_pollen"], 12.0)
        self.assertIsNone(read["pollen"]["birch_pollen"])

    def test_reads_are_cached_per_query(self):
        client = FakeAir()
        client.now(39.74, -104.99)
        client.now(39.74, -104.99)
        self.assertEqual(len(client.calls), 1)

    def test_forecast_starts_at_the_current_hour_and_finds_the_peak(self):
        read = FakeAir().forecast(39.74, -104.99, hours=24)
        self.assertEqual(read["hours"], 24)
        self.assertEqual(read["first_hour"], current_hour_utc())
        self.assertEqual(read["peak"]["aqi"], 120.0)
        self.assertEqual(read["cleanest"]["aqi"], 40.0)
        self.assertEqual(read["peak"]["category"], "Unhealthy for Sensitive Groups")
        stamps = [row["time"] for row in read["series"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(read["units"]["us_aqi"], "USAQI")

    def test_forecast_asks_for_enough_days_and_never_more_than_the_api_allows(self):
        client = FakeAir()
        client.forecast(39.74, -104.99, hours=72)
        self.assertEqual(client.calls[-1]["forecast_days"], "4")
        with self.assertRaises(ValueError):
            client.forecast(39.74, -104.99, hours=73)

    def test_ranking_returns_one_row_per_place_worst_first(self):
        client = FakeAir()
        read = client.ranking([("Delhi", 28.61, 77.21), ("Denver", 39.74, -104.99)])
        self.assertEqual([row["place"] for row in read["places"]], ["Delhi", "Denver"])
        self.assertEqual(read["worst"]["place"], "Delhi")
        self.assertEqual(read["best"]["place"], "Denver")
        self.assertEqual(read["units"]["us_aqi"], "USAQI")
        self.assertEqual(len(client.calls[-1]["latitude"].split(",")), 2)

    def test_ranking_rejects_an_empty_or_oversized_list(self):
        with self.assertRaises(ValueError):
            FakeAir().ranking([])
        with self.assertRaises(ValueError):
            FakeAir().ranking([(f"p{index}", index, index) for index in range(30)])

    def test_ranking_notices_a_mismatched_location_count(self):
        client = FakeAir()
        client._fetch = lambda url, params, headers: {"latitude": 1.0, "current": {}}
        with self.assertRaises(ValueError):
            client.ranking([("Delhi", 28.61, 77.21), ("Denver", 39.74, -104.99)])

    def test_watch_state_compares_only_the_band_and_the_crossing(self):
        client = FakeAir()
        over = client.watch_state(28.61, 77.21, 100)
        self.assertEqual(over, {"band": "Unhealthy", "above": True})
        self.assertEqual(over, client.watch_state(28.61, 77.21, 100))
        self.assertEqual(client.watch_state(39.74, -104.99, 50), {"band": "Good", "above": True})

    def test_the_400_body_reason_is_surfaced(self):
        exc = HTTPError("https://air-quality-api.open-meteo.com/v1/air-quality", 400, "Bad Request",
                        {}, io.BytesIO(b'{"error":true,"reason":"Latitude must be in range of -90 to 90."}'))
        self.assertEqual(AirQualityClient._reason(exc), "Latitude must be in range of -90 to 90.")
        plain = HTTPError("https://air-quality-api.open-meteo.com/v1/air-quality", 500, "Server Error",
                          {}, io.BytesIO(b"<html>boom</html>"))
        self.assertIn("Server Error", AirQualityClient._reason(plain))

    def test_an_error_payload_from_the_real_transport_is_an_upstream_error(self):
        # Open-Meteo flags errors with `"error": true` (a bool), which the kit's
        # string check does not catch — the client's own transport has to.
        client = AirQualityClient()
        body = b'{"error":true,"reason":"Invalid value: bogus_field"}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            with self.assertRaises(UpstreamError) as caught:
                client.now(39.74, -104.99)
        self.assertIn("Invalid value: bogus_field", str(caught.exception))

    def test_a_400_with_a_reason_body_is_reported_with_that_reason(self):
        client = AirQualityClient()
        exc = HTTPError("https://air-quality-api.open-meteo.com/v1/air-quality", 400, "Bad Request",
                        {}, io.BytesIO(b'{"error":true,"reason":"Latitude must be in range of -90 to 90."}'))
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(UpstreamError) as caught:
                client.now(39.74, -104.99)
        self.assertIn("Latitude must be in range of -90 to 90.", str(caught.exception))

    def test_a_payload_without_current_is_rejected(self):
        client = FakeAir()
        client._fetch = lambda url, params, headers: {"latitude": 1.0}
        with self.assertRaises(ValueError):
            client.now(39.74, -104.99)


class ParseTests(unittest.TestCase):
    def test_now_is_the_default_skill(self):
        parsed = parse(_message("how is the air today?"))
        self.assertEqual(parsed["skill"], "air-now")
        self.assertFalse(parsed["explicit"])

    def test_a_place_the_list_does_not_have_is_still_reported(self):
        # "in Atlantis" must be answered with "I do not know it", not "which place?", so the
        # caller learns the name was not understood.
        parsed = parse(_message("how is the air in Atlantis?"))
        self.assertEqual(parsed["skill"], "air-now")
        self.assertEqual(parsed["params"]["place"], "Atlantis")
        self.assertNotIn("location", AirQualityAgent(FakeAir()).missing(parsed["skill"], parsed["params"]))
        self.assertEqual(unknown_place_from_text("anything near New Atlantis"), "New Atlantis")
        self.assertIsNone(unknown_place_from_text("how is the air here?"))
        self.assertIsNone(unknown_place_from_text("how is the air today?"))

    def test_place_and_point_parsing(self):
        self.assertEqual(parse(_message("how bad is the air in Delhi?"))["params"]["place"], "delhi")
        self.assertEqual(place_from_text("air quality in New York tonight"), "new york")
        self.assertIsNone(place_from_text("no city here"))
        by_point = parse(_message("what is the aqi at 39.74,-104.99?"))
        self.assertEqual(by_point["params"]["point"], "39.74,-104.99")

    def test_forecast_skill_and_hours(self):
        parsed = parse(_message("what will the air quality be like in Denver for the next 48 hours?"))
        self.assertEqual(parsed["skill"], "air-forecast")
        self.assertEqual(parsed["params"]["hours"], 48)
        self.assertEqual(parse(_message("air forecast for Denver"))["params"]["hours"], 24)
        self.assertEqual(hours_from_text("next 12 hours"), 12)
        self.assertEqual(hours_from_text("48 hours"), 48)
        self.assertIsNone(hours_from_text("no window here"))

    def test_ranking_skill_collects_every_place_named(self):
        parsed = parse(_message("which of Denver, Phoenix and Los Angeles has the worst air?"))
        self.assertEqual(parsed["skill"], "air-ranking")
        self.assertEqual(parsed["params"]["places"], ["denver", "phoenix", "los angeles"])
        defaulted = parse(_message("compare air quality across cities"))
        self.assertEqual(defaulted["skill"], "air-ranking")
        self.assertGreater(len(defaulted["params"]["places"]), 3)
        self.assertEqual(places_from_text("delhi or beijing?"), ["delhi", "beijing"])

    def test_watch_skill_and_threshold(self):
        parsed = parse(_message("tell me when the air quality in Denver passes 150"))
        self.assertEqual(parsed["skill"], "air-watch")
        self.assertEqual(parsed["params"]["place"], "denver")
        self.assertEqual(parsed["params"]["threshold"], 150.0)
        defaulted = parse(_message("notify me about bad air in Sacramento"))
        self.assertEqual(defaulted["params"]["threshold"], 100.0)
        self.assertEqual(threshold_from_text("above 200"), 200.0)
        self.assertIsNone(threshold_from_text("above nothing"))

    def test_data_part_wins_over_text(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "air-forecast", "place": "Denver", "hours": 6}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "air-forecast")
        self.assertEqual(parsed["params"]["hours"], 6)
        self.assertTrue(parsed["explicit"])

    def test_places_parameter_accepts_a_string_or_a_list(self):
        as_string = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "air-ranking", "places": "Denver, Phoenix"}}]}
        self.assertEqual(parse(as_string)["params"]["places"], ["Denver", "Phoenix"])
        as_list = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "air-ranking", "places": ["Delhi"]}}]}
        self.assertEqual(parse(as_list)["params"]["places"], ["Delhi"])

    def test_resolve_places_separates_known_from_unknown(self):
        resolved, unknown = resolve_places(["Denver", "Atlantis"], AirQualityClient())
        self.assertEqual([row[0] for row in resolved], ["Denver"])
        self.assertEqual(unknown, ["Atlantis"])
        pointed, _ = resolve_places(["39.74,-104.99"], AirQualityClient())
        self.assertEqual(pointed[0][0], "39.74,-104.99")


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
        self.assertEqual(ids, ["air-now", "air-forecast", "air-ranking", "air-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = AirQualityAgent(FakeAir())
        self.assertEqual(agent.missing("air-now", {}), ["location"])
        self.assertEqual(agent.missing("air-now", {"place": "Denver"}), [])
        self.assertEqual(agent.missing("air-ranking", {}), [])
        self.assertEqual(agent.missing("air-watch", {}), ["location"])

    def test_now_lists_the_band_and_says_what_it_means(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-now", "params": {"place": "Delhi"}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("US AQI 162", result["message"])
        self.assertIn("Unhealthy", result["message"])
        self.assertIn("PM2.5 58.3 µg/m³", result["message"])
        self.assertIn("Europe-only", result["message"])
        self.assertIn(DATASET, result["message"])
        self.assertEqual(result["artifact"]["category"], "Unhealthy")
        self.assertIsNone(result["watch"])

    def test_now_for_an_unknown_place_is_honest(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-now", "params": {"place": "atlantis"}, "missing": []})
        self.assertIn("will not guess", result["message"])
        self.assertEqual(result["artifact"]["known"], False)

    def test_forecast_reports_the_peak_hour(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-forecast", "params": {"place": "Denver", "hours": 24}, "missing": []})
        self.assertIn("Hourly air-quality outlook for Denver", result["message"])
        self.assertIn("Peak US AQI 120", result["message"])
        self.assertEqual(result["artifact"]["hours"], 24)
        self.assertEqual(len(result["artifact"]["series"]), 24)
        self.assertIn("model output", result["message"])

    def test_ranking_puts_the_worst_first_and_mentions_skipped_names(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-ranking", "params": {"places": ["Delhi", "Denver", "Atlantis"]}, "missing": []})
        self.assertIn("worst air first", result["message"])
        self.assertLess(result["message"].index("Delhi"), result["message"].index("Denver"))
        self.assertIn("Skipped (not in this server's place list): Atlantis", result["message"])
        self.assertEqual(result["artifact"]["unknown"], ["Atlantis"])

    def test_ranking_with_nothing_usable_asks_for_a_place(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-ranking", "params": {"places": ["Atlantis"]}, "missing": []})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("None of those places", result["message"])

    def test_watch_records_the_threshold_and_the_reduced_observation(self):
        result = AirQualityAgent(FakeAir()).run(
            {"skill": "air-watch", "params": {"place": "Delhi", "threshold": 100}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "air-watch")
        self.assertEqual(watch["threshold"], 100.0)
        self.assertEqual(watch["observed"], {"band": "Unhealthy", "above": True})
        self.assertEqual(watch["latitude"], 28.61)
        self.assertNotIn("observed", result["artifact"]["watching"])
        self.assertEqual(result["artifact"]["current"]["aqi"], 162.0)
        self.assertIn("Point a pushNotificationConfig", result["message"])

    def test_missing_input_prompts(self):
        result = AirQualityAgent(FakeAir()).run({"skill": "air-now", "params": {}, "missing": ["location"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which place", result["message"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, AirQualityAgent(FakeAir()))
        task = handler.handle("message/send", {"message": _message("how bad is the air in Delhi?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], DATASET)
        self.assertEqual(data["place"], "Delhi")
        self.assertEqual(data["aqi"], 162.0)

    def test_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, AirQualityAgent(FakeAir()))
        first = handler.handle("message/send", {"message": _message("how is the air here?")})
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

    def test_watcher_fires_on_a_crossing_and_stays_quiet_inside_a_band(self):
        client = FakeAir()  # Denver reads 50 US AQI, Moderate, below the 100 threshold
        agent = AirQualityAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when the air in Denver passes 100")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)  # the setup reading is the baseline
        self.assertEqual(watcher.tick(), 0)

        # A worse number inside the same band is not news.
        client.values = {"us_aqi": 175, "pm2_5": 70.0}
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("at or above your threshold", self._posted_text(posted[0]))
        self.assertEqual(watcher.tick(), 0)

        # Clean air again: one page, phrased as a recovery.
        client.values = {"us_aqi": 40, "pm2_5": 6.0}
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("dropped back below 100 US AQI", self._posted_text(posted[1]))

    def test_describe_watch_change_covers_the_cases(self):
        agent = AirQualityAgent(FakeAir())
        watch = {"label": "Denver", "threshold": 100.0}
        self.assertIn("at or above your threshold",
                      agent.describe_watch_change(watch, {"band": "Moderate", "above": False},
                                                  {"band": "Unhealthy", "above": True}))
        self.assertIn("dropped back below 100 US AQI",
                      agent.describe_watch_change(watch, {"band": "Unhealthy", "above": True},
                                                  {"band": "Good", "above": False}))
        self.assertIn("is now Good",
                      agent.describe_watch_change(watch, None, {"band": "Good", "above": False}))


if __name__ == "__main__":
    unittest.main()
