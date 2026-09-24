"""Tests for the FAA airport status server.

    python3 servers/airports/tests/test_agent.py
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
    AirportAgent,
    code_from_text,
    codes_for_place,
    parse,
    place_from_text,
)
from data import (  # noqa: E402
    DATASET,
    AirportStatusClient,
    describe_entry,
    duration_phrase,
    parse_minutes,
    reason_kind,
    to_iso_utc,
)

#: The live document from 2026-09-24 00:01 UTC, with one arrival delay added so both
#: `Arrival_Departure` types are covered. Note the two "Airport Closures" blocks: the real
#: feed repeats the block name, so blocks must be concatenated rather than keyed by name.
STATUS_XML = """<AIRPORT_STATUS_INFORMATION>
<Update_Time>Thu Sep 24 00:01:24 2026 GMT</Update_Time>
<Dtd_File>http://www.fly.faa.gov/AirportStatus.dtd</Dtd_File>
<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List>
<Ground_Delay><ARPT>BOS</ARPT><Reason>runway construction</Reason><Avg>2 hours and 7 minutes</Avg><Max>5 hours and 7 minutes</Max></Ground_Delay>
<Ground_Delay><ARPT>SFO</ARPT><Reason>VOL:Multi-taxi</Reason><Avg>34 minutes</Avg><Max>1 hour and 5 minutes</Max></Ground_Delay>
</Ground_Delay_List></Delay_type>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List>
<Delay><ARPT>ORD</ARPT><Reason>VOL:Multi-taxi</Reason><Arrival_Departure Type="Departure"><Min>16 minutes</Min><Max>30 minutes</Max><Trend>Increasing</Trend></Arrival_Departure></Delay>
<Delay><ARPT>EWR</ARPT><Reason>WX:Thunderstorms</Reason><Arrival_Departure Type="Arrival"><Min>31 minutes</Min><Max>45 minutes</Max><Trend>Decreasing</Trend></Arrival_Departure></Delay>
</Arrival_Departure_Delay_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>
<Airport><ARPT>ALO</ARPT><Reason>!ALO 09/021 ALO AD AP CLSD EXC HEL 2609181928-2609240200</Reason><Start>Sep 18 at 19:28 UTC.</Start><Reopen>Sep 24 at 02:00 UTC.</Reopen></Airport>
</Airport_Closure_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>
<Airport><ARPT>LAX</ARPT><Reason>!LAX 05/277 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT</Reason><Start>May 27 at 18:26 UTC.</Start><Reopen>May 28 at 16:00 UTC.</Reopen></Airport>
</Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>
"""


class FakeFaa(AirportStatusClient):
    """An AirportStatusClient whose transport is a canned feed, so data.py runs for real."""

    def __init__(self, xml=None) -> None:
        super().__init__()
        self.xml = STATUS_XML if xml is None else xml
        self.calls: list[str] = []

    def _http_get_text(self, url, params, headers):
        self.calls.append(url)
        return self.xml


class HelperTests(unittest.TestCase):
    def test_parse_minutes_reads_the_faas_prose(self):
        self.assertEqual(parse_minutes("2 hours and 7 minutes"), 127)
        self.assertEqual(parse_minutes("1 hour and 1 minute"), 61)
        self.assertEqual(parse_minutes("16 minutes"), 16)
        self.assertEqual(parse_minutes("45"), 45)
        self.assertEqual(parse_minutes("2 hours"), 120)
        self.assertEqual(parse_minutes("30 seconds"), 0)  # under a minute, so no minutes
        self.assertIsNone(parse_minutes(""))
        self.assertIsNone(parse_minutes(None))
        self.assertIsNone(parse_minutes("soon"))

    def test_duration_phrase_is_readable_without_inventing_precision(self):
        self.assertEqual(duration_phrase(127), "2 h 7 m")
        self.assertEqual(duration_phrase(120), "2 h")
        self.assertEqual(duration_phrase(45), "45 m")
        self.assertIsNone(duration_phrase(None))

    def test_to_iso_utc_normalises_the_update_time(self):
        self.assertEqual(to_iso_utc("Thu Sep 24 00:01:24 2026 GMT"), "2026-09-24T00:01:24Z")
        self.assertEqual(to_iso_utc("2026-09-24 00:01:24"), "2026-09-24T00:01:24Z")
        self.assertIsNone(to_iso_utc(""))
        self.assertIsNone(to_iso_utc("some time"))

    def test_reason_kind_only_reads_unambiguous_shorthand(self):
        self.assertEqual(reason_kind("VOL:Multi-taxi"), "traffic volume")
        self.assertEqual(reason_kind("WX:Thunderstorms"), "weather")
        self.assertEqual(reason_kind("EQPT:ILS"), "equipment")
        self.assertIsNone(reason_kind("runway construction"))
        self.assertIsNone(reason_kind(None))

    def test_validators(self):
        self.assertEqual(AirportStatusClient.check_airport("sfo"), "SFO")
        with self.assertRaises(ValueError):
            AirportStatusClient.check_airport("S")
        with self.assertRaises(ValueError):
            AirportStatusClient.check_airport("SFOO!")
        self.assertEqual(AirportStatusClient.check_kind("Ground-Delay"), "ground-delay")
        with self.assertRaises(ValueError):
            AirportStatusClient.check_kind("strike")

    def test_codes_and_cities_come_from_the_shipped_table(self):
        self.assertEqual(AirportStatusClient.airport_city("SFO"), ("San Francisco", "CA"))
        self.assertIsNone(AirportStatusClient.airport_city("ZZZ"))
        self.assertEqual(AirportStatusClient.code_for_place("Chicago"), ["MDW", "ORD"])
        self.assertEqual(AirportStatusClient.code_for_place("denver"), ["DEN"])
        self.assertEqual(AirportStatusClient.code_for_place("Atlantis"), [])
        self.assertEqual(AirportStatusClient.code_for_place(""), [])

    def test_describe_entry_reads_like_a_sentence(self):
        row = {"kind": "ground-delay", "airport": "BOS", "city": "Boston",
               "reason": "runway construction"}
        self.assertEqual(describe_entry(row),
                         "ground delay program at BOS (Boston): runway construction")
        self.assertEqual(describe_entry({"kind": "closure", "airport": "ALO", "city": None,
                                         "reason": None}),
                         "airport closure at ALO")


class ClientTests(unittest.TestCase):
    def test_status_parses_every_block(self):
        read = FakeFaa().status()
        self.assertEqual(read["dataset"], DATASET)
        self.assertEqual(read["update_time"], "2026-09-24T00:01:24Z")
        self.assertEqual(read["counts"], {"ground-delay": 2, "arrival-delay": 1,
                                          "departure-delay": 1, "closure": 2})

    def test_ground_delays_carry_average_and_maximum(self):
        read = FakeFaa().status()
        self.assertEqual([row["airport"] for row in read["ground_delays"]], ["BOS", "SFO"])
        bos = read["ground_delays"][0]
        self.assertEqual(bos["average_minutes"], 127)
        self.assertEqual(bos["max_minutes"], 307)
        self.assertEqual(bos["average_text"], "2 hours and 7 minutes")
        self.assertEqual(bos["city"], "Boston")
        self.assertIsNone(bos["reason_kind"])
        self.assertEqual(bos["id"], "ground-delay:BOS:runway construction")

    def test_arrival_and_departure_delays_keep_their_min_max_and_trend(self):
        read = FakeFaa().status()
        self.assertEqual([row["airport"] for row in read["arrival_delays"]], ["EWR"])
        self.assertEqual([row["airport"] for row in read["departure_delays"]], ["ORD"])
        arrival = read["arrival_delays"][0]
        self.assertEqual(arrival["min_minutes"], 31)
        self.assertEqual(arrival["max_minutes"], 45)
        self.assertEqual(arrival["trend"], "Decreasing")
        self.assertEqual(arrival["reason_kind"], "weather")
        self.assertIsNone(arrival["average_minutes"])
        self.assertEqual(read["departure_delays"][0]["trend"], "Increasing")

    def test_both_closure_blocks_are_kept(self):
        read = FakeFaa().status()
        self.assertEqual([row["airport"] for row in read["closures"]], ["ALO", "LAX"])
        alo = read["closures"][0]
        self.assertEqual(alo["start"], "Sep 18 at 19:28 UTC.")
        self.assertEqual(alo["reopen"], "Sep 24 at 02:00 UTC.")
        self.assertIn("CLSD EXC HEL", alo["reason"])
        self.assertIsNone(alo["max_minutes"])

    def test_airport_slices_the_snapshot(self):
        read = FakeFaa().airport("SFO")
        self.assertEqual(read["count"], 1)
        self.assertEqual(read["city"], "San Francisco")
        self.assertEqual([row["kind"] for row in read["entries"]], ["ground-delay"])
        self.assertEqual(len(read["programs"]), 1)
        empty = FakeFaa().airport("ATL")
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["city"], "Atlanta")

    def test_entries_are_worst_first_and_grouped_by_kind(self):
        rows = AirportStatusClient.entries(FakeFaa().status())
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds[:2], ["ground-delay", "ground-delay"])
        self.assertEqual(kinds[2], "closure")
        self.assertEqual([row["airport"] for row in rows[:2]], ["BOS", "SFO"])

    def test_watch_state_compares_only_the_open_items(self):
        client = FakeFaa()
        everything = client.watch_state()
        self.assertEqual(len(everything["entries"]), 6)
        self.assertNotIn("update_time", everything)
        sfo = client.watch_state("SFO")
        self.assertEqual(list(sfo["entries"]), ["ground-delay:SFO:VOL:Multi-taxi"])
        programs = client.watch_state(kinds=("ground-delay",))
        self.assertEqual(len(programs["entries"]), 2)
        self.assertEqual(client.watch_state("ATL")["entries"], {})

    def test_reads_are_cached(self):
        client = FakeFaa()
        client.status()
        client.airport("SFO")
        client.watch_state()
        self.assertEqual(len(client.calls), 1)

    def test_broken_or_wrong_xml_is_a_value_error(self):
        with self.assertRaises(ValueError):
            FakeFaa(xml="<AIRPORT_STATUS_INFORMATION><Delay_type").status()
        with self.assertRaises(ValueError):
            FakeFaa(xml="<something_else/>").status()


class ParseTests(unittest.TestCase):
    def test_status_is_the_default_when_an_airport_is_named(self):
        parsed = parse(_message("is SFO delayed right now?"))
        self.assertEqual(parsed["skill"], "airport-status")
        self.assertEqual(parsed["params"]["airports"], ["SFO"])

    def test_city_names_resolve_to_their_airports(self):
        parsed = parse(_message("how is Chicago doing today?"))
        self.assertEqual(parsed["params"]["airports"], ["MDW", "ORD"])
        self.assertEqual(place_from_text("anything wrong in Denver?"), "Denver")
        self.assertEqual(codes_for_place("Denver"), ["DEN"])
        self.assertIsNone(place_from_text("nothing here"))

    def test_unknown_codes_need_context(self):
        self.assertIsNone(code_from_text("nothing to see"))
        self.assertEqual(code_from_text("check FAT for delays"), "FAT")
        self.assertEqual(code_from_text("is JFK affected?"), "JFK")

    def test_delay_and_closure_phrases_pick_their_skills(self):
        self.assertEqual(parse(_message("what are the worst delays in the country?"))["skill"],
                         "airports-delays")
        self.assertEqual(parse(_message("are any airports closed?"))["skill"],
                         "airports-closures")
        nationwide = parse(_message("is the system backed up right now?"))
        self.assertEqual(nationwide["skill"], "airports-delays")

    def test_watch_skill_reads_the_airport_and_kind(self):
        parsed = parse(_message("tell me when SFO gets a ground delay program"))
        self.assertEqual(parsed["skill"], "airport-watch")
        self.assertEqual(parsed["params"]["airports"], ["SFO"])
        systemwide = parse(_message("notify me when any ground delay program starts"))
        self.assertEqual(systemwide["skill"], "airport-watch")
        self.assertNotIn("airports", systemwide["params"])
        by_kind = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "airport-watch", "airport": "ORD",
                                      "kind": "ground-delay"}}]}
        self.assertEqual(parse(by_kind)["params"]["kind"], "ground-delay")

    def test_data_part_wins_over_text(self):
        payload = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "airport-status", "airports": ["ORD", "MDW"]}},
            {"kind": "text", "text": "is SFO delayed?"}]}
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "airport-status")
        self.assertEqual(parsed["params"]["airports"], ["ORD", "MDW"])
        self.assertTrue(parsed["explicit"])

    def test_airports_parameter_accepts_a_comma_string(self):
        payload = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "airports-delays", "airports": "ord, mdw"}}]}
        self.assertEqual(parse(payload)["params"]["airports"], ["ORD", "MDW"])


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
        self.assertEqual(ids, ["airport-status", "airports-delays", "airports-closures",
                               "airport-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = AirportAgent(FakeFaa())
        self.assertEqual(agent.missing("airport-status", {}), ["airport"])
        self.assertEqual(agent.missing("airport-status", {"airports": ["SFO"]}), [])
        self.assertEqual(agent.missing("airports-delays", {}), [])
        self.assertEqual(agent.missing("airport-watch", {}), [])

    def test_status_reports_a_program_with_its_reason_and_timings(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-status", "params": {"airports": ["SFO"]}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("as of 2026-09-24T00:01:24Z UTC", result["message"])
        self.assertIn("SFO (San Francisco) — ground delay: VOL:Multi-taxi [traffic volume] "
                      "(average 34 m, up to 1 h 5 m)", result["message"])
        self.assertIn(DATASET, result["message"])
        self.assertEqual(result["artifact"]["count"], 1)
        self.assertIsNone(result["watch"])

    def test_status_for_an_unaffected_airport_is_honest(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-status", "params": {"airports": ["ATL"]}, "missing": []})
        self.assertIn("ATL does not appear in the FAA's current status snapshot", result["message"])
        self.assertIn("as of 2026-09-24T00:01:24Z UTC", result["message"])
        self.assertIn("not that operations are normal", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_status_for_several_quiet_airports_reads_plurally(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-status", "params": {"airports": ["ATL", "DEN"]}, "missing": []})
        self.assertIn("ATL and DEN do not appear", result["message"])
        self.assertIn("reported for them", result["message"])

    def test_status_without_an_airport_asks_for_one(self):
        result = AirportAgent(FakeFaa()).run({"skill": "airport-status", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which airport", result["message"])

    def test_status_covers_closures_with_their_window(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-status", "params": {"airports": ["ALO"]}, "missing": []})
        self.assertIn("ALO — !ALO 09/021", result["message"])
        self.assertIn("from Sep 18 at 19:28 UTC.", result["message"])
        self.assertIn("until Sep 24 at 02:00 UTC.", result["message"])

    def test_delays_messages_are_worst_first_with_system_counts(self):
        result = AirportAgent(FakeFaa()).run({"skill": "airports-delays", "params": {}, "missing": []})
        self.assertIn("4 delay item(s) in the FAA snapshot", result["message"])
        self.assertIn("2 ground delay program(s), 1 arrival and 1 departure delay report(s)",
                      result["message"])
        self.assertLess(result["message"].index("BOS"), result["message"].index("SFO"))
        self.assertNotIn("ALO", result["message"])  # closures are not in this skill
        self.assertEqual(result["artifact"]["count"], 4)

    def test_delays_can_be_filtered_by_kind(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airports-delays", "params": {"kind": "ground-delay"}, "missing": []})
        self.assertEqual(result["artifact"]["count"], 2)
        self.assertNotIn("ORD", result["message"])

    def test_delays_with_no_match_says_so(self):
        result = AirportAgent(FakeFaa(xml=STATUS_XML.replace(
            "Ground Delay Programs", "Ground Delay Programs").split("<Delay_type><Name>General")[0]
            + "</AIRPORT_STATUS_INFORMATION>")).run(
            {"skill": "airports-delays", "params": {"kind": "arrival-delay"}, "missing": []})
        self.assertIn("No delay of that kind is in the FAA's current snapshot", result["message"])

    def test_closures_lists_every_closed_airport(self):
        result = AirportAgent(FakeFaa()).run({"skill": "airports-closures", "params": {}, "missing": []})
        self.assertIn("2 airport closure(s)", result["message"])
        self.assertIn("ALO —", result["message"])
        self.assertIn("LAX (Los Angeles) —", result["message"])
        self.assertIn("runway or a category of traffic", result["message"])

    def test_closures_when_there_are_none(self):
        closed = FakeFaa(xml="<AIRPORT_STATUS_INFORMATION>"
                             "<Update_Time>Thu Sep 24 00:01:24 2026 GMT</Update_Time>"
                             "</AIRPORT_STATUS_INFORMATION>")
        result = AirportAgent(closed).run({"skill": "airports-closures", "params": {}, "missing": []})
        self.assertIn("No airport closure is in the FAA's current snapshot", result["message"])

    def test_watch_records_the_scope_and_the_open_items(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-watch", "params": {"airports": ["SFO"]}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "airport-watch")
        self.assertEqual(watch["airport"], "SFO")
        self.assertEqual(list(watch["observed"]["entries"]),
                         ["ground-delay:SFO:VOL:Multi-taxi"])
        self.assertNotIn("observed", result["artifact"]["watching"])
        self.assertIn("Watching SFO (San Francisco)", result["message"])
        self.assertIn("Point a pushNotificationConfig", result["message"])

    def test_watch_can_cover_the_whole_system(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-watch", "params": {}, "missing": []})
        self.assertEqual(result["watch"]["airport"], None)
        self.assertIn("the whole national system", result["message"])
        self.assertEqual(result["artifact"]["count"], 6)

    def test_watch_on_several_airports_is_declined(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-watch", "params": {"airports": ["ORD", "MDW"]}, "missing": []})
        self.assertIsNone(result["watch"])
        self.assertIn("covers one airport", result["message"])

    def test_missing_input_prompts(self):
        result = AirportAgent(FakeFaa()).run(
            {"skill": "airport-status", "params": {}, "missing": ["airport"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which airport", result["message"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, AirportAgent(FakeFaa()))
        task = handler.handle("message/send", {"message": _message("is SFO delayed?")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], DATASET)
        self.assertEqual(data["airports"], ["SFO"])

    def test_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, AirportAgent(FakeFaa()))
        ask = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "airport-status"}}]}
        first = handler.handle("message/send", {"message": ask})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": _message("ORD", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["airports"], ["ORD"])


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

    def test_watcher_fires_when_a_program_appears_or_clears(self):
        client = FakeFaa()
        agent = AirportAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when SFO gets a ground delay program")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook",
                                                "token": "t"})
        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)  # baseline: SFO already has a program
        self.assertEqual(watcher.tick(), 0)

        # The average delay is revised but the program stays: no page, because only the set of
        # open items is compared.
        client.xml = STATUS_XML.replace("<Avg>34 minutes</Avg>", "<Avg>50 minutes</Avg>")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 0)

        # The program clears: that is news.
        client.xml = STATUS_XML.replace(
            "<Ground_Delay><ARPT>SFO</ARPT><Reason>VOL:Multi-taxi</Reason>"
            "<Avg>34 minutes</Avg><Max>1 hour and 5 minutes</Max></Ground_Delay>", "")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("Cleared at SFO: ground delay program at SFO (San Francisco): VOL:Multi-taxi",
                      self._posted_text(posted[0]))
        self.assertEqual(watcher.tick(), 0)

        # And when a closure appears for the watched airport, it fires as new.
        client.xml = STATUS_XML.replace("<ARPT>SFO</ARPT><Reason>VOL:Multi-taxi</Reason>",
                                        "<ARPT>SFO</ARPT><Reason>VOL:Multi-taxi</Reason>") + ""
        client.xml = client.xml.replace(
            "<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>",
            "<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>"
            "<Airport><ARPT>SFO</ARPT><Reason>!SFO runway sweep</Reason>"
            "<Start>Sep 24 at 03:00 UTC.</Start><Reopen>Sep 24 at 04:00 UTC.</Reopen></Airport>")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("New at SFO: airport closure at SFO", self._posted_text(posted[1]))

    def test_describe_watch_change_covers_each_case(self):
        agent = AirportAgent(FakeFaa())
        watch = {"airport": "SFO"}
        self.assertIn("New at SFO", agent.describe_watch_change(
            watch, {"entries": {}}, {"entries": {"a": "ground delay program at SFO"}}))
        self.assertIn("Cleared at SFO", agent.describe_watch_change(
            watch, {"entries": {"a": "ground delay program at SFO"}}, {"entries": {}}))
        self.assertIn("opened and 1 cleared", agent.describe_watch_change(
            watch, {"entries": {"a": "old at SFO"}}, {"entries": {"b": "new at SFO"}}))
        self.assertIn("national system", agent.describe_watch_change(
            {"airport": None}, None, None))


if __name__ == "__main__":
    unittest.main()
