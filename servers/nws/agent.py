"""The NWS weather-alerts agent: three skills over api.weather.gov.

  - alerts-active:  active watches/warnings/advisories, most severe first
  - alerts-summary: authoritative live counts (national and per state)
  - alerts-watch:   watch a state, zone or point and get POSTed when a new alert appears

Watch-capable: alerts-watch stores {"kind": "alerts-watch", area/zone/point} with the
current alert id set, and the server POSTs when that set changes.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET,
    NWSAlertsClient,
    PRODUCT,
    SEVERITY_ORDER,
    STATE_CODES,
    STATE_NAMES,
    UpstreamError,
)

SKILL_ALERTS_ACTIVE = "alerts-active"
SKILL_ALERTS_SUMMARY = "alerts-summary"
SKILL_ALERTS_WATCH = "alerts-watch"

MAX_WATCH_IDS = 25

CARD_SKILLS = [
    {
        "id": SKILL_ALERTS_ACTIVE,
        "name": "Active weather alerts",
        "description": (
            "Active National Weather Service watches, warnings and advisories for a state, an NWS "
            "zone, or a latitude/longitude point, most severe first. Includes headline, timing, area "
            "description and the safety instruction."
        ),
        "tags": ["weather", "nws", "alerts", "warnings", "safety"],
        "examples": [
            "Any weather alerts in NY right now?",
            '{"skill": "alerts-active", "state": "NY", "severity": "Severe"}',
            '{"skill": "alerts-active", "point": "40.71,-74.01"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_ALERTS_SUMMARY,
        "name": "Active alert counts",
        "description": (
            "Live National Weather Service counts: how many alerts are active across the country "
            "(split into land and marine) and how many are active in a given state."
        ),
        "tags": ["weather", "nws", "alerts", "counts", "summary"],
        "examples": [
            "How many weather alerts are active nationwide?",
            '{"skill": "alerts-summary", "state": "TX"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_ALERTS_WATCH,
        "name": "Watch a place for new alerts",
        "description": (
            "Watch a state, NWS zone or point and have the server call your webhook when a new "
            "weather alert is issued there (or an existing one clears)."
        ),
        "tags": ["weather", "nws", "alerts", "watch", "webhook"],
        "examples": [
            "Tell me when a new alert is issued for Miami.",
            '{"skill": "alerts-watch", "state": "FL", "event_contains": "Hurricane"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_ZONE_RE = re.compile(r"\b([A-Z]{3}\d{3})\b")
# "alerts in ny" (lowercase) still means New York; a bare "ny" elsewhere does not.
_LOWER_CODE_RE = re.compile(r"\b(?:in|for|across|state of)\s+([a-z]{2})\b")
#: Two-letter codes that are also ordinary English words. "in or out" is not Oregon.
_AMBIGUOUS_LOWER_CODES = frozenset(
    {"al", "as", "de", "hi", "id", "in", "la", "ma", "me", "mi", "mo", "ms", "ne", "oh", "ok", "or", "pa"}
)
_POINT_RE = re.compile(r"\b(-?\d{1,2}(?:\.\d+)?,\s?-?\d{1,3}(?:\.\d+)?)\b")
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert me|tell me when|let me know|subscribe|ping)\b", re.IGNORECASE)
_SUMMARY_WORDS = re.compile(r"\b(how many|count|counts|summary|nationwide|total|statewide)\b", re.IGNORECASE)
_SEVERITY_WORDS = {value.lower(): value for value in SEVERITY_ORDER}

EVENT_KEYWORDS = {
    "tornado": "Tornado",
    "flood": "Flood",
    "flash flood": "Flash Flood",
    "thunderstorm": "Thunderstorm",
    "heat": "Heat",
    "wind": "Wind",
    "winter": "Winter",
    "snow": "Snow",
    "ice": "Ice",
    "storm": "Storm",
    "hurricane": "Hurricane",
    "tropical": "Tropical",
    "fire": "Fire",
    "freeze": "Freeze",
    "frost": "Frost",
    "fog": "Fog",
    "dust": "Dust",
    "rip current": "Rip Current",
    "beach": "Beach",
    "small craft": "Small Craft",
    "gale": "Gale",
    "air quality": "Air Quality",
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


def state_from_text(text: str) -> str | None:
    """A state code in UPPERCASE ('NY'), a lowercase code right after a preposition ('in ny'),
    or a full state name ('California').

    Bare lowercase two-letter words are never states, so 'ok' in 'ok thanks' is not Oklahoma.
    """
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


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    area = _first(data, "state", "area", "state_code")
    zone = _first(data, "zone", "zone_id")
    point = _first(data, "point", "lat_lon")
    severity = _first(data, "severity")
    event_contains = _first(data, "event", "event_contains")
    limit = _first(data, "limit", default=10)

    if not zone:
        match = _ZONE_RE.search(text)
        if match:
            zone = match.group(1)
    if not point:
        match = _POINT_RE.search(text)
        if match:
            point = re.sub(r"\s+", "", match.group(1))
    if not area:
        area = state_from_text(text)
    if not severity:
        lowered = text.lower()
        for word, value in _SEVERITY_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                severity = value
                break
    if not event_contains:
        lowered = text.lower()
        for keyword, canonical in EVENT_KEYWORDS.items():
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                event_contains = canonical
                break

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_summary = bool(_SUMMARY_WORDS.search(text))
    if requested in (SKILL_ALERTS_ACTIVE, SKILL_ALERTS_SUMMARY, SKILL_ALERTS_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_ALERTS_WATCH
    elif asked_for_summary:
        skill = SKILL_ALERTS_SUMMARY
    else:
        skill = SKILL_ALERTS_ACTIVE

    params: dict = {"limit": limit}
    if area:
        params["area"] = str(area).upper()
    if zone:
        params["zone"] = str(zone).upper()
    if point:
        params["point"] = str(point)
    if severity:
        params["severity"] = severity
    if event_contains:
        params["event_contains"] = event_contains
    explicit = bool(requested or asked_for_watch or asked_for_summary)
    return {"skill": skill, "params": params, "explicit": explicit}


def _observation(alerts: list[dict]) -> dict:
    """Watch observation: the id set plus the newest headline, so 'new alert' is provable."""
    if not alerts:
        return {"count": 0, "ids": [], "newest_id": "", "newest_headline": "", "newest_event": ""}
    by_id = {alert.get("id") or "": alert for alert in alerts}
    newest_id = max(by_id, key=lambda key: (by_id[key].get("onset") or "", key))
    newest = by_id[newest_id]
    return {
        "count": len(alerts),
        "ids": sorted(by_id)[:MAX_WATCH_IDS],
        "newest_id": newest_id,
        "newest_headline": (newest.get("headline") or "")[:200],
        "newest_event": newest.get("event") or "",
    }


def run_alerts_active(params: dict, client: NWSAlertsClient) -> dict:
    alerts = client.active_alerts(
        area=params.get("area"),
        zone=params.get("zone"),
        point=params.get("point"),
        severity=params.get("severity"),
        event_contains=params.get("event_contains"),
        limit=int(params.get("limit", 10)),
    )
    place = client.place_label(params.get("area"), params.get("zone"), params.get("point"))
    artifact = {
        "dataset": DATASET,
        "product": PRODUCT,
        "freshness": client.freshness(),
        "source": "National Weather Service",
        "place": place,
        "count": len(alerts),
        "alerts": alerts,
    }
    if not alerts:
        return {
            "final_state": "completed",
            "message": f"No active NWS alerts match for {place} right now. (Read live from api.weather.gov.)",
            "artifact": artifact,
            "watch": None,
        }
    by_severity: dict[str, int] = {}
    for alert in alerts:
        key = alert.get("severity") or "Unknown"
        by_severity[key] = by_severity.get(key, 0) + 1
    breakdown = ", ".join(f"{name} {count}" for name, count in by_severity.items())
    listing = "\n".join(f"  • {alert.get('headline') or alert.get('event')}" for alert in alerts[:3])
    return {
        "final_state": "completed",
        "message": (
            f"{len(alerts)} active NWS alert(s) for {place} ({breakdown}):\n{listing}\n"
            f"Read live from api.weather.gov."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_alerts_summary(params: dict, client: NWSAlertsClient) -> dict:
    counts = client.counts()
    area = params.get("area")
    artifact = {
        "dataset": DATASET,
        "product": f"{PRODUCT} (national counts)",
        "freshness": client.freshness(),
        "source": "National Weather Service",
        "counts": counts,
        "area": area,
    }
    if area:
        in_area = int(counts["areas"].get(area, 0))
        ranked = sorted(counts["areas"].items(), key=lambda item: -item[1])[:5]
        top = ", ".join(f"{code} {value}" for code, value in ranked)
        artifact["area_count"] = in_area
        return {
            "final_state": "completed",
            "message": (
                f"NWS has {counts['total']} active alerts nationwide ({counts['land']} land, "
                f"{counts['marine']} marine). {NWSAlertsClient.place_label(area)} has {in_area}. "
                f"Busiest areas right now: {top}."
            ),
            "artifact": artifact,
            "watch": None,
        }
    ranked = sorted(counts["areas"].items(), key=lambda item: -item[1])[:5]
    top = ", ".join(f"{code} {value}" for code, value in ranked)
    return {
        "final_state": "completed",
        "message": (
            f"{counts['total']} active NWS alerts nationwide ({counts['land']} land, "
            f"{counts['marine']} marine). Busiest areas right now: {top}."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_alerts_watch(params: dict, client: NWSAlertsClient) -> dict:
    alerts = client.active_alerts(
        area=params.get("area"),
        zone=params.get("zone"),
        point=params.get("point"),
        event_contains=params.get("event_contains"),
        limit=100,
    )
    place = client.place_label(params.get("area"), params.get("zone"), params.get("point"))
    observed = _observation(alerts)
    watch = {
        "kind": SKILL_ALERTS_WATCH,
        "dataset": DATASET,
        "place": place,
        "observed": observed,
    }
    for key in ("area", "zone", "point", "event_contains"):
        if params.get(key):
            watch[key] = params[key]
    artifact = {
        "dataset": DATASET,
        "product": PRODUCT,
        "freshness": client.freshness(),
        "source": "National Weather Service",
        "place": place,
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current_alerts": alerts[:5],
        "current_count": observed["count"],
    }
    if observed["count"]:
        return {
            "final_state": "completed",
            "message": (
                f"Watching {place} for weather alerts. Right now there are {observed['count']} active, "
                f"most recent: {observed['newest_headline'] or observed['newest_event']}. Point a "
                "pushNotificationConfig at this task and I will POST when the alert set changes."
            ),
            "artifact": artifact,
            "watch": watch,
        }
    return {
        "final_state": "completed",
        "message": (
            f"Watching {place} for weather alerts. Nothing active there right now. Point a "
            "pushNotificationConfig at this task and I will POST when an alert is issued."
        ),
        "artifact": artifact,
        "watch": watch,
    }


class NWSAlertsAgent(SkillAgent):
    name = "nws-alerts"
    card_name = "NWS Weather Alerts Agent"
    card_description = (
        "Read-only agent over the National Weather Service alerts feed (api.weather.gov): active "
        "watches, warnings and advisories by state, NWS zone or latitude/longitude point, live "
        "national and per-state counts, and a watch skill that POSTs to your webhook when a new "
        "alert is issued for a place. Every answer is read live from the NWS API, and says so."
    )
    env_prefix = "NWS"
    datasets = ("api.weather.gov/alerts/active",)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_ALERTS_WATCH,)

    def __init__(self, client: NWSAlertsClient | None = None) -> None:
        self.client = client or NWSAlertsClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_ALERTS_WATCH:
            has_place = any(params.get(key) for key in ("area", "zone", "point"))
            return [] if has_place else ["location"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return (
            "Which place should I watch? Give me a state (name or two-letter code like NY), an NWS "
            "zone id (like NYZ072), or a latitude/longitude point."
        )

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if request["skill"] == SKILL_ALERTS_SUMMARY:
            return run_alerts_summary(request["params"], self.client)
        if request["skill"] == SKILL_ALERTS_WATCH:
            return run_alerts_watch(request["params"], self.client)
        return run_alerts_active(request["params"], self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if not any(watch.get(key) for key in ("area", "zone", "point")):
            return None
        alerts = self.client.active_alerts(
            area=watch.get("area"),
            zone=watch.get("zone"),
            point=watch.get("point"),
            event_contains=watch.get("event_contains"),
            limit=100,
        )
        return _observation(alerts)

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        place = watch.get("place") or NWSAlertsClient.place_label(
            watch.get("area"), watch.get("zone"), watch.get("point")
        )
        before = previous or {}
        after = observed or {}
        headline = after.get("newest_headline") or after.get("newest_event") or "an alert"
        if after.get("count", 0) > before.get("count", 0):
            return f"New NWS alert for {place}: {headline}"
        if after.get("count", 0) < before.get("count", 0):
            return f"NWS alerts for {place} are clearing: {after.get('count', 0)} active now (was {before.get('count', 0)})."
        return f"NWS alerts for {place} changed: now {after.get('count', 0)} active. Latest: {headline}"


__all__ = [
    "CARD_SKILLS",
    "EVENT_KEYWORDS",
    "NWSAlertsAgent",
    "SKILL_ALERTS_ACTIVE",
    "SKILL_ALERTS_SUMMARY",
    "SKILL_ALERTS_WATCH",
    "UpstreamError",
    "message_data",
    "message_text",
    "parse",
    "run_alerts_active",
    "run_alerts_summary",
    "run_alerts_watch",
    "state_from_text",
]
