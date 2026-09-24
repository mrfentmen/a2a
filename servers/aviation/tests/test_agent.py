"""Tests for the aviation weather server.

    python3 servers/aviation/tests/test_agent.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import (  # noqa: E402
    AviationAgent,
    SKILL_METAR,
    SKILL_NEARBY,
    SKILL_TAF,
    SKILL_WATCH,
    code_from_text,
    codes_for_place,
    count_from_text,
    parse,
    place_from_text,
    point_from_text,
    radius_from_text,
    run_metar,
    run_nearby,
    run_taf,
    run_watch,
)
from data import (  # noqa: E402
    DATASET,
    FLIGHT_CATEGORIES,
    MAX_AIRPORTS,
    MAX_HOURS,
    MAX_LISTED,
    MAX_RADIUS_MILES,
    METAR_PATH,
    TAF_PATH,
    AviationWeatherClient,
    ceiling_ft,
    cloud_layers,
    describe_clouds,
    number_or_none,
    to_iso_utc,
    visibility_miles,
    wind_phrase,
)

#: The live KDEN observation from 2026-09-24 05:00 UTC, plus a low-weather neighbour and a
#: station with the optional decoded fields missing, so both shapes are covered.
METAR_ROWS = [
    {
        "icaoId": "KDEN", "receiptTime": "2026-09-24T04:56:37.353Z", "obsTime": 1790225580,
        "reportTime": "2026-09-24T05:00:00.000Z", "temp": 16.1, "dewp": 11.7, "wdir": 200,
        "wspd": 3, "visib": "10+", "altim": 1024.5, "slp": 1018.2, "qcField": 4,
        "metarType": "METAR",
        "rawOb": "METAR KDEN 240453Z 20003KT 10SM FEW080 BKN150 OVC200 16/12 A3025 RMK AO2 SLP182",
        "lat": 39.8466, "lon": -104.6562, "elev": 1656, "name": "Denver Intl, CO, US", "cover": "OVC",
        "clouds": [{"cover": "FEW", "base": 8000}, {"cover": "BKN", "base": 15000},
                   {"cover": "OVC", "base": 20000}],
        "fltCat": "VFR",
    },
    {
        "icaoId": "KBJC", "reportTime": "2026-09-24T04:55:00.000Z", "temp": 14.0, "dewp": 13.0,
        "wdir": "VRB", "wspd": 5, "wgst": 18, "visib": 2.0, "altim": 1023.0,
        "rawOb": "METAR KBJC 240455Z VRB05G18KT 2SM BR OVC004 14/13 A3020",
        "lat": 39.9088, "lon": -105.1172, "elev": 1729, "name": "Broomfield, CO, US",
        "clouds": [{"cover": "OVC", "base": 400}], "fltCat": "IFR",
    },
    {
        "icaoId": "KAPA", "reportTime": "2026-09-24T04:53:00.000Z", "wspd": 0,
        "rawOb": "METAR KAPA 240453Z 00000KT CLR 18/02 A3022",
        "lat": 39.5701, "lon": -104.8493, "name": "Denver, CO, US",
        "clouds": [], "fltCat": "VFR",
    },
]

TAF_ROWS = [
    {
        "icaoId": "KDEN", "issueTime": "2026-09-24T03:39:00.000Z", "bulletinTime": "2026-09-24T03:39:00.000Z",
        "validTimeFrom": 1790222400, "validTimeTo": 1790316000, "mostRecent": 1, "remarks": " AMD",
        "lat": 39.84657, "lon": -104.65623, "elev": 1656, "name": "Denver Intl",
        "rawTAF": "TAF KDEN 240339Z 2404/2506 26007KT P6SM SCT050 BKN110 OVC200",
        "fcsts": [
            {"timeFrom": 1790222400, "timeTo": 1790226000, "wdir": 260, "wspd": 7, "visib": "6+",
             "clouds": [{"cover": "SCT", "base": 5000}, {"cover": "BKN", "base": 11000},
                        {"cover": "OVC", "base": 20000}]},
            {"timeFrom": 1790226000, "timeTo": 1790233200, "fcstChange": "FM", "wdir": 300, "wspd": 12,
             "wgst": 22, "visib": 3.0, "wxString": "-SHRA",
             "clouds": [{"cover": "BKN", "base": 900}, {"cover": "OVC", "base": 2500}]},
            {"timeFrom": 1790233200, "timeTo": 1790236800, "probability": 30, "visib": 1.0,
             "vertVis": 300, "clouds": [{"cover": "VV", "base": None}]},
        ],
    },
]


class FakeAviation(AviationWeatherClient):
    """An AviationWeatherClient whose transport is canned, so data.py runs for real."""

    def __init__(self, metar=None, taf=None, calls=None) -> None:
        self.metar = METAR_ROWS if metar is None else metar
        self.taf = TAF_ROWS if taf is None else taf
        self.calls = calls if calls is not None else []
        # The kit stores the transport on the instance, so injection is through `fetch=`.
        super().__init__(fetch=self.transport, cache_ttl=0.0)

    def transport(self, url, params, headers):
        self.calls.append({"url": url, "params": dict(params)})
        source = self.taf if str(url).endswith(TAF_PATH) else self.metar
        wanted = [code.strip().upper() for code in str(params.get("ids") or "").split(",") if code.strip()]
        return [row for row in source if not wanted or row["icaoId"] in wanted]


class HelperTests(unittest.TestCase):
    def test_times_accept_epoch_seconds_and_iso_strings(self):
        self.assertEqual(to_iso_utc(1790225580), "2026-09-24T04:53:00Z")
        self.assertEqual(to_iso_utc("2026-09-24T05:00:00.000Z"), "2026-09-24T05:00:00Z")
        self.assertEqual(to_iso_utc("2026-09-24T05:00:00+02:00"), "2026-09-24T03:00:00Z")
        self.assertIsNone(to_iso_utc(None))
        self.assertIsNone(to_iso_utc("soon"))

    def test_a_missing_number_stays_missing(self):
        self.assertEqual(number_or_none("16.1"), 16.1)
        self.assertIsNone(number_or_none(""))
        self.assertIsNone(number_or_none(None))
        self.assertIsNone(number_or_none("///"))

    def test_visibility_keeps_its_cap_and_stays_a_number(self):
        self.assertEqual(visibility_miles("10+"), (10.0, True))
        self.assertEqual(visibility_miles(2.0), (2.0, False))
        self.assertEqual(visibility_miles("6+"), (6.0, True))
        self.assertEqual(visibility_miles(None), (None, False))
        self.assertEqual(visibility_miles(""), (None, False))

    def test_cloud_layers_and_ceiling(self):
        layers = cloud_layers([{"cover": "FEW", "base": 8000}, {"cover": "OVC", "base": 20000},
                               {"cover": "BKN", "base": 15000}])
        self.assertEqual([layer["cover"] for layer in layers], ["FEW", "OVC", "BKN"])
        self.assertEqual(layers[0]["cover_text"], "few")
        self.assertEqual(ceiling_ft(layers), 15000)
        self.assertIsNone(ceiling_ft(cloud_layers([{"cover": "SCT", "base": 5000}])))
        self.assertEqual(ceiling_ft(cloud_layers([{"cover": "VV", "base": 300}])), 300)
        self.assertEqual(cloud_layers(None), [])

    def test_describe_clouds(self):
        self.assertEqual(describe_clouds(cloud_layers([{"cover": "BKN", "base": 15000}])),
                         "broken at 15,000 ft")
        self.assertEqual(describe_clouds([]), "no cloud layers reported")

    def test_wind_phrase_covers_calm_variable_and_gusts(self):
        self.assertEqual(wind_phrase(200, 3, None), "200° at 3 kt")
        self.assertEqual(wind_phrase(0, 0, None), "calm")
        self.assertEqual(wind_phrase("VRB", 5, 18), "variable at 5 kt gusting 18 kt")
        self.assertEqual(wind_phrase(None, 10, None), "direction not reported at 10 kt")
        self.assertEqual(wind_phrase(90, None, None), "wind not reported")


class ClientTests(unittest.TestCase):
    def client(self, **kwargs) -> FakeAviation:
        return FakeAviation(**kwargs)

    def test_an_observation_carries_the_service_category_and_the_raw_report(self):
        read = self.client().observation("KDEN")
        self.assertTrue(read["found"])
        self.assertEqual(read["dataset"], DATASET)
        self.assertEqual(read["city"], "Denver")
        row = read["observation"]
        self.assertEqual(row["flight_category"], "VFR")
        self.assertEqual(row["temperature_c"], 16.1)
        self.assertEqual(row["dewpoint_c"], 11.7)
        self.assertEqual(row["wind"]["phrase"], "200° at 3 kt")
        self.assertEqual(row["visibility_miles"], 10.0)
        self.assertTrue(row["visibility_capped"])
        self.assertEqual(row["ceiling_ft"], 15000)
        self.assertEqual(row["clouds_text"], "few at 8,000 ft, broken at 15,000 ft, overcast at 20,000 ft")
        self.assertEqual(row["altimeter_hpa"], 1024.5)
        self.assertEqual(row["report_time"], "2026-09-24T05:00:00Z")
        self.assertIn("METAR KDEN", row["raw"])

    def test_an_observation_with_the_optional_fields_missing_stays_missing(self):
        row = self.client().observation("KAPA")["observation"]
        self.assertEqual(row["wind"]["phrase"], "calm")
        self.assertIsNone(row["temperature_c"])
        self.assertIsNone(row["dewpoint_c"])
        self.assertIsNone(row["visibility_miles"])
        self.assertIsNone(row["altimeter_hpa"])
        self.assertIsNone(row["ceiling_ft"])
        self.assertEqual(row["clouds"], [])
        self.assertEqual(row["flight_category"], "VFR")

    def test_a_station_with_no_report_is_found_false_not_an_empty_reading(self):
        read = self.client().observation("ZZZZ")
        self.assertFalse(read["found"])
        self.assertIsNone(read["observation"])
        self.assertEqual(read["station"], "ZZZZ")

    def test_a_204_style_empty_payload_reads_as_no_rows(self):
        client = FakeAviation(metar=[])
        self.assertEqual(client.observations(["KDEN"]), [])
        self.assertFalse(client.observation("KDEN")["found"])

    def test_several_stations_are_one_request(self):
        calls: list = []
        client = FakeAviation(calls=calls)
        rows = client.observations(["KDEN", "KBJC"])
        self.assertEqual([row["station"] for row in rows], ["KDEN", "KBJC"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"]["ids"], "KDEN,KBJC")
        self.assertEqual(calls[0]["params"]["format"], "json")

    def test_a_forecast_keeps_the_service_period_order(self):
        read = self.client().forecast("KDEN")
        self.assertTrue(read["found"])
        row = read["forecast"]
        self.assertEqual(read["city"], "Denver")
        self.assertEqual(row["period_count"], 3)
        self.assertTrue(row["amended"])
        self.assertEqual(row["issued_at"], "2026-09-24T03:39:00Z")
        self.assertEqual(row["valid_from"], "2026-09-24T04:00:00Z")
        self.assertEqual(row["valid_to"], "2026-09-25T06:00:00Z")
        first, second, third = row["periods"]
        self.assertEqual(first["wind"]["phrase"], "260° at 7 kt")
        self.assertEqual(second["change"], "FM")
        self.assertEqual(second["wind"]["phrase"], "300° at 12 kt gusting 22 kt")
        self.assertEqual(second["ceiling_ft"], 900)
        self.assertEqual(second["weather"], "-SHRA")
        self.assertEqual(third["probability"], 30)
        self.assertEqual(third["vertical_visibility_ft"], 300)
        self.assertIn("TAF KDEN", row["raw"])

    def test_a_forecast_for_a_station_with_no_taf_is_found_false(self):
        read = self.client().forecast("ZZZZ")
        self.assertFalse(read["found"])
        self.assertIsNone(read["forecast"])

    def test_nearby_asks_for_a_bbox_once_and_sorts_by_distance(self):
        calls: list = []
        read = FakeAviation(calls=calls).nearby(39.74, -104.99, radius_miles=60)
        self.assertEqual(len(calls), 1)
        self.assertIn("bbox", calls[0]["params"])
        box = [float(part) for part in calls[0]["params"]["bbox"].split(",")]
        self.assertLess(box[0], box[2])  # south < north
        self.assertLess(box[1], box[3])  # west < east
        self.assertLess(box[0], 39.74)  # the point is inside the box it asked for
        self.assertGreater(box[2], 39.74)
        self.assertLess(box[1], -104.99)
        self.assertGreater(box[3], -104.99)
        self.assertEqual([row["station"] for row in read["stations"]], ["KBJC", "KAPA", "KDEN"])
        self.assertEqual([row["miles"] for row in read["stations"]],
                         sorted(row["miles"] for row in read["stations"]))
        self.assertEqual(read["found"], 3)
        self.assertEqual(read["in_box"], 3)

    def test_nearby_filters_by_radius_honestly(self):
        client = FakeAviation()
        everything = client.nearby(39.74, -104.99, radius_miles=60)
        wanted = [row["station"] for row in everything["stations"] if row["miles"] <= 15]
        self.assertTrue(wanted)  # the fixture has stations inside 15 miles
        read = client.nearby(39.74, -104.99, radius_miles=15)
        self.assertEqual([row["station"] for row in read["stations"]], wanted)
        self.assertEqual(read["in_box"], len(everything["stations"]))  # the box is unchanged
        self.assertLess(read["found"], read["in_box"])  # the radius kept fewer than the box held

    def test_nearby_refuses_a_point_off_the_planet(self):
        with self.assertRaises(ValueError):
            FakeAviation().nearby(91, -104.99)

    def test_watch_state_keeps_only_the_reported_category(self):
        observed = FakeAviation().watch_state("KDEN")
        self.assertEqual(list(observed["stations"]), ["KDEN"])
        entry = observed["stations"]["KDEN"]
        self.assertEqual(entry["flight_category"], "VFR")
        self.assertEqual(entry["report_time"], "2026-09-24T05:00:00Z")
        self.assertIn("METAR KDEN", entry["raw"])
        self.assertNotIn("temperature", entry)

    def test_watch_state_for_a_silent_station_is_empty(self):
        self.assertEqual(FakeAviation().watch_state("ZZZZ"), {"stations": {}})

    def test_validators_reject_what_the_service_would_reject(self):
        client = AviationWeatherClient()
        self.assertEqual(client.check_airport("kden"), "KDEN")
        self.assertEqual(client.check_airports("KDEN, KBJC"), ["KDEN", "KBJC"])
        self.assertEqual(client.check_airports(["KDEN"]), ["KDEN"])
        self.assertEqual(client.check_airports(""), [])
        for bad in ("", "K", "KDENCO"):
            with self.assertRaises(ValueError):
                client.check_airport(bad)
        with self.assertRaises(ValueError):
            client.check_airports([f"K{i:03d}" for i in range(MAX_AIRPORTS + 1)])
        self.assertEqual(client.check_radius(str(MAX_RADIUS_MILES)), MAX_RADIUS_MILES)
        self.assertEqual(client.check_hours(str(MAX_HOURS)), MAX_HOURS)
        self.assertEqual(client.check_positive(str(MAX_LISTED)), MAX_LISTED)
        for bad in ("0", str(MAX_RADIUS_MILES + 1), "far"):
            with self.assertRaises(ValueError):
                client.check_radius(bad)
        for bad in ("0", str(MAX_HOURS + 1), "later"):
            with self.assertRaises(ValueError):
                client.check_hours(bad)
        for bad in ("0", str(MAX_LISTED + 1), "many"):
            with self.assertRaises(ValueError):
                client.check_positive(bad)
        self.assertEqual(client.check_point("39.74, -104.99"), (39.74, -104.99))
        self.assertEqual(client.check_point("51.51,-0.13"), (51.51, -0.13))
        with self.assertRaises(ValueError):
            client.check_point("39.74")

    def test_the_shipped_tables_are_reference_geography_only(self):
        self.assertEqual(AviationWeatherClient.airport_city("KDEN"), ("Denver", "CO"))
        self.assertEqual(AviationWeatherClient.airport_city("DEN"), ("Denver", "CO"))
        self.assertIsNone(AviationWeatherClient.airport_city("ZZZZ"))
        self.assertEqual(AviationWeatherClient.resolve_code("DEN"), "KDEN")
        self.assertEqual(AviationWeatherClient.resolve_code("KDEN"), "KDEN")
        self.assertEqual(AviationWeatherClient.resolve_code("EGLL"), "EGLL")
        self.assertEqual(AviationWeatherClient.code_for_place("Chicago"), ["KMDW", "KORD"])
        self.assertEqual(AviationWeatherClient.code_for_place("KDEN"), ["KDEN"])
        self.assertEqual(AviationWeatherClient.code_for_place(""), [])
        self.assertEqual(AviationWeatherClient.city("Denver")[2], "Denver")
        self.assertEqual(AviationWeatherClient.city("denver")[1], -104.99)
        self.assertIsNone(AviationWeatherClient.city("Atlantis"))

    def test_distance_is_great_circle_miles(self):
        self.assertEqual(AviationWeatherClient.distance_miles(39.74, -104.99, 39.74, -104.99), 0.0)
        miles = AviationWeatherClient.distance_miles(39.74, -104.99, 39.8466, -104.6562)
        self.assertGreater(miles, 18)
        self.assertLess(miles, 24)
        self.assertEqual(FLIGHT_CATEGORIES, ("VFR", "MVFR", "IFR", "LIFR"))


class ParseTests(unittest.TestCase):
    def message(self, text: str, **data) -> dict:
        parts: list[dict] = [{"kind": "text", "text": text}]
        if data:
            parts.append({"kind": "data", "data": data})
        return {"kind": "message", "role": "user", "messageId": "m", "parts": parts}

    def test_a_code_reading(self):
        result = parse(self.message("what is the weather at KDEN right now?"))
        self.assertEqual(result["skill"], SKILL_METAR)
        self.assertEqual(result["params"]["airports"], ["KDEN"])
        self.assertTrue(result["explicit"])

    def test_a_city_resolves_to_its_airports(self):
        result = parse(self.message("what is the weather in Chicago?"))
        self.assertEqual(result["skill"], SKILL_METAR)
        self.assertEqual(result["params"]["airports"], ["KMDW", "KORD"])
        self.assertEqual(result["params"]["place"], "Chicago")

    def test_a_place_with_a_city_code_is_still_a_station_reading(self):
        result = parse(self.message("what are the conditions at KDEN?"))
        self.assertEqual(result["skill"], SKILL_METAR)
        self.assertEqual(result["params"]["airports"], ["KDEN"])

    def test_forecast_words_with_a_station_ask_for_the_taf(self):
        result = parse(self.message("what is the TAF for KJFK?"))
        self.assertEqual(result["skill"], SKILL_TAF)
        self.assertEqual(result["params"]["airports"], ["KJFK"])

    def test_the_word_taf_in_json_asks_for_the_taf_too(self):
        result = parse(self.message("forecast please", skill=SKILL_TAF, airports=["KDEN"]))
        self.assertEqual(result["skill"], SKILL_TAF)
        self.assertEqual(result["params"]["airports"], ["KDEN"])

    def test_nearby_by_place(self):
        result = parse(self.message("which airports are reporting near Denver?"))
        self.assertEqual(result["skill"], SKILL_NEARBY)
        self.assertEqual(result["params"]["place"], "Denver")

    def test_nearby_by_point_with_a_radius(self):
        result = parse(self.message("stations within 60 miles of 39.74,-104.99"))
        self.assertEqual(result["skill"], SKILL_NEARBY)
        self.assertEqual(result["params"]["point"], "39.74,-104.99")
        self.assertEqual(result["params"]["radius"], 60.0)

    def test_a_point_that_starts_with_zero_longitude_is_kept(self):
        result = parse(self.message("any stations near 51.51,-0.13?"))
        self.assertEqual(result["skill"], SKILL_NEARBY)
        self.assertEqual(result["params"]["point"], "51.51,-0.13")

    def test_a_count_is_carried_through(self):
        result = parse(self.message("show me 3 stations near Denver"))
        self.assertEqual(result["params"]["limit"], 3)

    def test_watch_words_beat_everything_else(self):
        result = parse(self.message("tell me when KDEN goes IFR"))
        self.assertEqual(result["skill"], SKILL_WATCH)
        self.assertEqual(result["params"]["airports"], ["KDEN"])

    def test_a_bare_faa_code_in_the_data_is_resolved_to_icao(self):
        result = parse(self.message("anything", skill=SKILL_METAR, airports=["DEN"]))
        self.assertEqual(result["params"]["airports"], ["KDEN"])

    def test_an_explicit_skill_in_the_data_wins(self):
        result = parse(self.message("anything", skill=SKILL_NEARBY, place="Denver", radius_miles=80))
        self.assertEqual(result["skill"], SKILL_NEARBY)
        self.assertEqual(result["params"]["radius"], 80)
        self.assertEqual(result["params"]["place"], "Denver")

    def test_a_forecast_word_with_no_station_asks_the_forecast_skill(self):
        result = parse(self.message("what is the forecast?"))
        self.assertEqual(result["skill"], SKILL_TAF)

    def test_an_empty_message_is_not_explicit(self):
        result = parse(self.message(""))
        self.assertFalse(result["explicit"])

    def test_code_from_text_reads_station_codes_and_not_english(self):
        self.assertEqual(code_from_text("weather at KDEN"), "KDEN")
        self.assertEqual(code_from_text("how is KJFK?"), "KJFK")
        self.assertEqual(code_from_text("how is DEN?"), "KDEN")  # the FAA spelling is resolved
        self.assertIsNone(code_from_text("what is the weather like today?"))
        self.assertIsNone(code_from_text("METAR please"))

    def test_place_point_radius_and_count_helpers(self):
        self.assertEqual(place_from_text("near Denver"), "Denver")
        self.assertEqual(place_from_text("near New York"), "New York")
        self.assertIsNone(place_from_text("near here"))
        self.assertEqual(point_from_text("at 39.74,-104.99"), "39.74,-104.99")
        self.assertEqual(point_from_text("at 51.51,-0.13"), "51.51,-0.13")
        self.assertIsNone(point_from_text("at 1,000 ft"))
        self.assertEqual(radius_from_text("within 60 miles"), 60.0)
        self.assertIsNone(radius_from_text("near here"))
        self.assertEqual(count_from_text("the nearest 4 stations"), 4)
        self.assertIsNone(count_from_text("near Denver"))
        self.assertEqual(codes_for_place("Chicago"), ["KMDW", "KORD"])


class RunTests(unittest.TestCase):
    def agent(self, **kwargs) -> AviationAgent:
        return AviationAgent(client=FakeAviation(**kwargs))

    def test_metar_answer_is_decoded_with_the_raw_report(self):
        result = run_metar({"airports": ["KDEN"]}, self.agent().client)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("KDEN (Denver) — current observation:", result["message"])
        self.assertIn("flight category VFR", result["message"])
        self.assertIn("Wind 200° at 3 kt.", result["message"])
        self.assertIn("Visibility 10+ mi, ceiling 15,000 ft.", result["message"])
        self.assertIn("Temperature 16.1 °C, dewpoint 11.7 °C.", result["message"])
        self.assertIn("Altimeter 1024.5 hPa.", result["message"])
        self.assertIn("Raw report: METAR KDEN", result["message"])
        self.assertIn("not the whole region around it", result["message"])
        self.assertEqual(result["artifact"]["count"], 1)
        self.assertEqual(result["artifact"]["endpoint"], METAR_PATH)
        self.assertEqual(result["artifact"]["dataset"], DATASET)
        self.assertIsNone(result["watch"])

    def test_metar_for_a_station_with_no_report_says_so(self):
        result = run_metar({"airports": ["ZZZZ"]}, self.agent().client)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("No observation is published for ZZZZ right now", result["message"])
        self.assertIn("empty response, not an error", result["message"])
        self.assertEqual(result["artifact"]["count"], 0)

    def test_metar_without_a_station_asks_for_one(self):
        result = run_metar({}, self.agent().client)
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("Which station?", result["message"])

    def test_several_stations_are_each_reported(self):
        result = run_metar({"airports": ["KDEN", "KBJC"]}, self.agent().client)
        self.assertIn("KBJC (Broomfield)", result["message"])
        self.assertIn("flight category IFR", result["message"])
        self.assertIn("Wind variable at 5 kt gusting 18 kt.", result["message"])
        self.assertIn("Visibility 2 mi, ceiling 400 ft.", result["message"])
        self.assertEqual(result["artifact"]["count"], 2)

    def test_taf_answer_lists_every_period_and_the_raw_text(self):
        result = run_taf({"airports": ["KDEN"]}, self.agent().client)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("KDEN (Denver) — TAF issued 2026-09-24T03:39:00Z", result["message"])
        self.assertIn("from 2026-09-24T04:00:00Z", result["message"])
        self.assertIn("to 2026-09-25T06:00:00Z", result["message"])
        self.assertIn("amended", result["message"])
        self.assertIn("300° at 12 kt gusting 22 kt", result["message"])
        self.assertIn("-SHRA", result["message"])
        self.assertIn("30% probability", result["message"])
        self.assertIn("vertical visibility 300 ft", result["message"])
        self.assertIn("Raw TAF: TAF KDEN", result["message"])
        self.assertIn("not merged", result["message"])
        self.assertEqual(result["artifact"]["endpoint"], TAF_PATH)

    def test_taf_for_a_station_with_none_says_so(self):
        result = run_taf({"airports": ["ZZZZ"]}, self.agent().client)
        self.assertIn("No TAF is published for ZZZZ right now", result["message"])

    def test_nearby_names_each_station_with_miles_and_conditions(self):
        result = run_nearby({"place": "Denver"}, self.agent().client)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("3 reporting station(s) within 40 miles of Denver", result["message"])
        self.assertIn("KAPA (Denver) — VFR", result["message"])
        self.assertIn("KBJC (Broomfield) — IFR", result["message"])
        self.assertIn("mi — ", result["message"])
        self.assertIn("straight-line miles computed here", result["message"])
        self.assertIn("only returns stations that reported recently", result["message"])
        self.assertEqual(result["artifact"]["found"], 3)

    def test_nearby_accepts_a_point_and_a_radius(self):
        result = run_nearby({"point": "39.74,-104.99", "radius": 60}, self.agent().client)
        self.assertEqual(result["artifact"]["radius_miles"], 60.0)
        self.assertIn("the point 39.74,-104.99", result["message"])

    def test_nearby_with_nothing_in_the_radius_is_honest(self):
        result = run_nearby({"place": "Denver", "radius": 5}, self.agent().client)
        self.assertIn("No reporting station is within 5 miles of Denver", result["message"])
        self.assertIn("Widen the radius", result["message"])
        self.assertEqual(result["artifact"]["stations"], [])

    def test_nearby_refuses_a_place_it_does_not_know(self):
        result = run_nearby({"place": "Atlantis"}, self.agent().client)
        self.assertIn("I do not know the place 'Atlantis'", result["message"])
        self.assertFalse(result["artifact"]["known"])

    def test_nearby_without_a_place_asks_for_one(self):
        result = run_nearby({}, self.agent().client)
        self.assertEqual(result["final_state"], "input-required")

    def test_watch_records_the_station_and_its_category(self):
        result = run_watch({"airports": ["KDEN"]}, self.agent().client)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn("Watching KDEN (Denver) — flight category VFR", result["message"])
        self.assertIn("only the category is compared", result["message"])
        watch = result["watch"]
        self.assertEqual(watch["kind"], SKILL_WATCH)
        self.assertEqual(watch["station"], "KDEN")
        self.assertEqual(watch["observed"]["stations"]["KDEN"]["flight_category"], "VFR")

    def test_watch_for_a_silent_station_does_not_open_a_watch(self):
        result = run_watch({"airports": ["ZZZZ"]}, self.agent().client)
        self.assertIsNone(result["watch"])
        self.assertIn("no flight category to watch yet", result["message"])

    def test_watch_refuses_more_than_one_station(self):
        result = run_watch({"airports": ["KDEN", "KBJC"]}, self.agent().client)
        self.assertIsNone(result["watch"])
        self.assertIn("A watch covers one station", result["message"])

    def test_watch_without_a_station_asks_for_one(self):
        result = run_watch({}, self.agent().client)
        self.assertEqual(result["final_state"], "input-required")

    def test_the_agent_routes_and_fills_in_missing_input(self):
        agent = self.agent()
        first = agent.run({"skill": SKILL_METAR, "params": {"hours": 2}, "missing": ["airport"]})
        self.assertEqual(first["final_state"], "input-required")
        self.assertIn("Which station?", first["message"])
        second = agent.run({"skill": SKILL_NEARBY, "params": {}, "missing": ["place"]})
        self.assertIn("Which place?", second["message"])
        third = agent.run({"skill": SKILL_METAR, "params": {"airports": ["KDEN"], "hours": 2},
                           "missing": []})
        self.assertEqual(third["final_state"], "completed")
        self.assertEqual(third["artifact"]["stations"], ["KDEN"])

    def test_the_agent_declares_its_card(self):
        agent = self.agent()
        self.assertEqual(agent.name, "aviation")
        self.assertEqual(agent.card_name, "Aviation Weather Agent")
        self.assertEqual(agent.env_prefix, "AVIATION")
        self.assertEqual(agent.watch_kinds, (SKILL_WATCH,))
        self.assertEqual([skill["id"] for skill in agent.card_skills],
                         [SKILL_METAR, SKILL_TAF, SKILL_NEARBY, SKILL_WATCH])

    def test_the_watch_probe_and_change_text(self):
        agent = self.agent()
        watch = {"kind": SKILL_WATCH, "station": "KDEN", "observed": agent.client.watch_state("KDEN")}
        probed = agent.probe_watch(watch)
        self.assertEqual(probed["stations"]["KDEN"]["flight_category"], "VFR")
        self.assertIsNone(agent.probe_watch({"kind": "something-else"}))
        self.assertIsNone(agent.probe_watch({"kind": SKILL_WATCH}))
        worsened = agent.describe_watch_change(watch, watch["observed"],
                                              {"stations": {"KDEN": {"flight_category": "IFR"}}})
        self.assertEqual(worsened, "KDEN (Denver) flight category worsened: VFR to IFR")
        improved = agent.describe_watch_change(watch, {"stations": {"KDEN": {"flight_category": "LIFR"}}},
                                               {"stations": {"KDEN": {"flight_category": "MVFR"}}})
        self.assertEqual(improved, "KDEN (Denver) flight category improved: LIFR to MVFR")
        same = agent.describe_watch_change(watch, watch["observed"], watch["observed"])
        self.assertEqual(same, "KDEN (Denver) reported VFR")
        gone = agent.describe_watch_change(watch, watch["observed"], {"stations": {}})
        self.assertEqual(gone, "the reported flight category at KDEN (Denver) changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
