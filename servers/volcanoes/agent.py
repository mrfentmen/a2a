"""The volcano agent: four skills over the USGS Volcano Science Center's alert levels.

  - volcano-alerts: volcanoes above NORMAL right now, most severe first, with the synopsis
  - volcano-list:   every monitored volcano, filtered by region or alert level
  - volcano-lookup: one volcano by name: level, colour code, threat, region and last notice
  - volcano-watch:  watch a region or a volcano and POST when an alert level changes

Watch-capable: volcano-watch stores {"kind": "volcano-watch", region/name, observed} where
observed is {"levels": {volcano: level}, "elevated": [...]}. Levels move on observatory
decisions, not on a clock, so every change really is news.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET,
    MAX_WATCH_VOLCANOES,
    VolcanoClient,
    is_elevated,
    severity,
)

SKILL_VOLCANO_ALERTS = "volcano-alerts"
SKILL_VOLCANO_LIST = "volcano-list"
SKILL_VOLCANO_LOOKUP = "volcano-lookup"
SKILL_VOLCANO_WATCH = "volcano-watch"

MAX_LISTED_VOLCANOES = 12
MAX_SYNOPSIS = 260

#: Well-known volcano names, used only to recognise a name in free text when the caller does
#: not say "volcano". They are a parsing hint: every lookup still resolves against the live
#: monitored-volcano list, so a name that is not there returns nothing, and a volcano that is
#: missing from this hint list can still be found by saying "the X volcano".
KNOWN_VOLCANOES = (
    "Kilauea", "Mauna Loa", "Mauna Kea", "Hualalai", "Haleakala", "Kama'ehuakanaloa",
    "Mount St. Helens", "Mount Rainier", "Mount Baker", "Mount Adams", "Mount Hood",
    "Mount Shasta", "Mount Jefferson", "Mount Bachelor", "Glacier Peak", "Lassen Volcanic Center",
    "Crater Lake", "Medicine Lake volcano", "Newberry", "Three Sisters", "Yellowstone",
    "Long Valley Caldera", "Mammoth Mountain", "Mono-Inyo Craters", "Clear Lake Volcanic Field",
    "Coso Volcanic Field", "Salton Buttes", "Valles Caldera", "Mount Churchill", "Novarupta",
    "Katmai", "Redoubt", "Spurr", "Iliamna", "Augustine", "Great Sitkin", "Little Sitkin",
    "Shishaldin", "Okmok", "Makushin", "Akutan", "Pavlof", "Veniaminof", "Semisopochnoi",
    "Gareloi", "Tanaga", "Takawangha", "Kanaga", "Kasatochi", "Aniakchak", "Fourpeaked",
    "Wrangell", "Edgecumbe", "Amukta", "Seguam", "Yunaska", "Carlisle", "Kagamil", "Bogoslof",
    "Ahyi Seamount", "Anatahan", "Pagan", "Guguan", "Sarigan", "Alamagan", "Agrigan",
    "Asuncion", "Farallon de Pajaros", "Daikoku seamount", "Fukujin seamount", "East Diamante",
    "Ukinrek Maars", "Wapi Lava Field", "Craters of the Moon volcanic field",
)

#: Region names the observatories were using on 2026-09-23. They are a parsing hint only:
#: volcano-list matches against the live geojson, so a renamed region cannot be invented here.
REGIONS = (
    "Alaska", "Alaska Peninsula", "Aleutians", "American Samoa", "Arizona", "California",
    "Colorado", "Cook Inlet-South Central", "Hawaii", "Idaho", "Interior Alaska", "Nevada",
    "New Mexico", "Northern Mariana Islands", "Oregon", "Seward Peninsula", "Southeast Alaska",
    "Southwest Alaska", "Utah", "Washington", "Wrangell Volcanic Field", "Wyoming",
)

CARD_SKILLS = [
    {
        "id": SKILL_VOLCANO_ALERTS,
        "name": "Elevated volcanoes",
        "description": (
            "Volcanoes currently above NORMAL alert level, most severe first: alert level, "
            "aviation colour code, previous level, the Smithsonian threat ranking, the "
            "observatory's synopsis and the notice link. Optionally scoped to a region."
        ),
        "tags": ["volcano", "usgs", "alert level", "eruption", "ash", "aviation"],
        "examples": [
            "Which volcanoes are elevated right now?",
            '{"skill": "volcano-alerts", "region": "Hawaii"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_VOLCANO_LIST,
        "name": "Monitored volcanoes",
        "description": (
            "Every volcano USGS monitors (161 of them, coordinates included), with its alert "
            "level and colour code, filterable by region or alert level, plus counts per level."
        ),
        "tags": ["volcano", "usgs", "monitoring", "list", "region"],
        "examples": [
            "What volcanoes does USGS monitor in Alaska?",
            '{"skill": "volcano-list", "level": "ADVISORY"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_VOLCANO_LOOKUP,
        "name": "Find a volcano by name",
        "description": (
            "Look a volcano up by name (partial names work) and show its alert level, colour "
            "code, threat ranking, region, observatory, coordinates and latest synopsis."
        ),
        "tags": ["volcano", "usgs", "lookup", "status"],
        "examples": [
            "What is Kilauea doing?",
            '{"skill": "volcano-lookup", "name": "Great Sitkin"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_VOLCANO_WATCH,
        "name": "Watch alert levels",
        "description": (
            "Watch one volcano, a region, or the whole country and have the server POST to your "
            "webhook when any alert level in that scope changes (a move up to WATCH or WARNING "
            "is reported first)."
        ),
        "tags": ["volcano", "usgs", "watch", "webhook", "alert level"],
        "examples": [
            "Tell me when any volcano in Alaska changes alert level.",
            '{"skill": "volcano-watch", "name": "Kilauea"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_WATCH_WORDS = re.compile(r"\b(watch|notify|tell me when|let me know|ping me|alert me|subscribe|keep an eye)\b",
                          re.IGNORECASE)
_ALERTS_WORDS = re.compile(
    r"\b(elevated|above normal|erupt\w*|unrest|warning|advisory|right now|going on|active)\b",
    re.IGNORECASE,
)
_LIST_WORDS = re.compile(
    r"\b(which volcanoes|what volcanoes|list|every volcano|all volcanoes|monitors?|monitored|"
    r"how many volcanoes|catalog|inventory|volcanoes in|volcanoes are in)\b",
    re.IGNORECASE,
)
_LOOKUP_WORDS = re.compile(
    r"\b(about|details?|tell me about|look ?up|status of|activity (?:at|of|for)|called|named|doing)\b",
    re.IGNORECASE,
)
_ABOUT_NAME_RE = re.compile(
    r"\b(?:about|of|at|for|called|named|look ?up|status of|activity (?:at|of|for))\s+"
    r"(?:the\s+|a\s+|an\s+)?([A-Za-z][\w'\-.]*(?:\s+[A-Za-z][\w'\-.]*){0,2})",
    re.IGNORECASE,
)
_VOLCANO_NAME_RE = re.compile(
    r"\b([A-Za-z][\w'\-.]*(?:\s+[A-Za-z][\w'\-.]*){0,2})\s+(?:volcano|seamount|caldera)\b",
    re.IGNORECASE,
)
#: Words that can sit in front of "volcano" without being a volcano's name.
_IGNORED_NAMES = frozenset(
    {"the", "a", "an", "which", "what", "any", "all", "this", "that", "there", "is", "are",
     "some", "when", "where", "if", "every", "each", "one", "another", "other", "monitored",
     "active", "elevated", "alert", "level", "status", "usgs", "our", "about", "of", "at", "for",
     "called", "named", "me", "my", "tell", "notify", "watch", "keep", "eye", "let", "know",
     "ping", "look", "up", "activity", "give", "details", "on", "it", "now"}
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


def region_from_text(text: str) -> str | None:
    """A region the observatories actually use. Longest names win."""
    lowered = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    for region in sorted(REGIONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(region.lower())}\b", lowered):
            return region
    return None


def name_from_text(text: str) -> str | None:
    """'what is Kilauea doing?' -> Kilauea; 'status of Great Sitkin' -> Great Sitkin.

    The known-name list runs first because it is precise; the "X volcano" patterns are the
    fallback for names it does not carry, and they refuse to read a question word as a name.
    """
    hint = known_volcano_in_text(text)
    if hint:
        return hint
    for pattern in (_VOLCANO_NAME_RE, _ABOUT_NAME_RE):
        match = pattern.search(text)
        if not match:
            continue
        candidate = " ".join(match.group(1).split())
        candidate = re.sub(r"\s+(?:volcano|seamount|caldera)$", "", candidate, flags=re.IGNORECASE)
        if len(candidate) < 3 or candidate.split()[0].lower() in _IGNORED_NAMES:
            continue
        return candidate
    return None


def known_volcano_in_text(text: str) -> str | None:
    """A volcano name from the hint list, longest first ('Mauna Loa' before 'Mauna Kea')."""
    for name in sorted(KNOWN_VOLCANOES, key=len, reverse=True):
        pattern = r"\b" + r"\s+".join(re.escape(part) for part in name.split()) + r"\b"
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    name = _first(data, "name", "volcano")
    region = _first(data, "region", "area")
    level = _first(data, "level", "alert_level")
    limit = _first(data, "limit", default=MAX_LISTED_VOLCANOES)

    if not name:
        name = name_from_text(text)
    if not region:
        region = region_from_text(text)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_list = bool(_LIST_WORDS.search(text))
    asked_for_lookup = bool(_LOOKUP_WORDS.search(text))
    asked_for_alerts = bool(_ALERTS_WORDS.search(text))

    known = (SKILL_VOLCANO_ALERTS, SKILL_VOLCANO_LIST, SKILL_VOLCANO_LOOKUP, SKILL_VOLCANO_WATCH)
    if requested in known:
        skill = requested
    elif asked_for_watch:
        skill = SKILL_VOLCANO_WATCH
    elif name and asked_for_lookup:
        skill = SKILL_VOLCANO_LOOKUP
    elif asked_for_lookup and not (asked_for_alerts or asked_for_list):
        # "give me the details" with no name: the lookup skill asks for one.
        skill = SKILL_VOLCANO_LOOKUP
    elif asked_for_alerts:
        skill = SKILL_VOLCANO_ALERTS
    elif asked_for_list:
        skill = SKILL_VOLCANO_LIST
    else:
        skill = SKILL_VOLCANO_ALERTS

    params: dict = {"limit": limit}
    if skill == SKILL_VOLCANO_WATCH:
        if name:
            params["name"] = str(name)
        if region:
            params["region"] = str(region)
    elif skill == SKILL_VOLCANO_LOOKUP:
        if name:
            params["name"] = str(name)
    elif skill == SKILL_VOLCANO_LIST:
        if region:
            params["region"] = str(region)
        if level:
            params["level"] = str(level)
    else:
        if region:
            params["region"] = str(region)
        if name:
            params["name"] = str(name)
    explicit = bool(requested or asked_for_watch or asked_for_list or asked_for_lookup or asked_for_alerts)
    return {"skill": skill, "params": params, "explicit": explicit}


def _level_bit(row: dict) -> str:
    level = row.get("level") or "UNASSIGNED"
    color = row.get("color") or "UNASSIGNED"
    bit = f"{level}/{color}"
    previous = row.get("previous_level")
    if previous and str(previous).upper() != level:
        bit += f" (was {str(previous).upper()}/{row.get('previous_color') or 'UNASSIGNED'})"
    return bit


def _shorten(text: str | None, limit: int = MAX_SYNOPSIS) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return body[:limit].rstrip(" ,;") + " ..."


def _volcano_line(row: dict) -> str:
    bits = [f"  • {row.get('name') or 'unnamed volcano'} — {_level_bit(row)}"]
    if row.get("region"):
        bits.append(f"({row['region']})")
    if row.get("threat"):
        bits.append(f"{row['threat']}")
    if row.get("latitude") is not None and row.get("longitude") is not None:
        bits.append(f"at {row['latitude']},{row['longitude']}")
    return ", ".join(bits)


def run_volcano_alerts(params: dict, client: VolcanoClient) -> dict:
    region = params.get("region")
    source = "elevated"
    if region:
        read = client.volcanoes(region=region, elevated_only=True, limit=MAX_WATCH_VOLCANOES)
        rows = read["volcanoes"]
        source = f"monitored-volcano list for {read['scope']}"
    else:
        read = client.elevated()
        rows = read["volcanoes"]
    name_filter = str(params.get("name") or "").lower()
    if name_filter:
        rows = [row for row in rows if name_filter in str(row.get("name") or "").lower()]
    artifact = {
        "dataset": DATASET,
        "source": "USGS Volcano Science Center alert levels"
                  + (f" ({source})" if source != "elevated" else ""),
        "scope": region or "the United States",
        "count": len(rows),
        "as_of": read.get("as_of"),
        "volcanoes": rows,
    }
    if not rows:
        scope = f" in {region}" if region else ""
        return {
            "final_state": "completed",
            "message": (f"No volcano{scope} is above NORMAL alert level in USGS's list right now, "
                        f"so this is the quiet reading, not a missing one."),
            "artifact": artifact,
            "watch": None,
        }
    lines = [f"{len(rows)} volcano(s) above NORMAL right now"
             + (f" in {region}" if region else "")
             + (f", as of {read['as_of']}" if read.get("as_of") else "") + ":"]
    for row in rows[:MAX_LISTED_VOLCANOES]:
        lines.append(_volcano_line(row))
        if row.get("synopsis"):
            lines.append(f"      {_shorten(row['synopsis'])}")
    if len(rows) > MAX_LISTED_VOLCANOES:
        lines.append(f"      ... {len(rows) - MAX_LISTED_VOLCANOES} more are in the artifact.")
    lines.append(
        f"\nRead live from the USGS Volcano Science Center ({DATASET}). Alert levels are set by the "
        f"observatories by their own judgement, so a level change is a decision, not a measurement; "
        f"NORMAL or UNASSIGNED elsewhere means no alert level is in force, not that a volcano is "
        f"dead. Aviation colour codes are for ash in the air."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_volcano_list(params: dict, client: VolcanoClient) -> dict:
    region = params.get("region")
    level = params.get("level")
    limit = client.check_positive(params.get("limit") or MAX_LISTED_VOLCANOES)
    read = client.volcanoes(region=region,
                            level=client.check_level(level) if level else None,
                            limit=limit)
    artifact = {
        "dataset": DATASET,
        "source": "USGS Volcano Science Center monitored-volcano list",
        "scope": read["scope"],
        "count": read["count"],
        "by_level": read["by_level"],
        "truncated": read["truncated"],
        "volcanoes": read["volcanoes"],
        "regions_available": client.regions(),
    }
    if not read["count"]:
        return {
            "final_state": "completed",
            "message": (f"No monitored volcano matches that filter. USGS groups them by region: "
                        f"{', '.join(client.regions())}."),
            "artifact": artifact,
            "watch": None,
        }
    counts = ", ".join(f"{level_name} {count}" for level_name, count in sorted(
        read["by_level"].items(), key=lambda item: -severity(item[0])))
    lines = [f"{read['count']} monitored volcano(s) for {read['scope']} — {counts}:"]
    for row in read["volcanoes"][:MAX_LISTED_VOLCANOES]:
        lines.append(_volcano_line(row))
    if read["count"] > MAX_LISTED_VOLCANOES:
        lines.append(f"      ... {read['count'] - MAX_LISTED_VOLCANOES} more are in the artifact.")
    lines.append(
        f"\nRead live from the USGS Volcano Science Center's monitored-volcano list ({DATASET}). "
        f"Most are NORMAL or UNASSIGNED, which is the ordinary state of a monitored volcano."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_volcano_lookup(params: dict, client: VolcanoClient) -> dict:
    name = params.get("name")
    if not name:
        return {
            "final_state": "input-required",
            "message": "Which volcano? Give me the name, or part of it, as USGS spells it.",
            "artifact": None,
            "watch": None,
        }
    read = client.lookup(str(name))
    artifact = {
        "dataset": DATASET,
        "source": "USGS Volcano Science Center monitored-volcano list",
        "query": read["query"],
        "count": read["count"],
        "volcanoes": read["volcanoes"],
        "latest_notice": None,
    }
    if not read["count"]:
        return {
            "final_state": "completed",
            "message": (f"No monitored volcano's name contains {read['query']!r}. USGS monitors 161 "
                        f"volcanoes and seamounts, mostly in Alaska, Hawaii, the Cascades, "
                        f"California, Yellowstone and the Northern Mariana Islands."),
            "artifact": artifact,
            "watch": None,
        }
    lines = []
    for row in read["volcanoes"]:
        lines.append(_volcano_line(row))
        details = []
        if row.get("observatory_name"):
            details.append(f"observatory: {row['observatory_name']}")
        if row.get("color"):
            details.append(f"aviation colour: {row['color']}")
        if row.get("alert_date"):
            details.append(f"level set {row['alert_date']}")
        if details:
            lines.append(f"      {', '.join(details)}")
        if row.get("url"):
            lines.append(f"      {row['url']}")
        if row.get("synopsis"):
            lines.append(f"      Last notice: {_shorten(row['synopsis'])}")
    notice = client.latest_notice(str(name))
    if notice:
        artifact["latest_notice"] = notice
        lines.append(f"\nMost recent elevated notice on file: {notice['name']} "
                     f"({_level_bit(notice)}) sent {notice.get('sent') or 'time not reported'}.")
        if notice.get("notice_url"):
            lines.append(f"  {notice['notice_url']}")
    lines.append(
        f"\nRead live from the USGS Volcano Science Center ({DATASET}). A synopsis is the "
        f"observatory's own wording at the time of the notice; check the notice link for the "
        f"current statement."
    )
    return {"final_state": "completed", "message": "\n".join(lines), "artifact": artifact, "watch": None}


def run_volcano_watch(params: dict, client: VolcanoClient) -> dict:
    region = params.get("region")
    name = params.get("name")
    observed = client.watch_state(region=region, name=name)
    if not observed["levels"]:
        return {
            "final_state": "completed",
            "message": (f"No monitored volcano matches that watch, so there is nothing to watch. "
                        f"Regions USGS uses: {', '.join(client.regions())}."),
            "artifact": {"dataset": DATASET, "region": region, "name": name, "matched": 0},
            "watch": None,
        }
    scope = f"the volcano {name}" if name else (f"the {region} region" if region else "every monitored volcano")
    watch = {
        "kind": SKILL_VOLCANO_WATCH,
        "dataset": DATASET,
        "region": region,
        "name": name,
        "observed": observed,
    }
    elevated_now = observed["elevated"]
    artifact = {
        "dataset": DATASET,
        "source": "USGS Volcano Science Center monitored-volcano list",
        "watching": {key: value for key, value in watch.items() if key != "observed"},
        "count": len(observed["levels"]),
        "elevated_now": elevated_now,
    }
    message = (f"Watching {scope} — {len(observed['levels'])} volcano(s) in scope.")
    if elevated_now:
        message += f" Currently above NORMAL: {', '.join(elevated_now[:8])}."
    message += ("\nPoint a pushNotificationConfig at this task and I will POST when any alert level "
                "in scope changes, naming which volcano moved and to what. Observatories set these "
                "levels by hand, so a change here is a real decision rather than timer noise.")
    return {"final_state": "completed", "message": message, "artifact": artifact, "watch": watch}


class VolcanoAgent(SkillAgent):
    name = "volcanoes"
    card_name = "Volcano Alert-Level Agent"
    card_description = (
        "Read-only agent over the USGS Volcano Science Center's alert levels: volcanoes above NORMAL "
        "right now with the observatory's own synopsis, the full monitored-volcano list (161 "
        "volcanoes and seamounts) by region and alert level, lookup by name with colour code and "
        "threat ranking, and a watch that POSTs when an alert level changes for a volcano, a region "
        "or the whole country. Every answer names the level and the colour code behind it."
    )
    env_prefix = "VOLCANO"
    datasets = (DATASET,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_VOLCANO_WATCH,)

    def __init__(self, client: VolcanoClient | None = None) -> None:
        self.client = client or VolcanoClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_VOLCANO_LOOKUP and not params.get("name"):
            return ["name"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return ("Which volcano? Give me the name, or part of it, as USGS spells it — Kilauea, "
                "Mauna Loa, Great Sitkin, Mount St. Helens, Yellowstone Caldera.")

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        skill, params = request["skill"], request["params"]
        if skill == SKILL_VOLCANO_LIST:
            return run_volcano_list(params, self.client)
        if skill == SKILL_VOLCANO_LOOKUP:
            return run_volcano_lookup(params, self.client)
        if skill == SKILL_VOLCANO_WATCH:
            return run_volcano_watch(params, self.client)
        return run_volcano_alerts(params, self.client)

    def probe_watch(self, watch: dict) -> dict | None:
        if watch.get("kind") != SKILL_VOLCANO_WATCH:
            return None
        return self.client.watch_state(region=watch.get("region"), name=watch.get("name"))

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        before = (previous or {}).get("levels") or {}
        after = (observed or {}).get("levels") or {}
        scope = (f"the volcano {watch['name']}" if watch.get("name")
                 else (f"the {watch['region']} region" if watch.get("region") else "the country"))
        moved = []
        for volcano, level in after.items():
            old = before.get(volcano)
            if old is not None and old != level:
                moved.append((severity(level) - severity(old), volcano, old, level))
        moved.sort(key=lambda item: (-item[0], item[1]))
        if moved:
            delta, volcano, old, new = moved[0]
            # severity is 0-4 and injective, so a recorded move always has a direction.
            direction = "up" if delta > 0 else "down"
            more = f" (and {len(moved) - 1} other change(s))" if len(moved) > 1 else ""
            return (f"Volcano alert level {direction} in {scope}: {volcano} moved from {old} to {new}"
                    f"{more}")
        added = sorted(set(after) - set(before))
        if added:
            return (f"{len(added)} volcano(s) newly appear in {scope}: {', '.join(added[:3])}")
        removed = sorted(set(before) - set(after))
        if removed:
            return (f"{len(removed)} volcano(s) left {scope}: {', '.join(removed[:3])}")
        return f"A monitored volcano's alert level changed in {scope}"


__all__ = [
    "CARD_SKILLS",
    "KNOWN_VOLCANOES",
    "MAX_LISTED_VOLCANOES",
    "REGIONS",
    "SKILL_VOLCANO_ALERTS",
    "SKILL_VOLCANO_LIST",
    "SKILL_VOLCANO_LOOKUP",
    "SKILL_VOLCANO_WATCH",
    "VolcanoAgent",
    "is_elevated",
    "known_volcano_in_text",
    "name_from_text",
    "parse",
    "region_from_text",
    "run_volcano_alerts",
    "run_volcano_list",
    "run_volcano_lookup",
    "run_volcano_watch",
]
