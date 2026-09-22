"""The NYC 311 agent: two skills over the live 311 dataset.

  - complaint-status: look up one complaint by its unique key (watch-capable)
  - complaints-near:  recent complaints around a ZIP / street / borough

Parsing accepts plain text ("did complaint 12345678 get fixed?") or a DataPart:
{"skill": "complaint-status", "unique_key": "12345678"}.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import DATASET_311, DATASET_311_TITLE, UpstreamError, NYC311Client

SKILL_COMPLAINT_STATUS = "complaint-status"
SKILL_COMPLAINTS_NEAR = "complaints-near"

CARD_SKILLS = [
    {
        "id": SKILL_COMPLAINT_STATUS,
        "name": "311 complaint status",
        "description": (
            "Look up one NYC 311 complaint by its unique key and report status, dates, type, "
            "and resolution. Supports webhook watching: the server notifies your endpoint when "
            "the complaint's status changes."
        ),
        "tags": ["nyc", "311", "civic", "status"],
        "examples": [
            "Did complaint 12345678 get fixed?",
            '{"skill": "complaint-status", "unique_key": "12345678"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_COMPLAINTS_NEAR,
        "name": "311 complaints near a place",
        "description": (
            "List the most recent NYC 311 complaints in a ZIP code, on a street, or in a borough, "
            "with complaint types and statuses."
        ),
        "tags": ["nyc", "311", "civic", "neighborhood"],
        "examples": [
            "What 311 complaints were reported in 11235 in the last 30 days?",
            '{"skill": "complaints-near", "zip_code": "11235", "days": 14}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_ID_RE = re.compile(r"\b(\d{6,9})\b")
_ZIP_RE = re.compile(r"\b(1[01]\d{3})\b")
_NEAR_WORDS = re.compile(r"\b(near|around|neighborhood|block|zip|nearby|area)\b", re.IGNORECASE)

_TYPE_KEYWORDS = {
    "rat": "Rodent",
    "rats": "Rodent",
    "rodent": "Rodent",
    "noise": "Noise",
    "pothole": "Street Condition",
    "potholes": "Street Condition",
    "heat": "HEAT/HOT WATER",
    "trash": "Dirty Condition",
    "garbage": "Dirty Condition",
    "parking": "Illegal Parking",
    "tree": "Damaged Tree",
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
    """Turn an incoming message into {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested_skill = _first(data, "skill", "skill_id")

    unique_key = _first(data, "unique_key", "complaint_id")
    zip_code = _first(data, "zip_code", "zip", "zipcode")
    street = _first(data, "street", "street_name")
    borough = _first(data, "borough")
    type_contains = _first(data, "complaint_type", "type")
    days = _first(data, "days", default=30)
    limit = _first(data, "limit", default=10)

    if not unique_key:
        match = _ID_RE.search(text)
        if match:
            unique_key = match.group(1)
    if not zip_code:
        match = _ZIP_RE.search(text)
        if match:
            zip_code = match.group(1)
    if not type_contains:
        lowered = text.lower()
        for keyword, canonical in _TYPE_KEYWORDS.items():
            if re.search(rf"\b{keyword}\b", lowered):
                type_contains = canonical
                break

    if requested_skill in (SKILL_COMPLAINT_STATUS, SKILL_COMPLAINTS_NEAR):
        skill = requested_skill
    elif unique_key:
        skill = SKILL_COMPLAINT_STATUS
    elif zip_code or street or borough or _NEAR_WORDS.search(text):
        skill = SKILL_COMPLAINTS_NEAR
    else:
        skill = SKILL_COMPLAINT_STATUS

    params: dict = {}
    if skill == SKILL_COMPLAINT_STATUS:
        if unique_key:
            params["unique_key"] = str(unique_key)
    else:
        if zip_code:
            params["zip_code"] = str(zip_code)
        if street:
            params["street"] = str(street)
        if borough:
            params["borough"] = str(borough)
        if type_contains:
            params["complaint_type"] = str(type_contains)
        params["days"] = days
        params["limit"] = limit
    explicit = bool(requested_skill or unique_key or zip_code or street or borough)
    return {"skill": skill, "params": params, "explicit": explicit}


def _human_complaint(row: dict) -> str:
    parts = [f"{row.get('complaint_type', 'Complaint')}"]
    if row.get("descriptor"):
        parts.append(f"({row['descriptor']})")
    line = f"Complaint {row.get('unique_key')}: " + " ".join(parts)
    line += f" — status {row.get('status', 'unknown')}."
    if row.get("created_date"):
        line += f" Filed {row['created_date'][:10]}."
    if row.get("closed_date"):
        line += f" Closed {row['closed_date'][:10]}."
    if row.get("resolution_description"):
        line += f" Resolution: {row['resolution_description']}"
    return line


def run_complaint_status(params: dict, client: NYC311Client) -> dict:
    key = str(params["unique_key"])
    row = client.get_complaint(key)
    freshness = client.freshness()
    if row is None:
        return {
            "final_state": "completed",
            "message": (
                f"I found no 311 complaint with key {key} in the dataset ({DATASET_311}). "
                "Double-check the number on the 311 site."
            ),
            "artifact": {
                "dataset": DATASET_311,
                "dataset_title": DATASET_311_TITLE,
                "freshness": freshness,
                "found": False,
                "complaint": None,
            },
            "watch": None,
        }
    return {
        "final_state": "completed",
        "message": _human_complaint(row),
        "artifact": {
            "dataset": DATASET_311,
            "dataset_title": DATASET_311_TITLE,
            "freshness": freshness,
            "found": True,
            "complaint": row,
        },
        "watch": {
            "kind": SKILL_COMPLAINT_STATUS,
            "unique_key": key,
            "dataset": DATASET_311,
            "observed": row.get("status"),
        },
    }


def run_complaints_near(params: dict, client: NYC311Client) -> dict:
    rows = client.recent_complaints(
        zip_code=params.get("zip_code"),
        street=params.get("street"),
        borough=params.get("borough"),
        complaint_type_contains=params.get("complaint_type"),
        days=int(params.get("days", 30)),
        limit=int(params.get("limit", 10)),
    )
    freshness = client.freshness()
    query = {key: value for key, value in params.items() if key != "limit"}
    if not rows:
        return {
            "final_state": "completed",
            "message": "No 311 requests matched that place and time window.",
            "artifact": {
                "dataset": DATASET_311,
                "dataset_title": DATASET_311_TITLE,
                "freshness": freshness,
                "query": query,
                "count": 0,
                "complaints": [],
            },
            "watch": None,
        }
    by_type: dict[str, int] = {}
    for row in rows:
        name = row.get("complaint_type", "Unknown")
        by_type[name] = by_type.get(name, 0) + 1
    top = ", ".join(f"{name} ({count})" for name, count in sorted(by_type.items(), key=lambda kv: -kv[1])[:3])
    window = f"the last {int(params.get('days', 30))} days"
    return {
        "final_state": "completed",
        "message": f"Most recent 311 requests for that place ({window}): {len(rows)} shown. Top types: {top}.",
        "artifact": {
            "dataset": DATASET_311,
            "dataset_title": DATASET_311_TITLE,
            "freshness": freshness,
            "query": query,
            "count": len(rows),
            "complaints": rows,
        },
        "watch": None,
    }


class NYC311Agent(SkillAgent):
    name = "nyc311"
    card_name = "NYC Open Data Agent"
    card_description = (
        "Read-only agent for NYC Open Data. Looks up a 311 complaint by its unique key and lists "
        "recent 311 complaints near a ZIP, street, or borough. Every answer cites the dataset "
        "(erm2-nwe9) and its freshness. Supports streaming and webhook push notifications when a "
        "watched complaint's status changes."
    )
    env_prefix = "NYC311"
    datasets = (DATASET_311,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_COMPLAINT_STATUS,)

    def __init__(self, client: NYC311Client | None = None) -> None:
        self.client = client or NYC311Client()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_COMPLAINT_STATUS:
            return [] if params.get("unique_key") else ["unique_key"]
        has_place = any(params.get(key) for key in ("zip_code", "street", "borough"))
        return [] if has_place else ["location"]

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_COMPLAINT_STATUS:
            return (
                "Which complaint? Give me the 311 unique key (6-9 digits, shown on your 311 "
                "report) and I will look it up."
            )
        return "Which place? Give me a ZIP code, a street name, or a borough."

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if request["skill"] == SKILL_COMPLAINT_STATUS:
            return run_complaint_status(request["params"], self.client)
        return run_complaints_near(request["params"], self.client)

    def probe_watch(self, watch: dict):
        """Current status of the watched complaint."""
        key = watch.get("unique_key")
        if not key:
            return None
        row = self.client.get_complaint(key)
        return None if not row else row.get("status")

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        return (
            f"Complaint {watch.get('unique_key')} status changed from {previous or 'unknown'} "
            f"to {observed or 'unknown'}."
        )


__all__ = [
    "CARD_SKILLS",
    "NYC311Agent",
    "SKILL_COMPLAINTS_NEAR",
    "SKILL_COMPLAINT_STATUS",
    "UpstreamError",
    "message_data",
    "message_text",
    "parse",
    "run_complaint_status",
    "run_complaints_near",
]
