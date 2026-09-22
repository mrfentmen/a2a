"""The NOAA tides agent: five skills over the CO-OPS API.

  - tide-predictions: upcoming high/low tides for a station (or the nearest one)
  - tide-next:        the next high and the next low
  - water-level:      the latest observed water level, placed against flood stages
  - stations:         find tide stations by name, state, or distance from a point
  - tide-watch:       watch a station and get POSTed when the water crosses a threshold

Watch-capable: tide-watch stores the station and the threshold, and the observation
it polls is deliberately tiny (station + above/below), so a notification fires when
the water crosses the threshold rather than on every reading.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET,
    FLOOD_STAGES,
    STATIONS_DATASET,
    UNIT_LABELS,
    UNITS,
    NoaaTidesClient,
    UpstreamError,
    to_feet,
)

SKILL_TIDE_PREDICTIONS = "tide-predictions"
SKILL_TIDE_NEXT = "tide-next"
SKILL_WATER_LEVEL = "water-level"
SKILL_STATIONS = "stations"
SKILL_TIDE_WATCH = "tide-watch"

SOURCE = "NOAA Center for Operational Oceanographic Products and Services (CO-OPS)"
DEFAULT_RADIUS_KM = 50.0

CARD_SKILLS = [
    {
        "id": SKILL_TIDE_PREDICTIONS,
        "name": "Tide predictions",
        "description": (
            "Upcoming high and low tides for a NOAA tide station, in station local time, from the "
            "current observation forward. Give a seven-digit station id, 'lat,lon' for the nearest "
            "station, or a station name."
        ),
        "tags": ["tides", "noaa", "ocean", "predictions", "tide-table"],
        "examples": [
            "what are the tides at station 8518750 today?",
            "tide table for the next 3 days near 40.7006,-74.0142",
            '{"skill": "tide-predictions", "station_name": "The Battery", "days": 2}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_TIDE_NEXT,
        "name": "Next high and low tide",
        "description": "The next high tide and the next low tide for a NOAA tide station, with heights.",
        "tags": ["tides", "noaa", "ocean", "next"],
        "examples": [
            "when is the next high tide at 8518750?",
            "next low tide near 40.7006,-74.0142",
            '{"skill": "tide-next", "station": "8518750"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_WATER_LEVEL,
        "name": "Observed water level",
        "description": (
            "The latest observed water level at a station, with observation time, quality flag, and "
            "where it sits against that station's published flood stages (action, minor, moderate, major)."
        ),
        "tags": ["tides", "noaa", "water-level", "flood", "observation"],
        "examples": [
            "what is the water level at The Battery right now?",
            "how high is the water at 8518750?",
            '{"skill": "water-level", "station": "8518750"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_STATIONS,
        "name": "Find tide stations",
        "description": (
            "Search the NOAA tide-station catalogue by name, by two-letter state code, or by distance "
            "from a latitude/longitude point. 3499 tide-prediction stations worldwide (US coasts and "
            "territories)."
        ),
        "tags": ["tides", "noaa", "stations", "search"],
        "examples": [
            "which tide stations are near 40.7,-74.0?",
            "find tide stations named Battery",
            '{"skill": "stations", "state": "NY", "limit": 10}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_TIDE_WATCH,
        "name": "Watch a station for high water",
        "description": (
            "Watch a tide station and have the server call your webhook when the observed water level "
            "crosses a threshold — either a height you give, or one of NOAA's published flood stages "
            "(action, minor, moderate, major). Defaults to the station's minor flood stage."
        ),
        "tags": ["tides", "noaa", "watch", "flood", "webhook"],
        "examples": [
            "tell me when the water at The Battery hits minor flood stage",
            "notify me when 8518750 goes above 6 ft",
            '{"skill": "tide-watch", "station": "8518750", "flood_stage": "moderate"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_STATION_RE = re.compile(r"\b(\d{7})\b")
_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?,\s?-?\d{1,3}(?:\.\d+)?)\b")
#: "tides in New York", "station The Battery" — a short run of capitalised words.
_NAME_RE = re.compile(
    r"\b(?:at|in|for|near|station|tides?)\s+((?:[A-Z][A-Za-z'’.\-]+(?:\s+(?:[A-Z][A-Za-z'’.\-]+|of|the)){0,3}))"
)
_DAYS_RE = re.compile(r"\b(?:next|for|following)?\s*(\d{1,2})\s*days?\b", re.IGNORECASE)
_THRESHOLD_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:ft|feet|foot|m|meters?|metres?)\b", re.IGNORECASE)
_STAGE_RE = re.compile(
    rf"\b({'|'.join(FLOOD_STAGES)})\s+(?:flood|flooding|stage|water)\b", re.IGNORECASE
)
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert me|tell me when|let me know|subscribe|ping)\b", re.IGNORECASE)
#: "the next high tide", "upcoming low tide" — but not "the next 3 days".
_NEXT_WORDS = re.compile(r"\b(?:next|upcoming)\s+(?:high|low)?\s*tides?\b", re.IGNORECASE)
_TIDE_WORDS = re.compile(r"\b(tide|tides|tidal|high water|low water)\b", re.IGNORECASE)
_LEVEL_WORDS = re.compile(r"\b(water level|how high is the water|current level|level right now)\b", re.IGNORECASE)
_STATION_SEARCH_WORDS = re.compile(
    r"\b(which stations?|find (?:a |the )?stations?|stations? (?:near|in|by|named|called)|list stations?|tide stations?)\b",
    re.IGNORECASE,
)
_UNITS_HINT = {"metric": "metric", "meters": "metric", "metres": "metric",
               "english": "english", "feet": "english", "foot": "english", "ft": "english"}

HELP = (
    "I read NOAA's tide predictions and observed water levels (CO-OPS). Ask me:\n"
    "  • what are the tides at station 8518750 today?\n"
    "  • when is the next high tide at The Battery?\n"
    "  • how high is the water at 8518750 right now?\n"
    "  • which tide stations are near 40.7006,-74.0142?\n"
    "  • tell me when the water at The Battery hits minor flood stage\n"
    "Predictions are NOAA's, times are station local time, and I say which datum "
    "heights are measured from. I do not forecast storms."
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


def station_name_from_text(text: str, station: str | None) -> str | None:
    """A station name only when the text offers one — never the whole question."""
    if station:
        return None
    match = _NAME_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip(" .,'’")
    if len(candidate.split()) > 4 or len(candidate) < 3:
        return None
    if _TIDE_WORDS.fullmatch(candidate) or _WATCH_WORDS.fullmatch(candidate):
        return None
    return candidate


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    station = _first(data, "station", "station_id")
    if station is None:
        match = _STATION_RE.search(text)
        if match:
            station = match.group(1)
    point = _first(data, "point", "lat_lon")
    if point is None:
        match = _POINT_RE.search(text)
        if match:
            point = re.sub(r"\s+", "", match.group(1))
    station_name = _first(data, "station_name", "name")
    if station_name is None:
        station_name = station_name_from_text(text, station or point)
    state = _first(data, "state", "state_code")
    days = _first(data, "days")
    if days is None:
        match = _DAYS_RE.search(text)
        if match:
            days = match.group(1)
    threshold = _first(data, "threshold", "level")
    flood_stage = _first(data, "flood_stage", "stage")
    if threshold is None:
        match = _THRESHOLD_RE.search(text)
        if match:
            threshold = match.group(1)
    lowered = text.lower()
    if flood_stage is None:
        match = _STAGE_RE.search(text)
        if match:
            flood_stage = match.group(1).lower()
    units = _first(data, "units")
    if units is None:
        for word, value in _UNITS_HINT.items():
            if re.search(rf"\b{word}\b", lowered):
                units = value
                break
    radius_km = _first(data, "radius_km", "radius")
    limit = _first(data, "limit", default=10)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    if requested in (SKILL_TIDE_PREDICTIONS, SKILL_TIDE_NEXT, SKILL_WATER_LEVEL,
                     SKILL_STATIONS, SKILL_TIDE_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_TIDE_WATCH
    elif _STATION_SEARCH_WORDS.search(text):
        skill = SKILL_STATIONS
    elif _LEVEL_WORDS.search(text):
        skill = SKILL_WATER_LEVEL
    elif _TIDE_WORDS.search(text) and _NEXT_WORDS.search(text):
        skill = SKILL_TIDE_NEXT
    else:
        skill = SKILL_TIDE_PREDICTIONS

    # A station search is a place question, not a tide table: it does not carry a station.
    if skill == SKILL_STATIONS and station_name is None and station is None and point is None and not state:
        match = _NAME_RE.search(text)
        if match:
            station_name = match.group(1).strip(" .,'’")

    params: dict = {"limit": limit}
    for key, value in (("station", station), ("point", point), ("station_name", station_name),
                       ("state", state), ("days", days), ("threshold", threshold),
                       ("flood_stage", flood_stage), ("units", units), ("radius_km", radius_km)):
        if value is not None:
            params[key] = value
    if not requested and (not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE)):
        skill, params = "help", {}
    explicit = bool(requested or asked_for_watch or _STATION_SEARCH_WORDS.search(text)
                    or _LEVEL_WORDS.search(text))
    return {"skill": skill, "params": params, "explicit": explicit}


# -- shared shape ---------------------------------------------------------


def _artifact(params: dict, client: NoaaTidesClient, **extra) -> dict:
    artifact = {
        "dataset": DATASET,
        "product": "NOAA CO-OPS tide predictions and observed water levels",
        "source": SOURCE,
        "freshness": client.freshness(),
        "datum": client.datum,
    }
    artifact.update(extra)
    return artifact


def _missing_station(skill: str) -> dict:
    return {
        "final_state": "input-required",
        "message": (
            "Which tide station? Give me a seven-digit NOAA station id (for example 8518750, "
            "The Battery in New York), a latitude/longitude point such as 40.7006,-74.0142 for the "
            "nearest station, or a station name."
        ),
        "artifact": None,
        "watch": None,
    }


def _resolve_station(params: dict, client: NoaaTidesClient) -> tuple[str | None, str, dict | None]:
    """(station_id, name, problem_response) — problem is a finished response body."""
    if params.get("station"):
        return client.check_station(params["station"]), "", None
    if params.get("point"):
        radius = params.get("radius_km", DEFAULT_RADIUS_KM)
        found = client.nearest_station(params["point"], radius)
        if not found:
            return None, "", {
                "final_state": "completed",
                "message": (
                    f"No NOAA tide station within {float(radius):g} km of {params['point']}. Try a "
                    "larger radius, or ask which stations are near that point."
                ),
                "artifact": _artifact(params, client, point=params["point"], station=None),
                "watch": None,
            }
        return found["id"], found.get("name") or "", None
    if params.get("station_name"):
        name = params["station_name"]
        matches = client.stations(name=name, state=params.get("state"), limit=10)
        exact = [row for row in matches if (row.get("name") or "").lower() == name.lower()]
        if len(exact) == 1:
            return exact[0]["id"], exact[0]["name"], None
        if len(matches) == 1:
            return matches[0]["id"], matches[0]["name"], None
        if not matches:
            return None, "", {
                "final_state": "completed",
                "message": (
                    f"No NOAA tide station matches “{name}”. Ask which tide stations are near a "
                    "point, or give the seven-digit station id."
                ),
                "artifact": _artifact(params, client, station_name=name, station=None),
                "watch": None,
            }
        listing = "\n".join(
            f"  • {row['id']} — {row.get('name')}"
            + (f", {row['state']}" if row.get("state") else "")
            + (f" ({row['distance_km']} km away)" if "distance_km" in row else "")
            for row in matches[:5]
        )
        return None, "", {
            "final_state": "input-required",
            "message": f"“{name}” matches more than one station. Which one?\n{listing}",
            "artifact": _artifact(params, client, station_name=name, candidates=matches[:5]),
            "watch": None,
        }
    return None, "", None


# -- skills ---------------------------------------------------------------


def run_tide_predictions(params: dict, client: NoaaTidesClient) -> dict:
    station, name, problem = _resolve_station(params, client)
    if problem:
        return problem
    days = client.check_days(params.get("days", 2))
    table = client.upcoming_tides(station, days=days, limit=12)
    place = table["place"] or name or station
    artifact = _artifact(params, client, station=station, place=place, days=days,
                         events=table["events"], total_events=table["total_events"],
                         observed_at=table["observed_at"])
    if not table["events"]:
        return {
            "final_state": "completed",
            "message": (
                f"NOAA published no high/low predictions for {place} in the next {days} day(s). "
                f"(Read live from {DATASET}.)"
            ),
            "artifact": artifact,
            "watch": None,
        }
    unit_label = UNIT_LABELS[client.units]
    listing = "\n".join(
        f"  • {event['time']}  {'high' if event['type'] == 'high' else 'low':<4} "
        f"{event['value']:.3f} {unit_label}" + ("" if event["type"] else " (intermediate)")
        for event in table["events"][:8]
    )
    return {
        "final_state": "completed",
        "message": (
            f"Tides at {place}, station local time, {client.datum} datum, from "
            f"{table['observed_at'] or 'now'}:\n{listing}\n"
            f"Source: {DATASET} (read live). NOAA's predictions, not my own."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_tide_next(params: dict, client: NoaaTidesClient) -> dict:
    station, name, problem = _resolve_station(params, client)
    if problem:
        return problem
    table = client.next_tides(station)
    place = table["place"] or name or station
    unit_label = UNIT_LABELS[client.units]
    artifact = _artifact(params, client, station=station, place=place,
                         next_high=table["next_high"], next_low=table["next_low"],
                         observed_at=table["observed_at"])
    lines = []
    for label, event in (("Next high tide", table["next_high"]), ("Next low tide", table["next_low"])):
        if event:
            lines.append(f"  • {label}: {event['time']} at {event['value']:.3f} {unit_label}")
        else:
            lines.append(f"  • {label}: not published in the next 48 hours")
    return {
        "final_state": "completed",
        "message": (
            f"{place}, station local time ({client.datum} datum):\n" + "\n".join(lines) +
            f"\nSource: {DATASET} (read live)."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_water_level(params: dict, client: NoaaTidesClient) -> dict:
    station, name, problem = _resolve_station(params, client)
    if problem:
        return problem
    observation = client.latest_observation(station)
    if not observation:
        return {
            "final_state": "completed",
            "message": (
                f"NOAA has no recent water-level observation for station {station}. Some stations "
                f"report predictions only. (Read live from {DATASET}.)"
            ),
            "artifact": _artifact(params, client, station=station, observation=None),
            "watch": None,
        }
    stage = client.flood_stage_for_value(station, observation["value"])
    levels = client.flood_levels(station)
    unit_label = UNIT_LABELS[client.units]
    artifact = _artifact(params, client, station=station, place=observation["name"] or name or station,
                         observation=observation, flood_stage=stage, flood_levels=levels)
    flood_line = ""
    if levels.get("nos_minor") or levels.get("nws_minor"):
        flood_line = (
            f" NOAA's minor flood stage here is "
            f"{(levels.get('nos_minor') or levels.get('nws_minor')):g} ft over MLLW."
        )
    return {
        "final_state": "completed",
        "message": (
            f"{observation['name'] or station} was at {observation['value']:.3f} {unit_label} over "
            f"{client.datum} at {observation['time']} station local time — {stage}.{flood_line}\n"
            f"Observation quality: {observation['quality_label']}. Source: {DATASET} (read live)."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_stations(params: dict, client: NoaaTidesClient) -> dict:
    matches = client.stations(
        name=params.get("station_name"),
        state=params.get("state"),
        point=params.get("point"),
        radius_km=params.get("radius_km", DEFAULT_RADIUS_KM),
        limit=params.get("limit", 10),
    )
    artifact = _artifact(params, client, station_matches=matches, count=len(matches))
    artifact["dataset"] = STATIONS_DATASET
    if not matches:
        return {
            "final_state": "completed",
            "message": (
                "No NOAA tide station matches that. The catalogue holds 3499 tide-prediction "
                "stations; try a state code (NY) or a point like 40.7,-74.0."
            ),
            "artifact": artifact,
            "watch": None,
        }
    listing = "\n".join(
        f"  • {row['id']} — {row.get('name')}"
        + (f", {row['state']}" if row.get("state") else "")
        + (f" — {row['distance_km']} km away" if "distance_km" in row else "")
        for row in matches[:10]
    )
    return {
        "final_state": "completed",
        "message": f"{len(matches)} NOAA tide station(s):\n{listing}\nSource: {STATIONS_DATASET} (read live).",
        "artifact": artifact,
        "watch": None,
    }


def run_tide_watch(params: dict, client: NoaaTidesClient) -> dict:
    station, name, problem = _resolve_station(params, client)
    if problem:
        return problem
    units = client.check_units(params.get("units", client.units))
    stage = client.check_flood_stage(params["flood_stage"]) if params.get("flood_stage") else None
    threshold = client.check_threshold(params["threshold"]) if params.get("threshold") is not None else None
    source = "the threshold you gave"
    if threshold is None:
        wanted = stage or "minor"
        threshold = client.threshold_for_stage(station, wanted, units)
        if threshold is None:
            return {
                "final_state": "completed",
                "message": (
                    f"NOAA publishes no {wanted} flood stage for station {station}, so I need a "
                    f"number instead: tell me a height in {UNIT_LABELS[units]}, for example "
                    f"“notify me when {station} goes above 6 ft”."
                ),
                "artifact": _artifact(params, client, station=station, watching=None),
                "watch": None,
            }
        stage = wanted
        source = f"NOAA's {wanted} flood stage for this station"

    observation = client.latest_observation(station)
    place = (observation or {}).get("name") or name or f"station {station}"
    threshold_ft = round(to_feet(threshold, units), 3)
    above = False
    reading = "no recent observation"
    if observation:
        # The reading is in the server's units, the threshold in the caller's: compare in feet.
        above = to_feet(observation["value"], client.units) >= threshold_ft
        reading = (
            f"latest reading {observation['value']:.3f} {UNIT_LABELS[units]} at "
            f"{observation['time']} station local time"
        )
    watch = {
        "kind": SKILL_TIDE_WATCH,
        "dataset": DATASET,
        "station": station,
        "station_name": place,
        "threshold": threshold,
        "threshold_ft": threshold_ft,
        "threshold_source": source,
        "units": units,
        "flood_stage": stage,
        "observed": {"station": station, "threshold_ft": threshold_ft, "above": above},
    }
    artifact = _artifact(
        params, client, station=station, place=place, threshold=threshold, units=units,
        threshold_ft=threshold_ft, threshold_source=source, flood_stage=stage,
        currently_above=above, observation=observation,
        watching={key: value for key, value in watch.items() if key != "observed"},
    )
    state = "at or above" if above else "below"
    return {
        "final_state": "completed",
        "message": (
            f"Watching {place} against {threshold:g} {UNIT_LABELS[units]} ({source}) — it is {state} "
            f"that level right now ({reading}). Point a pushNotificationConfig at this task and I "
            "will POST when the water crosses it."
        ),
        "artifact": artifact,
        "watch": watch,
    }


class NoaaTidesAgent(SkillAgent):
    name = "noaa-tides"
    card_name = "NOAA Tides Agent"
    card_description = (
        "Read-only agent over NOAA CO-OPS tide data: high/low tide predictions, the next high and "
        "low, the latest observed water level placed against published flood stages, tide-station "
        "lookup by name/state/point, and a watch skill that POSTs to your webhook when the water "
        "crosses a height or flood stage. Every answer is read live from NOAA and names the datum."
    )
    env_prefix = "NOAA_TIDES"
    datasets = (DATASET, STATIONS_DATASET)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_TIDE_WATCH,)

    def __init__(self, client: NoaaTidesClient | None = None) -> None:
        self.client = client or NoaaTidesClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_STATIONS:
            has_place = any(params.get(key) for key in ("station_name", "state", "point"))
            return [] if has_place else ["place"]
        if skill in (SKILL_TIDE_PREDICTIONS, SKILL_TIDE_NEXT, SKILL_WATER_LEVEL, SKILL_TIDE_WATCH):
            has_place = any(params.get(key) for key in ("station", "point", "station_name"))
            return [] if has_place else ["station"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_STATIONS:
            return (
                "Which stations are you after? Give me a station name, a two-letter state code such "
                "as NY, or a point like 40.7,-74.0."
            )
        return _missing_station(skill)["message"]

    def run(self, request: dict) -> dict:
        skill = request["skill"]
        params = request["params"]
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(skill, request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if skill == SKILL_TIDE_PREDICTIONS:
            return run_tide_predictions(params, self.client)
        if skill == SKILL_TIDE_NEXT:
            return run_tide_next(params, self.client)
        if skill == SKILL_WATER_LEVEL:
            return run_water_level(params, self.client)
        if skill == SKILL_STATIONS:
            return run_stations(params, self.client)
        if skill == SKILL_TIDE_WATCH:
            return run_tide_watch(params, self.client)
        return {"final_state": "completed", "message": HELP, "artifact": None, "watch": None}

    def probe_watch(self, watch: dict) -> dict | None:
        """The comparison itself: station, threshold, and whether the water is above it.

        Deliberately excludes the reading, so a notification fires when the water crosses
        the threshold rather than on every observation NOAA publishes.
        """
        station = watch.get("station")
        threshold_ft = watch.get("threshold_ft")
        if not station or threshold_ft is None:
            return None
        observation = self.client.latest_observation(station)
        if not observation:
            return None
        return {
            "station": station,
            "threshold_ft": threshold_ft,
            "above": to_feet(observation["value"], self.client.units) >= float(threshold_ft),
        }

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        place = watch.get("station_name") or f"station {watch.get('station')}"
        units = watch.get("units") or self.client.units
        label = UNIT_LABELS.get(units, units)
        threshold = watch.get("threshold")
        source = watch.get("threshold_source") or "your threshold"
        stage = watch.get("flood_stage")
        reading = ""
        try:
            observation = self.client.latest_observation(watch.get("station"))
        except UpstreamError:
            observation = None
        if observation:
            reading = (
                f" Latest reading: {observation['value']:.3f} {UNIT_LABELS.get(observation['units'], label)} "
                f"at {observation['time']} station local time."
            )
        if (observed or {}).get("above"):
            heading = f"{place} is at or above {source}"
            if stage:
                heading += f" ({stage})"
            return f"{heading} — {threshold:g} {label}.{reading}"
        return f"{place} has dropped back below {source} ({threshold:g} {label}).{reading}"


__all__ = [
    "CARD_SKILLS",
    "DEFAULT_RADIUS_KM",
    "HELP",
    "NoaaTidesAgent",
    "SKILL_STATIONS",
    "SKILL_TIDE_NEXT",
    "SKILL_TIDE_PREDICTIONS",
    "SKILL_TIDE_WATCH",
    "SKILL_WATER_LEVEL",
    "UpstreamError",
    "message_data",
    "message_text",
    "parse",
    "run_stations",
    "run_tide_next",
    "run_tide_predictions",
    "run_tide_watch",
    "run_water_level",
    "station_name_from_text",
]
