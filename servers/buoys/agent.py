"""The buoy agent: five skills over NOAA's National Data Buoy Center observations.

  - buoy-conditions: the newest reading at a station, or at the nearest reporting station
  - buoy-trend:      that station's recent readings with the window's min/max per field
  - buoys-near:      stations within a radius of a point or city, nearest first
  - buoys-list:      search the 1,354-station catalogue by name, owner, programme or sensors
  - buoy-watch:      watch a station's wave height / wind / temperature for a threshold crossing

Watch-capable: buoy-watch stores {"kind": "buoy-watch", station, field, threshold, observed}
where observed is only {"above": true|false}. Buoys report every 10-60 minutes and every value
moves constantly, so the crossing is the news — not the raw number.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from a2a_kit import SkillAgent

from data import (
    CITY_COORDS,
    DATASET,
    LABELS,
    UNIT_LABELS,
    WATCH_FIELDS,
    BuoyClient,
)

SKILL_BUOY_CONDITIONS = "buoy-conditions"
SKILL_BUOY_TREND = "buoy-trend"
SKILL_BUOYS_NEAR = "buoys-near"
SKILL_BUOYS_LIST = "buoys-list"
SKILL_BUOY_WATCH = "buoy-watch"

#: Offshore stations are spaced hundreds of miles apart, so resolving a coastal city to its
#: nearest buoy gets a wider default than a land-based "near me" search would.
DEFAULT_RADIUS_MILES = 250.0
DEFAULT_TREND_HOURS = 24
DEFAULT_WATCH_FIELD = "wave_height"
#: Wave-height threshold the watch uses when the caller names none: a small-craft day.
DEFAULT_WATCH_THRESHOLDS = {"english": 8.0, "metric": 2.5}
MAX_LISTED_STATIONS = 10
STALE_AFTER_MINUTES = 180

CARD_SKILLS = [
    {
        "id": SKILL_BUOY_CONDITIONS,
        "name": "Buoy conditions",
        "description": (
            "The newest NDBC observation at a station (for example 41025), or at the nearest "
            "reporting station to a city or latitude/longitude point: wind speed, direction and "
            "gusts, wave height with its dominant period and direction, sea and air temperature, "
            "pressure and tendency, visibility and tide. Missing sensors are reported as missing, "
            "and the age of the reading is always stated."
        ),
        "tags": ["buoy", "ndbc", "noaa", "waves", "wind", "sea state", "marine"],
        "examples": [
            "What are the conditions at buoy 41025?",
            "How rough is the water off Miami right now?",
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_BUOY_TREND,
        "name": "Buoy trend",
        "description": (
            "A station's recent readings (up to 240 rows, newest first) with the low, high and "
            "latest value of each field it actually reports, so you can see whether the sea is "
            "building or easing."
        ),
        "tags": ["buoy", "ndbc", "trend", "history", "waves"],
        "examples": [
            "How have the waves at 44009 changed over the last 24 hours?",
            '{"skill": "buoy-trend", "station": "41025", "hours": 48}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_BUOYS_NEAR,
        "name": "Buoys near a place",
        "description": (
            "NDBC stations within a radius of a latitude/longitude point or a coastal city, "
            "nearest first, with real distances, station type, owner and whether each one is "
            "reporting right now."
        ),
        "tags": ["buoy", "ndbc", "nearby", "distance", "stations"],
        "examples": [
            "Which buoys are near Key West?",
            '{"skill": "buoys-near", "point": "34.68,-75.36", "radius_miles": 200}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_BUOYS_LIST,
        "name": "Station catalogue",
        "description": (
            "Search the NDBC station catalogue by name, id, owner, programme or sensor package. "
            "The catalogue holds every station NDBC lists, including ones that are not reporting."
        ),
        "tags": ["buoy", "ndbc", "station", "catalogue", "search"],
        "examples": [
            "Find NDBC stations with the word Diamond in the name.",
            '{"skill": "buoys-list", "program": "Moored Buoy"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_BUOY_WATCH,
        "name": "Watch a buoy's reading",
        "description": (
            "Watch a station — or the nearest reporting station to a place — and have the server "
            "POST to your webhook when a reading crosses your threshold (default: an 8 ft wave, "
            "or 2.5 m in metric)."
        ),
        "tags": ["buoy", "ndbc", "watch", "webhook", "waves", "alert"],
        "examples": [
            "Tell me when the waves at 41025 pass 8 feet.",
            '{"skill": "buoy-watch", "place": "Cape Hatteras", "field": "wind_speed", "threshold": 25}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
#: NDBC ids are five digits (41025) or four letters and a digit (SANF1), plus a few five-letter ones.
_STATION_RE = re.compile(r"\b(\d{5}|[A-Z]{4}\d|[A-Z]{5})\b")
_HOURS_RE = re.compile(r"\b(?:last|past|previous|over the last|over the past|next)\s+(\d{1,3})\s*(?:hours?|hrs?|h)\b",
                       re.IGNORECASE)
_BARE_HOURS_RE = re.compile(r"\b(\d{1,3})\s*(?:hours?|hrs?)\b", re.IGNORECASE)
_RADIUS_RE = re.compile(r"\bwithin\s+([\d,]+)\s*(?:mi|mile|miles|nm|nautical miles?)\b", re.IGNORECASE)
_THRESHOLD_RE = re.compile(
    r"\b(?:above|over|exceeds?|greater than|more than|at least|pass(?:es)?|reaches?|hits?|tops?)\s+"
    r"(\d{1,3}(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe|keep an eye)\b",
                          re.IGNORECASE)
_TREND_WORDS = re.compile(r"\b(trend|trending|history|historic|over the last|past \d+|last \d+ hours?|"
                          r"recent|changing|building|easing|risen|fallen)\b", re.IGNORECASE)
#: "which buoys are near X" is a station list; "how rough is it near X" is a reading.
_NEAR_LIST_WORDS = re.compile(
    r"\b(?:which|what|any|are there)\s+(?:buoys?|stations?)\b|\b(?:buoys?|stations?)\s+(?:near|nearby|close)\b|"
    r"\b(?:nearest|closest)\s+(?:buoys?|stations?)\b",
    re.IGNORECASE,
)
_LIST_WORDS = re.compile(
    r"\b(catalogue|catalog|list|search|find|look for|monitors?|stations? called|which stations)\b",
    re.IGNORECASE,
)
#: "find stations with the word Diamond in the name" / `"Diamond Shoals"` -> a search term.
_QUERY_RE = re.compile(r"\b(?:word|named|called)\s+([A-Za-z0-9'\-]{2,30})|\"([^\"]{2,30})\"")
#: Most specific first: "sea temp" contains "sea", which alone must not mean wave height.
_FIELD_PATTERNS = (
    ("water_temp", re.compile(r"\b(sea temp\w*|water temp\w*|sst|sea surface temp\w*)\b", re.IGNORECASE)),
    ("air_temp", re.compile(r"\b(air temp\w*)\b", re.IGNORECASE)),
    ("wind_gust", re.compile(r"\bgusts?\b", re.IGNORECASE)),
    ("wind_speed", re.compile(r"\b(wind|winds|breeze)\b", re.IGNORECASE)),
    ("wave_height", re.compile(r"\b(waves?|wave height|sea state|seas|swell)\b", re.IGNORECASE)),
)


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


def station_from_text(text: str) -> str | None:
    """An NDBC station id, read the way NDBC writes them."""
    match = _STATION_RE.search(text)
    if match:
        return match.group(1)
    return None


def place_from_text(text: str) -> str | None:
    """A coastal city from the built-in list. Longest names win."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


#: "...near Atlantis" with no match in the place list: a name the caller gave that this server
#: cannot resolve. Reported honestly instead of being treated as "no place given".
_UNKNOWN_PLACE_RE = re.compile(
    r"\b(?:in|at|near|off|around|for)\s+(?:the\s+|a\s+|an\s+)?"
    r"([A-Z][\w'\-]{2,20}(?:\s+[A-Z][\w'\-]{2,20})?)")
_IGNORED_PLACES = frozenset({"The", "Us", "USA", "My", "Here", "This", "It", "There", "Today",
                             "Tomorrow", "Buoy", "Buoys", "Station", "Stations"})


def unknown_place_from_text(text: str) -> str | None:
    """A capitalised place name after a preposition that the built-in list does not have."""
    match = _UNKNOWN_PLACE_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip()
    if candidate.split()[0] in _IGNORED_PLACES:
        return None
    return candidate


def hours_from_text(text: str) -> int | None:
    match = _HOURS_RE.search(text) or _BARE_HOURS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def radius_from_text(text: str) -> float | None:
    match = _RADIUS_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def threshold_from_text(text: str) -> float | None:
    match = _THRESHOLD_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if -200 <= value <= 1000 else None


def field_from_text(text: str) -> str | None:
    for field, pattern in _FIELD_PATTERNS:
        if pattern.search(text):
            return field
    return None


def query_from_text(text: str) -> str | None:
    """A catalogue search term the caller spelled out, in quotes or after 'named'/'called'."""
    match = _QUERY_RE.search(text)
    if not match:
        return None
    return (match.group(1) or match.group(2)).strip()


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    station = _first(data, "station", "station_id", "buoy")
    point = _first(data, "point", "lat_lon")
    place = _first(data, "place", "city", "location")
    hours = _first(data, "hours")
    radius = _first(data, "radius_miles", "radius")
    field = _first(data, "field", "measurement")
    threshold = _first(data, "threshold")

    if not station:
        station = station_from_text(text)
    if not point:
        match = _POINT_RE.search(text)
        if match:
            point = f"{match.group(1)},{match.group(2)}"
    if not place:
        place = place_from_text(text)
    if not place:
        place = unknown_place_from_text(text)
    if hours is None:
        hours = hours_from_text(text)
    if radius is None:
        radius = radius_from_text(text)
    if not field:
        field = field_from_text(text)
    if threshold is None:
        threshold = threshold_from_text(text)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_trend = bool(_TREND_WORDS.search(text))
    asked_for_near_list = bool(_NEAR_LIST_WORDS.search(text))
    asked_for_list = bool(_LIST_WORDS.search(text))

    known = (SKILL_BUOY_CONDITIONS, SKILL_BUOY_TREND, SKILL_BUOYS_NEAR, SKILL_BUOYS_LIST,
             SKILL_BUOY_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_BUOY_WATCH
    elif asked_for_trend:
        skill = SKILL_BUOY_TREND
    elif asked_for_near_list:
        skill = SKILL_BUOYS_NEAR
    elif asked_for_list and not (station or point or place):
        skill = SKILL_BUOYS_LIST
    else:
        skill = SKILL_BUOY_CONDITIONS

    params: dict = {"limit": MAX_LISTED_STATIONS}
    if skill == SKILL_BUOYS_LIST:
        query = _first(data, "query", "text") or query_from_text(text)
        if query:
            params["query"] = str(query)
        if _first(data, "program", "pgm"):
            params["program"] = str(_first(data, "program", "pgm"))
        if _first(data, "owner"):
            params["owner"] = str(_first(data, "owner"))
        if _first(data, "type"):
            params["type"] = str(_first(data, "type"))
        if _first(data, "met_only") is not None:
            params["met_only"] = bool(_first(data, "met_only"))
        return {"skill": skill, "params": params,
                "explicit": bool(requested or asked_for_list)}

    if station:
        params["station"] = str(station)
    if point:
        params["point"] = str(point)
    if place:
        params["place"] = str(place)
    if skill == SKILL_BUOY_TREND:
        params["hours"] = hours if hours is not None else DEFAULT_TREND_HOURS
    if skill == SKILL_BUOYS_NEAR:
        params["radius_miles"] = radius if radius is not None else DEFAULT_RADIUS_MILES
    if skill == SKILL_BUOY_WATCH:
        params["field"] = str(field).lower() if field else DEFAULT_WATCH_FIELD
        if threshold is not None:
            params["threshold"] = threshold
    explicit = bool(requested or asked_for_watch or asked_for_trend or asked_for_near_list or asked_for_list)
    return {"skill": skill, "params": params, "explicit": explicit}


def resolve_target(params: dict, client: BuoyClient, radius_miles: float = DEFAULT_RADIUS_MILES
                   ) -> tuple[str | None, str | None, dict | None]:
    """(station id, label, station row) from an id, a point, or the nearest reporting station."""
    if params.get("station"):
        wanted = client.check_station(params["station"])
        row = client.station(wanted)
        if row is None:
            return None, None, {"known": False, "station": wanted}
        return wanted, row["name"] or wanted, row
    if params.get("point"):
        lat, lon = client.check_point(params["point"])
        label = f"the point {lat},{lon}"
    elif params.get("place"):
        found = client.city(params["place"])
        if not found:
            return None, None, {"known": False, "place": params["place"]}
        lat, lon, label = found
    else:
        return None, None, None
    near = client.near(lat, lon, radius_miles=radius_miles, limit=1, reporting_only=True)
    if not near["count"]:
        return None, label, {"found": 0, "latitude": lat, "longitude": lon,
                             "radius_miles": radius_miles}
    row = near["stations"][0]
    return row["id"], f"{row['id']} near {label} ({row['distance_miles']:,.1f} miles away)", row


def age_minutes(stamp: str | None) -> int | None:
    """How old an observation is, in minutes, so nothing stale is passed off as current."""
    if not stamp:
        return None
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)


def _number(value, unit: str = "") -> str:
    if value is None:
        return "not published"
    return f"{value:g}{(' ' + unit) if unit else ''}"


def _wind_phrase(observation: dict) -> str | None:
    speed = observation.get("wind_speed")
    if speed is None:
        return None
    unit = observation["units"]["wind_speed"]
    direction = observation.get("wind_direction")
    degrees = observation.get("wind_direction_degrees")
    where = f" from {direction} ({degrees:g}°)" if direction and degrees is not None else ""
    phrase = f"wind {speed:g} {unit}{where}"
    if observation.get("wind_gust") is not None:
        phrase += f", gusting {observation['wind_gust']:g} {unit}"
    return phrase


def _sea_phrase(observation: dict) -> str | None:
    height = observation.get("wave_height")
    if height is None:
        return None
    unit = observation["units"]["wave_height"]
    phrase = f"waves {height:g} {unit}"
    if observation.get("dominant_period") is not None:
        phrase += f" with a {observation['dominant_period']:g} s dominant period"
    if observation.get("wave_direction"):
        phrase += f" from {observation['wave_direction']}"
    return phrase


def _conditions_lines(observation: dict) -> list[str]:
    lines = []
    with_values = [part for part in (_wind_phrase(observation), _sea_phrase(observation)) if part]
    if with_values:
        lines.append("  • " + "; ".join(with_values))
    rest = []
    temp_unit = observation["units"]["water_temp"]
    if observation.get("water_temp") is not None:
        rest.append(f"sea surface {observation['water_temp']:g} {temp_unit}")
    if observation.get("air_temp") is not None:
        rest.append(f"air {observation['air_temp']:g} {observation['units']['air_temp']}")
    if observation.get("dew_point") is not None:
        rest.append(f"dew point {observation['dew_point']:g} {observation['units']['dew_point']}")
    if rest:
        lines.append("  • " + ", ".join(rest))
    pressure = observation.get("pressure")
    if pressure is not None:
        bit = f"pressure {_number(pressure, observation['units']['pressure'])}"
        if observation.get("pressure_tendency") is not None:
            bit += f" (tendency {observation['pressure_tendency']:+g} over 3 h)"
        lines.append("  • " + bit)
    extra = []
    if observation.get("visibility") is not None:
        extra.append(f"visibility {observation['visibility']:g} nmi")
    if observation.get("tide") is not None:
        extra.append(f"tide {observation['tide']:g} ft")
    if extra:
        lines.append("  • " + ", ".join(extra))
    missing = [LABELS[name] for name in ("wind_speed", "wave_height", "water_temp", "air_temp",
                                         "pressure", "visibility")
               if observation.get(name) is None]
    if missing:
        lines.append(f"  • Not published by this station right now: {', '.join(missing)}.")
    return lines


def run_buoy_conditions(params: dict, client: BuoyClient) -> dict:
    station, label, row = resolve_target(params, client)
    if station is None:
        if row and row.get("known") is False:
            return {
                "final_state": "completed",
                "message": (f"NDBC's catalogue has no station {row.get('station') or row.get('place')!r}, "
                            f"so I will not guess one."),
                "artifact": {"dataset": DATASET, **row},
                "watch": None,
            }
        if row and row.get("found") == 0:
            return {
                "final_state": "completed",
                "message": (f"No reporting NDBC station is within {row['radius_miles']:g} miles of "
                            f"{label}, so there is nothing to read there."),
                "artifact": {"dataset": DATASET, **row},
                "watch": None,
            }
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET, "known": False, "place": params.get("place")},
            "watch": None,
        }
    observation = client.conditions(station)
    if observation is None:
        return {
            "final_state": "completed",
            "message": (f"Station {station} is in NDBC's catalogue but has no current observation, "
                        f"so it is not reporting right now. I will not invent a reading for it."),
            "artifact": {"dataset": DATASET, "station": station, "observing": False,
                         "station_info": row},
            "watch": None,
        }
    if all(observation.get(name) is None for name in WATCH_FIELDS
           + ("pressure", "dew_point", "visibility", "tide", "dominant_period", "average_period")):
        return {
            "final_state": "completed",
            "message": (f"Station {station} is in NDBC's latest-observation table, but every sensor in "
                        f"that row reads missing right now, so there is no reading to report. Its "
                        f"row was last published at {observation['time']} UTC."),
            "artifact": {"dataset": DATASET, "station": station, "observing": False,
                         "station_info": row, "observation": observation},
            "watch": None,
        }
    age = age_minutes(observation["time"])
    lines = [f"Latest NDBC observation from {label}, at {observation['time']} UTC"
             + (f" — {age} minute(s) old." if age is not None else ".")]
    lines.extend(_conditions_lines(observation))
    if age is not None and age > STALE_AFTER_MINUTES:
        lines.append(f"  • That reading is {age // 60} hours old: this station is reporting late, "
                     f"not calm.")
    lines.append(
        f"\nRead live from NOAA's National Data Buoy Center ({DATASET}). Values are the station's "
        f"own sensors in {UNIT_LABELS[client.units]} for this server; NDBC publishes them in m/s, m, "
        f"°C and hPa and they are converted here. A buoy measures one spot, not a coastline."
    )
    artifact = {
        "dataset": DATASET,
        "source": "NOAA National Data Buoy Center latest observations",
        "station": station,
        "label": label,
        "age_minutes": age,
        "units_system": client.units,
        "station_info": row,
        "observation": observation,
    }
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_buoy_trend(params: dict, client: BuoyClient) -> dict:
    station, label, row = resolve_target(params, client)
    if station is None:
        if row and row.get("known") is False:
            message = (f"NDBC's catalogue has no station {row.get('station') or row.get('place')!r}, "
                       f"so I will not guess one.")
        elif row and row.get("found") == 0:
            message = (f"No reporting NDBC station is within {row['radius_miles']:g} miles of {label}.")
        else:
            message = "I do not know that place, so I will not guess coordinates for it."
        return {"final_state": "completed", "message": message,
                "artifact": {"dataset": DATASET, **(row or {"known": False})}, "watch": None}
    hours = client.check_positive(params.get("hours") or DEFAULT_TREND_HOURS, "hours", maximum=240)
    read = client.history(station, hours=hours)
    artifact = {
        "dataset": DATASET,
        "source": "NOAA National Data Buoy Center per-station tables",
        "station": station,
        "label": label,
        "hours_requested": hours,
        "units_system": client.units,
        "station_info": row,
        **{key: read[key] for key in ("count", "first", "last", "fields", "stats", "series")},
    }
    if not read["count"]:
        return {
            "final_state": "completed",
            "message": f"Station {station} has no rows in its recent-observation file.",
            "artifact": artifact,
            "watch": None,
        }
    age = age_minutes(read["last"])
    lines = [f"Last {read['count']} observations from station {station}, newest first "
             f"({read['last']} to {read['first']} UTC"
             + (f", newest {age} minute(s) old" if age is not None else "") + "):"]
    for field in read["fields"]:
        stats = read["stats"][field]
        unit = stats["unit"]
        lines.append(f"  • {LABELS[field]}: latest {_number(stats['latest'], unit)}, "
                     f"low {_number(stats['min'], unit)}, high {_number(stats['max'], unit)} "
                     f"across {stats['samples']} reading(s)")
    for observation in read["series"][:6]:
        wind = observation.get("wind_speed")
        waves = observation.get("wave_height")
        lines.append(f"      {observation['time']}  wind {_number(wind)} "
                     f"{observation['units']['wind_speed']}"
                     f"  waves {_number(waves)} {observation['units']['wave_height']}".rstrip())
    if read["count"] > 6:
        lines.append(f"      ... {read['count'] - 6} more rows are in the artifact.")
    lines.append(
        f"\nRead live from NDBC's per-station table ({DATASET}); rows are newest-first and most "
        f"stations report hourly, so a single missing row is a gap in reporting, not zero."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_buoys_near(params: dict, client: BuoyClient) -> dict:
    radius = float(params.get("radius_miles") or DEFAULT_RADIUS_MILES)
    if params.get("point"):
        lat, lon = client.check_point(params["point"])
        label = f"the point {lat},{lon}"
    elif params.get("place"):
        found = client.city(params["place"])
        if not found:
            return {
                "final_state": "completed",
                "message": "I do not know that place, so I will not guess coordinates for it.",
                "artifact": {"dataset": DATASET, "known": False, "place": params.get("place")},
                "watch": None,
            }
        lat, lon, label = found
    else:
        return {
            "final_state": "completed",
            "message": "Which place? Give me a city from this server's list or a latitude,longitude point.",
            "artifact": {"dataset": DATASET, "known": False},
            "watch": None,
        }
    met_only = bool(params.get("met_only"))
    read = client.near(lat, lon, radius_miles=radius, limit=client.check_positive(params.get("limit") or MAX_LISTED_STATIONS),
                       reporting_only=True, met_only=met_only)
    artifact = {
        "dataset": DATASET,
        "source": "NOAA National Data Buoy Center station catalogue",
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "radius_miles": radius,
        "count": read["count"],
        "truncated": read["truncated"],
        "stations": read["stations"],
    }
    if not read["count"]:
        return {
            "final_state": "completed",
            "message": (f"No reporting NDBC station sits within {radius:g} miles of {label} "
                        f"({lat},{lon}). NDBC's offshore buoys are spaced hundreds of miles apart."),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{read['count']} reporting NDBC station(s) within {radius:g} miles of {label} "
             f"({lat},{lon}), nearest first:"]
    for row in read["stations"]:
        sensors = ", ".join(name for name, present in
                            (("weather", row["sensors"]["met"]),
                             ("currents", row["sensors"]["currents"]),
                             ("water quality", row["sensors"]["waterquality"]),
                             ("DART tsunami", row["sensors"]["dart"])) if present) or "no sensors flagged"
        lines.append(f"  • {row['id']} — {row['name'] or 'unnamed'}, {row['distance_miles']:,.1f} miles away"
                     f", {row['type'] or 'type not stated'}"
                     + (f", run by {row['owner']}" if row["owner"] else "")
                     + f" (measures {sensors})")
    if read["truncated"]:
        lines.append(f"      ... more stations are in the artifact.")
    lines.append(
        f"\nRead live from NDBC's station catalogue ({DATASET}). Distances are great-circle miles "
        f"from the point you gave to the station's published position; a station is one instrument "
        f"at one spot, and the nearest one may not be the one your harbour listens to."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_buoys_list(params: dict, client: BuoyClient) -> dict:
    query = params.get("query")
    program = params.get("program")
    owner = params.get("owner")
    type_ = params.get("type")
    met_only = bool(params.get("met_only"))
    limit = client.check_positive(params.get("limit") or MAX_LISTED_STATIONS)
    if not any((query, program, owner, type_, met_only)):
        return {
            "final_state": "input-required",
            "message": ("What should I search for? Give me part of a station id or name, or one of "
                        "these programmes: " + ", ".join(client.programs()) + "."),
            "artifact": None,
            "watch": None,
        }
    read = client.stations(text=query, program=program, owner=owner, type_=type_, met_only=met_only,
                           limit=limit)
    artifact = {
        "dataset": DATASET,
        "source": "NOAA National Data Buoy Center station catalogue",
        "query": query,
        "program": program,
        "owner": owner,
        "type": type_,
        "met_only": met_only,
        "count": read["count"],
        "reporting": read["reporting"],
        "truncated": read["truncated"],
        "stations": read["stations"],
        "programmes_available": client.programs(),
        "types_available": client.station_types(),
    }
    if not read["count"]:
        return {
            "final_state": "completed",
            "message": (f"No NDBC station matches that search. Programmes on file: "
                        f"{', '.join(client.programs())}."),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{read['count']} station(s) match, {read['reporting']} of them reporting right now:"]
    for row in read["stations"]:
        lines.append(f"  • {row['id']} — {row['name'] or 'unnamed'}"
                     + (f", {row['type']}" if row["type"] else "")
                     + (f", {row['program']}" if row["program"] else "")
                     + f" at {_number(row['latitude'])},{_number(row['longitude'])}"
                     + (", reporting" if row["reporting"] else ", not reporting"))
    if read["truncated"]:
        lines.append("      ... more matches are in the artifact.")
    lines.append(
        f"\nRead live from NDBC's station catalogue ({DATASET}). The catalogue lists stations NDBC "
        f"operates or hosts; only the ones marked reporting have a fresh observation."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_buoy_watch(params: dict, client: BuoyClient) -> dict:
    field = client.check_field(params.get("field") or DEFAULT_WATCH_FIELD)
    threshold = params.get("threshold")
    threshold = (client.check_threshold(threshold) if threshold is not None
                 else DEFAULT_WATCH_THRESHOLDS[client.units])
    station, label, row = resolve_target(params, client)
    if station is None:
        if row and row.get("found") == 0:
            message = (f"No reporting NDBC station is within {row['radius_miles']:g} miles of {label}, "
                       f"so there is nothing to watch there.")
        elif row and row.get("known") is False:
            message = f"NDBC's catalogue has no station {row.get('station')!r}."
        else:
            message = "I do not know that place, so I will not guess coordinates for it."
        return {"final_state": "completed", "message": message,
                "artifact": {"dataset": DATASET, **(row or {"known": False})}, "watch": None}
    observation = client.conditions(station)
    if observation is None:
        return {
            "final_state": "completed",
            "message": (f"Station {station} has no current observation, so I cannot watch a reading "
                        f"that is not being published."),
            "artifact": {"dataset": DATASET, "station": station, "observing": False},
            "watch": None,
        }
    try:
        observed = client.watch_state(station, field, threshold)
    except ValueError as exc:
        return {
            "final_state": "completed",
            "message": f"I cannot watch that: {exc}. Pick a field this station publishes.",
            "artifact": {"dataset": DATASET, "station": station, "observing": True,
                         "observation": observation},
            "watch": None,
        }
    unit = observation["units"].get(field) or ""
    watch = {
        "kind": SKILL_BUOY_WATCH,
        "dataset": DATASET,
        "station": station,
        "label": label,
        "field": field,
        "threshold": threshold,
        "units": client.units,
        "observed": observed,
    }
    artifact = {
        "dataset": DATASET,
        "source": "NOAA National Data Buoy Center latest observations",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current": observation,
    }
    current = observation.get(field)
    state = (f"Right now {LABELS[field]} at {station} is {_number(current, unit)}, "
             + ("at or above your threshold." if observed["above"] else "below your threshold."))
    message = (
        f"Watching {LABELS[field]} at {label} for {threshold:g} {unit or 'the station units'} or more. "
        f"{state}\nPoint a pushNotificationConfig at this task and I will POST when the reading "
        f"crosses your line (in either direction). The station is read on every poll, but only "
        f"crossings are sent, so the ordinary 10-minute wobble will not page you."
    )
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class BuoyAgent(SkillAgent):
    name = "buoys"
    card_name = "NDBC Buoy Agent"
    card_description = (
        "Read-only agent over NOAA's National Data Buoy Center: the newest observation at any of the "
        "1,354 catalogued stations (wind, gusts, wave height, dominant period, sea and air "
        "temperature, pressure and tendency, visibility, tide), the nearest reporting station to a "
        "coastal city or point, a station's recent trend with per-field lows and highs, catalogue "
        "search by name/owner/programme/sensors, and a crossing watch that POSTs to your webhook. "
        "Missing sensors and stale readings are stated, never smoothed over."
    )
    env_prefix = "BUOY"
    datasets = (DATASET,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_BUOY_WATCH,)

    def __init__(self, client: BuoyClient | None = None) -> None:
        self.client = client or BuoyClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_BUOYS_LIST:
            return []
        if not (params.get("station") or params.get("point") or params.get("place")):
            return ["location"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return ("Which station? Give me an NDBC id (like 41025 or SANF1), a coastal city from this "
                "server's list (Miami, Cape Hatteras, Key West, Honolulu, Seattle), or a "
                "latitude,longitude point.")

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        skill, params = request["skill"], request["params"]
        if skill == SKILL_BUOY_TREND:
            return run_buoy_trend(params, self.client)
        if skill == SKILL_BUOYS_NEAR:
            return run_buoys_near(params, self.client)
        if skill == SKILL_BUOYS_LIST:
            return run_buoys_list(params, self.client)
        if skill == SKILL_BUOY_WATCH:
            return run_buoy_watch(params, self.client)
        return run_buoy_conditions(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        """None means "nothing to compare this round": a sensor dropout must not page anyone."""
        if watch.get("kind") != SKILL_BUOY_WATCH:
            return None
        station = watch.get("station")
        field = watch.get("field")
        if not station or not field:
            return None
        try:
            return self.client.watch_state(str(station), str(field), float(watch["threshold"]))
        except ValueError:
            return None

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = previous or {}
        after = observed or {}
        field = str(watch.get("field") or DEFAULT_WATCH_FIELD)
        label = LABELS.get(field, field)
        unit = self.client.wind_unit() if field in ("wind_speed", "wind_gust") else (
            self.client.distance_unit() if field == "wave_height" else self.client.temperature_unit())
        where = watch.get("label") or f"station {watch.get('station')}"
        threshold = float(watch.get("threshold", 0))
        if after.get("above") and not before.get("above"):
            return f"{label.capitalize()} at {where} is now at or above your threshold of {threshold:g} {unit}"
        if before.get("above") and not after.get("above"):
            return f"{label.capitalize()} at {where} dropped back below your threshold of {threshold:g} {unit}"
        return f"The watched {label} at {where} crossed your threshold of {threshold:g} {unit}"


__all__ = [
    "CARD_SKILLS",
    "DEFAULT_RADIUS_MILES",
    "DEFAULT_TREND_HOURS",
    "DEFAULT_WATCH_FIELD",
    "DEFAULT_WATCH_THRESHOLDS",
    "MAX_LISTED_STATIONS",
    "SKILL_BUOY_CONDITIONS",
    "SKILL_BUOY_TREND",
    "SKILL_BUOY_WATCH",
    "SKILL_BUOYS_LIST",
    "SKILL_BUOYS_NEAR",
    "STALE_AFTER_MINUTES",
    "WATCH_FIELDS",
    "BuoyAgent",
    "age_minutes",
    "field_from_text",
    "hours_from_text",
    "parse",
    "place_from_text",
    "query_from_text",
    "radius_from_text",
    "resolve_target",
    "run_buoy_conditions",
    "run_buoy_trend",
    "run_buoy_watch",
    "run_buoys_list",
    "run_buoys_near",
    "station_from_text",
    "threshold_from_text",
    "unknown_place_from_text",
]
