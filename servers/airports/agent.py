"""The airport-status agent: four skills over the FAA's public NAS status feed.

  - airport-status:    what the current snapshot says about one airport (or a city's airports)
  - airports-delays:   every ground delay program and arrival/departure delay, worst first
  - airports-closures: every closed airport with its start and reopen times
  - airport-watch:     watch an airport (or the whole system) and POST when a delay or closure
                       appears or clears

Watch-capable: airport-watch stores {"kind": "airport-watch", airport, observed} where observed
is {"entries": {id: description}}. Delay averages are revised every few minutes, so only the set
of entries is compared — the news is a program or closure appearing or clearing.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    AIRPORTS,
    DATASET,
    MAX_LISTED,
    AirportStatusClient,
    duration_phrase,
)

SKILL_AIRPORT_STATUS = "airport-status"
SKILL_AIRPORTS_DELAYS = "airports-delays"
SKILL_AIRPORTS_CLOSURES = "airports-closures"
SKILL_AIRPORT_WATCH = "airport-watch"

#: Uppercase codes are how the FAA writes them, so the code patterns are case-sensitive.
_CODE_RE = re.compile(r"\b([A-Z]{3,4})\b")
_CODE_CONTEXT_RE = re.compile(r"\b(?:airport|field|code|at|for|into)\s+([A-Z]{3,4})\b")
_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe)\b",
                          re.IGNORECASE)
_CLOSURE_WORDS = re.compile(r"\b(clos(?:e|ed|ure|ures|ing)|shut ?down|shut)\b", re.IGNORECASE)
_DELAY_WORDS = re.compile(r"\b(delays?|delayed|ground stop|ground delay|waiting|backed up|"
                          r"system ?wide|nationwide|everywhere|across the (?:country|system))\b",
                          re.IGNORECASE)
#: Phrasings that mean "the whole system", and so beat a single airport code in routing.
_NATIONWIDE_WORDS = re.compile(
    r"\b(nationwide|national|country|countrywide|system ?wide|everywhere|worst|overall|across the)\b",
    re.IGNORECASE)
#: Uppercase words that look like codes but are just English.
_NOT_CODES = frozenset({"THE", "AND", "NOT", "FOR", "ARE", "YOU", "ITS", "ALL", "NEW", "MAY",
                        "NOW", "ANY", "HOW", "WHY", "WHAT", "WHO", "VFR", "IFR", "UTC", "GMT"})

CARD_SKILLS = [
    {
        "id": SKILL_AIRPORT_STATUS,
        "name": "Airport status",
        "description": (
            "What the FAA's current snapshot says about an airport: an active ground delay "
            "program with its average and maximum delay, arrival and departure delay windows "
            "with the trend, and any closure with its start and reopen times. Accepts an airport "
            "code (SFO) or a city from this server's list (Chicago, which covers MDW and ORD)."
        ),
        "tags": ["airport", "faa", "delays", "flight", "closure", "nas"],
        "examples": [
            "Is SFO delayed right now?",
            '{"skill": "airport-status", "airports": ["ORD", "MDW"]}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIRPORTS_DELAYS,
        "name": "National delay picture",
        "description": (
            "Every ground delay program and every arrival/departure delay in the FAA's current "
            "snapshot, worst first, with the reason and how long the delays run."
        ),
        "tags": ["airport", "faa", "delays", "nationwide", "system"],
        "examples": [
            "What are the worst airport delays in the country right now?",
            '{"skill": "airports-delays", "kind": "ground-delay"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIRPORTS_CLOSURES,
        "name": "Airport closures",
        "description": (
            "Airports the FAA lists as closed right now, with the full notice text and both the "
            "stated start and reopen times."
        ),
        "tags": ["airport", "faa", "closure", "runway", "notam"],
        "examples": [
            "Are any airports closed?",
            '{"skill": "airports-closures"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_AIRPORT_WATCH,
        "name": "Watch an airport",
        "description": (
            "Watch one airport, or the whole national system, and have the server POST to your "
            "webhook when a delay program or closure appears for it or clears. Optional kind "
            "filter: ground-delay, arrival-delay, departure-delay or closure."
        ),
        "tags": ["airport", "faa", "watch", "webhook", "alert"],
        "examples": [
            "Tell me when SFO gets a ground delay program.",
            '{"skill": "airport-watch", "airport": "ORD", "kind": "ground-delay"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]


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
    """An airport code, read the way the FAA writes them (SFO, ORD).

    A code from the shipped table wins; otherwise an uppercase three or four letter token is
    taken as a code (people write codes in capitals), unless it is an ordinary English word.
    """
    for match in _CODE_RE.finditer(text):
        candidate = match.group(1)
        if candidate in AIRPORTS:
            return candidate
    match = _CODE_CONTEXT_RE.search(text)
    if match:
        return match.group(1)
    for match in _CODE_RE.finditer(text):
        candidate = match.group(1)
        if candidate not in _NOT_CODES:
            return candidate
    return None


def place_from_text(text: str) -> str | None:
    """A city name from the airport table, longest names first ('San Francisco' before 'San')."""
    lowered = " " + re.sub(r"[^a-z ]+", " ", text.lower()).strip() + " "
    lowered = re.sub(r"\s+", " ", lowered)
    for city in sorted({city for city, _ in AIRPORTS.values()}, key=len, reverse=True):
        if f" {city.lower()} " in lowered:
            return city
    return None


def codes_for_place(place: str) -> list[str]:
    """'Chicago' -> ['MDW', 'ORD']: every airport in the table for that city."""
    return AirportStatusClient.code_for_place(place)


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    given = _first(data, "airports", "airport", "code")
    kind = _first(data, "kind", "type")
    place = _first(data, "place", "city")

    airports: list[str] = []
    if isinstance(given, str):
        airports = [part.strip().upper() for part in given.split(",") if part.strip()]
    elif isinstance(given, list):
        airports = [str(part).strip().upper() for part in given if str(part).strip()]
    if not airports:
        code = code_from_text(text)
        if code:
            airports = [code]
    if not place:
        place = place_from_text(text)
    if not airports and place:
        airports = codes_for_place(place)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_closures = bool(_CLOSURE_WORDS.search(text))
    asked_for_delays = bool(_DELAY_WORDS.search(text))

    known = (SKILL_AIRPORT_STATUS, SKILL_AIRPORTS_DELAYS, SKILL_AIRPORTS_CLOSURES,
             SKILL_AIRPORT_WATCH)
    # An airport the caller actually named beats a vague "delays" phrase: asking about SFO should
    # report everything happening at SFO. "Worst delays in the country" has no airport and lands
    # on the national picture instead.
    asked_for_nationwide = bool(_NATIONWIDE_WORDS.search(text))
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_AIRPORT_WATCH
    elif asked_for_closures and not airports:
        skill = SKILL_AIRPORTS_CLOSURES
    elif airports and not asked_for_nationwide:
        skill = SKILL_AIRPORT_STATUS
    elif asked_for_delays or asked_for_nationwide:
        skill = SKILL_AIRPORTS_DELAYS
    elif airports:
        skill = SKILL_AIRPORT_STATUS
    else:
        skill = SKILL_AIRPORTS_DELAYS

    params: dict = {"limit": MAX_LISTED}
    if airports:
        params["airports"] = airports
    if kind:
        params["kind"] = str(kind)
    explicit = bool(requested or asked_for_watch or asked_for_closures or asked_for_delays)
    return {"skill": skill, "params": params, "explicit": explicit}


def _delay_bit(row: dict) -> str:
    """The timing half of a line, using whichever columns that kind of entry actually has."""
    bits = []
    if row.get("average_minutes") is not None:
        bits.append(f"average {duration_phrase(row['average_minutes'])}")
    if row.get("min_minutes") is not None and row.get("max_minutes") is not None:
        bits.append(f"{duration_phrase(row['min_minutes'])} to {duration_phrase(row['max_minutes'])}")
    elif row.get("max_minutes") is not None:
        bits.append(f"up to {duration_phrase(row['max_minutes'])}")
    if row.get("trend"):
        bits.append(str(row["trend"]).lower())
    return ", ".join(bits)


def _entry_line(row: dict) -> str:
    where = row["airport"] + (f" ({row['city']})" if row.get("city") else "")
    kind = row["kind"].replace("-", " ")
    line = f"  • {where} — {kind}"
    if row.get("reason"):
        line += f": {row['reason']}"
    if row.get("reason_kind") and row["reason_kind"] not in str(row.get("reason") or "").lower():
        line += f" [{row['reason_kind']}]"
    timing = _delay_bit(row)
    if timing:
        line += f" ({timing})"
    return line


def _closure_line(row: dict) -> str:
    where = row["airport"] + (f" ({row['city']})" if row.get("city") else "")
    line = f"  • {where}"
    if row.get("reason"):
        line += f" — {row['reason']}"
    window = []
    if row.get("start"):
        window.append(f"from {row['start']}")
    if row.get("reopen"):
        window.append(f"until {row['reopen']}")
    if window:
        line += f" ({', '.join(window)})"
    return line


SNAPSHOT_NOTE = (
    "The FAA publishes this snapshot continuously and only lists airports that are affected right "
    "now, so an airport missing from it means nothing is being reported for it — not that "
    "operations are normal."
)


def run_airport_status(params: dict, client: AirportStatusClient) -> dict:
    codes = [client.check_airport(code) for code in params.get("airports") or []]
    if not codes:
        return {
            "final_state": "input-required",
            "message": ("Which airport? Give me a code (SFO, ORD, JFK) or a city from this "
                        "server's list (Chicago, Denver, Miami, Seattle)."),
            "artifact": None,
            "watch": None,
        }
    reads = [client.airport(code) for code in codes]
    total = sum(read["count"] for read in reads)
    update = reads[0]["update_time"]
    artifact = {
        "dataset": DATASET,
        "source": reads[0]["source"],
        "update_time": update,
        "airports": codes,
        "count": total,
        "reports": reads,
    }
    if not total:
        plural = len(codes) > 1
        return {
            "final_state": "completed",
            "message": (f"{' and '.join(codes)} {'do not appear' if plural else 'does not appear'} in "
                        f"the FAA's current status snapshot, so no delay program, delay or closure is "
                        f"reported for {'them' if plural else 'it'} as of {update} UTC. "
                        f"{SNAPSHOT_NOTE}"),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{total} item(s) reported for {', '.join(codes)} as of {update} UTC:"]
    for read in reads:
        if not read["count"]:
            lines.append(f"  • {read['airport']} — nothing reported in the current snapshot.")
            continue
        for row in read["entries"]:
            lines.append(_entry_line(row) if row["kind"] != "closure" else _closure_line(row))
    lines.append(f"\nRead live from the FAA's NAS status feed ({DATASET}). {SNAPSHOT_NOTE}")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_airports_delays(params: dict, client: AirportStatusClient) -> dict:
    snapshot = client.status()
    rows = [row for row in AirportStatusClient.entries(snapshot) if row["kind"] != "closure"]
    kind = params.get("kind")
    if kind:
        wanted = client.check_kind(kind)
        rows = [row for row in rows if row["kind"] == wanted]
    codes = [client.check_airport(code) for code in params.get("airports") or []]
    if codes:
        rows = [row for row in rows if row["airport"] in codes]
    limit = client.check_positive(params.get("limit") or MAX_LISTED)
    artifact = {
        "dataset": DATASET,
        "source": snapshot["source"],
        "update_time": snapshot["update_time"],
        "counts": snapshot["counts"],
        "filter": {"kind": kind, "airports": codes},
        "count": len(rows),
        "delays": rows[:limit],
        "truncated": len(rows) > limit,
    }
    if not rows:
        return {
            "final_state": "completed",
            "message": (f"No delay of that kind is in the FAA's current snapshot "
                        f"({snapshot['update_time']} UTC). {SNAPSHOT_NOTE}"),
            "artifact": artifact,
            "watch": None,
        }
    counts = snapshot["counts"]
    lines = [
        f"{len(rows)} delay item(s) in the FAA snapshot of {snapshot['update_time']} UTC "
        f"(system-wide: {counts['ground-delay']} ground delay program(s), "
        f"{counts['arrival-delay']} arrival and {counts['departure-delay']} departure delay "
        f"report(s)), worst first:"
    ]
    for row in rows[:limit]:
        lines.append(_entry_line(row))
    if len(rows) > limit:
        lines.append(f"      ... {len(rows) - limit} more are in the artifact.")
    lines.append(f"\nRead live from the FAA's NAS status feed ({DATASET}). {SNAPSHOT_NOTE}")
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_airports_closures(params: dict, client: AirportStatusClient) -> dict:
    snapshot = client.status()
    rows = list(snapshot["closures"])
    codes = [client.check_airport(code) for code in params.get("airports") or []]
    if codes:
        rows = [row for row in rows if row["airport"] in codes]
    limit = client.check_positive(params.get("limit") or MAX_LISTED)
    artifact = {
        "dataset": DATASET,
        "source": snapshot["source"],
        "update_time": snapshot["update_time"],
        "counts": snapshot["counts"],
        "count": len(rows),
        "closures": rows[:limit],
        "truncated": len(rows) > limit,
    }
    if not rows:
        return {
            "final_state": "completed",
            "message": (f"No airport closure is in the FAA's current snapshot "
                        f"({snapshot['update_time']} UTC). Closures here are published notices, "
                        f"often for runways or for transient aircraft, and they clear when the "
                        f"notice expires."),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{len(rows)} airport closure(s) in the FAA snapshot of {snapshot['update_time']} UTC:"]
    for row in rows[:limit]:
        lines.append(_closure_line(row))
    if len(rows) > limit:
        lines.append(f"      ... {len(rows) - limit} more are in the artifact.")
    lines.append(
        f"\nRead live from the FAA's NAS status feed ({DATASET}). These are the FAA's own notice "
        f"texts, shorthand included; a closed airport notice with a long window (months) is "
        f"usually a runway or a category of traffic, not a shut aerodrome."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_airport_watch(params: dict, client: AirportStatusClient) -> dict:
    codes = [client.check_airport(code) for code in params.get("airports") or []]
    if len(codes) > 1:
        return {
            "final_state": "completed",
            "message": "A watch covers one airport or the whole system; give me a single code.",
            "artifact": {"dataset": DATASET, "airports": codes},
            "watch": None,
        }
    code = codes[0] if codes else None
    kind = params.get("kind")
    kinds = (client.check_kind(kind),) if kind else None
    observed = client.watch_state(code, kinds=kinds)
    scope = f"{code} ({client.airport_city(code)[0]})" if code and client.airport_city(code) else (
        code or "the whole national system")
    if kinds:
        scope += f", limited to {kind} items"
    watch = {
        "kind": SKILL_AIRPORT_WATCH,
        "dataset": DATASET,
        "airport": code,
        "filter_kind": kind,
        "observed": observed,
    }
    artifact = {
        "dataset": DATASET,
        "source": "FAA Air Traffic Control System Command Center (fly.faa.gov)",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "count": len(observed["entries"]),
        "entries_now": observed["entries"],
    }
    message = f"Watching {scope} — {len(observed['entries'])} item(s) reported there right now."
    if observed["entries"]:
        message += "\n" + "\n".join(f"  • {text}" for text in list(observed["entries"].values())[:5])
    message += (
        "\nPoint a pushNotificationConfig at this task and I will POST when a delay program or "
        "closure appears for that scope or clears. Delay averages are revised every few minutes, "
        "so only the set of open items is compared — routine revisions will not page you."
    )
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class AirportAgent(SkillAgent):
    name = "airports"
    card_name = "FAA Airport Status Agent"
    card_description = (
        "Read-only agent over the FAA's public NAS status snapshot: an airport's ground delay "
        "program, arrival and departure delay windows and closures (by code or by city), the "
        "national delay picture worst-first with the FAA's own reason text, the full closure list "
        "with start and reopen times, and a watch that POSTs to your webhook when a delay program "
        "or closure appears or clears for one airport or for the whole system."
    )
    env_prefix = "FAA"
    datasets = (DATASET,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_AIRPORT_WATCH,)

    def __init__(self, client: AirportStatusClient | None = None) -> None:
        self.client = client or AirportStatusClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_AIRPORT_STATUS and not params.get("airports"):
            return ["airport"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return ("Which airport? Give me a code the FAA uses (SFO, ORD, JFK, ATL) or a city from "
                "this server's list, such as Chicago, Denver, Miami or Seattle.")

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        skill, params = request["skill"], request["params"]
        if skill == SKILL_AIRPORTS_DELAYS:
            return run_airports_delays(params, self.client)
        if skill == SKILL_AIRPORTS_CLOSURES:
            return run_airports_closures(params, self.client)
        if skill == SKILL_AIRPORT_WATCH:
            return run_airport_watch(params, self.client)
        return run_airport_status(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if watch.get("kind") != SKILL_AIRPORT_WATCH:
            return None
        kind = watch.get("filter_kind")
        return self.client.watch_state(watch.get("airport"), kinds=(kind,) if kind else None)

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = (previous or {}).get("entries") or {}
        after = (observed or {}).get("entries") or {}
        scope = watch.get("airport") or "the national system"
        added = [text for key, text in after.items() if key not in before]
        cleared = [text for key, text in before.items() if key not in after]
        if added and cleared:
            return (f"{len(added)} item(s) opened and {len(cleared)} cleared at {scope}: "
                    f"{added[0]}")
        if added:
            more = f" (and {len(added) - 1} more)" if len(added) > 1 else ""
            return f"New at {scope}: {added[0]}{more}"
        if cleared:
            more = f" (and {len(cleared) - 1} more)" if len(cleared) > 1 else ""
            return f"Cleared at {scope}: {cleared[0]}{more}"
        return f"The FAA status snapshot for {scope} changed"


__all__ = [
    "CARD_SKILLS",
    "SKILL_AIRPORT_STATUS",
    "SKILL_AIRPORT_WATCH",
    "SKILL_AIRPORTS_CLOSURES",
    "SKILL_AIRPORTS_DELAYS",
    "SNAPSHOT_NOTE",
    "AirportAgent",
    "code_from_text",
    "codes_for_place",
    "parse",
    "place_from_text",
    "run_airport_status",
    "run_airport_watch",
    "run_airports_closures",
    "run_airports_delays",
]
