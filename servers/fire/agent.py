"""The wildfire agent: five skills over NIFC's interagency incident layer.

  - fire-active:  active wildfires, largest first, by state / size / containment
  - fire-near:    active wildfires within a radius of a point or a known city
  - fire-summary: national or per-state totals, acres and uncontained counts
  - fire-lookup:  find an incident by name and show its details
  - fire-watch:   watch a state for new large fires and get POSTed when they appear

Watch-capable: fire-watch stores {"kind": "fire-watch", state, min_acres, observed} where
observed is the set of incident ids that qualify plus their names. Those change only when
a fire appears on or leaves the active list, so the watcher fires on news rather than on
every acre a fire grows.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    CITY_COORDS,
    DATASET,
    STATE_CODES,
    STATE_NAMES,
    WildfireClient,
)

SKILL_FIRE_ACTIVE = "fire-active"
SKILL_FIRE_NEAR = "fire-near"
SKILL_FIRE_SUMMARY = "fire-summary"
SKILL_FIRE_LOOKUP = "fire-lookup"
SKILL_FIRE_WATCH = "fire-watch"

DEFAULT_RADIUS_MILES = 100.0
DEFAULT_WATCH_ACRES = 1000.0
MAX_WATCH_IDS = 50

CARD_SKILLS = [
    {
        "id": SKILL_FIRE_ACTIVE,
        "name": "Active wildfires",
        "description": (
            "Active wildfire incidents, largest first, optionally filtered by US state, minimum "
            "size in acres, and whether they are still largely uncontained. Fields come from the "
            "interagency WFIGS layer: size, percent contained, discovery time, cause, managing "
            "organization and coordinates."
        ),
        "tags": ["wildfire", "fire", "nifc", "wfigs", "acres"],
        "examples": [
            "What wildfires are burning in California right now?",
            '{"skill": "fire-active", "state": "CA", "min_acres": 1000}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FIRE_NEAR,
        "name": "Fires near a place",
        "description": (
            "Active wildfires within a radius of a latitude/longitude point or a known US city, "
            "nearest first, with the great-circle distance in miles for each one."
        ),
        "tags": ["wildfire", "fire", "nearby", "distance", "evacuation"],
        "examples": [
            "Any fires within 100 miles of Denver?",
            '{"skill": "fire-near", "point": "39.74,-104.99", "radius_miles": 150}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FIRE_SUMMARY,
        "name": "Wildfire totals",
        "description": (
            "Live totals for the country or one state: how many active incidents there are, the "
            "total acres they cover, how many are still less than half contained, and the states "
            "carrying the most fire."
        ),
        "tags": ["wildfire", "fire", "totals", "summary", "acres"],
        "examples": [
            "How much fire is burning in the country right now?",
            '{"skill": "fire-summary", "state": "OR"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FIRE_LOOKUP,
        "name": "Find an incident by name",
        "description": (
            "Search the active incident list by name (partial names work) and return the matching "
            "incidents with their size, containment, discovery time and location."
        ),
        "tags": ["wildfire", "fire", "incident", "search", "name"],
        "examples": [
            "Tell me about the Timber fire.",
            '{"skill": "fire-lookup", "name": "Plaskett"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FIRE_WATCH,
        "name": "Watch for new large fires",
        "description": (
            "Watch a state (or the whole country) and have the server call your webhook when a new "
            "incident at or above your acreage threshold appears on the active list, or when one "
            "leaves it."
        ),
        "tags": ["wildfire", "fire", "watch", "webhook", "alert"],
        "examples": [
            "Tell me when a new large fire starts in Oregon.",
            '{"skill": "fire-watch", "state": "OR", "min_acres": 5000}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
#: "5,000 acres" is a size, not a coordinate, so a point is never read straight off a size phrase.
_ACRES_AFTER_RE = re.compile(r"\s*acres?\b", re.IGNORECASE)
_ACRES_RE = re.compile(r"\b(?:over|above|at least|more than|greater than|bigger than|>=)?\s*([\d][\d,]*)\s*acres?\b",
                       re.IGNORECASE)
_RADIUS_RE = re.compile(r"\bwithin\s+([\d][\d,]*)\s*(?:mi|mile|miles)\b", re.IGNORECASE)
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe|keep an eye)\b",
                          re.IGNORECASE)
_SUMMARY_WORDS = re.compile(
    r"\b(how many|how much|how big|total|totals|summary|nationwide|country|overall|statewide)\b", re.IGNORECASE)
_UNCONTAINED_WORDS = re.compile(r"\b(uncontained|not contained|out of control|zero percent|0 percent)\b",
                                re.IGNORECASE)
_LOOKUP_WORDS = re.compile(r"\b(about|details?|tell me about|look ?up|search|find|named|called)\b", re.IGNORECASE)
_NEAR_WORDS = re.compile(r"\b(near me|nearby|close by|closest|around here|nearby me|within \d+)\b", re.IGNORECASE)
#: "near Gotham" names a place the built-in list may not know, and that must be refused
#: rather than silently answered with the national list. "near me" stays place-less.
_NEAR_PLACE_RE = re.compile(r"\b(?:near|around|close to)\s+(?!me\b)([A-Za-z][\w'\-]{1,30})", re.IGNORECASE)
_NAME_RE = re.compile(r"\b(?:about|on|called|named|search for|look ?up|find)\s+(?:the\s+)?([A-Za-z][\w'\- ]{1,40}?)\s+(?:fire|incident)s?\b",
                      re.IGNORECASE)
_CAPS_NAME_RE = re.compile(r"\b([A-Z][\w'\-]{2,})\s+(?:fire|incident)\b")
_LOWER_CODE_RE = re.compile(r"\b(?:in|for|across|state of|near)\s+([a-z]{2})\b")
#: Two-letter codes that are also ordinary English words. "in or out" is not Oregon.
_AMBIGUOUS_LOWER_CODES = frozenset(
    {"al", "as", "de", "hi", "id", "in", "la", "ma", "me", "mi", "mo", "ms", "ne", "oh", "ok", "or", "pa"}
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


def state_from_text(text: str) -> str | None:
    """A state code in UPPERCASE ('CA'), a lowercase code after a preposition ('in or'),
    or a full state name ('Oregon'). Bare lowercase words are never states."""
    match = re.search(r"\b([A-Z]{2})\b", text)
    if match and match.group(1) in STATE_CODES:
        return match.group(1)
    match = _LOWER_CODE_RE.search(text)
    if match and match.group(1) not in _AMBIGUOUS_LOWER_CODES and match.group(1).upper() in STATE_CODES:
        return match.group(1).upper()
    lowered = text.lower()
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return code
    return None


def place_from_text(text: str) -> str | None:
    """A US city from the built-in list. Longest names win."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


def acres_from_text(text: str) -> float | None:
    match = _ACRES_RE.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def radius_from_text(text: str) -> float | None:
    match = _RADIUS_RE.search(text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


#: Words that can sit in front of "fire" without naming one ("find the fire", "any big fire").
_NOT_A_NAME = frozenset({"the", "a", "an", "this", "that", "these", "those", "large", "big", "new",
                         "any", "some", "every", "another", "other", "next", "latest", "wild"})


def name_from_text(text: str) -> str | None:
    """'tell me about the Timber fire' -> Timber."""
    match = _NAME_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        candidate = re.sub(r"^(?:the|a|an)\s+", "", candidate, flags=re.IGNORECASE)
        if 2 <= len(candidate) <= 40 and candidate.lower() not in _NOT_A_NAME:
            return candidate
    match = _CAPS_NAME_RE.search(text)
    if match:
        return match.group(1)
    return None


def point_from_text(text: str) -> str | None:
    """A 'lat,lon' pair the caller spelled out — never the pieces of a thousands-separated size."""
    for match in _POINT_RE.finditer(text):
        latitude, longitude = match.group(1), match.group(2)
        if _ACRES_AFTER_RE.match(text[match.end():]):
            continue  # "over 5,000 acres"
        digits = longitude.lstrip("-")
        if len(digits) > 1 and digits.startswith("0"):
            continue  # "12,345" — a thousands group, not a longitude
        return f"{latitude},{longitude}"
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    point = _first(data, "point", "lat_lon")
    place = _first(data, "place", "city")
    state = _first(data, "state", "area")
    min_acres = _first(data, "min_acres", "acres")
    radius = _first(data, "radius_miles", "radius")
    name = _first(data, "name", "incident")
    contained_below = _first(data, "contained_below")
    limit = _first(data, "limit", default=10)

    if not point:
        point = point_from_text(text)
    if not place:
        place = place_from_text(text) or (match.group(1).lower() if (match := _NEAR_PLACE_RE.search(text)) else None)
    if not state:
        state = state_from_text(text)
    if min_acres is None:
        min_acres = acres_from_text(text)
    if radius is None:
        radius = radius_from_text(text)
    if not name:
        name = name_from_text(text)
    if contained_below is None and _UNCONTAINED_WORDS.search(text):
        contained_below = 50

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_summary = bool(_SUMMARY_WORDS.search(text))
    asked_for_lookup = bool(_LOOKUP_WORDS.search(text))

    known = (SKILL_FIRE_ACTIVE, SKILL_FIRE_NEAR, SKILL_FIRE_SUMMARY, SKILL_FIRE_LOOKUP, SKILL_FIRE_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_FIRE_WATCH
    elif name and asked_for_lookup:
        skill = SKILL_FIRE_LOOKUP
    elif point or place or _NEAR_WORDS.search(text):
        skill = SKILL_FIRE_NEAR
    elif asked_for_summary:
        skill = SKILL_FIRE_SUMMARY
    else:
        skill = SKILL_FIRE_ACTIVE

    params: dict = {"limit": limit}
    if skill == SKILL_FIRE_WATCH:
        if min_acres is None:
            min_acres = DEFAULT_WATCH_ACRES
        params["min_acres"] = min_acres
    elif min_acres is not None:
        params["min_acres"] = min_acres
    if skill == SKILL_FIRE_NEAR:
        params["radius_miles"] = radius if radius is not None else DEFAULT_RADIUS_MILES
        if point:
            params["point"] = str(point)
        if place:
            params["place"] = str(place)
    if state and skill in (SKILL_FIRE_ACTIVE, SKILL_FIRE_SUMMARY, SKILL_FIRE_WATCH):
        params["state"] = str(state)
    if contained_below is not None and skill == SKILL_FIRE_ACTIVE:
        params["contained_below"] = contained_below
    if name and skill == SKILL_FIRE_LOOKUP:
        params["name"] = str(name)
    explicit = bool(requested or asked_for_watch or asked_for_summary or asked_for_lookup)
    return {"skill": skill, "params": params, "explicit": explicit}


def resolve_point(params: dict, client: WildfireClient) -> tuple[float, float, str] | None:
    if params.get("point"):
        lat, lon = client.check_point(params["point"])
        return lat, lon, f"the point {lat},{lon}"
    if params.get("place"):
        found = WildfireClient.city(params["place"])
        if found:
            lat, lon, label = found
            return lat, lon, label
    return None


def _fire_line(row: dict, distance: bool = False) -> str:
    contained = row.get("percent_contained")
    containment = "containment not reported" if contained is None else f"{contained:g}% contained"
    place = ", ".join(part for part in (row.get("county"), row.get("state")) if part)
    line = (f"  • {row.get('name') or 'unnamed incident'} — {row.get('acres') or 0:,.0f} acres, {containment}"
            f"{f' ({place})' if place else ''}, discovered {row.get('discovered') or 'date not reported'}")
    if distance and row.get("distance_miles") is not None:
        line += f", {row['distance_miles']:,.1f} miles away"
    return line


def run_fire_active(params: dict, client: WildfireClient) -> dict:
    result = client.incidents(
        state=params.get("state"),
        min_acres=params.get("min_acres"),
        limit=int(params.get("limit") or 10),
        contained_below=params.get("contained_below"),
    )
    scope = f"{result['where']}" if result["where"] != "1=1" else "every active incident"
    artifact = {
        "dataset": DATASET,
        "source": "National Interagency Fire Center (WFIGS incident locations)",
        "scope": scope,
        "count": result["count"],
        "acres": result["acres"],
        "incidents": result["incidents"],
        "limit": int(params.get("limit") or 10),
    }
    if not result["count"]:
        return {
            "final_state": "completed",
            "message": f"No active wildfire matches {scope} in the interagency list right now.",
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{result['count']} active wildfire(s) matching {scope}, largest first "
             f"({result['acres']:,.0f} acres in this set):"]
    lines.extend(_fire_line(row) for row in result["incidents"][:8])
    lines.append(f"\nRead live from NIFC's WFIGS incident layer ({DATASET}). Coordinates for each fire "
                 f"are in the artifact. This lists what agencies have reported, not every fire burning.")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_fire_near(params: dict, client: WildfireClient) -> dict:
    resolved = resolve_point(params, client)
    if not resolved:
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET, "place": params.get("place"), "known": False},
            "watch": None,
        }
    lat, lon, label = resolved
    radius = float(params.get("radius_miles") or DEFAULT_RADIUS_MILES)
    result = client.near(lat, lon, radius_miles=radius, limit=int(params.get("limit") or 10),
                         min_acres=params.get("min_acres"))
    artifact = {
        "dataset": DATASET,
        "source": "National Interagency Fire Center (WFIGS incident locations)",
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "radius_miles": radius,
        "count": result["count"],
        "acres": result["acres"],
        "incidents": result["incidents"],
    }
    if not result["count"]:
        return {
            "final_state": "completed",
            "message": f"No active wildfire is listed within {radius:g} miles of {label} ({lat},{lon}) "
                       f"right now. (Read live from NIFC's interagency incident layer, {DATASET}.)",
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{result['count']} active wildfire(s) within {radius:g} miles of {label} ({lat},{lon}), nearest first:"]
    lines.extend(_fire_line(row, distance=True) for row in result["incidents"][:8])
    lines.append(f"\nDistances are great-circle miles from that point to the reported fire location, "
                 f"computed here from {DATASET}. Distance is not risk: smoke, wind and terrain decide "
                 f"that, and this is not an evacuation notice - follow local authorities.")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_fire_summary(params: dict, client: WildfireClient) -> dict:
    result = client.summary(state=params.get("state"))
    artifact = {
        "dataset": DATASET,
        "source": "National Interagency Fire Center (WFIGS incident locations)",
        "scope": result["scope"],
        "count": result["count"],
        "acres": result["acres"],
        "uncontained": result["uncontained"],
        "biggest": result["biggest"],
        "by_state": result["by_state"],
    }
    if result.get("page_full"):
        artifact["note"] = f"read the first {result['page_limit']} rows of the layer"
    lines = [
        f"{result['count']} active wildfire(s) on the interagency list for {result['scope']}, "
        f"covering {result['acres']:,.0f} acres.",
        f"  • Still under half contained: {result['uncontained']}",
    ]
    if result["biggest"]:
        lines.append("  • Largest fires:")
        for row in result["biggest"][:3]:
            lines.append(f"      {_fire_line(row).strip().removeprefix('• ')}")
    if result["by_state"]:
        ranked = ", ".join(f"{entry['state']} {entry['acres']:,.0f} acres ({entry['count']})"
                           for entry in result["by_state"][:5])
        lines.append(f"  • Most acres by state: {ranked}")
    lines.append(f"\nRead live from NIFC's WFIGS incident layer ({DATASET}). Reported acreage and "
                 "containment come from the managing agencies and are updated as they report, not "
                 "continuously.")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_fire_lookup(params: dict, client: WildfireClient) -> dict:
    name = params.get("name")
    if not name:
        return {
            "final_state": "input-required",
            "message": "Which incident? Give me the name, or part of it, as the agencies spell it.",
            "artifact": None,
            "watch": None,
        }
    result = client.lookup(name, limit=int(params.get("limit") or 5))
    artifact = {
        "dataset": DATASET,
        "source": "National Interagency Fire Center (WFIGS incident locations)",
        "query": result["query"],
        "count": result["count"],
        "incidents": result["incidents"],
    }
    if not result["count"]:
        return {
            "final_state": "completed",
            "message": f"No active incident whose name contains {result['query']!r} is on the "
                       f"interagency list right now. The list only holds currently active incidents.",
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{result['count']} active incident(s) matching {result['query']!r}:"]
    for row in result["incidents"]:
        lines.append(_fire_line(row))
        details = []
        if row.get("cause"):
            details.append(f"cause: {row['cause']}")
        if row.get("type_name"):
            details.append(f"type: {row['type_name']}")
        if row.get("management"):
            details.append(f"managing: {row['management']}")
        if row.get("gacc"):
            details.append(f"coordination center: {row['gacc']}")
        if details:
            lines.append(f"    {', '.join(details)}")
        if row.get("latitude") is not None:
            lines.append(f"    location: {row['latitude']},{row['longitude']} (last updated "
                         f"{row.get('last_updated') or 'unknown'})")
    lines.append(f"\nRead live from NIFC's WFIGS incident layer ({DATASET}).")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def _watch_key(row: dict) -> str:
    """A stable id per incident: NIFC's own id, or name+discovery time when it has none."""
    return str(row.get("id") or f"{row.get('name')}|{row.get('discovered')}")


def _observation(client: WildfireClient, state: str | None, min_acres: float) -> dict:
    """What a fire watch compares between polls: which incidents qualify, and their names."""
    rows = client.incidents(state=state, min_acres=min_acres, limit=100)["incidents"]
    names = {_watch_key(row): str(row.get("name") or "unnamed incident") for row in rows}
    return {"count": len(rows), "ids": sorted(names)[:MAX_WATCH_IDS], "names": names}


def run_fire_watch(params: dict, client: WildfireClient) -> dict:
    state = params.get("state")
    min_acres = float(params.get("min_acres") or DEFAULT_WATCH_ACRES)
    client.check_acres(min_acres, "min_acres")
    if state:
        state = client.check_state(state)
    observed = _observation(client, state, min_acres)
    where = f"{state} " if state else ""
    watch = {
        "kind": SKILL_FIRE_WATCH,
        "dataset": DATASET,
        "state": state,
        "min_acres": min_acres,
        "observed": observed,
    }
    artifact = {
        "dataset": DATASET,
        "source": "National Interagency Fire Center (WFIGS incident locations)",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current_count": observed["count"],
        "current": [_fire_line(row).strip() for row in
                    client.incidents(state=state, min_acres=min_acres, limit=5)["incidents"]],
    }
    message = (
        f"Watching {where}for incidents of {min_acres:,.0f} acres or more. Right now "
        f"{observed['count']} incident(s) on the active list meet that."
    )
    if observed["names"]:
        listed = ", ".join(sorted(observed["names"].values())[:5])
        message += f" They are: {listed}."
    message += ("\nPoint a pushNotificationConfig at this task and I will POST when a new incident "
                "appears on the list or one leaves it.")
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class WildfireAgent(SkillAgent):
    name = "wildfire"
    card_name = "Wildfire Incident Agent"
    card_description = (
        "Read-only agent over the interagency wildfire incident layer that NIFC publishes: active "
        "wildfires by state, size and containment; incidents within a radius of a latitude/longitude "
        "point or a known US city with real distances; national and per-state totals; lookup by "
        "incident name; and a watch skill that POSTs to your webhook when a new large fire appears "
        "in the state you are watching. Every answer is read live and names the layer it came from."
    )
    env_prefix = "FIRE"
    datasets = (DATASET,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_FIRE_WATCH,)

    def __init__(self, client: WildfireClient | None = None) -> None:
        self.client = client or WildfireClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_FIRE_NEAR and not (params.get("point") or params.get("place")):
            return ["location"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return (
            "Which place? Give me a latitude/longitude point (like 39.74,-104.99) or a city from "
            "this server's list, such as Denver, Missoula, Sacramento, Seattle, Austin or Flagstaff."
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
        if skill == SKILL_FIRE_NEAR:
            return run_fire_near(params, self.client)
        if skill == SKILL_FIRE_SUMMARY:
            return run_fire_summary(params, self.client)
        if skill == SKILL_FIRE_LOOKUP:
            return run_fire_lookup(params, self.client)
        if skill == SKILL_FIRE_WATCH:
            return run_fire_watch(params, self.client)
        return run_fire_active(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if "min_acres" not in watch:
            return None
        return _observation(self.client, watch.get("state"), float(watch["min_acres"]))

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = previous or {}
        after = observed or {}
        where = f" in {watch['state']}" if watch.get("state") else " nationwide"
        added = [key for key in after.get("ids", []) if key not in (before.get("ids") or [])]
        removed = [key for key in (before.get("ids") or []) if key not in after.get("ids", [])]
        if added:
            names = ", ".join(after.get("names", {}).get(key, key) for key in added[:3])
            return (f"New wildfire at or above {float(watch.get('min_acres', DEFAULT_WATCH_ACRES)):,.0f} acres"
                    f"{where}: {names}")
        if removed:
            names = ", ".join((before.get("names") or {}).get(key, key) for key in removed[:3])
            return (f"{len(removed)} incident(s) left the active list{where} (contained or handed back): "
                    f"{names}")
        return f"The wildfire list{where} changed: now {after.get('count', 0)} incident(s) qualify."


__all__ = [
    "CARD_SKILLS",
    "DEFAULT_RADIUS_MILES",
    "DEFAULT_WATCH_ACRES",
    "SKILL_FIRE_ACTIVE",
    "SKILL_FIRE_LOOKUP",
    "SKILL_FIRE_NEAR",
    "SKILL_FIRE_SUMMARY",
    "SKILL_FIRE_WATCH",
    "WildfireAgent",
    "acres_from_text",
    "name_from_text",
    "place_from_text",
    "radius_from_text",
    "run_fire_active",
    "run_fire_lookup",
    "run_fire_near",
    "run_fire_summary",
    "run_fire_watch",
    "state_from_text",
]
