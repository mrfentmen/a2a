"""The USGS earthquake agent: four skills over the live global catalog.

  - quakes-recent:  recent earthquakes worldwide, filtered by magnitude and time window
  - quakes-near:    earthquakes within a radius of a point (or a known metro)
  - quakes-summary: live counts by magnitude band (24h M2.5+/M4.5+, 7d M6.0+)
  - quakes-watch:   watch a place or a worldwide magnitude threshold, POST on a new quake

Watch-capable: quakes-watch stores {"kind": "quakes-watch", min_magnitude, point/radius}
with the current event-id list, so a new quake changes the observation and fires a push.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET,
    MAGNITUDE_BANDS,
    PRODUCT,
    USGSQuakeClient,
    UpstreamError,
)

SKILL_QUAKES_RECENT = "quakes-recent"
SKILL_QUAKES_NEAR = "quakes-near"
SKILL_QUAKES_SUMMARY = "quakes-summary"
SKILL_QUAKES_WATCH = "quakes-watch"

#: How many newest event ids a watch keeps, to detect a new quake.
MAX_WATCH_IDS = 25
#: A watch only pings for quakes at or above this magnitude unless the caller says otherwise.
DEFAULT_WATCH_MAGNITUDE = 4.0
DEFAULT_WATCH_HOURS = 168
DEFAULT_RADIUS_KM = 300

#: Approximate metro centres, so "quakes near Tokyo" works without coordinates.
CITY_POINTS = {
    "anchorage": (61.22, -149.90),
    "athens": (37.98, 23.73),
    "christchurch": (-43.53, 172.64),
    "honolulu": (21.31, -157.86),
    "istanbul": (41.01, 28.98),
    "jakarta": (-6.21, 106.85),
    "kathmandu": (27.72, 85.32),
    "lima": (-12.05, -77.04),
    "los angeles": (34.05, -118.24),
    "manila": (14.60, 120.98),
    "mexico city": (19.43, -99.13),
    "new york": (40.71, -74.01),
    "osaka": (34.69, 135.50),
    "port-au-prince": (18.54, -72.34),
    "quito": (-0.18, -78.47),
    "reykjavik": (64.15, -21.94),
    "san francisco": (37.77, -122.42),
    "san salvador": (13.69, -89.19),
    "santiago": (-33.45, -70.67),
    "seattle": (47.61, -122.33),
    "taipei": (25.03, 121.57),
    "tehran": (35.69, 51.39),
    "tokyo": (35.68, 139.69),
    "wellington": (-41.29, 174.78),
}

CARD_SKILLS = [
    {
        "id": SKILL_QUAKES_RECENT,
        "name": "Recent earthquakes",
        "description": (
            "Earthquakes recorded by the USGS in a time window, worldwide, filtered by minimum "
            "magnitude and sorted newest first. Each event carries magnitude, place, depth, "
            "tsunami flag, felt reports and the USGS event page URL."
        ),
        "tags": ["earthquake", "usgs", "seismic", "hazard", "realtime"],
        "examples": [
            "Any earthquakes above magnitude 4.5 in the last 24 hours?",
            '{"skill": "quakes-recent", "min_magnitude": 5, "hours": 168, "limit": 20}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_QUAKES_NEAR,
        "name": "Earthquakes near a place",
        "description": (
            "Earthquakes within a radius of a latitude/longitude point, or of a known metro area "
            "such as Tokyo, Lima, Istanbul or San Francisco. Answers 'has anything shaken near me'."
        ),
        "tags": ["earthquake", "usgs", "location", "radius", "hazard"],
        "examples": [
            "any quakes near Tokyo this month?",
            '{"skill": "quakes-near", "point": "35.68,139.69", "radius_km": 300, "days": 30}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_QUAKES_SUMMARY,
        "name": "Live earthquake counts",
        "description": (
            "How much the ground has moved lately: how many quakes the USGS catalogued worldwide "
            "in the last 24 hours at magnitude 2.5+ and 4.5+, and in the last 7 days at 6.0+, plus "
            "the largest recent event."
        ),
        "tags": ["earthquake", "usgs", "counts", "summary", "seismic"],
        "examples": [
            "How many earthquakes today?",
            '{"skill": "quakes-summary"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_QUAKES_WATCH,
        "name": "Watch for new earthquakes",
        "description": (
            "Watch a place (point or metro, with a radius) or a worldwide magnitude threshold, and "
            "have the server POST to your webhook when a new qualifying earthquake appears."
        ),
        "tags": ["earthquake", "usgs", "watch", "webhook", "alert"],
        "examples": [
            "tell me when there is a quake near Tokyo",
            '{"skill": "quakes-watch", "min_magnitude": 5.5}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?)\b")
_MAG_RE = re.compile(r"\b(?:m|mag|magnitude)\s?(\d{1,2}(?:\.\d+)?)\b")
_MAG_PLUS_RE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*(?:\+|or\s+(?:higher|greater|above|more|bigger)\b)")
_WINDOW_RE = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s*(hours?|days?|weeks?|months?|years?)\b")
_RADIUS_RE = re.compile(r"\b(\d{1,5})\s*(?:km|kms|kilometers?|kilometres?)\b")
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert me|tell me when|let me know|subscribe|ping)\b", re.IGNORECASE)
_SUMMARY_WORDS = re.compile(r"\b(how many|count|counts|total|summary|overview)\b", re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near|around|within|close to|nearby|near me)\b", re.IGNORECASE)

_NAMED_WINDOWS = {
    "today": 24,
    "past week": 168,
    "last week": 168,
    "this week": 168,
    "past month": 720,
    "last month": 720,
    "this month": 720,
    "past year": 8760,
    "last year": 8760,
}


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


def detect_city(text_lower: str) -> str | None:
    """Longest matching metro name in the text, so 'new york' beats 'york'."""
    for name in sorted(CITY_POINTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text_lower):
            return name
    return None


def magnitude_from_text(text: str) -> float | None:
    lowered = text.lower()
    match = _MAG_RE.search(lowered) or _MAG_PLUS_RE.search(lowered)
    if not match:
        return None
    try:
        return USGSQuakeClient.check_magnitude(match.group(1))
    except ValueError:
        return None


def window_from_text(text: str) -> float | None:
    lowered = text.lower()
    match = _WINDOW_RE.search(lowered)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        if unit.startswith("hour"):
            hours = amount
        elif unit.startswith("day"):
            hours = amount * 24
        elif unit.startswith("week"):
            hours = amount * 168
        elif unit.startswith("month"):
            hours = amount * 720
        else:
            hours = amount * 8760
        return USGSQuakeClient.check_hours(hours)
    for phrase, hours in _NAMED_WINDOWS.items():
        if phrase in lowered:
            return float(hours)
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    lowered = text.lower()
    requested = _first(data, "skill", "skill_id")

    min_magnitude = _first(data, "min_magnitude", "magnitude", "minmag")
    hours = _first(data, "hours", "days")
    limit = _first(data, "limit", default=10)
    radius = _first(data, "radius_km", "radius")
    point = _first(data, "point", "lat_lon")
    place = _first(data, "place", "city")

    # `days` is accepted on the wire but the client speaks hours.
    if hours is not None and data.get("days") not in (None, "") and _first(data, "hours") in (None, ""):
        try:
            hours = float(hours) * 24
        except (TypeError, ValueError):
            hours = None

    if min_magnitude is None:
        min_magnitude = magnitude_from_text(text)
    if not point:
        match = _POINT_RE.search(text)
        if match:
            point = re.sub(r"\s+", "", match.group(1))
    if not point and not place:
        city = detect_city(lowered)
        if city:
            place = city
            latitude, longitude = CITY_POINTS[city]
            point = f"{latitude},{longitude}"
    if radius is None:
        match = _RADIUS_RE.search(lowered)
        if match:
            radius = match.group(1)
    if radius is None and point:
        radius = DEFAULT_RADIUS_KM
    if hours is None:
        hours = window_from_text(text)
    if hours is None:
        hours = 720 if point else 24

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_summary = bool(_SUMMARY_WORDS.search(text))
    asked_for_near = bool(_NEAR_WORDS.search(text)) or bool(point) or bool(place)
    if requested in (SKILL_QUAKES_RECENT, SKILL_QUAKES_NEAR, SKILL_QUAKES_SUMMARY, SKILL_QUAKES_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_QUAKES_WATCH
    elif asked_for_summary:
        skill = SKILL_QUAKES_SUMMARY
    elif asked_for_near:
        skill = SKILL_QUAKES_NEAR
    else:
        skill = SKILL_QUAKES_RECENT

    params: dict = {"hours": hours, "limit": limit}
    if min_magnitude is not None:
        params["min_magnitude"] = min_magnitude
    if point:
        params["point"] = str(point)
    if place:
        params["place"] = str(place)
    if radius is not None:
        params["radius_km"] = radius
    explicit = bool(requested or asked_for_watch or asked_for_summary)
    return {"skill": skill, "params": params, "explicit": explicit}


def _observation(quakes: list[dict]) -> dict:
    """Watch observation: the newest event plus the newest id list (newest first)."""
    if not quakes:
        return {
            "count": 0,
            "newest_id": "",
            "newest_time": "",
            "newest_magnitude": None,
            "newest_place": "",
            "ids": [],
        }
    newest = quakes[0]
    return {
        "count": len(quakes),
        "newest_id": newest.get("id") or "",
        "newest_time": newest.get("time") or "",
        "newest_magnitude": newest.get("magnitude"),
        "newest_place": newest.get("place") or "",
        "ids": [quake.get("id") or "" for quake in quakes[:MAX_WATCH_IDS]],
    }


def _place_label(params: dict) -> str:
    if params.get("place"):
        return str(params["place"])
    if params.get("point"):
        return f"the point {params['point']}"
    return "worldwide"


def _line(quake: dict) -> str:
    magnitude = quake.get("magnitude")
    magnitude_text = f"M{magnitude}" if magnitude is not None else "M?"
    depth = quake.get("depth_km")
    depth_text = f" at {depth} km depth" if depth is not None else ""
    return f"  • {magnitude_text} — {quake.get('place') or 'unknown place'}{depth_text} ({quake.get('time') or 'no time'})"


def run_recent(params: dict, client: USGSQuakeClient) -> dict:
    min_magnitude = float(params.get("min_magnitude", 2.5))
    hours = float(params.get("hours", 24))
    quakes = client.recent_quakes(min_magnitude=min_magnitude, hours=hours, limit=int(params.get("limit", 10)))
    artifact = {
        "dataset": DATASET,
        "product": PRODUCT,
        "freshness": client.freshness(),
        "source": "U.S. Geological Survey",
        "min_magnitude": min_magnitude,
        "window_hours": hours,
        "count": len(quakes),
        "quakes": quakes,
    }
    if not quakes:
        return {
            "final_state": "completed",
            "message": (
                f"No M{min_magnitude}+ earthquakes recorded worldwide in the last {hours:g} hours "
                "according to the USGS catalog."
            ),
            "artifact": artifact,
            "watch": None,
        }
    listing = "\n".join(_line(quake) for quake in quakes[:3])
    return {
        "final_state": "completed",
        "message": (
            f"{len(quakes)} M{min_magnitude}+ earthquake(s) worldwide in the last {hours:g} hours "
            f"(USGS catalog, read live):\n{listing}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_near(params: dict, client: USGSQuakeClient) -> dict:
    min_magnitude = float(params.get("min_magnitude", 1.0))
    hours = float(params.get("hours", 720))
    radius = float(params.get("radius_km", DEFAULT_RADIUS_KM))
    quakes = client.quakes_near(
        point=params["point"],
        radius_km=radius,
        min_magnitude=min_magnitude,
        hours=hours,
        limit=int(params.get("limit", 10)),
    )
    label = _place_label(params)
    artifact = {
        "dataset": DATASET,
        "product": PRODUCT,
        "freshness": client.freshness(),
        "source": "U.S. Geological Survey",
        "place": label,
        "point": params["point"],
        "radius_km": radius,
        "min_magnitude": min_magnitude,
        "window_hours": hours,
        "count": len(quakes),
        "quakes": quakes,
    }
    if not quakes:
        return {
            "final_state": "completed",
            "message": (
                f"No M{min_magnitude}+ earthquakes within {radius:g} km of {label} in the last "
                f"{hours:g} hours. (USGS catalog, read live.)"
            ),
            "artifact": artifact,
            "watch": None,
        }
    biggest = max(quakes, key=lambda quake: (quake.get("magnitude") if quake.get("magnitude") is not None else -99))
    listing = "\n".join(_line(quake) for quake in quakes[:3])
    return {
        "final_state": "completed",
        "message": (
            f"{len(quakes)} M{min_magnitude}+ earthquake(s) within {radius:g} km of {label} in the last "
            f"{hours:g} hours; largest M{biggest.get('magnitude')} "
            f"({biggest.get('place')}). (USGS catalog, read live.)\n{listing}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_summary(params: dict, client: USGSQuakeClient) -> dict:
    counts = client.counts()
    biggest = client.recent_quakes(min_magnitude=MAGNITUDE_BANDS[1], hours=24, limit=1)
    artifact = {
        "dataset": DATASET,
        "product": f"{PRODUCT} (counts)",
        "freshness": client.freshness(),
        "source": "U.S. Geological Survey",
        "counts": counts,
        "bands": list(MAGNITUDE_BANDS),
        "largest_last_24h": biggest[0] if biggest else None,
    }
    message = (
        f"In the last 24 hours the USGS catalog recorded {counts['last_24h_m2.5']} earthquakes at M2.5+ "
        f"worldwide, {counts['last_24h_m4.5']} of them at M4.5+. Over the last 7 days: "
        f"{counts['last_7d_m6.0']} at M6.0+."
    )
    if biggest:
        message += (
            f" Largest in the last 24 hours: M{biggest[0].get('magnitude')} — {biggest[0].get('place')}."
        )
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": None}


def run_watch(params: dict, client: USGSQuakeClient) -> dict:
    radius = float(params.get("radius_km", DEFAULT_RADIUS_KM))
    hours = float(params.get("hours", DEFAULT_WATCH_HOURS))
    threshold = float(params.get("min_magnitude", DEFAULT_WATCH_MAGNITUDE))
    if params.get("point"):
        quakes = client.quakes_near(
            point=params["point"], radius_km=radius, min_magnitude=threshold, hours=hours, limit=100
        )
    else:
        quakes = client.recent_quakes(min_magnitude=threshold, hours=hours, limit=100)
    label = _place_label(params)
    observed = _observation(quakes)
    watch = {
        "kind": SKILL_QUAKES_WATCH,
        "dataset": DATASET,
        "place": label,
        "min_magnitude": threshold,
        "window_hours": hours,
        "observed": observed,
    }
    if params.get("point"):
        watch["point"] = params["point"]
        watch["radius_km"] = radius
    artifact = {
        "dataset": DATASET,
        "product": PRODUCT,
        "freshness": client.freshness(),
        "source": "U.S. Geological Survey",
        "place": label,
        "min_magnitude": threshold,
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current_count": observed["count"],
        "current_quakes": quakes[:5],
    }
    scope = f"within {radius:g} km of {label}" if params.get("point") else "worldwide"
    if observed["count"]:
        latest = (
            f" Newest: M{observed['newest_magnitude']} — {observed['newest_place']} ({observed['newest_time']})."
            if observed["newest_id"]
            else ""
        )
        return {
            "final_state": "completed",
            "message": (
                f"Watching {scope} for M{threshold}+ earthquakes. {observed['count']} match in the last "
                f"{hours:g} hours.{latest} Point a pushNotificationConfig at this task and I will POST "
                "when a new one appears."
            ),
            "artifact": artifact,
            "watch": watch,
        }
    return {
        "final_state": "completed",
        "message": (
            f"Watching {scope} for M{threshold}+ earthquakes. Nothing matching in the last {hours:g} hours. "
            "Point a pushNotificationConfig at this task and I will POST when one happens."
        ),
        "artifact": artifact,
        "watch": watch,
    }


class USGSQuakeAgent(SkillAgent):
    name = "usgs-quakes"
    card_name = "USGS Earthquake Agent"
    card_description = (
        "Read-only agent over the live USGS earthquake catalog: recent earthquakes worldwide by "
        "magnitude and time window, quakes inside a radius of a point or known metro area, live "
        "catalog counts, and a watch skill that POSTs to your webhook when a new qualifying "
        "earthquake is recorded. Every answer is read live from the USGS FDSN event service."
    )
    env_prefix = "USGS"
    datasets = ("earthquake.usgs.gov/fdsnws/event/1",)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_QUAKES_WATCH,)

    def __init__(self, client: USGSQuakeClient | None = None) -> None:
        self.client = client or USGSQuakeClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_QUAKES_NEAR and not params.get("point"):
            return ["location"]
        if skill == SKILL_QUAKES_WATCH and not params.get("point") and params.get("min_magnitude") is None:
            return ["location or magnitude"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_QUAKES_NEAR:
            return (
                "Where? Give me a latitude/longitude pair like 35.68,139.69, or a metro area I know "
                "such as Tokyo, Lima, Istanbul, San Francisco, Mexico City or Wellington."
            )
        return (
            "What should I watch? Give me a place (coordinates or a metro area) and I will ping you "
            "for M4.0+ quakes within 300 km, or tell me a worldwide magnitude threshold like "
            "'magnitude 5.5'."
        )

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if request["skill"] == SKILL_QUAKES_SUMMARY:
            return run_summary(request["params"], self.client)
        if request["skill"] == SKILL_QUAKES_WATCH:
            return run_watch(request["params"], self.client)
        if request["skill"] == SKILL_QUAKES_NEAR:
            return run_near(request["params"], self.client)
        return run_recent(request["params"], self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        threshold = float(watch.get("min_magnitude", DEFAULT_WATCH_MAGNITUDE))
        hours = float(watch.get("window_hours", DEFAULT_WATCH_HOURS))
        if watch.get("point"):
            quakes = self.client.quakes_near(
                point=watch["point"],
                radius_km=float(watch.get("radius_km", DEFAULT_RADIUS_KM)),
                min_magnitude=threshold,
                hours=hours,
                limit=100,
            )
        else:
            quakes = self.client.recent_quakes(min_magnitude=threshold, hours=hours, limit=100)
        return _observation(quakes)

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = previous or {}
        after = observed or {}
        place = watch.get("place") or "worldwide"
        magnitude = after.get("newest_magnitude")
        magnitude_text = f"M{magnitude}" if magnitude is not None else "an earthquake"
        newest_time = after.get("newest_time") or ""
        previous_time = before.get("newest_time") or ""
        if newest_time and newest_time != previous_time:
            return (
                f"New earthquake for {place}: {magnitude_text} — {after.get('newest_place') or 'unknown place'} "
                f"({newest_time})."
            )
        if after.get("count", 0) < before.get("count", 0):
            return (
                f"Earthquakes for {place} are leaving the window: {after.get('count', 0)} match now "
                f"(was {before.get('count', 0)})."
            )
        return f"Earthquake set for {place} changed: {after.get('count', 0)} match now."


__all__ = [
    "CARD_SKILLS",
    "CITY_POINTS",
    "DEFAULT_RADIUS_KM",
    "DEFAULT_WATCH_HOURS",
    "DEFAULT_WATCH_MAGNITUDE",
    "SKILL_QUAKES_NEAR",
    "SKILL_QUAKES_RECENT",
    "SKILL_QUAKES_SUMMARY",
    "SKILL_QUAKES_WATCH",
    "UpstreamError",
    "USGSQuakeAgent",
    "detect_city",
    "magnitude_from_text",
    "message_data",
    "message_text",
    "parse",
    "run_near",
    "run_recent",
    "run_summary",
    "run_watch",
    "window_from_text",
]
