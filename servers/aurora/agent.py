"""The space-weather agent: five skills over NOAA's SWPC products.

  - aurora-now:        geomagnetic conditions right now plus the last 24 hours
  - aurora-forecast:   NOAA's predicted Kp for the next days, day by day
  - aurora-visibility: the OVATION model's aurora probability at a place or point
  - aurora-messages:   SWPC watches, warnings, alerts and summaries, parsed
  - aurora-watch:      watch geomagnetic activity and get POSTed when it shifts

Watch-capable: aurora-watch stores {"kind": "aurora-watch", threshold_kp, observed} where
observed is the activity band, whether a storm is in progress, and the newest SWPC
message id. Those are the values that change when something actually happens, so the
watcher fires on events rather than on every minute of numerical noise.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    CITY_COORDS,
    DATASET_ALERTS,
    DATASET_FORECAST,
    DATASET_KP,
    DATASET_KP_1M,
    DATASET_OVATION,
    STORM_SCALE,
    SpaceWeatherClient,
    is_storm,
    kp_band,
)

SKILL_AURORA_NOW = "aurora-now"
SKILL_AURORA_FORECAST = "aurora-forecast"
SKILL_AURORA_VISIBILITY = "aurora-visibility"
SKILL_AURORA_MESSAGES = "aurora-messages"
SKILL_AURORA_WATCH = "aurora-watch"

DEFAULT_THRESHOLD_KP = 5.0  # Kp 5 is the first rung of NOAA's storm scale

CARD_SKILLS = [
    {
        "id": SKILL_AURORA_NOW,
        "name": "Current space weather",
        "description": (
            "Geomagnetic conditions right now: NOAA's estimated planetary Kp, the plain-language "
            "activity band, the peak of the last 24 hours, and the newest SWPC message headline."
        ),
        "tags": ["space-weather", "aurora", "kp-index", "geomagnetic", "noaa"],
        "examples": [
            "How are the geomagnetic conditions right now?",
            '{"skill": "aurora-now"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AURORA_FORECAST,
        "name": "Kp forecast",
        "description": (
            "NOAA's 3-hourly predicted planetary Kp for the next days, grouped by UTC day with the "
            "days own peak, the storm grade that peak would reach, and any storm watch in force."
        ),
        "tags": ["space-weather", "aurora", "forecast", "kp-index", "storm"],
        "examples": [
            "What is the aurora forecast for the next 3 days?",
            '{"skill": "aurora-forecast", "days": 3}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AURORA_VISIBILITY,
        "name": "Aurora probability at a place",
        "description": (
            "NOAA's OVATION model gives the probability of aurora overhead at a latitude/longitude, "
            "or at a named city from this server's built-in list. Reports the model run time and the "
            "best probability within a couple of degrees of the point."
        ),
        "tags": ["aurora", "visibility", "ovation", "probability", "latitude"],
        "examples": [
            "What are the odds of seeing the aurora in Fairbanks tonight?",
            '{"skill": "aurora-visibility", "point": "64.84,-147.72"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AURORA_MESSAGES,
        "name": "SWPC messages",
        "description": (
            "Recent Space Weather Prediction Center messages - watches, warnings, alerts and "
            "summaries - parsed into code, kind, issue time and headline, optionally filtered by "
            "keyword such as storm, flare, radiation or blackout."
        ),
        "tags": ["space-weather", "swpc", "warnings", "alerts", "messages"],
        "examples": [
            "What has NOAA sent out about geomagnetic storms lately?",
            '{"skill": "aurora-messages", "contains": "storm", "limit": 5}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AURORA_WATCH,
        "name": "Watch geomagnetic activity",
        "description": (
            "Watch geomagnetic activity and have the server call your webhook when it shifts: the "
            "activity band changes, a storm starts or ends at your Kp threshold (default Kp 5), or "
            "SWPC issues a new storm message."
        ),
        "tags": ["aurora", "space-weather", "watch", "webhook", "storm"],
        "examples": [
            "Tell me when a geomagnetic storm starts.",
            '{"skill": "aurora-watch", "threshold_kp": 6}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_POINT_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
#: "kp 6", "kp of 5.5", "kp index above 6", "kp reaches 7" - the words between the two
#: may vary, so allow a short non-numeric gap rather than a fixed list of connectives.
_KP_RE = re.compile(r"\b(?:kp|k-index|k index|kindex)\b[^\d\n]{0,20}?(\d(?:\.\d)?)\b", re.IGNORECASE)
_GSCALE_RE = re.compile(r"\bG([1-5])\b", re.IGNORECASE)
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe|keep an eye)\b",
                          re.IGNORECASE)
_FORECAST_WORDS = re.compile(
    r"\b(forecast|tonight|tomorrow|next \d+ days?|next few days|this week|coming days|expected|predict(?:ed|ion)?)\b",
    re.IGNORECASE)
_VISIBILITY_WORDS = re.compile(r"\b(see|seeing|visible|visibility|view|odds|chance|probabilit(?:y|ies)|overhead)\b",
                               re.IGNORECASE)
_MESSAGE_WORDS = re.compile(
    r"\b(messages?|warnings?|watches|advisory|advisories|bulletins?|alerts?|news|issued|said|noaa say)\b",
    re.IGNORECASE)
_KEYWORDS = {
    "storm": "storm", "geomagnetic": "storm", "flare": "flare", "x-ray": "flare", "radiation": "radiation",
    "blackout": "blackout", "proton": "proton", "electron": "electron", "cme": "CME",
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


def place_from_text(text: str) -> str | None:
    """A city name from the built-in list. Longest names win ('new york' over 'york')."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", lowered):
            return name
    return None


def threshold_from_text(text: str) -> float | None:
    """'Kp 6', 'kp of 5.5' or 'a G3 storm' -> the Kp a watch should fire at."""
    match = _KP_RE.search(text)
    if match:
        return float(match.group(1))
    match = _GSCALE_RE.search(text)
    if match:
        return float(int(match.group(1)) + 4)  # G1 starts at Kp 5
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    point = _first(data, "point", "lat_lon")
    place = _first(data, "place", "city")
    threshold = _first(data, "threshold_kp", "kp")
    contains = _first(data, "contains", "keyword")
    limit = _first(data, "limit", default=5)
    days = _first(data, "days", default=3)

    if not point:
        match = _POINT_RE.search(text)
        if match:
            point = f"{match.group(1)},{match.group(2)}"
    if not place:
        place = place_from_text(text)
    if threshold is None:
        threshold = threshold_from_text(text)
    if not contains:
        lowered = text.lower()
        contains = next((value for word, value in _KEYWORDS.items()
                         if re.search(rf"\b{re.escape(word)}s?\b", lowered)), None)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_forecast = bool(_FORECAST_WORDS.search(text))
    asked_for_messages = bool(_MESSAGE_WORDS.search(text))
    asked_for_visibility = bool(_VISIBILITY_WORDS.search(text))

    known = (SKILL_AURORA_NOW, SKILL_AURORA_FORECAST, SKILL_AURORA_VISIBILITY, SKILL_AURORA_MESSAGES,
             SKILL_AURORA_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_AURORA_WATCH
    elif point or place:
        skill = SKILL_AURORA_VISIBILITY
    elif asked_for_forecast:
        skill = SKILL_AURORA_FORECAST
    elif asked_for_messages:
        skill = SKILL_AURORA_MESSAGES
    elif asked_for_visibility:
        skill = SKILL_AURORA_VISIBILITY
    else:
        skill = SKILL_AURORA_NOW

    # Only carry the parameters the chosen skill actually reads.
    params: dict = {}
    if skill == SKILL_AURORA_MESSAGES:
        params["limit"] = limit
        if contains:
            params["contains"] = str(contains)
    if skill == SKILL_AURORA_FORECAST:
        params["days"] = days
    if point:
        params["point"] = str(point)
    if place:
        params["place"] = str(place)
    if threshold is not None and skill == SKILL_AURORA_WATCH:
        params["threshold_kp"] = threshold
    explicit = bool(requested or asked_for_watch or asked_for_forecast or asked_for_messages)
    return {"skill": skill, "params": params, "explicit": explicit}


def resolve_place(params: dict, client: SpaceWeatherClient) -> tuple[float, float, str] | None:
    """(latitude, longitude, label) for a point, a city name, or nothing."""
    if params.get("point"):
        lat, lon = client.check_point(params["point"])
        return lat, lon, f"the point {lat},{lon}"
    if params.get("place"):
        found = SpaceWeatherClient.city(params["place"])
        if found:
            lat, lon = found
            return lat, lon, params["place"].title()
        return None
    return None


def _storm_phrase(kp: float) -> str:
    band = kp_band(kp)
    if is_storm(kp):
        return f"Kp {kp:g} - {band} on NOAA's storm scale"
    return f"Kp {kp:g} ({band})"


def run_aurora_now(params: dict, client: SpaceWeatherClient) -> dict:
    recent = client.recent(hours=24)
    now = recent["now"]
    latest = client.messages(limit=1)["messages"]
    newest = latest[0] if latest else None
    artifact = {
        "dataset": DATASET_KP_1M,
        "also_read": [recent["dataset"], DATASET_ALERTS],
        "source": "NOAA Space Weather Prediction Center",
        "freshness": now.get("time_tag"),
        "estimated_kp": now.get("estimated_kp"),
        "band": now.get("band"),
        "storm": now.get("storm"),
        "three_hourly": now.get("three_hourly"),
        "peak_kp_24h": recent["peak_kp"],
        "peak_band_24h": recent["peak_band"],
        "peak_time_tag_24h": recent["peak_time_tag"],
        "latest_message": (
            {"code": newest["code"], "kind": newest["kind"], "issue_datetime": newest["issue_datetime"],
             "headline": newest["headline"]}
            if newest else None
        ),
    }
    kp = now.get("estimated_kp")
    lines = [
        f"Geomagnetic conditions right now: estimated Kp {kp:g} ({now['band']})"
        + (" - a geomagnetic storm is in progress." if now["storm"] else "."),
        f"  • Peak over the last {recent['window_hours']} hours: "
        f"{_storm_phrase(recent['peak_kp'])} at {recent['peak_time_tag']}",
        f"  • Latest 3-hourly value: Kp {now['three_hourly'].get('kp')} at {now['three_hourly'].get('time_tag')} "
        f"(a-index {now['three_hourly'].get('a_running')}, {now['three_hourly'].get('station_count')} stations)",
    ]
    if newest:
        lines.append(f"  • Newest SWPC message: {newest['kind'] or 'MESSAGE'} {newest['code'] or ''} "
                     f"({newest['issue_datetime']}) - {newest['headline']}")
    lines.append(
        "\nRead live from NOAA SWPC. Kp is a 0-9 planet-wide index: 5 and above is NOAA's storm "
        "scale, G1 (minor) through G5 (extreme). It describes the geomagnetic field, not what you "
        "will see above your own head - ask for aurora-visibility at a place for that."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_aurora_forecast(params: dict, client: SpaceWeatherClient) -> dict:
    forecast = client.forecast(days=int(params.get("days") or 3))
    storms = [item for item in client.messages(limit=20, contains="storm")["messages"]
              if (item["kind"] or "").upper() in ("WATCH", "WARNING", "EXTENDED WARNING", "ALERT")]
    artifact = {
        "dataset": DATASET_FORECAST,
        "also_read": [DATASET_ALERTS],
        "source": "NOAA Space Weather Prediction Center",
        "freshness": forecast["days"][0]["rows"][0]["time_tag"] if forecast["days"] else None,
        "days": forecast["days"],
        "peak_kp": forecast["peak_kp"],
        "peak_band": forecast["peak_band"],
        "storm_messages": [
            {"code": item["code"], "kind": item["kind"], "issue_datetime": item["issue_datetime"],
             "headline": item["headline"], "g_scale": item["g_scale"]}
            for item in storms[:3]
        ],
        "predicted_rows": forecast["predicted_rows"],
    }
    if not forecast["days"]:
        return {
            "final_state": "completed",
            "message": "NOAA's Kp forecast file contained no predicted rows, so there is no forecast to show.",
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"NOAA's predicted planetary Kp, peak per UTC day (read live):"]
    for entry in forecast["days"]:
        lines.append(f"  • {entry['date']}: peak Kp {entry['max_kp']:g} ({entry['band']})"
                     + (" - storm level" if entry["storm"] else ""))
    lines.append(f"\nHighest expected in this window: {_storm_phrase(forecast['peak_kp'])}.")
    if storms:
        lines.append("SWPC storm messages in force:")
        for item in storms[:2]:
            lines.append(f"  • {item['kind']} {item['code'] or ''} - {item['headline']}")
    else:
        lines.append("No storm watch or warning is in force in the message feed right now.")
    lines.append(
        "\nKp forecast confidence drops after about a day, and a forecast is not a promise: "
        "NOAA revises these numbers as the solar wind arrives."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_aurora_visibility(params: dict, client: SpaceWeatherClient) -> dict:
    resolved = resolve_place(params, client)
    if not resolved:
        return {
            "final_state": "completed",
            "message": "I do not know that place, so I will not guess coordinates for it.",
            "artifact": {"dataset": DATASET_OVATION, "place": params.get("place"), "known": False},
            "watch": None,
        }
    lat, lon, label = resolved
    probability = client.aurora_probability(lat, lon)
    recent = client.recent(hours=24)
    now = recent["now"]
    artifact = {
        "dataset": DATASET_OVATION,
        "also_read": [DATASET_KP_1M, recent["dataset"]],
        "source": "NOAA Space Weather Prediction Center (OVATION model)",
        "freshness": probability.get("observation_time"),
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "probability_percent": probability["probability"],
        "max_probability_nearby_percent": probability["max_probability_nearby"],
        "radius_degrees": probability["radius_degrees"],
        "nearest_grid_cell": probability["nearest_cell"],
        "model_observation_time": probability.get("observation_time"),
        "model_forecast_time": probability.get("forecast_time"),
        "estimated_kp": now.get("estimated_kp"),
        "band": now.get("band"),
        "peak_kp_24h": recent["peak_kp"],
    }
    chance = probability["probability"]
    nearby = probability["max_probability_nearby"]
    lines = [
        f"Aurora probability overhead at {label} ({lat},{lon}): {chance}%"
        + (f", and up to {nearby}% within {probability['radius_degrees']:g} degrees of it." if nearby != chance else "."),
        f"  • Model run: observed {probability.get('observation_time')}, forecast {probability.get('forecast_time')}",
        f"  • Geomagnetic activity behind it: estimated Kp {now.get('estimated_kp'):g} ({now['band']}), "
        f"24-hour peak Kp {recent['peak_kp']:g} ({recent['peak_band']})",
    ]
    if chance >= 50:
        lines.append("  • That is a strong signal from the model - worth going outside if the sky is clear.")
    elif chance >= 20:
        lines.append("  • A real chance, but you would want dark sky and a clear horizon to the north.")
    else:
        lines.append("  • Low odds right now; the oval is not reaching this latitude.")
    lines.append(
        "\nNOAA's OVATION model gives the probability of aurora overhead at a point; it is a model "
        "run, not a sighting, and it says nothing about clouds - check a weather forecast too. "
        "The place coordinates for named cities come from this server's own list, not from NOAA; "
        "pass latitude,longitude for an exact point."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_aurora_messages(params: dict, client: SpaceWeatherClient) -> dict:
    result = client.messages(limit=int(params.get("limit") or 5), contains=params.get("contains"))
    messages = [
        {
            "product_id": item["product_id"],
            "issue_datetime": item["issue_datetime"],
            "code": item["code"],
            "serial": item["serial"],
            "kind": item["kind"],
            "g_scale": item["g_scale"],
            "headline": item["headline"],
            "summary": item["text"].strip().replace("\r\n", " ")[:1000],
        }
        for item in result["messages"]
    ]
    artifact = {
        "dataset": DATASET_ALERTS,
        "source": "NOAA Space Weather Prediction Center",
        "freshness": messages[0]["issue_datetime"] if messages else None,
        "count": len(messages),
        "filter": params.get("contains"),
        "messages": messages,
    }
    if not messages:
        what = f" containing {params['contains']!r}" if params.get("contains") else ""
        return {
            "final_state": "completed",
            "message": f"NOAA's message feed has no recent SWPC messages{what}.",
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{len(messages)} recent SWPC message(s)"
             + (f" matching {params['contains']!r}" if params.get("contains") else "") + ", newest first:"]
    for item in messages[:5]:
        lines.append(f"  • [{item['kind'] or 'MESSAGE'}] {item['code'] or ''} #{item['serial'] or ''} "
                     f"({item['issue_datetime']}) - {item['headline']}")
    lines.append("\nRead live from swpc.noaa.gov/products/alerts.json. Product codes identify the "
                 "series (WATA20 is a geomagnetic storm watch, ALTXMF an X-ray flux alert); NOAA's "
                 "own wording is kept in the artifact summary.")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def _observation(client: SpaceWeatherClient, threshold: float) -> dict:
    """What a watch compares between polls: the band, whether it storms, and the newest message."""
    now = client.kp_now()
    kp = now.get("estimated_kp")
    latest = client.messages(limit=1)["messages"]
    newest = latest[0] if latest else None
    return {
        "storming": bool(is_storm(kp) and kp >= threshold),
        "band": now.get("band") or "unknown",
        "newest_message": f"{newest['code'] or ''}#{newest['serial'] or ''}" if newest else "",
        "newest_kind": (newest["kind"] or "") if newest else "",
        "newest_headline": (newest["headline"] or "")[:140] if newest else "",
    }


def run_aurora_watch(params: dict, client: SpaceWeatherClient) -> dict:
    threshold = client.check_kp(_first(params, "threshold_kp", default=DEFAULT_THRESHOLD_KP))
    observed = _observation(client, threshold)
    now = client.kp_now()
    watch = {
        "kind": SKILL_AURORA_WATCH,
        "dataset": DATASET_KP_1M,
        "threshold_kp": threshold,
        "observed": observed,
    }
    if params.get("place"):
        watch["place"] = str(params["place"])
    artifact = {
        "dataset": DATASET_KP_1M,
        "also_read": [DATASET_ALERTS],
        "source": "NOAA Space Weather Prediction Center",
        "freshness": now.get("time_tag"),
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "current": {"estimated_kp": now.get("estimated_kp"), "band": now.get("band"),
                    "storm": now.get("storm")},
        "newest_message": {"code": observed["newest_message"], "kind": observed["newest_kind"],
                           "headline": observed["newest_headline"]} if observed["newest_message"] else None,
    }
    state = ("A geomagnetic storm at or above your threshold is in progress"
             if observed["storming"] else "Activity is below your threshold")
    message = (
        f"Watching geomagnetic activity at Kp {threshold:g} and above. Right now estimated Kp "
        f"{now.get('estimated_kp'):g} ({observed['band']}). {state}.\n"
        f"Newest SWPC message: {observed['newest_kind'] or 'none'} {observed['newest_message'] or ''} "
        f"{'- ' + observed['newest_headline'] if observed['newest_headline'] else ''}\n"
        "Point a pushNotificationConfig at this task and I will POST when the activity band changes, "
        "a storm starts or ends, or SWPC issues a new message."
    )
    if watch.get("place"):
        message += (f"\nThis watch fires on planetary activity, not on your local sky: for whether "
                    f"aurora would be overhead at {str(watch['place']).title()}, ask the "
                    "aurora-visibility skill.")
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class AuroraSpaceWeatherAgent(SkillAgent):
    name = "aurora-space-weather"
    card_name = "Aurora Space Weather Agent"
    card_description = (
        "Read-only agent over NOAA's Space Weather Prediction Center: current geomagnetic "
        "conditions and the 24-hour peak, NOAA's predicted planetary Kp for the next days, the "
        "OVATION model's aurora probability at a place or latitude/longitude point, recent SWPC "
        "watches and warnings parsed into fields, and a watch skill that POSTs to your webhook "
        "when geomagnetic activity shifts. Every number is read live from swpc.noaa.gov, and the "
        "answers say which product it came from."
    )
    env_prefix = "AURORA"
    datasets = (DATASET_KP_1M, DATASET_KP, DATASET_FORECAST, DATASET_ALERTS, DATASET_OVATION)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_AURORA_WATCH,)

    def __init__(self, client: SpaceWeatherClient | None = None) -> None:
        self.client = client or SpaceWeatherClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_AURORA_VISIBILITY and not (params.get("point") or params.get("place")):
            return ["location"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return (
            "Which place? Give me a latitude/longitude point (like 64.84,-147.72) or a city name "
            "from this server's list, such as Fairbanks, Tromso, Reykjavik, Edinburgh, Minneapolis, "
            "Toronto, Sydney or Ushuaia."
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
        if skill == SKILL_AURORA_FORECAST:
            return run_aurora_forecast(params, self.client)
        if skill == SKILL_AURORA_VISIBILITY:
            return run_aurora_visibility(params, self.client)
        if skill == SKILL_AURORA_MESSAGES:
            return run_aurora_messages(params, self.client)
        if skill == SKILL_AURORA_WATCH:
            return run_aurora_watch(params, self.client)
        return run_aurora_now(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if not watch.get("threshold_kp"):
            return None
        return _observation(self.client, float(watch["threshold_kp"]))

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = previous or {}
        after = observed or {}
        headline = after.get("newest_headline") or "a new SWPC message"
        if after.get("newest_message") and after["newest_message"] != before.get("newest_message"):
            return f"SWPC issued a new {after.get('newest_kind') or 'message'}: {headline}"
        if after.get("storming") and not before.get("storming"):
            return (f"Geomagnetic activity reached Kp {float(watch.get('threshold_kp', DEFAULT_THRESHOLD_KP)):g} "
                    f"- {after.get('band')} on NOAA's storm scale. Aurora may reach further south than usual.")
        if before.get("storming") and not after.get("storming"):
            return f"The geomagnetic storm has eased: activity is now {after.get('band') or 'lower'}."
        if after.get("band") != before.get("band"):
            return f"Geomagnetic activity changed: now {after.get('band') or 'unknown'} (was {before.get('band') or 'unknown'})."
        return f"Space weather changed. Newest SWPC message: {headline}"


__all__ = [
    "AuroraSpaceWeatherAgent",
    "CARD_SKILLS",
    "DEFAULT_THRESHOLD_KP",
    "SKILL_AURORA_FORECAST",
    "SKILL_AURORA_MESSAGES",
    "SKILL_AURORA_NOW",
    "SKILL_AURORA_VISIBILITY",
    "SKILL_AURORA_WATCH",
    "place_from_text",
    "run_aurora_forecast",
    "run_aurora_messages",
    "run_aurora_now",
    "run_aurora_visibility",
    "run_aurora_watch",
    "threshold_from_text",
]
