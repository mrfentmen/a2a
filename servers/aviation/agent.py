"""The aviation-weather agent: four skills over the Aviation Weather Center's public feeds.

  - metar:           an airport's current observation, decoded, with the raw report
  - taf:             an airport's forecast periods and the raw TAF text
  - aviation-nearby: the reporting stations within a radius of a place or point, nearest first
  - aviation-watch:  watch one airport's reported flight category and POST when it changes

Watch-capable: aviation-watch stores {"kind": "aviation-watch", station, observed} where observed
is {"stations": {code: {flight_category, report_time, raw}}}. Temperature, wind and visibility are
revised within a category all day long, so only the service's own flight category is compared.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    AIRPORTS,
    DATASET,
    DEFAULT_HOURS,
    DEFAULT_RADIUS_MILES,
    FLIGHT_CATEGORIES,
    MAX_LISTED,
    MAX_RADIUS_MILES,
    METAR_PATH,
    TAF_PATH,
    AviationWeatherClient,
)

SKILL_METAR = "metar"
SKILL_TAF = "taf"
SKILL_NEARBY = "aviation-nearby"
SKILL_WATCH = "aviation-watch"

#: Station codes are how the service writes them: uppercase, 3-5 characters.
_CODE_RE = re.compile(r"\b([A-Z]{3,5})\b")
_CODE_CONTEXT_RE = re.compile(r"\b(?:airport|station|field|code|at|for|into|near)\s+([A-Z]{3,5})\b")
_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_RADIUS_RE = re.compile(r"\b(\d{1,3})\s*(?:mi|mile|miles)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(?:top|nearest|closest|first|show(?: me)?)\s+(\d{1,2})\b"
                       r"|\b(\d{1,2})\s*(?:stations?|airports?)\b", re.IGNORECASE)
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe)\b",
                          re.IGNORECASE)
_TAF_WORDS = re.compile(r"\b(taf|forecast|outlook|next \d+ hours?|tomorrow|tonight|"
                        r"terminal aerodrome)\b", re.IGNORECASE)
_NEARBY_WORDS = re.compile(r"\b(near|nearby|around|close to|within|stations?|airports?|"
                           r"nearest|closest)\b", re.IGNORECASE)
_CONDITION_WORDS = re.compile(r"\b(metar|observation|observed|conditions?|weather|visibility|"
                              r"ceiling|wind|temperature|dewpoint|dew point|flight category|"
                              r"vfr|mvfr|ifr|lifr|clouds?|altimeter)\b", re.IGNORECASE)
#: Uppercase words that look like codes but are English or aviation vocabulary.
_NOT_CODES = frozenset({"THE", "AND", "NOT", "FOR", "ARE", "YOU", "ITS", "ALL", "NEW", "MAY",
                        "NOW", "ANY", "HOW", "WHY", "WHAT", "WHO", "VFR", "MVFR", "IFR", "LIFR",
                        "UTC", "GMT", "TAF", "METAR", "KT", "SM", "TODAY", "WEATHER"})


def message_text(message: dict) -> str:
    parts = message.get("parts") or []
    return " ".join(part.get("text", "") for part in parts if part.get("kind") == "text").strip()


def message_data(message: dict) -> dict:
    merged: dict = {}
    for part in message.get("parts") or []:
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            merged.update(part["data"])
    return merged


def _first(params: dict, *names, default=None):
    for name in names:
        if name in params and params[name] not in (None, ""):
            return params[name]
    return default


def code_from_text(text: str) -> str | None:
    """A station code, resolved to the ICAO spelling the service answers to (KDEN, KJFK).

    A code this server knows wins — either spelling, so DEN becomes KDEN — then an uppercase
    three-to-five letter token is taken as a code in a code-ish context, unless it is ordinary
    English or aviation vocabulary.
    """
    for match in _CODE_RE.finditer(text):
        candidate = match.group(1)
        resolved = AviationWeatherClient.resolve_code(candidate)
        if resolved in AIRPORTS:
            return resolved
    match = _CODE_CONTEXT_RE.search(text)
    if match:
        candidate = match.group(1)
        if candidate not in _NOT_CODES:
            return AviationWeatherClient.resolve_code(candidate)
    for match in _CODE_RE.finditer(text):
        candidate = match.group(1)
        if candidate not in _NOT_CODES:
            return AviationWeatherClient.resolve_code(candidate)
    return None


def place_from_text(text: str) -> str | None:
    """A city name from the shipped table, longest names first ('San Francisco' before 'San')."""
    lowered = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", text.lower())).strip() + " "
    for city in sorted({city for city, _ in AIRPORTS.values()}, key=len, reverse=True):
        if f" {city.lower()} " in lowered:
            return city
    return None


def point_from_text(text: str) -> str | None:
    """The first 'lat,lon' pair the caller spelled out, if any — never a thousands group."""
    for match in _POINT_RE.finditer(text):
        latitude, longitude = match.group(1), match.group(2)
        digits = longitude.lstrip("-")
        if "." not in digits and len(digits) > 1 and digits.startswith("0"):
            continue
        return f"{latitude},{longitude}"
    return None


def radius_from_text(text: str):
    match = _RADIUS_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def count_from_text(text: str):
    match = _COUNT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1) or match.group(2))
    except (TypeError, ValueError):
        return None


def codes_for_place(place: str) -> list[str]:
    """'Chicago' -> ['MDW', 'ORD']: every airport in the table for that city."""
    return AviationWeatherClient.code_for_place(place)


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    given = _first(data, "airports", "airport", "station", "ids", "code")
    place = _first(data, "place", "city")
    point = _first(data, "point", "coordinates")
    radius = _first(data, "radius_miles", "radius")
    count = _first(data, "limit", "count")

    if isinstance(given, str):
        codes = [AviationWeatherClient.resolve_code(part) for part in given.split(",") if part.strip()]
    elif isinstance(given, (list, tuple)):
        codes = [AviationWeatherClient.resolve_code(part) for part in given if str(part).strip()]
    else:
        codes = []
    if not codes:
        code = code_from_text(text)
        if code:
            codes = [code]
    if not place:
        place = place_from_text(text)
    if not codes and place:
        # "what is the weather in Chicago" is a question about that city's airports.
        codes = codes_for_place(place)
    if not point:
        point = point_from_text(text)
    if not radius:
        radius = radius_from_text(text)
    if not count:
        count = count_from_text(text)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_taf = bool(_TAF_WORDS.search(text))
    asked_for_nearby = bool(_NEARBY_WORDS.search(text))
    asked_for_conditions = bool(_CONDITION_WORDS.search(text))

    known = (SKILL_METAR, SKILL_TAF, SKILL_NEARBY, SKILL_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_WATCH
    # A forecast word with an airport is a TAF; a place without an airport is a list of stations.
    elif asked_for_taf and (codes or not (place or point)):
        skill = SKILL_TAF
    elif (place or point or asked_for_nearby) and not (codes and asked_for_conditions):
        skill = SKILL_NEARBY
    else:
        skill = SKILL_METAR

    params: dict = {}
    if codes:
        params["airports"] = codes
    if place:
        params["place"] = place
    if point:
        params["point"] = point
    if radius:
        params["radius"] = radius
    if count:
        params["limit"] = count
    params.setdefault("hours", DEFAULT_HOURS)
    explicit = bool(requested or asked_for_watch or asked_for_taf or asked_for_nearby
                    or asked_for_conditions)
    return {"skill": skill, "params": params, "explicit": explicit}


def _where(row: dict) -> str:
    """'KDEN (Denver)' — the station code the service uses, plus the city this server knows."""
    station = row.get("station") or "that station"
    return f"{station} ({row['city']})" if row.get("city") else station


def _visibility_phrase(value, capped: bool) -> str:
    """The value half of a visibility line: '10+ mi', '2 mi', or 'not reported'."""
    if value is None:
        return "not reported"
    number = f"{value:g}"
    return f"{number}+ mi" if capped else f"{number} mi"


def _ceiling_phrase(value) -> str:
    return "no ceiling reported" if value is None else f"ceiling {value:,} ft"


def observation_lines(row: dict, indent: str = "  • ") -> list[str]:
    """The decoded half of an observation, one line per measurement."""
    lines = [
        f"{indent}Reported {row['report_time'] or 'at an unstated time'}"
        + (f", flight category {row['flight_category']}" if row.get("flight_category") else "")
        + ".",
        f"{indent}Wind {row['wind']['phrase']}.",
        f"{indent}Visibility {_visibility_phrase(row['visibility_miles'], row['visibility_capped'])}, "
        f"{_ceiling_phrase(row['ceiling_ft'])}.",
        f"{indent}Sky: {row['clouds_text']}.",
    ]
    if row["temperature_c"] is not None or row["dewpoint_c"] is not None:
        temperature = "not reported" if row["temperature_c"] is None else f"{row['temperature_c']:g} °C"
        dewpoint = "not reported" if row["dewpoint_c"] is None else f"{row['dewpoint_c']:g} °C"
        lines.append(f"{indent}Temperature {temperature}, dewpoint {dewpoint}.")
    if row["altimeter_hpa"] is not None:
        lines.append(f"{indent}Altimeter {row['altimeter_hpa']:g} hPa.")
    return lines


NOT_FOUND_NOTE = (
    "A station code the service has no recent report for answers with an empty response, not an "
    "error, so an empty answer here means exactly that: no observation is published for that code "
    "right now."
)


def run_metar(params: dict, client: AviationWeatherClient) -> dict:
    codes = client.check_airports(params.get("airports") or [])
    if not codes:
        return {
            "final_state": "input-required",
            "message": ("Which station? Give me an ICAO code (KDEN, KJFK, EGLL) or a city from "
                        "this server's list (Denver, Chicago, Seattle)."),
            "artifact": None,
            "watch": None,
        }
    hours = client.check_hours(params.get("hours") or DEFAULT_HOURS)
    reads = [client.observation(code, hours=hours) for code in codes]
    found = [read for read in reads if read["found"]]
    artifact = {
        "dataset": DATASET,
        "source": "Aviation Weather Center (aviationweather.gov)",
        "endpoint": METAR_PATH,
        "hours": hours,
        "stations": codes,
        "count": len(found),
        "reports": reads,
    }
    if not found:
        return {
            "final_state": "completed",
            "message": (f"No observation is published for {', '.join(codes)} right now. "
                        f"{NOT_FOUND_NOTE}"),
            "artifact": artifact,
            "watch": None,
        }
    lines = []
    for read in reads:
        if not read["found"]:
            lines.append(f"{read['station']} — no observation published right now.")
            continue
        row = read["observation"]
        lines.append(f"{_where(row)} — current observation:")
        lines.extend(observation_lines(row))
        if row.get("raw"):
            lines.append(f"  • Raw report: {row['raw']}")
    lines.append(
        f"\nRead live from the Aviation Weather Center's METAR service ({DATASET}). The decoded "
        f"numbers and the service's own flight category are shown with the raw report behind them; "
        f"a METAR describes the airfield it is issued for, not the whole region around it."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_taf(params: dict, client: AviationWeatherClient) -> dict:
    codes = client.check_airports(params.get("airports") or [])
    if not codes:
        return {
            "final_state": "input-required",
            "message": ("Which station's forecast? Give me an ICAO code (KDEN, KJFK, EGLL) or a "
                        "city from this server's list."),
            "artifact": None,
            "watch": None,
        }
    reads = [client.forecast(code) for code in codes]
    found = [read for read in reads if read["found"]]
    artifact = {
        "dataset": DATASET,
        "source": "Aviation Weather Center (aviationweather.gov)",
        "endpoint": TAF_PATH,
        "stations": codes,
        "count": len(found),
        "forecasts": reads,
    }
    if not found:
        return {
            "final_state": "completed",
            "message": (f"No TAF is published for {', '.join(codes)} right now. {NOT_FOUND_NOTE}"),
            "artifact": artifact,
            "watch": None,
        }
    lines = []
    for read in reads:
        if not read["found"]:
            lines.append(f"{read['station']} — no forecast published right now.")
            continue
        row = read["forecast"]
        window = []
        if row["valid_from"]:
            window.append(f"from {row['valid_from']}")
        if row["valid_to"]:
            window.append(f"to {row['valid_to']}")
        lines.append(f"{_where(row)} — TAF issued {row['issued_at'] or 'at an unstated time'}"
                     + (f" ({', '.join(window)})" if window else "")
                     + (", amended" if row["amended"] else "") + ":")
        for period in row["periods"]:
            bits = []
            if period["change"]:
                bits.append(str(period["change"]))
            if period["probability"] is not None:
                bits.append(f"{period['probability']:g}% probability")
            bits.append(period["wind"]["phrase"])
            bits.append(f"visibility {_visibility_phrase(period['visibility_miles'], period['visibility_capped'])}")
            bits.append(_ceiling_phrase(period["ceiling_ft"]))
            if period["vertical_visibility_ft"] is not None:
                bits.append(f"vertical visibility {int(period['vertical_visibility_ft']):,} ft")
            if period["weather"]:
                bits.append(str(period["weather"]))
            lines.append(f"  • {period['from'] or '?'} to {period['to'] or '?'} — " + ", ".join(bits))
            lines.append(f"      Sky: {period['clouds_text']}.")
        if row.get("raw"):
            lines.append(f"  • Raw TAF: {row['raw']}")
    lines.append(
        f"\nRead live from the Aviation Weather Center's TAF service ({DATASET}). These are the "
        f"service's forecast periods in its own order; periods overlap by design (a temporary "
        f"condition sits inside a longer one), so they are not merged. A TAF is a forecast: it "
        f"announces the forecast conditions, not what a pilot will see."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_nearby(params: dict, client: AviationWeatherClient) -> dict:
    place = params.get("place")
    point = params.get("point")
    label = None
    if point:
        latitude, longitude = client.check_point(point)
        label = f"the point {latitude:g},{longitude:g}"
    elif place:
        found = client.city(place)
        if not found:
            return {
                "final_state": "completed",
                "message": (f"I do not know the place {place!r}, so I will not guess coordinates for "
                            f"it. Give me a latitude/longitude point or a US city from this server's "
                            f"list (Denver, Chicago, Seattle…)."),
                "artifact": {"dataset": DATASET, "place": place, "known": False},
                "watch": None,
            }
        latitude, longitude, label = found
    else:
        return {
            "final_state": "input-required",
            "message": ("Which place? Give me a city from this server's list (Denver, Chicago, "
                        "Seattle…) or a latitude/longitude point."),
            "artifact": None,
            "watch": None,
        }
    radius = client.check_radius(params.get("radius") or DEFAULT_RADIUS_MILES)
    limit = client.check_positive(params.get("limit") or MAX_LISTED)
    read = client.nearby(latitude, longitude, radius_miles=radius, limit=limit,
                         hours=client.check_hours(params.get("hours") or DEFAULT_HOURS))
    artifact = {
        "dataset": DATASET,
        "source": "Aviation Weather Center (aviationweather.gov)",
        "endpoint": METAR_PATH,
        "place": label,
        "latitude": latitude,
        "longitude": longitude,
        "radius_miles": radius,
        "bbox": read["bbox"],
        "in_box": read["in_box"],
        "found": read["found"],
        "stations": read["stations"],
    }
    if not read["stations"]:
        return {
            "final_state": "completed",
            "message": (f"No reporting station is within {radius:g} miles of {label} — the service "
                        f"returned {read['in_box']} observation(s) from the wider box and none of "
                        f"them fell inside that radius. Widen the radius (up to "
                        f"{MAX_RADIUS_MILES:g} miles) or name a station code."),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{read['found']} reporting station(s) within {radius:g} miles of {label}, "
             f"nearest first:"]
    for row in read["stations"]:
        category = row["flight_category"] or "category not reported"
        lines.append(f"  • {_where(row)} — {category} — {row['miles']} mi — "
                     f"{row['wind']['phrase']}, visibility "
                     f"{_visibility_phrase(row['visibility_miles'], row['visibility_capped'])}, "
                     f"{_ceiling_phrase(row['ceiling_ft'])}, reported {row['report_time'] or '?'}")
    lines.append(
        f"\nDistances are straight-line miles computed here from {label} to the coordinates the "
        f"service reports for each station. Read live from the Aviation Weather Center's METAR "
        f"service ({DATASET}) in one bounding-box request: the service only returns stations that "
        f"reported recently, so an airport with no current observation is simply absent. A station "
        f"reports the airfield it sits on, so a quiet station nearby is not a statement about the "
        f"sky where you are."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_watch(params: dict, client: AviationWeatherClient) -> dict:
    codes = client.check_airports(params.get("airports") or [])
    if len(codes) > 1:
        return {
            "final_state": "completed",
            "message": "A watch covers one station; give me a single ICAO code.",
            "artifact": {"dataset": DATASET, "airports": codes},
            "watch": None,
        }
    if not codes:
        return {
            "final_state": "input-required",
            "message": ("Which station should I watch? Give me one ICAO code (KDEN, KJFK, EGLL) or "
                        "a city from this server's list."),
            "artifact": None,
            "watch": None,
        }
    code = codes[0]
    read = client.observation(code, hours=client.check_hours(params.get("hours") or DEFAULT_HOURS))
    row = read["observation"] if read["found"] else None
    if not row:
        return {
            "final_state": "completed",
            "message": (f"No observation is published for {code} right now, so there is no flight "
                        f"category to watch yet. {NOT_FOUND_NOTE}"),
            "artifact": {"dataset": DATASET, "station": code, "found": False},
            "watch": None,
        }
    category = row["flight_category"] or "not reported"
    observed = client.watch_state(code)
    watch = {
        "kind": SKILL_WATCH,
        "dataset": DATASET,
        "station": code,
        "observed": observed,
    }
    artifact = {
        "dataset": DATASET,
        "source": "Aviation Weather Center (aviationweather.gov)",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "flight_category_now": row["flight_category"],
        "report_time": row["report_time"],
        "raw": row["raw"],
    }
    message = (f"Watching {_where(row)} — flight category {category} as of "
               f"{row['report_time'] or 'the last report'}.")
    message += (
        "\nPoint a pushNotificationConfig at this task and I will POST when the service's reported "
        "flight category changes (VFR to MVFR, IFR to LIFR, and back). Temperature, wind and "
        "visibility move within a category all day, so only the category is compared — routine "
        "revisions will not page you."
    )
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class AviationAgent(SkillAgent):
    name = "aviation"
    card_name = "Aviation Weather Agent"
    card_description = (
        "Read-only agent over the Aviation Weather Center's public feeds: an airport's current "
        "METAR observation decoded (wind, visibility, ceiling, temperature, altimeter, cloud "
        "layers) with the raw report and the service's own flight category, the airport's TAF "
        "forecast with every period and the raw text, the reporting stations within a radius of a "
        "place or point with straight-line miles, and a watch that POSTs to your webhook when an "
        "airport's reported flight category changes."
    )
    env_prefix = "AVIATION"
    datasets = (DATASET,)
    card_skills = [
        {
            "id": SKILL_METAR,
            "name": "Current observation (METAR)",
            "description": (
                "An airport's current METAR, decoded: wind with gusts, visibility, ceiling, cloud "
                "layers, temperature and dewpoint, altimeter, and the service's own flight "
                "category (VFR/MVFR/IFR/LIFR), with the raw report text alongside. Accepts an ICAO "
                "code (KDEN) or a city from this server's list (Denver, which covers DEN, APA "
                "and BJC where it knows them)."
            ),
            "tags": ["metar", "aviation", "weather", "visibility", "ceiling", "flight-category"],
            "examples": [
                "What is the weather at KDEN right now?",
                '{"skill": "metar", "airports": ["KDEN", "KBJC"]}',
            ],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["application/json", "text/plain"],
        },
        {
            "id": SKILL_TAF,
            "name": "Terminal forecast (TAF)",
            "description": (
                "An airport's TAF: the service's forecast periods with wind, visibility, ceiling "
                "and weather for each, plus the raw TAF text and its valid window."
            ),
            "tags": ["taf", "aviation", "forecast", "terminal-aerodrome"],
            "examples": [
                "What is the TAF for KJFK?",
                '{"skill": "taf", "airports": ["KDEN"]}',
            ],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["application/json", "text/plain"],
        },
        {
            "id": SKILL_NEARBY,
            "name": "Reporting stations near a place",
            "description": (
                "Every reporting station within a radius of a city or a latitude/longitude point, "
                "nearest first, each with its flight category, wind, visibility, ceiling and the "
                "straight-line miles from the place. One bounding-box request to the METAR service."
            ),
            "tags": ["metar", "aviation", "nearby", "radius", "bounding-box"],
            "examples": [
                "Which airports are reporting near Denver?",
                '{"skill": "aviation-nearby", "place": "Denver", "radius_miles": 60}',
            ],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["application/json", "text/plain"],
        },
        {
            "id": SKILL_WATCH,
            "name": "Watch a station's flight category",
            "description": (
                "Watch one airport and have the server POST to your webhook when the service's "
                "reported flight category changes — VFR to MVFR or IFR as conditions worsen, and "
                "back as they improve. Optional category filter (MVFR, IFR, LIFR)."
            ),
            "tags": ["metar", "aviation", "watch", "webhook", "flight-category", "alert"],
            "examples": [
                "Tell me when KDEN goes IFR.",
                '{"skill": "aviation-watch", "station": "KDEN", "category": "IFR"}',
            ],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["application/json", "text/plain"],
        },
    ]
    watch_kinds = (SKILL_WATCH,)

    def __init__(self, client: AviationWeatherClient | None = None) -> None:
        self.client = client or AviationWeatherClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill in (SKILL_METAR, SKILL_TAF, SKILL_WATCH) and not params.get("airports"):
            return ["airport"]
        if skill == SKILL_NEARBY and not (params.get("place") or params.get("point")):
            return ["place"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_NEARBY:
            return ("Which place? Give me a city from this server's list (Denver, Chicago, "
                    "Seattle…) or a latitude/longitude point such as 39.74,-104.99.")
        return ("Which station? Give me an ICAO code (KDEN, KJFK, EGLL) or a city from this "
                "server's list (Denver, Chicago, Seattle).")

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        skill, params = request["skill"], request["params"]
        if skill == SKILL_TAF:
            return run_taf(params, self.client)
        if skill == SKILL_NEARBY:
            return run_nearby(params, self.client)
        if skill == SKILL_WATCH:
            return run_watch(params, self.client)
        return run_metar(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if watch.get("kind") != SKILL_WATCH or not watch.get("station"):
            return None
        return self.client.watch_state(watch["station"])

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = ((previous or {}).get("stations") or {})
        after = ((observed or {}).get("stations") or {})
        station = watch.get("station") or "the station"
        city = self.client.airport_city(station)
        where = f"{station} ({city[0]})" if city else station
        old = (before.get(station) or {}).get("flight_category")
        new = (after.get(station) or {}).get("flight_category")
        if old and new and old != new:
            worse = FLIGHT_CATEGORIES.index(new) > FLIGHT_CATEGORIES.index(old) \
                if old in FLIGHT_CATEGORIES and new in FLIGHT_CATEGORIES else None
            shape = "worsened" if worse else "improved"
            return f"{where} flight category {shape}: {old} to {new}"
        if new:
            return f"{where} reported {new}"
        return f"the reported flight category at {where} changed"


__all__ = [
    "SKILL_METAR",
    "SKILL_NEARBY",
    "SKILL_TAF",
    "SKILL_WATCH",
    "AviationAgent",
    "code_from_text",
    "codes_for_place",
    "count_from_text",
    "observation_lines",
    "parse",
    "place_from_text",
    "point_from_text",
    "radius_from_text",
    "run_metar",
    "run_nearby",
    "run_taf",
    "run_watch",
]
