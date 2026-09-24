"""Tests for the NDBC buoy server.

    python3 servers/buoys/tests/test_agent.py
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import A2AHandler, PushWatcher, TaskStore  # noqa: E402

from agent import (  # noqa: E402
    CARD_SKILLS,
    DEFAULT_RADIUS_MILES,
    BuoyAgent,
    age_minutes,
    field_from_text,
    hours_from_text,
    parse,
    place_from_text,
    query_from_text,
    radius_from_text,
    station_from_text,
    threshold_from_text,
    unknown_place_from_text,
)
from data import (  # noqa: E402
    DATASET,
    BuoyClient,
    compass,
    haversine_miles,
    number,
    parse_table,
)

#: Column headers copied verbatim from the live latest_obs.txt on 2026-09-23.
LATEST_HEADER = (
    "#STN       LAT      LON  YYYY MM DD hh mm WDIR WSPD   GST WVHT  DPD APD MWD   PRES  PTDY"
    "  ATMP  WTMP  DEWP  VIS   TIDE\n"
    "#text      deg      deg   yr mo day hr mn degT  m/s   m/s   m   sec sec degT   hPa   hPa"
    "  degC  degC  degC  nmi     ft\n"
)

#: Per-station table header copied verbatim from the live realtime2 read on 2026-09-23.
REALTIME_HEADER = (
    "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE\n"
    "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft\n"
)


def stamp(hours_ago: float = 0.0) -> str:
    """A table timestamp in NDBC's own layout: `YYYY MM DD hh mm`."""
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return when.strftime("%Y %m %d %H %M")


def obs_row(station, lat, lon, when, wdir="MM", wspd="MM", gst="MM", wvht="MM", dpd="MM", apd="MM",
            mwd="MM", pres="MM", ptdy="MM", atmp="MM", wtmp="MM", dewp="MM", vis="MM", tide="MM") -> str:
    """One latest-observation row. Values are the raw table strings, `MM` meaning missing."""
    return (f"{station:<8} {lat:>6}  {lon:>7} {when}  {wdir:>3} {wspd:>5} {gst:>5} {wvht:>5}"
            f" {dpd:>4} {apd:>4} {mwd:>4} {pres:>7} {ptdy:>5} {atmp:>5} {wtmp:>5} {dewp:>5}"
            f" {vis:>4} {tide:>4}\n")


#: The real 41025 and SANF1 readings from 2026-09-23, plus a station whose sensor row is all
#: missing. 44099 is in the catalogue but not in the table at all, like NDBC's silent stations.
LATEST_OBS = LATEST_HEADER + "".join([
    obs_row("41025", "35.025", "-75.380", stamp(), "20", "16.0", "19.0", "3.3", "11", "6.4", "47",
            "1015.8", "MM", "24.8", "25.2", "21.4"),
    obs_row("SANF1", "24.455", "-81.877", stamp(0.5), "70", "4.0", "5.0", "0.5", "4", "3.6", "80",
            "1014.2", "0.4", "28.9", "29.7", "25.6"),
    obs_row("42001", "25.900", "-89.670", stamp(90)),
])

STATIONS_XML = """<?xml version="1.0" encoding="utf-8"?><stations created="2026-09-23T23:55:04UTC" count="4">
  <station id="41025" lat="35.025" lon="-75.38" elev="0" name="Diamond Shoals" owner="National Data Buoy Center" pgm="Moored Buoy" type="buoy" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="SANF1" lat="24.455" lon="-81.877" elev="0" name="Sand Key" owner="National Data Buoy Center" pgm="C-MAN" type="fixed" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="42001" lat="25.9" lon="-89.67" elev="0" name="Mid Gulf" owner="National Data Buoy Center" pgm="Moored Buoy" type="buoy" met="y" currents="y" waterquality="n" dart="y"/>
  <station id="44099" lat="36.5" lon="-70.0" elev="0" name="Silent One" owner="Woods Hole Oceanographic Institution" pgm="Moored Buoy" type="buoy" met="y" currents="n" waterquality="n" dart="n"/>
</stations>
"""


def realtime_rows(rows) -> str:
    """Per-station `realtime2` text from (when, wdir, wspd, gst, wvht, dpd, apd, mwd, pres, atmp,
    wtmp, dewp) tuples, newest first."""
    body = ""
    for row in rows:
        when, wdir, wspd, gst, wvht, dpd, apd, mwd, pres, atmp, wtmp, dewp = row
        body += (f"{when}  {wdir:>3} {wspd:>5} {gst:>5} {wvht:>5} {dpd:>4} {apd:>4} {mwd:>4}"
                 f" {pres:>7} {atmp:>5} {wtmp:>5} {dewp:>5}  MM    MM    MM\n")
    return REALTIME_HEADER + body


REALTIME = realtime_rows([
    (stamp(0.0), "20", "16.0", "19.0", "3.3", "11", "6.4", "47", "1015.8", "24.8", "25.2", "21.4"),
    (stamp(0.2), "20", "15.0", "18.0", "3.2", "8", "6.3", "39", "1016.1", "24.8", "25.2", "21.2"),
    (stamp(0.5), "30", "12.0", "14.0", "2.4", "7", "5.9", "41", "1016.4", "25.1", "25.4", "21.6"),
])


class FakeNdbc(BuoyClient):
    """A BuoyClient whose transport is a canned NDBC read, so data.py runs for real."""

    def __init__(self, latest=None, stations=None, realtime=None, units=None) -> None:
        super().__init__(units=units)
        self.latest_text = LATEST_OBS if latest is None else latest
        self.stations_text = STATIONS_XML if stations is None else stations
        self.realtime_text = REALTIME if realtime is None else realtime
        self.calls: list[str] = []

    def _http_get_text(self, url, params, headers):
        self.calls.append(url)
        if url.endswith("latest_obs.txt"):
            return self.latest_text
        if url.endswith("activestations.xml"):
            return self.stations_text
        if "/realtime2/" in url:
            return self.realtime_text
        raise AssertionError(f"unexpected URL in test: {url}")


class HelperTests(unittest.TestCase):
    def test_compass_names_degrees(self):
        self.assertEqual(compass(0), "N")
        self.assertEqual(compass(20), "NNE")
        self.assertEqual(compass(47), "NE")
        self.assertEqual(compass(90), "E")
        self.assertEqual(compass(359), "N")
        self.assertIsNone(compass(None))
        self.assertIsNone(compass("MM"))

    def test_number_treats_MM_as_missing_not_zero(self):
        self.assertEqual(number("16.0"), 16.0)
        self.assertIsNone(number("MM"))
        self.assertIsNone(number(""))
        self.assertIsNone(number(None))
        self.assertIsNone(number("not a number"))

    def test_parse_table_reads_the_header_and_skips_comment_and_unit_lines(self):
        columns, rows = parse_table(REALTIME)
        self.assertEqual(columns[:6], ["YY", "MM", "DD", "hh", "mm", "WDIR"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][6], "16.0")
        columns, rows = parse_table(LATEST_OBS)
        self.assertEqual(columns[0], "STN")
        self.assertEqual(len(rows), 3)

    def test_haversine_is_zero_at_the_same_point(self):
        self.assertEqual(haversine_miles(35.0, -75.0, 35.0, -75.0), 0.0)
        self.assertTrue(68.5 <= haversine_miles(35.0, -75.0, 36.0, -75.0) <= 69.5)


class ClientTests(unittest.TestCase):
    def test_units_reject_anything_else(self):
        self.assertEqual(BuoyClient.check_units("METRIC"), "metric")
        with self.assertRaises(ValueError):
            BuoyClient.check_units("furlongs")

    def test_english_and_metric_conversions(self):
        english = FakeNdbc()
        metric = FakeNdbc(units="metric")
        self.assertEqual(english.wind(16.0), 35.8)
        self.assertEqual(metric.wind(16.0), 16.0)
        self.assertEqual(english.distance(3.3), 10.8)
        self.assertEqual(metric.distance(3.3), 3.3)
        self.assertEqual(english.temperature(25.2), 77.4)
        self.assertEqual(metric.temperature(25.2), 25.2)
        self.assertEqual(english.pressure(1015.8), 30.0)
        self.assertEqual(metric.pressure(1015.8), 1015.8)
        self.assertEqual(english.wind_unit(), "mph")
        self.assertEqual(metric.distance_unit(), "m")
        self.assertEqual(metric.pressure_unit(), "hPa")

    def test_validators(self):
        self.assertEqual(BuoyClient.check_station("41025"), "41025")
        self.assertEqual(BuoyClient.check_station("sanf1"), "SANF1")
        with self.assertRaises(ValueError):
            BuoyClient.check_station("41")
        self.assertEqual(BuoyClient.check_point("35.025, -75.38"), (35.025, -75.38))
        with self.assertRaises(ValueError):
            BuoyClient.check_point("35.025")
        with self.assertRaises(ValueError):
            BuoyClient.check_lat(91)
        with self.assertRaises(ValueError):
            BuoyClient.check_lon(-181)
        with self.assertRaises(ValueError):
            BuoyClient.check_positive(0)
        with self.assertRaises(ValueError):
            BuoyClient.check_miles(0)
        self.assertEqual(BuoyClient.check_field("WAVE_HEIGHT"), "wave_height")
        with self.assertRaises(ValueError):
            BuoyClient.check_field("salinity")
        self.assertEqual(BuoyClient.check_threshold("8"), 8.0)
        with self.assertRaises(ValueError):
            BuoyClient.check_threshold("2000")

    def test_city_lookup(self):
        self.assertEqual(BuoyClient.city("Miami"), (25.76, -80.19, "Miami"))
        self.assertEqual(BuoyClient.city("cape hatteras")[2], "Cape Hatteras")
        self.assertIsNone(BuoyClient.city("Atlantis"))

    def test_conditions_converts_the_station_row(self):
        read = FakeNdbc().conditions("41025")
        self.assertEqual(read["station"], "41025")
        self.assertEqual(read["wind_speed"], 35.8)
        self.assertEqual(read["wind_gust"], 42.5)
        self.assertEqual(read["wind_direction"], "NNE")
        self.assertEqual(read["wind_direction_degrees"], 20.0)
        self.assertEqual(read["wave_height"], 10.8)
        self.assertEqual(read["dominant_period"], 11.0)
        self.assertEqual(read["water_temp"], 77.4)
        self.assertEqual(read["pressure"], 30.0)
        self.assertIsNone(read["visibility"])
        self.assertIsNone(read["tide"])
        self.assertEqual(read["units"]["wave_height"], "ft")
        self.assertEqual(read["dataset"], DATASET)

    def test_conditions_for_an_unknown_station_is_none(self):
        self.assertIsNone(FakeNdbc().conditions("99999"))
        self.assertIsNone(FakeNdbc().conditions("44099"))  # catalogued but not in the table

    def test_latest_covers_every_row_in_the_table(self):
        read = FakeNdbc().latest()
        self.assertEqual(read["count"], 3)
        self.assertEqual([row["station"] for row in read["observations"]],
                         ["41025", "SANF1", "42001"])
        self.assertIsNone(read["observations"][2]["wind_speed"])

    def test_station_catalogue_parses_attributes(self):
        row = FakeNdbc().station("SANF1")
        self.assertEqual(row["name"], "Sand Key")
        self.assertEqual(row["type"], "fixed")
        self.assertEqual(row["program"], "C-MAN")
        self.assertEqual(row["latitude"], 24.455)
        self.assertTrue(row["sensors"]["met"])
        self.assertFalse(row["sensors"]["dart"])
        self.assertTrue(row["reporting"])
        silent = FakeNdbc().station("44099")
        self.assertFalse(silent["reporting"])
        self.assertIsNone(FakeNdbc().station("99999"))

    def test_stations_filters(self):
        client = FakeNdbc()
        self.assertEqual(client.stations(text="diamond")["count"], 1)
        self.assertEqual(client.stations(program="moored buoy")["count"], 3)
        self.assertEqual(client.stations(type_="fixed")["count"], 1)
        self.assertEqual(client.stations(met_only=True)["count"], 4)
        self.assertEqual(client.stations(owner="oceanographic")["count"], 1)
        reporting = client.stations(reporting_only=True)
        self.assertEqual(reporting["count"], 3)  # 44099 has no row in the latest table
        self.assertEqual(reporting["reporting"], 3)

    def test_programmes_and_types_come_from_the_data(self):
        client = FakeNdbc()
        self.assertEqual(client.programs(), ["C-MAN", "Moored Buoy"])
        self.assertEqual(client.station_types(), ["buoy", "fixed"])

    def test_near_sorts_by_real_distance_and_filters(self):
        client = FakeNdbc()
        read = client.near(25.76, -80.19, radius_miles=250)
        self.assertEqual([row["id"] for row in read["stations"]], ["SANF1"])
        self.assertTrue(130 <= read["stations"][0]["distance_miles"] <= 145,
                        read["stations"][0]["distance_miles"])
        self.assertEqual(client.near(25.76, -80.19, radius_miles=10)["count"], 0)
        wide = client.near(25.76, -80.19, radius_miles=700)
        self.assertEqual([row["id"] for row in wide["stations"]], ["SANF1", "42001"])

    def test_near_can_include_stations_that_are_not_reporting(self):
        client = FakeNdbc()
        read = client.near(41.0, -69.0, radius_miles=400, reporting_only=False)
        self.assertEqual([row["id"] for row in read["stations"]], ["44099"])
        self.assertFalse(read["stations"][0]["reporting"])

    def test_history_is_newest_first_with_per_field_stats(self):
        read = FakeNdbc().history("41025", hours=24)
        self.assertEqual(read["count"], 3)
        self.assertEqual(read["last"], read["series"][0]["time"])
        self.assertEqual(read["first"], read["series"][-1]["time"])
        self.assertEqual(read["stats"]["wave_height"]["latest"], 10.8)
        self.assertEqual(read["stats"]["wave_height"]["max"], 10.8)
        self.assertEqual(read["stats"]["wave_height"]["min"], 7.9)
        self.assertEqual(read["stats"]["wind_speed"]["max"], 35.8)
        self.assertEqual(read["stats"]["wave_height"]["unit"], "ft")
        self.assertIn("wave_height", read["fields"])
        self.assertNotIn("visibility", read["fields"])  # every row is MM

    def test_history_of_an_empty_file_is_empty_not_an_error(self):
        client = FakeNdbc(realtime=REALTIME_HEADER)
        read = client.history("41025")
        self.assertEqual(read["count"], 0)
        self.assertEqual(read["series"], [])

    def test_watch_state_compares_only_the_crossing(self):
        client = FakeNdbc()
        self.assertEqual(client.watch_state("41025", "wave_height", 8), {"above": True})
        self.assertEqual(client.watch_state("41025", "wave_height", 20), {"above": False})
        with self.assertRaises(ValueError):
            client.watch_state("41025", "visibility", 1)  # this station publishes no visibility
        with self.assertRaises(ValueError):
            client.watch_state("44099", "wave_height", 5)  # no current observation at all
        with self.assertRaises(ValueError):
            client.watch_state("42001", "wave_height", 5)  # in the table, but every sensor is MM

    def test_reads_are_cached_per_url(self):
        client = FakeNdbc()
        client.conditions("41025")
        client.conditions("41025")
        client.station("41025")
        client.stations(text="diamond")
        self.assertEqual(len([url for url in client.calls if url.endswith("latest_obs.txt")]), 1)
        self.assertEqual(len([url for url in client.calls if url.endswith("activestations.xml")]), 1)

    def test_broken_xml_is_a_value_error_not_a_crash(self):
        client = FakeNdbc(stations="<stations><station")
        with self.assertRaises(ValueError):
            client.stations(text="anything")


class ParseTests(unittest.TestCase):
    def test_conditions_is_the_default_skill(self):
        parsed = parse(_message("what is the sea state at 41025?"))
        self.assertEqual(parsed["skill"], "buoy-conditions")
        self.assertEqual(parsed["params"]["station"], "41025")

    def test_station_place_and_point_extraction(self):
        self.assertEqual(station_from_text("buoy 41025 please"), "41025")
        self.assertEqual(station_from_text("SANF1 report"), "SANF1")
        self.assertIsNone(station_from_text("no station here"))
        self.assertEqual(place_from_text("how rough is it near Key West?"), "key west")
        self.assertIsNone(place_from_text("no place here"))
        by_point = parse(_message("conditions at 35.025,-75.38"))
        self.assertEqual(by_point["params"]["point"], "35.025,-75.38")
        by_city = parse(_message("how big are the waves off Cape Hatteras?"))
        self.assertEqual(by_city["params"]["place"], "cape hatteras")

    def test_a_place_the_list_does_not_have_is_still_reported(self):
        parsed = parse(_message("how rough is the water off Atlantis?"))
        self.assertEqual(parsed["skill"], "buoy-conditions")
        self.assertEqual(parsed["params"]["place"], "Atlantis")
        self.assertIsNone(unknown_place_from_text("any buoys near me?"))
        self.assertEqual(unknown_place_from_text("conditions off Cape Decision"), "Cape Decision")

    def test_trend_skill_with_hours(self):
        parsed = parse(_message("how have the waves at 44009 changed over the last 24 hours?"))
        self.assertEqual(parsed["skill"], "buoy-trend")
        self.assertEqual(parsed["params"]["hours"], 24)
        self.assertEqual(parse(_message("trend for 41025"))["params"]["hours"], 24)
        self.assertEqual(hours_from_text("past 48 hours"), 48)
        self.assertIsNone(hours_from_text("no window"))

    def test_near_list_skill_with_radius(self):
        parsed = parse(_message("which buoys are near Key West within 200 miles?"))
        self.assertEqual(parsed["skill"], "buoys-near")
        self.assertEqual(parsed["params"]["radius_miles"], 200.0)
        self.assertEqual(radius_from_text("within 150 miles"), 150.0)
        defaulted = parse(_message("any buoys near Miami?"))
        self.assertEqual(defaulted["skill"], "buoys-near")
        self.assertEqual(defaulted["params"]["radius_miles"], DEFAULT_RADIUS_MILES)

    def test_list_skill_pulls_a_quoted_search_term(self):
        parsed = parse(_message('find NDBC stations with the word "Diamond" in the name'))
        self.assertEqual(parsed["skill"], "buoys-list")
        self.assertEqual(parsed["params"]["query"], "Diamond")
        self.assertEqual(query_from_text("stations named Sand Key"), "Sand")
        with_programme = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "buoys-list", "program": "Moored Buoy"}}]}
        self.assertEqual(parse(with_programme)["params"]["program"], "Moored Buoy")

    def test_watch_skill_reads_the_field_and_threshold(self):
        parsed = parse(_message("tell me when the waves at 41025 pass 8 feet"))
        self.assertEqual(parsed["skill"], "buoy-watch")
        self.assertEqual(parsed["params"]["station"], "41025")
        self.assertEqual(parsed["params"]["field"], "wave_height")
        self.assertEqual(parsed["params"]["threshold"], 8.0)
        by_wind = parse(_message("notify me if wind at SANF1 goes above 25"))
        self.assertEqual(by_wind["params"]["field"], "wind_speed")
        self.assertEqual(by_wind["params"]["threshold"], 25.0)
        self.assertNotIn("threshold", parse(_message("watch 41025"))["params"])
        self.assertEqual(parse(_message("watch 41025"))["params"]["field"], "wave_height")
        self.assertEqual(field_from_text("how warm is the sea temp?"), "water_temp")
        self.assertEqual(field_from_text("gusts?"), "wind_gust")
        self.assertIsNone(field_from_text("nothing here"))
        self.assertEqual(threshold_from_text("above 12"), 12.0)
        self.assertIsNone(threshold_from_text("above"))

    def test_data_part_wins_over_text(self):
        payload = {"kind": "message", "role": "user", "messageId": "m", "parts": [
            {"kind": "data", "data": {"skill": "buoy-conditions", "station": "SANF1"}}]}
        parsed = parse(payload)
        self.assertEqual(parsed["skill"], "buoy-conditions")
        self.assertEqual(parsed["params"]["station"], "SANF1")
        self.assertTrue(parsed["explicit"])


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
        self.assertEqual(ids, ["buoy-conditions", "buoy-trend", "buoys-near", "buoys-list",
                               "buoy-watch"])
        for skill in CARD_SKILLS:
            self.assertTrue(skill["description"])
            self.assertTrue(skill["examples"])

    def test_missing_rules(self):
        agent = BuoyAgent(FakeNdbc())
        self.assertEqual(agent.missing("buoy-conditions", {}), ["location"])
        self.assertEqual(agent.missing("buoy-conditions", {"station": "41025"}), [])
        self.assertEqual(agent.missing("buoys-list", {}), [])
        self.assertEqual(agent.missing("buoy-watch", {"place": "Miami"}), [])

    def test_conditions_reports_values_units_and_age(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"station": "41025"}, "missing": []})
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("Latest NDBC observation from Diamond Shoals", result["message"])
        self.assertIn("wind 35.8 mph from NNE (20°)", result["message"])
        self.assertIn("gusting 42.5 mph", result["message"])
        self.assertIn("waves 10.8 ft with a 11 s dominant period", result["message"])
        self.assertIn("sea surface 77.4 °F", result["message"])
        self.assertIn("Not published by this station right now: visibility", result["message"])
        self.assertIn(DATASET, result["message"])
        self.assertLess(result["artifact"]["age_minutes"], 5)

    def test_conditions_flags_a_stale_reading(self):
        stale = LATEST_HEADER + obs_row("41025", "35.025", "-75.380", stamp(20), "20", "16.0", "19.0",
                                        "3.3", "11", "6.4", "47", "1015.8", "MM", "24.8", "25.2", "21.4")
        result = BuoyAgent(FakeNdbc(latest=stale)).run(
            {"skill": "buoy-conditions", "params": {"station": "41025"}, "missing": []})
        self.assertIn("reporting late, not calm", result["message"])

    def test_conditions_for_an_unknown_station_is_honest(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"station": "99999"}, "missing": []})
        self.assertIn("no station '99999'", result["message"])
        self.assertIn("will not guess", result["message"])

    def test_conditions_for_a_station_outside_the_table(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"station": "44099"}, "missing": []})
        self.assertIn("has no current observation", result["message"])
        self.assertIn("will not invent", result["message"])
        self.assertFalse(result["artifact"]["observing"])

    def test_conditions_for_a_station_whose_sensors_are_all_missing(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"station": "42001"}, "missing": []})
        self.assertIn("every sensor in that row reads missing", result["message"])
        self.assertFalse(result["artifact"]["observing"])

    def test_conditions_resolves_a_place_to_the_nearest_reporting_station(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"place": "miami"}, "missing": []})
        self.assertIn("SANF1 near Miami", result["message"])
        self.assertEqual(result["artifact"]["station"], "SANF1")
        self.assertEqual(result["artifact"]["observation"]["wave_height"], 1.6)

    def test_conditions_for_a_place_with_no_station_nearby(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"point": "50.0,60.0"}, "missing": []})
        self.assertIn("No reporting NDBC station is within", result["message"])

    def test_conditions_for_an_unknown_place(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {"place": "atlantis"}, "missing": []})
        self.assertIn("will not guess", result["message"])
        self.assertFalse(result["artifact"]["known"])

    def test_trend_reports_lows_highs_and_rows(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-trend", "params": {"station": "41025", "hours": 24}, "missing": []})
        self.assertIn("Last 3 observations from station 41025", result["message"])
        self.assertIn("wave height: latest 10.8 ft, low 7.9 ft, high 10.8 ft", result["message"])
        self.assertIn("wind speed: latest 35.8 mph, low 26.8 mph, high 35.8 mph",
                      result["message"])
        self.assertEqual(result["artifact"]["count"], 3)

    def test_near_lists_stations_with_sensors_and_distance(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoys-near", "params": {"place": "miami", "radius_miles": 700}, "missing": []})
        self.assertIn("2 reporting NDBC station(s) within 700 miles of Miami", result["message"])
        self.assertIn("SANF1 — Sand Key", result["message"])
        self.assertIn("measures weather, currents, DART tsunami", result["message"])
        self.assertLess(result["message"].index("SANF1"), result["message"].index("42001"))

    def test_near_for_a_place_with_nothing_close(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoys-near", "params": {"point": "50.0,60.0", "radius_miles": 50},
             "missing": []})
        self.assertIn("No reporting NDBC station sits within 50 miles", result["message"])

    def test_list_searches_the_catalogue(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoys-list", "params": {"query": "Diamond"}, "missing": []})
        self.assertIn("1 station(s) match", result["message"])
        self.assertIn("41025 — Diamond Shoals", result["message"])
        self.assertIn("reporting", result["message"])

    def test_list_shows_stations_that_are_not_reporting(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoys-list", "params": {"owner": "Oceanographic"}, "missing": []})
        self.assertIn("44099 — Silent One", result["message"])
        self.assertIn("not reporting", result["message"])

    def test_list_without_a_search_asks_for_one(self):
        result = BuoyAgent(FakeNdbc()).run({"skill": "buoys-list", "params": {}, "missing": []})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Moored Buoy", result["message"])

    def test_watch_records_the_crossing(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-watch", "params": {"station": "41025", "field": "wave_height",
                                                "threshold": 8}, "missing": []})
        watch = result["watch"]
        self.assertEqual(watch["kind"], "buoy-watch")
        self.assertEqual(watch["station"], "41025")
        self.assertEqual(watch["field"], "wave_height")
        self.assertEqual(watch["threshold"], 8.0)
        self.assertEqual(watch["observed"], {"above": True})
        self.assertNotIn("observed", result["artifact"]["watching"])
        self.assertIn("Point a pushNotificationConfig", result["message"])
        self.assertIn("at or above your threshold", result["message"])

    def test_watch_defaults_to_an_eight_foot_wave(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-watch", "params": {"station": "SANF1"}, "missing": []})
        self.assertEqual(result["watch"]["field"], "wave_height")
        self.assertEqual(result["watch"]["threshold"], 8.0)
        self.assertEqual(result["watch"]["observed"], {"above": False})

    def test_watch_in_metric_uses_metric_defaults(self):
        result = BuoyAgent(FakeNdbc(units="metric")).run(
            {"skill": "buoy-watch", "params": {"station": "SANF1"}, "missing": []})
        self.assertEqual(result["watch"]["threshold"], 2.5)
        self.assertEqual(result["watch"]["units"], "metric")
        self.assertIn("m or more", result["message"])

    def test_watch_on_a_field_the_station_does_not_publish_is_refused(self):
        # A 41025 row with wind but no sea state: the wave watch has nothing to compare.
        wind_only = LATEST_HEADER + obs_row("41025", "35.025", "-75.380", stamp(), "20", "16.0",
                                            "19.0")
        result = BuoyAgent(FakeNdbc(latest=wind_only)).run(
            {"skill": "buoy-watch", "params": {"station": "41025", "field": "wave_height",
                                                "threshold": 8}, "missing": []})
        self.assertIsNone(result["watch"])
        self.assertIn("I cannot watch that", result["message"])
        self.assertIn("not publishing wave height", result["message"])

    def test_watch_on_a_field_no_station_can_watch_is_rejected(self):
        # check_field raises, and the protocol turns that into a failed task rather than a lie.
        store = TaskStore(":memory:")
        try:
            handler = A2AHandler(store, BuoyAgent(FakeNdbc()))
            message = {"kind": "message", "role": "user", "messageId": "m", "parts": [
                {"kind": "data",
                 "data": {"skill": "buoy-watch", "station": "41025", "field": "visibility"}}]}
            task = handler.handle("message/send", {"message": message})
            self.assertEqual(task["status"]["state"], "failed")
            self.assertIn("field must be one of", task["status"]["message"]["parts"][0]["text"])
        finally:
            store.close()

    def test_watch_on_a_station_with_no_reading_is_refused(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-watch", "params": {"station": "44099"}, "missing": []})
        self.assertIsNone(result["watch"])
        self.assertIn("has no current observation", result["message"])
        self.assertIn("cannot watch", result["message"])

    def test_missing_input_prompts(self):
        result = BuoyAgent(FakeNdbc()).run(
            {"skill": "buoy-conditions", "params": {}, "missing": ["location"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which station", result["message"])

    def test_age_minutes_handles_junk(self):
        self.assertIsNone(age_minutes(None))
        self.assertIsNone(age_minutes("yesterday"))
        self.assertGreaterEqual(age_minutes("2026-09-23T23:20Z"), 0)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_artifact(self):
        handler = A2AHandler(self.store, BuoyAgent(FakeNdbc()))
        task = handler.handle("message/send", {"message": _message("conditions at buoy 41025")})
        self.assertEqual(task["status"]["state"], "completed")
        data = task["artifacts"][-1]["parts"][0]["data"]
        self.assertEqual(data["dataset"], DATASET)
        self.assertEqual(data["observation"]["wind_speed"], 35.8)

    def test_input_required_then_follow_up(self):
        handler = A2AHandler(self.store, BuoyAgent(FakeNdbc()))
        first = handler.handle("message/send", {"message": _message("how are the conditions?")})
        self.assertEqual(first["status"]["state"], "input-required")
        follow = handler.handle(
            "message/send",
            {"message": _message("41025", taskId=first["id"], contextId=first["contextId"])},
        )
        self.assertEqual(follow["status"]["state"], "completed")
        self.assertEqual(follow["artifacts"][-1]["parts"][0]["data"]["station"], "41025")


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

    @staticmethod
    def _latest(wave_m: str) -> str:
        return LATEST_HEADER + obs_row("SANF1", "24.455", "-81.877", stamp(0.2), "70", "4.0", "5.0",
                                       wave_m, "9", "6.1", "80", "1014.2", "0.4", "28.9", "29.7", "25.6")

    def test_watcher_fires_on_a_crossing_and_stays_quiet_inside_it(self):
        client = FakeNdbc(latest=self._latest("0.5"))
        agent = BuoyAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when the waves at SANF1 pass 8 feet")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook",
                                                "token": "t"})

        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)  # baseline
        self.assertEqual(watcher.tick(), 0)

        # A bigger sea inside the same state is not news...
        client.latest_text = self._latest("1.5")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 0)

        # ...but crossing the line is.
        client.latest_text = self._latest("3.1")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("at or above your threshold of 8 ft", self._posted_text(posted[0]))
        self.assertEqual(watcher.tick(), 0)

        # And easing back below it is news too.
        client.latest_text = self._latest("1.0")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 1)
        self.assertIn("dropped back below your threshold of 8 ft", self._posted_text(posted[1]))

    def test_a_sensor_dropout_does_not_page_anyone(self):
        client = FakeNdbc(latest=self._latest("3.1"))
        agent = BuoyAgent(client)
        task = A2AHandler(self.store, agent).handle(
            "message/send", {"message": _message("tell me when the waves at SANF1 pass 8 feet")})
        self.store.set_push_config(task["id"], {"id": "cfg", "url": "https://example.com/hook",
                                                "token": "t"})
        posted = []
        watcher = PushWatcher(self.store, agent, interval=5,
                              http_post=lambda url, payload, headers: posted.append(payload) or 200)
        self.assertEqual(watcher.tick(), 0)
        client.latest_text = self._latest("MM")
        client._cache.clear()
        self.assertEqual(watcher.tick(), 0)
        self.assertEqual(posted, [])

    def test_describe_watch_change_covers_both_directions(self):
        agent = BuoyAgent(FakeNdbc())
        watch = {"field": "wave_height", "station": "41025", "label": "41025", "threshold": 8.0}
        self.assertIn("Wave height at 41025 is now at or above your threshold of 8 ft",
                      agent.describe_watch_change(watch, {"above": False}, {"above": True}))
        self.assertIn("dropped back below your threshold of 8 ft",
                      agent.describe_watch_change(watch, {"above": True}, {"above": False}))
        self.assertIn("crossed your threshold", agent.describe_watch_change(watch, None, None))


if __name__ == "__main__":
    unittest.main()
