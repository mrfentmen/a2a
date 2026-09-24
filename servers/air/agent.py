"""The air-quality agent: four skills over Open-Meteo's air-quality model.

  - air-now:      current US AQI, pollutants, UV and pollen at a point or known place
  - air-forecast: hourly US AQI from now to N hours (max 72), with the window's peak
  - air-ranking:  compare several places in one request and rank them worst-first
  - air-watch:    watch a place's AQI band and threshold crossing, POST to your webhook

Watch-capable: air-watch stores {"kind": "air-watch", point/place, threshold, observed}
where observed is {"aqi", "band", "above", "time"} reduced by the client to the band and
the threshold decision. The raw AQI moves every hour, so watching it would fire constantly;
bands and crossings are what a watcher actually wants to hear about.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    CITY_COORDS,
    DATASET,
    DEFAULT_RANKING,
    LABELS,
    MAX_PLACES,
    AirQualityClient,
)

SKILL_AIR_NOW = "air-now"
SKILL_AIR_FORECAST = "air-forecast"
SKILL_AIR_RANKING = "air-ranking"
SKILL_AIR_WATCH = "air-watch"

DEFAULT_FORECAST_HOURS = 24
DEFAULT_WATCH_AQI = 100.0
MAX_LISTED_PLACES = 10

CARD_SKILLS = [
    {
        "id": SKILL_AIR_NOW,
        "name": "Air quality right now",
        "description": (
            "Current air quality at a latitude/longitude point or a known place: US AQI with "
            "its EPA band and what that band means, PM2.5, PM10, ozone, nitrogen and sulphur "
            "dioxide, carbon monoxide, UV index, and pollen where the model publishes it."
        ),
        "tags": ["air", "air quality", "aqi", "pm2.5", "smoke", "pollen"],
        "examples": [
            "How bad is the air in Delhi right now?",
            '{"skill": "air-now", "point": "39.74,-104.99"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIR_FORECAST,
        "name": "Air quality forecast",
        "description": (
            "Hourly US AQI and PM2.5 from the current hour forward for up to 72 hours, with "
            "the peak hour in the window and the cleanest hour. Model output, read live."
        ),
        "tags": ["air", "air quality", "aqi", "forecast", "outlook"],
        "examples": [
            "What will the air quality be like in Denver for the next 48 hours?",
            '{"skill": "air-forecast", "place": "Los Angeles", "hours": 48}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIR_RANKING,
        "name": "Compare places by AQI",
        "description": (
            "Read the current US AQI for several places in one request and rank them worst "
            "first. Defaults to a built-in set of major cities when the caller names none."
        ),
        "tags": ["air", "air quality", "aqi", "compare", "ranking", "cities"],
        "examples": [
            "Which of Denver, Phoenix and Los Angeles has the worst air right now?",
            '{"skill": "air-ranking", "places": ["Denver", "Delhi", "Beijing"]}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIR_WATCH,
        "name": "Watch air quality",
        "description": (
            "Watch a point or place and have the server POST to your webhook when the US AQI "
            "band changes (for example Moderate to Unhealthy for Sensitive Groups) or when it "
            "crosses the threshold you set (default 100)."
        ),
        "tags": ["air", "air quality", "aqi", "watch", "webhook", "alert"],
        "examples": [
            "Tell me when the air quality in Denver passes 150.",
            '{"skill": "air-watch", "place": "Sacramento", "threshold": 100}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_HOURS_RE = re.compile(r"\b(?:next|coming|following|in the next|for the next|for)\s+(\d{1,3})\s*(?:hours?|hrs?|h)\b",
                       re.IGNORECASE)
_BARE_HOURS_RE = re.compile(r"\b(\d{1,3})\s*(?:hours?|hrs?)\b", re.IGNORECASE)
_THRESHOLD_RE = re.compile(
    r"\b(?:above|over|exceeds?|exceeding|greater than|more than|higher than|at least|pass(?:es)?|reaches?|hits?)\s+"
    r"(\d{1,3}(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe|keep an eye)\b",
                          re.IGNORECASE)
_FORECAST_WORDS = re.compile(
    r"\b(forecast|outlook|tomorrow|tonight|later|next \d+ hours?|for the next|will be|going to be|hourly)\b",
    re.IGNORECASE,
)
_RANKING_WORDS = re.compile(
    r"\b(worst|best|cleanest|dirtiest|compare|comparison|ranking|rank|which cit|versus|vs\.?|better air)\b",
    re.IGNORECASE,
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


def place_from_text(text: str) -> str | None:
    """A known place from the built-in list. Longest names win ('san jose' before 'jose')."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


#: "...in Atlantis" with no match in the place list: a name the caller gave that this server
#: cannot resolve. Reported honestly instead of being treated as "no place given".
_UNKNOWN_PLACE_RE = re.compile(
    r"\b(?:in|at|near|off|around|for)\s+(?:the\s+|a\s+|an\s+)?"
    r"([A-Z][\w'\-]{2,20}(?:\s+[A-Z][\w'\-]{2,20})?)")
_IGNORED_PLACES = frozenset({"The", "Us", "USA", "My", "Here", "This", "It", "There", "Today",
                             "Tomorrow"})


def unknown_place_from_text(text: str) -> str | None:
    """A capitalised place name after a preposition that the built-in list does not have."""
    match = _UNKNOWN_PLACE_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip()
    if candidate.split()[0] in _IGNORED_PLACES:
        return None
    return candidate


def places_from_text(text: str) -> list[str]:
    """Every known place named in the text, in the order they appear (for air-ranking)."""
    found: list[tuple[int, str]] = []
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(name)}\b", text.lower())
        if match:
            found.append((match.start(), name))
    found.sort()
    ordered: list[str] = []
    for _, name in found:
        if not any(name in kept for kept in ordered):
            ordered.append(name)
    return ordered


def hours_from_text(text: str) -> int | None:
    match = _HOURS_RE.search(text) or _BARE_HOURS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
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
    return value if 0 <= value <= 500 else None


def _places_param(data: dict, text: str) -> list[str] | None:
    raw = _first(data, "places", "cities")
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(part).strip() for part in raw if str(part).strip()]
    found = places_from_text(text)
    return found or None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    point = _first(data, "point", "lat_lon")
    place = _first(data, "place", "city", "location")
    hours = _first(data, "hours", "next_hours")
    threshold = _first(data, "threshold", "aqi")
    places = _places_param(data, text)

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
    if threshold is None:
        threshold = threshold_from_text(text)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_forecast = bool(_FORECAST_WORDS.search(text))
    asked_for_ranking = bool(_RANKING_WORDS.search(text))

    known = (SKILL_AIR_NOW, SKILL_AIR_FORECAST, SKILL_AIR_RANKING, SKILL_AIR_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_AIR_WATCH
    elif asked_for_ranking or (places and not point and not place):
        skill = SKILL_AIR_RANKING
    elif asked_for_forecast:
        skill = SKILL_AIR_FORECAST
    else:
        skill = SKILL_AIR_NOW

    params: dict = {}
    if skill == SKILL_AIR_RANKING:
        chosen = places or list(DEFAULT_RANKING)
        params["places"] = chosen[:MAX_PLACES]
    else:
        if point:
            params["point"] = str(point)
        if place:
            params["place"] = str(place)
    if skill == SKILL_AIR_FORECAST:
        params["hours"] = hours if hours is not None else DEFAULT_FORECAST_HOURS
    if skill == SKILL_AIR_WATCH:
        params["threshold"] = threshold if threshold is not None else DEFAULT_WATCH_AQI
    explicit = bool(requested or asked_for_watch or asked_for_forecast or asked_for_ranking)
    return {"skill": skill, "params": params, "explicit": explicit}


def resolve_point(params: dict, client: AirQualityClient) -> tuple[float, float, str] | None:
    """A point parameter wins over a place name; unknown places are never guessed."""
    if params.get("point"):
        lat, lon = client.check_point(params["point"])
        return lat, lon, f"the point {lat},{lon}"
    if params.get("place"):
        found = client.city(params["place"])
        if found:
            lat, lon, label = found
            return lat, lon, label
    return None


def resolve_places(names: list[str], client: AirQualityClient) -> tuple[list[tuple[str, float, float]], list[str]]:
    """Split a list of names into resolved (label, lat, lon) triples and unknown names."""
    resolved: list[tuple[str, float, float]] = []
    unknown: list[str] = []
    for name in names[:MAX_PLACES]:
        text = " ".join(str(name).split())
        if not text:
            continue
        point = _POINT_RE.fullmatch(text)
        if point:
            lat, lon = client.check_point(text)
            resolved.append((f"{lat},{lon}", lat, lon))
            continue
        found = client.city(text)
        if found:
            resolved.append((found[2], found[0], found[1]))
        else:
            unknown.append(text)
    return resolved, unknown


def _value(value, unit: str = "") -> str:
    if value is None:
        return "not published"
    return f"{value:g}{(' ' + unit) if unit else ''}"


def _unit(units: dict, field: str) -> str:
    return str(units.get(field) or "")


def run_air_now(params: dict, client: AirQualityClient) -> dict:
    resolved = resolve_point(params, client)
    if not resolved:
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET, "place": params.get("place"), "known": False},
            "watch": None,
        }
    lat, lon, label = resolved
    read = client.now(lat, lon)
    units = read["units"]
    artifact = {
        "dataset": DATASET,
        "source": "Open-Meteo air-quality API (CAMS global forecast model)",
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "time": read["time"],
        "aqi": read["aqi"],
        "category": read["category"],
        "pollutants": read["pollutants"],
        "pollen": read["pollen"],
        "uv_index": read["uv_index"],
        "units": units,
    }
    lines = [
        f"Air quality at {label} ({lat},{lon}) for the hour beginning {read['time']} UTC: "
        f"US AQI {_value(read['aqi'])} — {read['category']}."
    ]
    pollutant_bits = []
    for field in ("pm2_5", "pm10", "ozone", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide"):
        value = read["pollutants"].get(field)
        if value is not None:
            pollutant_bits.append(f"{LABELS[field]} {value:g} {_unit(units, field)}".strip())
    if pollutant_bits:
        lines.append("  • " + ", ".join(pollutant_bits))
    if read["uv_index"] is not None:
        lines.append(f"  • UV index {read['uv_index']:g}")
    if read["pollen"]:
        pollen_bits = [f"{LABELS[field]} {value:g} {_unit(units, field)}".strip()
                       for field, value in read["pollen"].items() if value is not None]
        lines.append("  • Pollen: " + ", ".join(pollen_bits))
    else:
        lines.append("  • Pollen: this model publishes none for this location "
                     "(pollen coverage is Europe-only), so a blank here is not zero.")
    lines.append(f"  • What the band means: {read['guidance']}.")
    lines.append(
        f"\nRead live from Open-Meteo's air-quality API ({DATASET}), which serves a model grid "
        f"cell — not your rooftop monitor. Values update hourly, and the band names are the "
        f"standard U.S. EPA AQI categories for the US AQI value shown."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_air_forecast(params: dict, client: AirQualityClient) -> dict:
    resolved = resolve_point(params, client)
    if not resolved:
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET, "place": params.get("place"), "known": False},
            "watch": None,
        }
    lat, lon, label = resolved
    hours = client.check_hours(params.get("hours") or DEFAULT_FORECAST_HOURS)
    read = client.forecast(lat, lon, hours)
    artifact = {
        "dataset": DATASET,
        "source": "Open-Meteo air-quality API (CAMS global forecast model)",
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "hours": read["hours"],
        "first_hour": read["first_hour"],
        "last_hour": read["last_hour"],
        "peak": read["peak"],
        "cleanest": read["cleanest"],
        "units": read["units"],
        "series": read["series"],
    }
    lines = [
        f"Hourly air-quality outlook for {label} ({lat},{lon}), {read['hours']} hours from "
        f"{read['first_hour']} to {read['last_hour']} UTC:",
        f"  • Peak US AQI {_value(read['peak']['aqi'])} at {read['peak']['time']} "
        f"({read['peak']['category']}); cleanest hour {_value(read['cleanest']['aqi'])} AQI "
        f"at {read['cleanest']['time']}.",
    ]
    for row in read["series"][:12]:
        lines.append(f"      {row['time']}  AQI {_value(row['aqi'])}"
                     f"  PM2.5 {_value(row['pm2_5'])} {_unit(read['units'], 'pm2_5')}".rstrip())
    if read["hours"] > 12:
        lines.append(f"      ... {read['hours'] - 12} more hours are in the artifact.")
    lines.append(
        f"\nRead live from Open-Meteo's air-quality API ({DATASET}). This is hourly model output "
        f"on a grid cell, not a measurement and not a health advisory; smoke episodes can move "
        f"much faster than a forecast grid, so check local agency readings before you rely on it."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_air_ranking(params: dict, client: AirQualityClient) -> dict:
    names = params.get("places") or list(DEFAULT_RANKING)
    if isinstance(names, str):
        names = [part.strip() for part in names.split(",") if part.strip()]
    resolved, unknown = resolve_places(list(names), client)
    if not resolved:
        return {
            "final_state": "input-required",
            "message": (
                "None of those places are in this server's list. Give me coordinates (like "
                "39.74,-104.99) or a city name it knows, such as Denver, Delhi, Beijing or London."
            ),
            "artifact": {"dataset": DATASET, "unknown": unknown},
            "watch": None,
        }
    read = client.ranking(resolved)
    artifact = {
        "dataset": DATASET,
        "source": "Open-Meteo air-quality API (CAMS global forecast model)",
        "requested": len(names),
        "count": read["count"],
        "unknown": unknown,
        "time": read["places"][0]["time"] if read["places"] else None,
        "units": read["units"],
        "worst": read["worst"],
        "best": read["best"],
        "places": read["places"],
    }
    lines = [
        f"{read['count']} place(s) compared in one request, worst air first "
        f"(US AQI, hour beginning {artifact['time']} UTC):"
    ]
    for row in read["places"][:MAX_LISTED_PLACES]:
        lines.append(
            f"  • {row['place']} — US AQI {_value(row['aqi'])} ({row['category']}), "
            f"PM2.5 {_value(row['pm2_5'])} {_unit(read['units'], 'pm2_5')}".rstrip()
        )
    if read["count"] > MAX_LISTED_PLACES:
        lines.append(f"  • ... {read['count'] - MAX_LISTED_PLACES} more in the artifact.")
    if unknown:
        lines.append(f"  • Skipped (not in this server's place list): {', '.join(unknown)}.")
    lines.append(
        f"\nEvery row is the same model hour from Open-Meteo's air-quality API ({DATASET}); an AQI "
        f"gap smaller than about 10 points is not a real difference between cities."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_air_watch(params: dict, client: AirQualityClient) -> dict:
    resolved = resolve_point(params, client)
    if not resolved:
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET, "place": params.get("place"), "known": False},
            "watch": None,
        }
    lat, lon, label = resolved
    threshold = client.check_threshold(params.get("threshold") or DEFAULT_WATCH_AQI)
    current = client.now(lat, lon)
    observed = client.watch_state(lat, lon, threshold)
    watch = {
        "kind": SKILL_AIR_WATCH,
        "dataset": DATASET,
        "place": params.get("place"),
        "point": params.get("point"),
        "label": label,
        "latitude": lat,
        "longitude": lon,
        "threshold": threshold,
        "observed": observed,
    }
    artifact = {
        "dataset": DATASET,
        "source": "Open-Meteo air-quality API (CAMS global forecast model)",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current": {**current, "watch": observed},
    }
    if observed["above"]:
        state = (f"Right now it reads US AQI {_value(current['aqi'])} ({current['category']}), which "
                 f"is at or above your threshold.")
    else:
        state = (f"Right now it reads US AQI {_value(current['aqi'])} ({current['category']}), below "
                 f"your threshold.")
    message = (
        f"Watching air quality at {label} ({lat},{lon}) for a US AQI of {threshold:g} or more, and "
        f"for band changes. {state}\nPoint a pushNotificationConfig at this task and I will POST "
        f"when the reading crosses your line or the EPA band changes. The reading is checked on "
        f"every poll, but only crossings and band changes are sent, so the ordinary hourly wobble "
        f"inside a band will not page you — notifications name the band, not the raw number."
    )
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class AirQualityAgent(SkillAgent):
    name = "air"
    card_name = "Air Quality Agent"
    card_description = (
        "Read-only agent over Open-Meteo's keyless air-quality model: current US AQI with its EPA "
        "band and what it means, PM2.5, PM10, ozone, nitrogen and sulphur dioxide, carbon monoxide, "
        "UV and pollen at any point or known city; an hourly AQI outlook up to 72 hours; one-request "
        "comparisons across cities; and a watch that POSTs to your webhook when the AQI band changes "
        "or a threshold is crossed. Every answer says which model grid it came from."
    )
    env_prefix = "AIR"
    datasets = (DATASET,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_AIR_WATCH,)

    def __init__(self, client: AirQualityClient | None = None) -> None:
        self.client = client or AirQualityClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill != SKILL_AIR_RANKING and not (params.get("point") or params.get("place")):
            return ["location"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return (
            "Which place? Give me a latitude/longitude point (like 39.74,-104.99) or a city from "
            "this server's list, such as Denver, Los Angeles, New York, London, Delhi or Beijing."
        )

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        skill, params = request["skill"], request["params"]
        if skill == SKILL_AIR_FORECAST:
            return run_air_forecast(params, self.client)
        if skill == SKILL_AIR_RANKING:
            return run_air_ranking(params, self.client)
        if skill == SKILL_AIR_WATCH:
            return run_air_watch(params, self.client)
        return run_air_now(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if "threshold" not in watch:
            return None
        latitude, longitude = watch.get("latitude"), watch.get("longitude")
        if latitude is None or longitude is None:
            resolved = resolve_point(watch, self.client)
            if not resolved:
                return None
            latitude, longitude = resolved[0], resolved[1]
        return self.client.watch_state(latitude, longitude, float(watch["threshold"]))

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = previous or {}
        after = observed or {}
        label = watch.get("label") or watch.get("place") or watch.get("point") or "the watched place"
        threshold = float(watch.get("threshold", DEFAULT_WATCH_AQI))
        if after.get("above") and not before.get("above"):
            return (f"Air quality at {label} is now {after.get('band')}, at or above your threshold "
                    f"of {threshold:g} US AQI")
        if before.get("above") and not after.get("above"):
            return (f"Air quality at {label} dropped back below {threshold:g} US AQI: now "
                    f"{after.get('band')}")
        before_band = before.get("band")
        if not before_band:
            return (f"Air quality at {label} is now {after.get('band')}"
                    + (f", at or above your threshold of {threshold:g} US AQI" if after.get("above") else ""))
        if after.get("band") != before_band:
            return f"Air quality at {label} moved from {before_band} to {after.get('band')}"
        return (f"Air quality at {label} changed: now {after.get('band')}"
                f"{' (at or above your threshold)' if after.get('above') else ''}")


__all__ = [
    "CARD_SKILLS",
    "DEFAULT_FORECAST_HOURS",
    "DEFAULT_WATCH_AQI",
    "SKILL_AIR_FORECAST",
    "SKILL_AIR_NOW",
    "SKILL_AIR_RANKING",
    "SKILL_AIR_WATCH",
    "AirQualityAgent",
    "hours_from_text",
    "parse",
    "place_from_text",
    "places_from_text",
    "resolve_places",
    "unknown_place_from_text",
    "resolve_point",
    "run_air_forecast",
    "run_air_now",
    "run_air_ranking",
    "run_air_watch",
    "threshold_from_text",
]
