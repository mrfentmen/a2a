"""The NYC street-flooding agent: three skills over the live FloodNet datasets.

  - flood-recent:  street-flooding events recorded in the last N hours
  - flood-sensors: the FloodNet sensors near a ZIP or borough
  - flood-watch:   watch sensors and push a notification on the next flood event

Watch-capable: flood-watch stores {"kind": "flood-events", sensor_ids: [...]} and
the server POSTs to your webhook when the newest event start time changes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from a2a_kit import SkillAgent

from data import (
    BOROUGHS,
    DATASET_FLOOD_EVENTS,
    DATASET_FLOOD_EVENTS_TITLE,
    DATASET_FLOOD_SENSORS,
    DATASET_FLOOD_SENSORS_TITLE,
    FloodClient,
    UpstreamError,
)

SKILL_FLOOD_RECENT = "flood-recent"
SKILL_FLOOD_SENSORS = "flood-sensors"
SKILL_FLOOD_WATCH = "flood-watch"

MAX_WATCH_SENSORS = 200

CARD_SKILLS = [
    {
        "id": SKILL_FLOOD_RECENT,
        "name": "Recent street flooding",
        "description": (
            "Street-flooding events recorded by FloodNet sensors in the last N hours, citywide or "
            "filtered by ZIP, borough, or sensor. Reports depth, duration, and drain time. The "
            "dataset records completed events, so each answer states the window it looked at."
        ),
        "tags": ["nyc", "flood", "floodnet", "climate", "sensors"],
        "examples": [
            "Which streets flooded in the last 24 hours?",
            '{"skill": "flood-recent", "zip_code": "11211", "hours": 168}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FLOOD_SENSORS,
        "name": "FloodNet sensors near a place",
        "description": (
            "The FloodNet street-flooding sensors deployed in a ZIP code or borough: sensor name, "
            "street, coordinates, and how deep the street has to get before that sensor reports."
        ),
        "tags": ["nyc", "flood", "floodnet", "sensors", "coverage"],
        "examples": [
            "Which flood sensors cover 11211?",
            '{"skill": "flood-sensors", "borough": "Queens"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_FLOOD_WATCH,
        "name": "Watch a street for flooding",
        "description": (
            "Watch FloodNet sensors (by sensor id, ZIP, or borough) and have the server call your "
            "webhook when a new street-flooding event is recorded on them."
        ),
        "tags": ["nyc", "flood", "floodnet", "watch", "webhook"],
        "examples": [
            "Tell me when any sensor in 11211 floods again.",
            '{"skill": "flood-watch", "sensor_id": "BK-richardson-st-n-11th-st-1x59w1"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_ZIP_RE = re.compile(r"\b(1[01]\d{3})\b")
_WINDOW_RE = re.compile(r"\blast\s+(\d{1,3})\s*(hour|hours|day|days|week|weeks)\b", re.IGNORECASE)
_WINDOW_HOURS = {"hour": 1, "hours": 1, "day": 24, "days": 24, "week": 168, "weeks": 168}
_SENSOR_ID_RE = re.compile(r"\b([A-Za-z]{2}-[A-Za-z0-9-]{4,80})\b")
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert|ping|tell me when|let me know|subscribe)\b", re.IGNORECASE)
_SENSOR_WORDS = re.compile(r"\b(sensors?|coverage|monitors?|stations?)\b", re.IGNORECASE)
_BOROUGH_WORDS = {
    "brooklyn": "Brooklyn",
    "queens": "Queens",
    "bronx": "Bronx",
    "manhattan": "Manhattan",
    "staten island": "Staten Island",
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


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    zip_code = _first(data, "zip_code", "zip", "zipcode")
    borough = _first(data, "borough")
    sensor_id = _first(data, "sensor_id", "sensor")
    hours = _first(data, "hours", "window_hours", default=72)
    limit = _first(data, "limit", default=20)

    if not zip_code:
        match = _ZIP_RE.search(text)
        if match:
            zip_code = match.group(1)
    if not borough:
        lowered = text.lower()
        for word, canonical in _BOROUGH_WORDS.items():
            if re.search(rf"\b{word}\b", lowered):
                borough = canonical
                break
    if not sensor_id:
        match = _SENSOR_ID_RE.search(text)
        if match:
            sensor_id = match.group(1)
    if "hours" not in data and "window_hours" not in data:
        match = _WINDOW_RE.search(text)
        if match:
            hours = int(match.group(1)) * _WINDOW_HOURS[match.group(2).lower()]

    asked_for_watch = bool(_WATCH_WORDS.search(text) or sensor_id)
    asked_for_sensors = bool(_SENSOR_WORDS.search(text))
    if requested in (SKILL_FLOOD_RECENT, SKILL_FLOOD_SENSORS, SKILL_FLOOD_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_FLOOD_WATCH
    elif asked_for_sensors:
        skill = SKILL_FLOOD_SENSORS
    else:
        skill = SKILL_FLOOD_RECENT

    params: dict = {}
    if zip_code:
        params["zip_code"] = str(zip_code)
    if borough:
        params["borough"] = str(borough)
    if sensor_id:
        params["sensor_id"] = str(sensor_id)
    if skill == SKILL_FLOOD_RECENT:
        params["hours"] = hours
        params["limit"] = limit
    explicit = bool(requested or asked_for_watch or asked_for_sensors)
    return {"skill": skill, "params": params, "explicit": explicit}


def _age(stamp: str | None) -> str:
    if not stamp:
        return "unknown time"
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    minutes = (datetime.now(timezone.utc) - when).total_seconds() / 60
    if minutes < 90:
        return f"{int(minutes)} min ago"
    if minutes < 60 * 36:
        return f"{int(minutes / 60)} h ago"
    return f"{int(minutes / 1440)} days ago"


def _depth(event: dict) -> float:
    try:
        return float(event.get("max_depth_inches") or 0)
    except (TypeError, ValueError):
        return 0.0


def run_flood_recent(params: dict, client: FloodClient) -> dict:
    sensor_ids = None
    if params.get("sensor_id"):
        sensor_ids = [client.check_sensor_id(params["sensor_id"])]
    elif params.get("zip_code") or params.get("borough"):
        sensor_ids = client.sensor_ids(params.get("zip_code"), params.get("borough"))
    hours = int(params.get("hours", 72))
    events = client.recent_events(sensor_ids, hours=hours, limit=int(params.get("limit", 20)))
    freshness = client.freshness(DATASET_FLOOD_EVENTS)
    place = params.get("zip_code") or params.get("borough") or ("sensor " + params["sensor_id"] if params.get("sensor_id") else "New York City")

    if sensor_ids is not None and not sensor_ids:
        return {
            "final_state": "completed",
            "message": f"No FloodNet sensors are deployed in {place}, so there is nothing to report for it.",
            "artifact": {
                "dataset": DATASET_FLOOD_EVENTS,
                "dataset_title": DATASET_FLOOD_EVENTS_TITLE,
                "freshness": freshness,
                "window_hours": hours,
                "place": place,
                "sensor_count": 0,
                "count": 0,
                "events": [],
            },
            "watch": None,
        }

    artifact = {
        "dataset": DATASET_FLOOD_EVENTS,
        "dataset_title": DATASET_FLOOD_EVENTS_TITLE,
        "freshness": freshness,
        "window_hours": hours,
        "place": place,
        "sensor_count": len(sensor_ids) if sensor_ids else None,
        "count": len(events),
        "events": events,
    }
    if not events:
        return {
            "final_state": "completed",
            "message": (
                f"No street-flooding events recorded on FloodNet sensors for {place} in the last "
                f"{hours} hours. This dataset only records completed flood events, so a dry result "
                "means no event was recorded in that window."
            ),
            "artifact": artifact,
            "watch": None,
        }

    deepest = max(events, key=_depth)
    return {
        "final_state": "completed",
        "message": (
            f"{len(events)} street-flooding event(s) recorded for {place} in the last {hours} hours. "
            f"Deepest: {deepest.get('sensor_name')} at {deepest.get('max_depth_inches')} in, "
            f"{_age(deepest.get('flood_start_time'))}."
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_flood_sensors(params: dict, client: FloodClient) -> dict:
    sensors = client.sensors(params.get("zip_code"), params.get("borough"), limit=int(params.get("limit", 25)))
    freshness = client.freshness(DATASET_FLOOD_SENSORS)
    place = params.get("zip_code") or params.get("borough")
    if not sensors:
        return {
            "final_state": "completed",
            "message": f"No FloodNet sensors are deployed in {place}.",
            "artifact": {
                "dataset": DATASET_FLOOD_SENSORS,
                "dataset_title": DATASET_FLOOD_SENSORS_TITLE,
                "freshness": freshness,
                "place": place,
                "count": 0,
                "sensors": [],
            },
            "watch": None,
        }
    names = "; ".join(f"{row.get('sensor_name')} ({row.get('street_name')})" for row in sensors[:5])
    return {
        "final_state": "completed",
        "message": f"{len(sensors)} FloodNet sensor(s) in {place}: {names}",
        "artifact": {
            "dataset": DATASET_FLOOD_SENSORS,
            "dataset_title": DATASET_FLOOD_SENSORS_TITLE,
            "freshness": freshness,
            "place": place,
            "count": len(sensors),
            "sensors": sensors,
        },
        "watch": None,
    }


def run_flood_watch(params: dict, client: FloodClient) -> dict:
    if params.get("sensor_id"):
        sensor = client.sensor(params["sensor_id"])
        if sensor is None:
            return {
                "final_state": "completed",
                "message": (
                    f"No FloodNet sensor has the id {params['sensor_id']}. Ask for the sensors in a "
                    "ZIP code to see the ids that exist."
                ),
                "artifact": {
                    "dataset": DATASET_FLOOD_SENSORS,
                    "dataset_title": DATASET_FLOOD_SENSORS_TITLE,
                    "freshness": client.freshness(DATASET_FLOOD_SENSORS),
                    "found": False,
                    "sensor": None,
                },
                "watch": None,
            }
        sensors = [sensor]
    else:
        sensors = client.sensors(params.get("zip_code"), params.get("borough"), limit=MAX_WATCH_SENSORS)
    if not sensors:
        return {
            "final_state": "completed",
            "message": f"No FloodNet sensors are deployed in {params.get('zip_code') or params.get('borough')}, so there is nothing to watch there.",
            "artifact": None,
            "watch": None,
        }

    sensor_ids = [row["sensor_id"] for row in sensors if row.get("sensor_id")]
    latest = client.latest_event(sensor_ids)
    place = params.get("zip_code") or params.get("borough") or sensors[0].get("sensor_name")
    observed = _observation(latest)
    if latest:
        message = (
            f"Watching {len(sensor_ids)} FloodNet sensor(s) for {place}. Latest recorded event: "
            f"{latest.get('sensor_name')} at {latest.get('max_depth_inches')} in, "
            f"{_age(latest.get('flood_start_time'))}. Point a pushNotificationConfig at this task and "
            "I will POST when a new event is recorded."
        )
    else:
        message = (
            f"Watching {len(sensor_ids)} FloodNet sensor(s) for {place}. No flood event has ever been "
            "recorded on them. Point a pushNotificationConfig at this task and I will POST when one is."
        )
    return {
        "final_state": "completed",
        "message": message,
        "artifact": {
            "dataset": DATASET_FLOOD_EVENTS,
            "dataset_title": DATASET_FLOOD_EVENTS_TITLE,
            "freshness": client.freshness(DATASET_FLOOD_EVENTS),
            "watching": sensor_ids,
            "place": place,
            "latest_event": latest,
        },
        "watch": {
            "kind": SKILL_FLOOD_WATCH,
            "sensor_ids": sensor_ids,
            "place": place,
            "dataset": DATASET_FLOOD_EVENTS,
            "observed": observed,
        },
    }


def _observation(event: dict | None) -> dict:
    """Watch observation: always a dict so 'no event yet' differs from 'unreadable'."""
    if not event:
        return {"last_start": "", "sensor_name": None, "max_depth_inches": None}
    return {
        "last_start": event.get("flood_start_time") or "",
        "sensor_name": event.get("sensor_name"),
        "max_depth_inches": event.get("max_depth_inches"),
    }


class FloodAgent(SkillAgent):
    name = "nycflood"
    card_name = "NYC Street Flooding Agent"
    card_description = (
        "Read-only agent over the FloodNet street-flooding datasets: recent flooding events recorded "
        "by the 491 city sensors, which sensors cover a ZIP or borough, and a watch skill that POSTs "
        "to your webhook when a new flood event is recorded. Every answer cites its dataset "
        "(aq7i-eu5q / kb2e-tjy3) and freshness. FloodNet records completed events, so answers state "
        "the time window instead of claiming a live water level."
    )
    env_prefix = "NYC_FLOOD"
    datasets = (DATASET_FLOOD_EVENTS, DATASET_FLOOD_SENSORS)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_FLOOD_WATCH,)

    def __init__(self, client: FloodClient | None = None) -> None:
        self.client = client or FloodClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        has_place = bool(params.get("zip_code") or params.get("borough") or params.get("sensor_id"))
        if skill == SKILL_FLOOD_RECENT:
            return []
        return [] if has_place else ["location"]

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_FLOOD_SENSORS:
            return "Which area? Give me a ZIP code or a borough name."
        return (
            "What should I watch? Give me a FloodNet sensor id (for example "
            "BK-richardson-st-n-11th-st-1x59w1), a ZIP code, or a borough."
        )

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if request["skill"] == SKILL_FLOOD_SENSORS:
            return run_flood_sensors(request["params"], self.client)
        if request["skill"] == SKILL_FLOOD_WATCH:
            return run_flood_watch(request["params"], self.client)
        return run_flood_recent(request["params"], self.client)

    def probe_watch(self, watch: dict):
        sensor_ids = watch.get("sensor_ids") or []
        if not sensor_ids:
            return None
        return _observation(self.client.latest_event(sensor_ids))

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        name = (observed or {}).get("sensor_name") or "a watched sensor"
        depth = (observed or {}).get("max_depth_inches") or "unknown"
        start = (observed or {}).get("last_start") or "an unknown time"
        return f"New street-flooding event recorded at {name}: {depth} in starting {start}."


__all__ = [
    "CARD_SKILLS",
    "FloodAgent",
    "SKILL_FLOOD_RECENT",
    "SKILL_FLOOD_SENSORS",
    "SKILL_FLOOD_WATCH",
    "UpstreamError",
    "message_data",
    "message_text",
    "parse",
    "run_flood_recent",
    "run_flood_sensors",
    "run_flood_watch",
]
