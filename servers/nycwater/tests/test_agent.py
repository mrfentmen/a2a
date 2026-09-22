"""Tests for the NYC drinking water (DEP monitoring) server.

    python3 servers/nycwater/tests/test_agent.py
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

from agent import CAVEAT, CARD_SKILLS, WaterAgent, parse  # noqa: E402
from data import WaterClient  # noqa: E402

SAMPLE = {
    "sample_number": "202620761",
    "sample_date": "2026-08-31T00:00:00.000",
    "sample_time": "9:32",
    "sample_site": "55450",
    "sample_class": "Compliance",
    "residual_free_chlorine_mg_l": "0.24",
    "turbidity_ntu": "0.52",
    "coliform_quanti_tray_mpn_100ml": "<1",
    "e_coli_quanti_tray_mpn_100ml": "<1",
    "dataset": "bkwf-xfky",
}

POSITIVE = {
    **SAMPLE,
    "sample_number": "202620762",
    "sample_date": "2026-09-30T00:00:00.000",
    "coliform_quanti_tray_mpn_100ml": "12.4",
    "e_coli_quanti_tray_mpn_100ml": "2",
}


class FakeWater(WaterClient):
    """Same interface as the real client, no network."""

    def __init__(self, samples=None, sites=None):
        self.samples = list(samples if samples is not None else [SAMPLE])
        self.site_rows = list(sites if sites is not None else [{"sample_site": "55450", "samples": "4132",
                                                               "latest": "2026-08-31T00:00:00.000"}])

    def latest_samples(self, site=None, sample_class=None, limit=20):
        rows = [row for row in self.samples if site is None or row["sample_site"] == site]
        return rows[:limit]

    def site_window(self, site, days=90, limit=300):
        return [row for row in self.samples if row["sample_site"] == site][:limit]

    def busiest_sites(self, limit=20):
        return self.site_rows[:limit]

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


class SummarizeTests(unittest.TestCase):
    def test_counts_detections_and_chemistry(self):
        summary = WaterClient.__new__(WaterClient).summarize([SAMPLE, POSITIVE])
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["coliform_detections"], 1)
        self.assertEqual(summary["e_coli_detections"], 1)
        self.assertEqual(summary["chlorine_mg_l"]["avg"], 0.24)
        self.assertEqual(summary["turbidity_ntu"]["max"], 0.52)

    def test_censored_values_are_zero_and_missing_is_skipped(self):
        blank = {**SAMPLE, "coliform_quanti_tray_mpn_100ml": "<1", "e_coli_quanti_tray_mpn_100ml": None}
        summary = WaterClient.__new__(WaterClient).summarize([blank])
        self.assertEqual(summary["coliform_detections"], 0)
        self.assertEqual(summary["e_coli_samples"], 0)

    def test_empty_rows(self):
        summary = WaterClient.__new__(WaterClient).summarize([])
        self.assertEqual(summary["samples"], 0)
        self.assertIsNone(summary["chlorine_mg_l"]["avg"])


class ValidationTests(unittest.TestCase):
    def test_check_site(self):
        self.assertEqual(WaterClient.check_site("55450"), "55450")
        self.assertEqual(WaterClient.check_site("1S03A"), "1S03A")
        with self.assertRaises(ValueError):
            WaterClient.check_site("this is not a site")


class ParseTests(unittest.TestCase):
    def test_site_code_in_quality_question(self):
        parsed = parse(message("how is the water testing at site 55450?"))
        self.assertEqual(parsed["skill"], "water-quality")
        self.assertEqual(parsed["params"]["site"], "55450")
        self.assertFalse(parsed["explicit"])

    def test_site_listing_is_explicit(self):
        parsed = parse(message("which water monitoring sites report the most?"))
        self.assertEqual(parsed["skill"], "water-sites")
        self.assertTrue(parsed["explicit"])

    def test_watch_is_explicit(self):
        parsed = parse(message("tell me when site 55450 gets a new sample"))
        self.assertEqual(parsed["skill"], "water-watch")
        self.assertTrue(parsed["explicit"])

    def test_defaults_to_quality(self):
        parsed = parse(message("how is the water?"))
        self.assertEqual(parsed["skill"], "water-quality")
        self.assertIsNone(parsed["params"].get("site"))

    def test_data_part_wins(self):
        payload = {
            "kind": "message",
            "role": "user",
            "messageId": "m1",
            "parts": [{"kind": "data", "data": {"skill": "water-watch", "site": "55450"}}],
        }
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "water-watch")
        self.assertEqual(parsed["params"]["site"], "55450")


class AgentTests(unittest.TestCase):
    def test_card_skills_are_complete(self):
        ids = [skill["id"] for skill in CARD_SKILLS]
        self.assertEqual(ids, ["water-quality", "water-sites", "water-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = WaterAgent(FakeWater())
        self.assertEqual(agent.missing("water-quality", {}), [])
        self.assertEqual(agent.missing("water-watch", {}), ["site"])
        self.assertEqual(agent.missing("water-watch", {"site": "55450"}), [])

    def test_quality_message_carries_caveat_and_summary(self):
        agent = WaterAgent(FakeWater())
        result = agent.run({"skill": "water-quality", "params": {"site": "55450", "days": 365}, "missing": []})
        self.assertIn(CAVEAT, result["message"])
        self.assertEqual(result["artifact"]["summary"]["samples"], 1)
        self.assertEqual(result["artifact"]["dataset"], "bkwf-xfky")

    def test_quality_with_unknown_site(self):
        agent = WaterAgent(FakeWater())
        result = agent.run({"skill": "water-quality", "params": {"site": "99999", "days": 90}, "missing": []})
        self.assertIn("No drinking water samples", result["message"])
        self.assertEqual(result["artifact"]["samples"], [])

    def test_sites_listing(self):
        agent = WaterAgent(FakeWater())
        result = agent.run({"skill": "water-sites", "params": {}, "missing": []})
        self.assertEqual(result["artifact"]["count"], 1)
        self.assertIn("55450", result["message"])

    def test_watch_unknown_site(self):
        agent = WaterAgent(FakeWater(samples=[SAMPLE]))
        result = agent.run({"skill": "water-watch", "params": {"site": "99999"}, "missing": []})
        self.assertFalse(result["artifact"]["found"])
        self.assertIsNone(result["watch"])

    def test_watch_sets_observation(self):
        agent = WaterAgent(FakeWater())
        result = agent.run({"skill": "water-watch", "params": {"site": "55450"}, "missing": []})
        self.assertEqual(result["watch"]["kind"], "water-watch")
        self.assertEqual(result["watch"]["observed"]["sample_number"], "202620761")

    def test_probe_and_describe(self):
        agent = WaterAgent(FakeWater())
        observed = agent.probe_watch({"site": "55450"})
        self.assertEqual(observed["sample_date"], SAMPLE["sample_date"])
        self.assertIsNone(agent.probe_watch({}))
        self.assertIn("New drinking water sample at site 55450", agent.describe_watch_change({"site": "55450"}, None, observed))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, WaterAgent(FakeWater()))
        task = handler.handle("message/send", {"message": message("how is the water testing at site 55450?")})
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(task["artifacts"][-1]["parts"][0]["data"]["site"], "55450")

    def test_watch_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, WaterAgent(FakeWater()))
        first = handler.handle("message/send", {"message": message("watch the water")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": message("site 55450", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["metadata"]["watch"]["site"], "55450")

    def test_stream_sequence(self):
        handler = A2AHandler(self.store, WaterAgent(FakeWater()))
        events = list(handler.handle("message/stream", {"message": message("how is the water?")}))
        self.assertEqual([event["kind"] for event in events],
                         ["task", "status-update", "artifact-update", "status-update"])
        self.assertTrue(events[-1]["final"])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_watcher_fires_when_a_new_sample_lands(self):
        client = FakeWater()
        agent = WaterAgent(client)
        task = A2AHandler(self.store, agent).handle("message/send", {"message": message("watch site 55450")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook"})

        posted = []
        watcher = PushWatcher(
            self.store, agent, interval=5,
            http_post=lambda url, payload, headers: posted.append(payload) or 200,
        )
        self.assertEqual(watcher.tick(), 0)  # same latest sample

        client.samples = [POSITIVE, SAMPLE]
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(len(posted), 1)
        self.assertEqual(
            self.store.get_task(task["id"])["metadata"]["last_observed"]["sample_number"], "202620762"
        )


if __name__ == "__main__":
    unittest.main()
