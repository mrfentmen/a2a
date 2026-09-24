"""Tests for the volcano alert-level server.

    python3 servers/volcanoes/tests/test_agent.py
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
    VolcanoAgent,
    name_from_text,
    parse,
    region_from_text,
)
from data import (  # noqa: E402
    DATASET,
    VolcanoClient,
    alert_level_name,
    color_name,
    is_elevated,
    severity,
    to_iso_utc,
)

#: Feature shapes copied from the live geojson read on 2026-09-23.
FEATURES = [
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [-155.287, 19.421]},
     "properties": {"volcanoName": "Kilauea", "vnum": "332010", "volcanoCd": "hi3", "obs": "hvo",
                    "region": "Hawaii", "noticeId": "DOI-USGS-HVO-2026-09-23T17:16:19+00:00",
                    "noticeSynopsis": "HVO Kilauea ORANGE/WATCH - Precursory overflows from the "
                                      "north vent resumed last night.",
                    "noticeUrl": "https://volcanoes.usgs.gov/hans2/view/notice/DOI-USGS-HVO",
                    "alertLevel": "WATCH", "colorCode": "ORANGE",
                    "alertDate": "2026-09-23 18:23:22", "colorDate": "2026-09-23 18:23:22",
                    "nvewsThreat": "Very High Threat",
                    "volcanoUrl": "https://www.usgs.gov/volcanoes/kilauea"}},
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [-176.1109, 52.0765]},
     "properties": {"volcanoName": "Great Sitkin", "vnum": "311120", "volcanoCd": "ak111",
                    "obs": "avo", "region": "Aleutians",
                    "noticeSynopsis": "AVO Great Sitkin ORANGE/WATCH - Slow eruption of lava "
                                      "within the summit crater continues.",
                    "noticeUrl": "https://volcanoes.usgs.gov/hans2/view/notice/DOI-USGS-AVO",
                    "alertLevel": "WATCH", "colorCode": "ORANGE",
                    "alertDate": "2026-09-23 17:54:19", "colorDate": "2026-09-23 17:54:19",
                    "nvewsThreat": "High Threat",
                    "volcanoUrl": "https://www.avo.alaska.edu/activity/GreatSitkin.php"}},
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [145.6196, 20.4196]},
     "properties": {"volcanoName": "Ahyi Seamount", "vnum": "284141", "volcanoCd": "nmi2",
                    "obs": "nmi", "region": "Northern Mariana Islands",
                    "noticeSynopsis": "NMI Ahyi Seamount YELLOW/ADVISORY - No plumes seen in "
                                      "satellite images.",
                    "alertLevel": "ADVISORY", "colorCode": "YELLOW",
                    "alertDate": "2026-09-18 00:41:48", "colorDate": "2026-09-18 00:41:48",
                    "nvewsThreat": "Very Low Threat",
                    "volcanoUrl": "https://www.usgs.gov/volcanoes/ahyi-seamount"}},
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [-121.7, 44.1]},
     "properties": {"volcanoName": "Mount Hood", "vnum": "322010", "volcanoCd": "or1", "obs": "cvo",
                    "region": "Oregon", "alertLevel": "NORMAL", "colorCode": "GREEN",
                    "nvewsThreat": "Very High Threat",
                    "volcanoUrl": "https://www.usgs.gov/volcanoes/mount-hood"}},
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [-176.5852, 51.9905]},
     "properties": {"volcanoName": "Adagdak", "vnum": "311800", "volcanoCd": "ak3", "obs": "avo",
                    "region": "Aleutians", "alertLevel": "UNASSIGNED", "colorCode": "UNASSIGNED",
                    "nvewsThreat": "Moderate Threat",
                    "volcanoUrl": "https://avo.alaska.edu/volcanoes/volcinfo.php?volcname=Adagdak"}},
]

#: Record shapes copied from the live `elevated` read on 2026-09-23.
ELEVATED = [
    {"noticeId": "DOI-USGS-HVO-2026-09-23T17:16:19+00:00", "vName": "Kilauea", "vnum": "332010",
     "volcanoCd": "hi3", "nvewsThreat": "Very High Threat", "lat": 19.421, "long": -155.287,
     "noticeSynopsis": "HVO Kilauea ORANGE/WATCH - Precursory overflows from the north vent resumed.",
     "obs": "hvo", "alertLevel": "WATCH", "colorCode": "ORANGE", "sentUtc": "2026-09-23 18:23:22",
     "alertDate": "2026-09-23 18:23:22", "alertLevelPrev": "ADVISORY", "colorCodePrev": "YELLOW",
     "noticeUrl": "https://volcanoes.usgs.gov/hans2/view/notice/DOI-USGS-HVO"},
    {"noticeId": "DOI-USGS-AVO-2026-09-23T17:51:33+00:00", "vName": "Shishaldin", "vnum": "311360",
     "volcanoCd": "ak252", "nvewsThreat": "High Threat", "lat": 54.7554, "long": -163.9711,
     "noticeSynopsis": "AVO Shishaldin YELLOW/ADVISORY - Low-level unrest continues.",
     "obs": "avo", "alertLevel": "ADVISORY", "colorCode": "YELLOW", "sentUtc": "2026-09-23 17:54:19",
     "alertDate": "2026-09-23 17:54:19", "alertLevelPrev": "ADVISORY", "colorCodePrev": "YELLOW",
     "noticeUrl": "https://volcanoes.usgs.gov/hans2/view/notice/DOI-USGS-AVO"},
    {"noticeId": "DOI-USGS-AVO-2026-09-23T17:51:33+00:00", "vName": "Great Sitkin", "vnum": "311120",
     "volcanoCd": "ak111", "nvewsThreat": "High Threat", "lat": 52.0765, "long": -176.1109,
     "noticeSynopsis": "AVO Great Sitkin ORANGE/WATCH - Slow eruption of lava continues.",
     "obs": "avo", "alertLevel": "WATCH", "colorCode": "ORANGE", "sentUtc": "2026-09-23 17:54:19",
     "alertDate": "2026-09-23 17:54:19", "alertLevelPrev": "ADVISORY", "colorCodePrev": "YELLOW",
     "noticeUrl": "https://volcanoes.usgs.gov/hans2/view/notice/DOI-USGS-AVO"},
]


class FakeVsc(VolcanoClient):
    """A VolcanoClient whose transport is a canned VSC read, so data.py runs for real."""

    def __init__(self, features=None, elevated=None) -> None:
        super().__init__()
        self.features = list(FEATURES if features is None else features)
        self.elevated_records = list(ELEVATED if elevated is None else elevated)
        self.calls: list[str] = []

    def _http_get(self, url, params, headers):
        self.calls.append(url)
        if url.endswith("/elevated"):
            return list(self.elevated_records)
        return {"type": "FeatureCollection", "features": list(self.features)}


class ClientTests(unittest.TestCase):
    def test_severity_ranks_levels_and_unknowns_stay_low(self):
        self.assertLess(severity("NORMAL"), severity("ADVISORY"))
        self.assertLess(severity("ADVISORY"), severity("WATCH"))
        self.assertLess(severity("WATCH"), severity("WARNING"))
        self.assertEqual(severity("nonsense"), 0)
        self.assertEqual(severity(None), 0)

    def test_is_elevated_covers_advisory_up(self):
        self.assertFalse(is_elevated("NORMAL"))
        self.assertFalse(is_elevated("UNASSIGNED"))
        self.assertFalse(is_elevated(None))
        self.assertTrue(is_elevated("advisory"))
        self.assertTrue(is_elevated("WARNING"))

    def test_level_and_colour_names_are_clamped(self):
        self.assertEqual(alert_level_name("watch"), "WATCH")
        self.assertEqual(alert_level_name("orange"), "UNASSIGNED")  # a colour is not a level
        self.assertEqual(color_name("orange"), "ORANGE")
        self.assertEqual(color_name("WATCH"), "UNASSIGNED")  # a level is not a colour

    def test_to_iso_utc_normalises_the_sent_utc_format(self):
        self.assertEqual(to_iso_utc("2026-09-23 18:23:22"), "2026-09-23T18:23:22Z")
        self.assertEqual(to_iso_utc("2026-09-23T18:23:22"), "2026-09-23T18:23:22Z")
        self.assertIsNone(to_iso_utc(""))
        self.assertIsNone(to_iso_utc("yesterday"))

    def test_validators(self):
        self.assertEqual(VolcanoClient.check_level("advisory"), "ADVISORY")
        with self.assertRaises(ValueError):
            VolcanoClient.check_level("ORANGE")
        with self.assertRaises(ValueError):
            VolcanoClient.check_positive(0)
        with self.assertRaises(ValueError):
            VolcanoClient.check_positive(500)
        self.assertEqual(VolcanoClient.check_region("  Hawaii "), "Hawaii")
        with self.assertRaises(ValueError):
            VolcanoClient.check_region("x")
        self.assertEqual(VolcanoClient.check_name("Kilauea"), "Kilauea")
        with self.assertRaises(ValueError):
            VolcanoClient.check_name("")

    def test_slow_service_defaults(self):
        client = VolcanoClient()
        self.assertEqual(client.timeout, 90.0)
        self.assertEqual(client.cache_ttl, 900.0)

    def test_elevated_rows_are_parsed_and_sorted_by_severity(self):
        read = FakeVsc().elevated()
        self.assertEqual(read["dataset"], DATASET)
        self.assertEqual(read["count"], 3)
        self.assertEqual([row["name"] for row in read["volcanoes"]],
                         ["Great Sitkin", "Kilauea", "Shishaldin"])
        kilauea = [row for row in read["volcanoes"] if row["name"] == "Kilauea"][0]
        self.assertEqual(kilauea["level"], "WATCH")
        self.assertEqual(kilauea["color"], "ORANGE")
        self.assertEqual(kilauea["previous_level"], "ADVISORY")
        self.assertEqual(kilauea["sent"], "2026-09-23T18:23:22Z")
        self.assertEqual(kilauea["observatory_name"], "Hawaiian Volcano Observatory")
        self.assertEqual(kilauea["latitude"], 19.421)
        self.assertEqual(read["as_of"], "2026-09-23T18:23:22Z")

    def test_geojson_rows_use_lon_lat_order(self):
        row = FakeVsc().lookup("Kilauea")["volcanoes"][0]
        self.assertEqual(row["longitude"], -155.287)
        self.assertEqual(row["latitude"], 19.421)
        self.assertEqual(row["region"], "Hawaii")

    def test_volcanoes_filter_by_region_level_and_elevation(self):
        client = FakeVsc()
        aleutians = client.volcanoes(region="aleutian")
        self.assertEqual(aleutians["count"], 2)
        self.assertEqual(aleutians["by_level"], {"WATCH": 1, "UNASSIGNED": 1})
        normal = client.volcanoes(level="NORMAL")
        self.assertEqual([row["name"] for row in normal["volcanoes"]], ["Mount Hood"])
        elevated = client.volcanoes(elevated_only=True)
        self.assertEqual([row["name"] for row in elevated["volcanoes"]],
                         ["Great Sitkin", "Kilauea", "Ahyi Seamount"])
        self.assertEqual(elevated["scope"], "elevated volcanoes")
        with self.assertRaises(ValueError):
            client.volcanoes(level="ORANGE")

    def test_volcanoes_reports_truncation_instead_of_hiding_it(self):
        read = FakeVsc().volcanoes(limit=2)
        self.assertEqual(len(read["volcanoes"]), 2)
        self.assertTrue(read["truncated"])
        self.assertEqual(read["count"], 5)

    def test_lookup_is_a_case_insensitive_substring(self):
        client = FakeVsc()
        self.assertEqual(client.lookup("sitkin")["count"], 1)
        self.assertEqual(client.lookup("SITKIN")["volcanoes"][0]["name"], "Great Sitkin")
        self.assertEqual(client.lookup("zzz")["count"], 0)
        with self.assertRaises(ValueError):
            client.lookup("x")

    def test_latest_notice_matches_the_elevated_list(self):
        client = FakeVsc()
        notice = client.latest_notice("Kilauea")
        self.assertEqual(notice["level"], "WATCH")
        self.assertIn("north vent", notice["synopsis"])
        self.assertIsNone(client.latest_notice("Mount Hood"))

    def test_regions_come_from_the_data(self):
        self.assertEqual(FakeVsc().regions(),
                         ["Aleutians", "Hawaii", "Northern Mariana Islands", "Oregon"])

    def test_watch_state_scopes_by_name_region_or_the_whole_country(self):
        client = FakeVsc()
        everything = client.watch_state()
        self.assertEqual(len(everything["levels"]), 5)
        self.assertEqual(everything["elevated"], ["Ahyi Seamount", "Great Sitkin", "Kilauea"])
        hawaii = client.watch_state(region="Hawaii")
        self.assertEqual(hawaii["levels"], {"Kilauea": "WATCH"})
        one = client.watch_state(name="Kilauea")
        self.assertEqual(one["levels"], {"Kilauea": "WATCH"})

    def test_reads_are_cached_per_url(self):
        client = FakeVsc()
        client.elevated()
        client.elevated()
        client.watch_state()
        client.watch_state()
        self.assertEqual(len([url for url in client.calls if url.endswith("/elevated")]), 1)
        self.assertEqual(len([url for url in client.calls if url.endswith("/geojson")]), 1)

    def test_a_payload_of_the_wrong_shape_is_rejected(self):
        client = FakeVsc()
        client._fetch = lambda url, params, headers: {"volcanoes": []}
        with self.assertRaises(ValueError):
            client.elevated()


class ParseTests(unittest.TestCase):
    def test_alerts_is_the_default_skill(self):
        parsed = parse(_message("is any volcano erupting right now?"))
        self.assertEqual(parsed["skill"], "volcano-alerts")
        self.assertTrue(parsed["explicit"])

    def test_region_scoped_alerts(self):
        parsed = parse(_message("anything elevated in Hawaii?"))
        self.assertEqual(parsed["skill"], "volcano-alerts")
        self.assertEqual(parsed["params"]["region"], "Hawaii")

    def test_lookup_by_name(self):
        parsed = parse(_message("what is Kilauea doing?"))
        self.assertEqual(parsed["skill"], "volcano-lookup")
        self.assertEqual(parsed["params"]["name"], "Kilauea")
        self.assertEqual(name_from_text("status of Great Sitkin"), "Great Sitkin")
        self.assertEqual(name_from_text("tell me about the Mauna Loa volcano"), "Mauna Loa")
        # A name the hint list does not carry still parses when the word "volcano" is used.
        self.assertEqual(name_from_text("tell me about the Adagdak volcano"), "Adagdak")
        self.assertIsNone(name_from_text("which volcanoes are elevated?"))
        self.assertIsNone(name_from_text("notify me when any volcano in Alaska goes on watch"))
        with_point = parse(_message("Mount St. Helens volcano status"))
        self.assertEqual(with_point["params"]["name"], "Mount St. Helens")

    def test_list_skill_and_filters(self):
        parsed = parse(_message("what volcanoes does USGS monitor in Alaska?"))
        self.assertEqual(parsed["skill"], "volcano-list")
        self.assertEqual(parsed["params"]["region"], "Alaska")
        by_level = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "volcano-list", "level": "ADVISORY"}}]}
        self.assertEqual(parse(by_level)["params"]["level"], "ADVISORY")

    def test_watch_skill_scoped_by_name_or_region(self):
        by_name = parse(_message("tell me when Kilauea changes alert level"))
        self.assertEqual(by_name["skill"], "volcano-watch")
        self.assertEqual(by_name["params"]["name"], "Kilauea")
        by_region = parse(_message("notify me when any volcano in Alaska goes on watch"))
        self.assertEqual(by_region["skill"], "volcano-watch")
        self.assertEqual(by_region["params"]["region"], "Alaska")
        self.assertNotIn("name", by_region["params"])

    def test_region_from_text_prefers_the_longest_name(self):
        self.assertEqual(region_from_text("volcanoes in the Northern Mariana Islands"),
                         "Northern Mariana Islands")
        self.assertEqual(region_from_text("anything in the Aleutians?"), "Aleutians")
        self.assertIsNone(region_from_text("volcanoes anywhere"))

    def test_data_part_wins_over_text(self):
        payload = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "volcano-lookup", "name": "Great Sitkin"}},
            {"kind": "text", "text": "something else entirely"}]}
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "volcano-lookup")
        self.assertEqual(parsed["params"]["name"], "Great Sitkin")


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
        self.assertEqual(ids, ["volcano-alerts", "volcano-list", "volcano-lookup", "volcano-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = VolcanoAgent(FakeVsc())
        self.assertEqual(agent.missing("volcano-lookup", {}), ["name"])
        self.assertEqual(agent.missing("volcano-lookup", {"name": "Kilauea"}), [])
        self.assertEqual(agent.missing("volcano-alerts", {}), [])
        self.assertEqual(agent.missing("volcano-watch", {}), [])

    def test_alerts_lists_levels_and_synopsis(self):
        result = VolcanoAgent(FakeVsc()).run({"skill": "volcano-alerts", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("3 volcano(s) above NORMAL", result["message"])
        self.assertIn("Kilauea — WATCH/ORANGE (was ADVISORY/YELLOW)", result["message"])
        self.assertIn("north vent", result["message"])
        self.assertIn("aviation", result["message"].lower())
        self.assertEqual(result["artifact"]["dataset"], DATASET)
        self.assertEqual(result["artifact"]["count"], 3)
        self.assertIsNone(result["watch"])

    def test_alerts_with_nothing_elevated_says_so_plainly(self):
        result = VolcanoAgent(FakeVsc(elevated=[])).run(
            {"skill": "volcano-alerts", "params": {}, "missing": []})
        self.assertIn("No volcano is above NORMAL", result["message"])
        self.assertIn("quiet reading", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_alerts_scoped_by_region_use_the_monitored_list(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-alerts", "params": {"region": "Hawaii"}, "missing": []})
        self.assertIn("in Hawaii", result["message"])
        self.assertEqual([row["name"] for row in result["artifact"]["volcanoes"]], ["Kilauea"])
        self.assertIn("monitored-volcano list", result["artifact"]["source"])

    def test_list_reports_counts_and_scope(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-list", "params": {"region": "Aleutians"}, "missing": []})
        self.assertIn("2 monitored volcano(s) for Aleutians", result["message"])
        self.assertIn("WATCH 1", result["message"])
        self.assertIn("Great Sitkin", result["message"])
        self.assertEqual(result["artifact"]["by_level"], {"WATCH": 1, "UNASSIGNED": 1})
        self.assertEqual(result["artifact"]["regions_available"][0], "Aleutians")

    def test_list_with_no_match_lists_the_known_regions(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-list", "params": {"region": "Atlantis"}, "missing": []})
        self.assertIn("No monitored volcano matches that filter", result["message"])
        self.assertIn("Hawaii", result["message"])

    def test_lookup_shows_colour_threat_and_links(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-lookup", "params": {"name": "Kilauea"}, "missing": []})
        self.assertIn("Kilauea — WATCH/ORANGE", result["message"])
        self.assertIn("observatory: Hawaiian Volcano Observatory", result["message"])
        self.assertIn("Very High Threat", result["message"])
        self.assertIn("usgs.gov/volcanoes/kilauea", result["message"])
        self.assertIn("Most recent elevated notice on file", result["message"])
        self.assertEqual(result["artifact"]["latest_notice"]["name"], "Kilauea")

    def test_lookup_without_a_name_asks(self):
        result = VolcanoAgent(FakeVsc()).run({"skill": "volcano-lookup", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which volcano", result["message"])

    def test_lookup_for_an_unknown_name_says_what_is_monitored(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-lookup", "params": {"name": "Atlantis"}, "missing": []})
        self.assertIn("No monitored volcano's name contains 'Atlantis'", result["message"])
        self.assertIsNone(result["artifact"]["latest_notice"])

    def test_watch_records_the_scope_and_levels(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-watch", "params": {"region": "Hawaii"}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "volcano-watch")
        self.assertEqual(watch["region"], "Hawaii")
        self.assertEqual(watch["observed"]["levels"], {"Kilauea": "WATCH"})
        self.assertEqual(watch["observed"]["elevated"], ["Kilauea"])
        self.assertNotIn("observed", result["artifact"]["watching"])
        self.assertIn("Point a pushNotificationConfig", result["message"])

    def test_watch_with_no_matching_volcano_declines_to_watch(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-watch", "params": {"region": "Atlantis"}, "missing": []})
        self.assertIsNone(result["watch"])
        self.assertIn("nothing to watch", result["message"])

    def test_missing_input_prompts(self):
        result = VolcanoAgent(FakeVsc()).run(
            {"skill": "volcano-lookup", "params": {}, "missing": ["name"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which volcano", result["message"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, VolcanoAgent(FakeVsc()))
        task = handler.handle("message/send", {"message": _message("which volcanoes are elevated?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], DATASET)
        self.assertEqual(data["count"], 3)

    def test_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, VolcanoAgent(FakeVsc()))
        first = handler.handle("message/send", {"message": _message("give me the details on it")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": _message("Kilauea", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["query"], "Kilauea")


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

    def test_watcher_reports_an_escalation(self):
        client = FakeVsc()
        agent = VolcanoAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when Kilauea changes alert level")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})

        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)  # the setup read is the baseline
        self.assertEqual(watcher.tick(), 0)

        client.features[0]["properties"]["alertLevel"] = "WARNING"
        client.features[0]["properties"]["colorCode"] = "RED"
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertEqual(self._posted_text(posted[0]),
                         "Volcano alert level up in the volcano Kilauea: Kilauea moved from WATCH "
                         "to WARNING")
        self.assertEqual(watcher.tick(), 0)

    def test_watcher_ignores_a_region_level_that_did_not_move(self):
        client = FakeVsc()
        agent = VolcanoAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("watch every volcano in Aleutians")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook", "token": "t"})
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: 204)
        self.assertEqual(watcher.tick(), 0)
        client._cache.clear()
        self.assertEqual(watcher.tick(), 0)

    def test_describe_watch_change_covers_each_case(self):
        agent = VolcanoAgent(FakeVsc())
        watch = {"region": "Hawaii", "name": None}
        self.assertIn("Volcano alert level up in the Hawaii region: Kilauea moved from ADVISORY to WATCH",
                      agent.describe_watch_change(watch, {"levels": {"Kilauea": "ADVISORY"}},
                                                  {"levels": {"Kilauea": "WATCH"}}))
        self.assertIn("Volcano alert level down in the Hawaii region: Kilauea moved from WATCH to NORMAL",
                      agent.describe_watch_change(watch, {"levels": {"Kilauea": "WATCH"}},
                                                  {"levels": {"Kilauea": "NORMAL"}}))
        self.assertIn("newly appear", agent.describe_watch_change(watch, {"levels": {}},
                                                                  {"levels": {"Kilauea": "WATCH"}}))
        self.assertIn("left the Hawaii region",
                      agent.describe_watch_change(watch, {"levels": {"Kilauea": "WATCH"}}, {"levels": {}}))
        self.assertIn("Kilauea moved from WATCH to UNASSIGNED",
                      agent.describe_watch_change(watch, {"levels": {"Kilauea": "WATCH"}},
                                                  {"levels": {"Kilauea": "UNASSIGNED"}}))


if __name__ == "__main__":
    unittest.main()
