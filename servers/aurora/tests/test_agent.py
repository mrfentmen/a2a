"""Tests for the aurora space-weather server.

    python3 servers/aurora/tests/test_agent.py
"""

from __future__ import annotations

import json
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
    AuroraSpaceWeatherAgent,
    parse,
    place_from_text,
    threshold_from_text,
)
from data import (  # noqa: E402
    CITY_COORDS,
    DATASET_ALERTS,
    DATASET_FORECAST,
    DATASET_KP_1M,
    DATASET_OVATION,
    SpaceWeatherClient,
    is_storm,
    kp_band,
    parse_message,
)

#: Shapes copied from the live SWPC products on 2026-09-22.
WATCH_TEXT = (
    "Space Weather Message Code: WATA20\r\n"
    "Serial Number: 1126\r\n"
    "Issue Time: 2026 Sep 21 1832 UTC\r\n"
    "\r\n"
    "WATCH: Geomagnetic Storm Category G1 Predicted \r\n"
    "Highest Storm Level Predicted by Day:\r\n"
    "Sep 22:  None (Below G1)   Sep 23:  None (Below G1)   Sep 24:  G1 (Minor)   \r\n"
)

RAW_ALERTS = [
    {"product_id": "ALTEF3", "issue_datetime": "2026-09-16 09:00:00.000",
     "message": "Space Weather Message Code: ALTEF3\r\nSerial Number: 3700\r\nIssue Time: 2026 Sep 16 0900 UTC\r\n\r\n"
                "CONTINUED ALERT: Electron 2MeV Integral Flux exceeded 1,000pfu\r\n"},
    {"product_id": "A20F", "issue_datetime": "2026-09-21 18:32:26.023", "message": WATCH_TEXT},
]

KP_NOW = {
    "dataset": DATASET_KP_1M,
    "time_tag": "2026-09-22T17:53:00",
    "estimated_kp": 0.33,
    "band": "quiet",
    "storm": False,
    "three_hourly": {"dataset": "swpc.noaa.gov/planetary-k-index", "time_tag": "2026-09-22T12:00:00",
                     "kp": 0.33, "a_running": 2, "station_count": 8},
}

RECENT = {
    "now": dict(KP_NOW),
    "window_hours": 24,
    "peak_kp": 2.0,
    "peak_band": "quiet",
    "peak_time_tag": "2026-09-22T00:00:00",
    "rows": [],
    "dataset": "swpc.noaa.gov/planetary-k-index",
}

FORECAST = {
    "dataset": DATASET_FORECAST,
    "days": [
        {"date": "2026-09-23", "max_kp": 4.33, "band": "active", "storm": False, "rows": [{"time_tag": "2026-09-23T03:00:00", "kp": 4.33}]},
        {"date": "2026-09-24", "max_kp": 5.33, "band": "G1 (minor)", "storm": True, "rows": [{"time_tag": "2026-09-24T00:00:00", "kp": 5.33}]},
    ],
    "predicted_rows": 16,
    "observed_rows": 65,
    "peak_kp": 5.33,
    "peak_band": "G1 (minor)",
}

PROBABILITY = {
    "dataset": DATASET_OVATION,
    "observation_time": "2026-09-22T17:49:00Z",
    "forecast_time": "2026-09-22T19:20:00Z",
    "probability": 5,
    "nearest_cell": {"latitude": 65, "longitude": 212},
    "radius_degrees": 2.0,
    "max_probability_nearby": 7,
    "cells_read": 65160,
}


#: A message that arrives after the others, so a watch has something new to fire on.
NEW_WARNING = {
    "product_id": "WARK05",
    "issue_datetime": "2026-09-22 18:00:00.000",
    "message": (
        "Space Weather Message Code: WARK05\r\nSerial Number: 2266\r\nIssue Time: 2026 Sep 22 1800 UTC\r\n\r\n"
        "WARNING: Geomagnetic K-index of 5 expected\r\n"
    ),
}


def messages_payload(alerts, limit: int, contains: str | None = None) -> dict:
    items = [parse_message(row) for row in alerts]
    items.sort(key=lambda item: str(item["issue_datetime"]), reverse=True)
    if contains:
        needle = contains.lower()
        items = [item for item in items if needle in item["text"].lower() or needle in (item["headline"] or "").lower()]
    return {"dataset": DATASET_ALERTS, "count": len(items), "messages": items[:limit]}


class FakeSWPC(SpaceWeatherClient):
    """Same interface as the real client, no network."""

    def __init__(self, kp: float = 0.33, alerts=None):
        self.kp = kp
        self.alerts = list(alerts if alerts is not None else RAW_ALERTS)
        self.calls: list[dict] = []

    def kp_now(self) -> dict:
        self.calls.append({"kind": "kp_now"})
        payload = dict(KP_NOW)
        payload["estimated_kp"] = self.kp
        payload["band"] = kp_band(self.kp)
        payload["storm"] = is_storm(self.kp)
        payload["three_hourly"] = dict(KP_NOW["three_hourly"])
        return payload

    def recent(self, hours: int = 24) -> dict:
        payload = dict(RECENT)
        payload["now"] = self.kp_now()
        payload["peak_kp"] = 6.0 if is_storm(self.kp) else 2.0
        payload["peak_band"] = kp_band(payload["peak_kp"])
        return payload

    def forecast(self, days: int = 3) -> dict:
        return dict(FORECAST, days=FORECAST["days"][:days])

    def messages(self, limit: int = 5, contains: str | None = None) -> dict:
        self.calls.append({"kind": "messages", "limit": limit, "contains": contains})
        return messages_payload(self.alerts, limit, contains)

    def aurora_probability(self, lat: float, lon: float, radius: float = 2.0) -> dict:
        self.calls.append({"kind": "probability", "lat": lat, "lon": lon})
        return dict(PROBABILITY)


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
        self.assertEqual(SpaceWeatherClient.check_point("64.84, -147.72"), (64.84, -147.72))
        with self.assertRaises(ValueError):
            SpaceWeatherClient.check_point("64.84")
        with self.assertRaises(ValueError):
            SpaceWeatherClient.check_point("91,-74")

    def test_check_kp(self):
        self.assertEqual(SpaceWeatherClient.check_kp("6"), 6.0)
        with self.assertRaises(ValueError):
            SpaceWeatherClient.check_kp("12")

    def test_city_lookup(self):
        self.assertEqual(SpaceWeatherClient.city("Fairbanks"), (64.84, -147.72))
        self.assertEqual(SpaceWeatherClient.city("new york"), (40.71, -74.01))
        self.assertIsNone(SpaceWeatherClient.city("Atlantis"))
        self.assertEqual(len(CITY_COORDS), len({name for name in CITY_COORDS}))

    def test_bands_and_storm_scale(self):
        self.assertEqual(kp_band(0.33), "quiet")
        self.assertEqual(kp_band(3.0), "unsettled")
        self.assertEqual(kp_band(4.0), "active")
        self.assertEqual(kp_band(5.0), "G1 (minor)")
        self.assertEqual(kp_band(6.67), "G3 (strong)")
        self.assertTrue(is_storm(5.0))
        self.assertFalse(is_storm(4.67))

    def test_parse_message_reads_the_fields(self):
        parsed = parse_message({"product_id": "A20F", "issue_datetime": "2026-09-21 18:32:26.023", "message": WATCH_TEXT})
        self.assertEqual(parsed["code"], "WATA20")
        self.assertEqual(parsed["serial"], "1126")
        self.assertEqual(parsed["kind"], "WATCH")
        self.assertEqual(parsed["g_scale"], "G1")
        self.assertIn("Geomagnetic Storm Category G1 Predicted", parsed["headline"])

    def test_parse_message_kind_prefers_extended_warning(self):
        text = "Space Weather Message Code: WARK04\r\nSerial Number: 5418\r\nIssue Time: 2026 Sep 16 0538 UTC\r\n\r\nEXTENDED WARNING: Geomagnetic K-index of 4 expected\r\n"
        parsed = parse_message({"product_id": "WARK04", "issue_datetime": "2026-09-16 05:38:03.320", "message": text})
        self.assertEqual(parsed["kind"], "EXTENDED WARNING")

    def test_parse_message_ignores_empty_rows(self):
        self.assertIsNone(parse_message({"product_id": "X", "issue_datetime": "2026-01-01 00:00:00", "message": ""}))

    def test_messages_are_newest_first(self):
        client = SpaceWeatherClient(fetch=lambda url, params, headers: RAW_ALERTS)
        result = client.messages(limit=2)
        self.assertEqual(result["messages"][0]["code"], "WATA20")
        self.assertEqual(result["messages"][1]["code"], "ALTEF3")


class ParseTests(unittest.TestCase):
    def test_now_is_the_default(self):
        parsed = parse(message("how are the geomagnetic conditions right now?"))
        self.assertEqual(parsed["skill"], "aurora-now")
        self.assertEqual(parsed["params"], {})

    def test_forecast_words(self):
        parsed = parse(message("what is the aurora forecast for the next 3 days?"))
        self.assertEqual(parsed["skill"], "aurora-forecast")
        self.assertEqual(parsed["params"]["days"], 3)

    def test_visibility_by_city_and_by_point(self):
        by_city = parse(message("what are the odds of seeing the aurora in Fairbanks?"))
        self.assertEqual(by_city["skill"], "aurora-visibility")
        self.assertEqual(by_city["params"]["place"], "fairbanks")
        by_point = parse(message("will I see the northern lights at 64.84,-147.72?"))
        self.assertEqual(by_point["skill"], "aurora-visibility")
        self.assertEqual(by_point["params"]["point"], "64.84,-147.72")

    def test_visibility_without_a_place(self):
        parsed = parse(message("what are the odds of seeing the aurora?"))
        self.assertEqual(parsed["skill"], "aurora-visibility")
        self.assertNotIn("place", parsed["params"])
        self.assertNotIn("point", parsed["params"])

    def test_watch_words_and_thresholds(self):
        self.assertEqual(parse(message("tell me when a geomagnetic storm starts"))["skill"], "aurora-watch")
        self.assertNotIn("threshold_kp", parse(message("tell me when a storm starts"))["params"])
        self.assertEqual(parse(message("tell me when kp reaches 6"))["params"]["threshold_kp"], 6.0)
        self.assertEqual(parse(message("notify me when a G3 storm hits"))["params"]["threshold_kp"], 7.0)

    def test_messages_with_keyword(self):
        parsed = parse(message("what has NOAA said about flares lately?"))
        self.assertEqual(parsed["skill"], "aurora-messages")
        self.assertEqual(parsed["params"]["contains"], "flare")

    def test_data_part_wins(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "aurora-messages", "contains": "storm", "limit": 3}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "aurora-messages")
        self.assertEqual(parsed["params"]["limit"], 3)
        self.assertEqual(parsed["params"]["contains"], "storm")

    def test_helpers(self):
        self.assertEqual(place_from_text("aurora over New York tonight?"), "new york")
        self.assertIsNone(place_from_text("aurora tonight?"))
        self.assertEqual(threshold_from_text("kp of 5.5 or more"), 5.5)
        self.assertEqual(threshold_from_text("a G1 storm"), 5.0)
        self.assertIsNone(threshold_from_text("a storm"))


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["aurora-now", "aurora-forecast", "aurora-visibility", "aurora-messages",
                               "aurora-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = AuroraSpaceWeatherAgent(FakeSWPC())
        self.assertEqual(agent.missing("aurora-now", {}), [])
        self.assertEqual(agent.missing("aurora-visibility", {}), ["location"])
        self.assertEqual(agent.missing("aurora-visibility", {"place": "fairbanks"}), [])
        self.assertEqual(agent.missing("aurora-watch", {}), [])

    def test_now_cites_products_and_peak(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run({"skill": "aurora-now", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("estimated Kp", result["message"])
        self.assertIn("Peak over the last 24 hours", result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET_KP_1M)
        self.assertIn(DATASET_ALERTS, result["artifact"]["also_read"])
        self.assertTrue(result["artifact"]["freshness"])
        self.assertIsNone(result["watch"])

    def test_forecast_lists_days_and_storm_messages(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run({"skill": "aurora-forecast", "params": {}, "missing": []})
        self.assertIn("2026-09-23: peak Kp 4.33 (active)", result["message"])
        self.assertIn("G1 (minor)", result["message"])
        self.assertIn("WATA20", result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET_FORECAST)
        self.assertEqual(result["artifact"]["days"][1]["storm"], True)

    def test_visibility_reports_the_model_run_and_the_cloud_caveat(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run(
            {"skill": "aurora-visibility", "params": {"place": "fairbanks"}, "missing": []}
        )
        self.assertIn("5%", result["message"])
        self.assertIn("up to 7% within 2 degrees", result["message"])
        self.assertIn("2026-09-22T19:20:00Z", result["message"])
        self.assertIn("clouds", result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET_OVATION)
        self.assertEqual(result["artifact"]["latitude"], 64.84)

    def test_visibility_for_an_unknown_place_is_honest(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run(
            {"skill": "aurora-visibility", "params": {"place": "atlantis"}, "missing": []}
        )
        self.assertIn("will not guess", result["message"])
        self.assertEqual(result["artifact"]["known"], False)

    def test_messages_filter_and_listing(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run(
            {"skill": "aurora-messages", "params": {"limit": 5, "contains": "storm"}, "missing": []}
        )
        self.assertIn("WATA20", result["message"])
        self.assertNotIn("ALTEF3", result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET_ALERTS)

    def test_watch_records_the_observation(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run({"skill": "aurora-watch", "params": {}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "aurora-watch")
        self.assertEqual(watch["threshold_kp"], 5.0)
        self.assertEqual(watch["observed"]["band"], "quiet")
        self.assertEqual(watch["observed"]["storming"], False)
        self.assertEqual(watch["observed"]["newest_message"], "WATA20#1126")
        self.assertNotIn("observed", result["artifact"]["watching"])

    def test_watch_when_a_storm_is_already_running(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC(kp=6.0)).run(
            {"skill": "aurora-watch", "params": {"threshold_kp": 5}, "missing": []}
        )
        self.assertTrue(result["watch"]["observed"]["storming"])
        self.assertIn("storm at or above your threshold is in progress", result["message"])

    def test_watch_with_a_place_explains_what_it_watches(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run(
            {"skill": "aurora-watch", "params": {"place": "fairbanks"}, "missing": []}
        )
        self.assertIn("aurora-visibility", result["message"])
        self.assertEqual(result["watch"]["place"], "fairbanks")

    def test_missing_input_prompts(self):
        result = AuroraSpaceWeatherAgent(FakeSWPC()).run(
            {"skill": "aurora-visibility", "params": {}, "missing": ["location"]}
        )
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which place", result["message"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, AuroraSpaceWeatherAgent(FakeSWPC()))
        task = handler.handle("message/send", {"message": message("how are the geomagnetic conditions?")})
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["artifacts"][-1]["parts"][0]["data"]["dataset"], DATASET_KP_1M)

    def test_visibility_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, AuroraSpaceWeatherAgent(FakeSWPC()))
        first = handler.handle("message/send", {"message": message("what are the odds of seeing the aurora?")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": message("Fairbanks", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["dataset"], DATASET_OVATION)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def _watcher(self, agent, posted):
        return PushWatcher(self.store, agent, interval=5,
                           http_post=lambda url, payload, headers: posted.append(payload) or 200)

    @staticmethod
    def _posted_text(payload) -> str:
        """The watcher hands the JSON-RPC notification to http_post as bytes."""
        body = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
        if isinstance(body, str):
            body = json.loads(body)
        return body["status"]["message"]["parts"][0]["text"]

    def test_watcher_fires_on_a_new_message_and_on_a_storm(self):
        client = FakeSWPC(kp=0.33, alerts=[RAW_ALERTS[0]])  # quiet, one electron alert
        agent = AuroraSpaceWeatherAgent(client)
        task = A2AHandler(self.store, agent).handle("message/send", {"message": message("tell me when a storm starts")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = self._watcher(agent, posted)
        self.assertEqual(watcher.tick(), 0)  # nothing has changed yet

        client.alerts = [NEW_WARNING] + list(RAW_ALERTS)  # SWPC issues a newer warning
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(len(posted), 1)
        self.assertIn("SWPC", self._posted_text(posted[0]))
        self.assertIn("WARNING", self._posted_text(posted[0]))
        self.assertEqual(watcher.tick(), 0)  # same message, nothing new

        client.kp = 6.0  # the storm actually arrives
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("storm", self._posted_text(posted[1]).lower())


if __name__ == "__main__":
    unittest.main()
