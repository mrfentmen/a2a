"""The NYC drinking water agent: three skills over the live DEP monitoring dataset.

  - water-quality: sample chemistry + coliform/E. coli detections for a site or citywide
  - water-sites:   the monitoring site codes and how recently each was sampled
  - water-watch:   watch one site and get POSTed when a new sample is logged

Watch-capable: water-watch stores {"kind": "water-watch", "site": "..."} and the
server POSTs to your webhook when a newer sample number appears for that site.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET_WATER,
    DATASET_WATER_TITLE,
    UpstreamError,
    WaterClient,
)

SKILL_WATER_QUALITY = "water-quality"
SKILL_WATER_SITES = "water-sites"
SKILL_WATER_WATCH = "water-watch"

CAVEAT = (
    "This is the raw distribution-monitoring record published by DEP, not a health ruling. "
    "For a concern about water in a building, call 311."
)

CARD_SKILLS = [
    {
        "id": SKILL_WATER_QUALITY,
        "name": "Drinking water sample results",
        "description": (
            "Free chlorine, turbidity, coliform, and E. coli results from New York City's "
            "distribution monitoring data, for one monitoring site code or citywide, over a "
            "chosen window. Reports how many samples were looked at and how many showed a "
            "coliform or E. coli detection."
        ),
        "tags": ["nyc", "water", "drinking-water", "dep", "quality"],
        "examples": [
            "How is the water testing at site 55450?",
            '{"skill": "water-quality", "site": "55450", "days": 180}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_WATER_SITES,
        "name": "Water monitoring sites",
        "description": (
            "The monitoring site codes in the drinking water dataset, with sample counts and the "
            "date of each site's most recent sample. Sites are published as codes only; the "
            "dataset carries no coordinates, so this cannot resolve an address."
        ),
        "tags": ["nyc", "water", "dep", "sites", "coverage"],
        "examples": [
            "Which water monitoring sites report the most?",
            '{"skill": "water-sites", "limit": 10}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_WATER_WATCH,
        "name": "Watch a water monitoring site",
        "description": (
            "Watch one monitoring site and have the server call your webhook when a new sample is "
            "logged for it, including the new sample's chemistry."
        ),
        "tags": ["nyc", "water", "dep", "watch", "webhook"],
        "examples": [
            "Tell me when site 55450 gets a new sample.",
            '{"skill": "water-watch", "site": "55450"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_SITE_RE = re.compile(r"\b([0-9]{5}|[0-9]?[A-Za-z]{2,4}[0-9]{0,4}[A-Za-z]?[0-9]?)\b")
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert|ping|tell me when|let me know|subscribe|new sample)\b", re.IGNORECASE)
# "sites" plural only: "at site 55450" is a quality question, not a site listing.
_SITE_WORDS = re.compile(r"\b(sites|stations|locations|coverage)\b", re.IGNORECASE)
_STOPWORDS = {"water", "quality", "sample", "samples", "site", "sites", "watch", "notify", "the", "and", "for", "when"}


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


def _site_from_text(text: str) -> str | None:
    """Pick a site code out of free text: 5-digit codes or compact codes like 1S03A."""
    for token in re.findall(r"[A-Za-z0-9]{3,10}", text):
        lowered = token.lower()
        if lowered in _STOPWORDS:
            continue
        if re.fullmatch(r"\d{5}", token) or re.fullmatch(r"\d?[A-Za-z]{2,4}\d{0,4}[A-Za-z]?\d?", token) and any(
            ch.isdigit() for ch in token
        ):
            return token
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")
    site = _first(data, "site", "sample_site", "site_code")
    days = _first(data, "days", default=90)
    limit = _first(data, "limit", default=20)

    if not site:
        site = _site_from_text(text)

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    asked_for_sites = bool(_SITE_WORDS.search(text))
    if requested in (SKILL_WATER_QUALITY, SKILL_WATER_SITES, SKILL_WATER_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_WATER_WATCH
    elif asked_for_sites:
        skill = SKILL_WATER_SITES
    else:
        skill = SKILL_WATER_QUALITY

    params: dict = {"days": days, "limit": limit}
    if site:
        params["site"] = str(site)
    explicit = bool(requested or asked_for_watch or asked_for_sites)
    return {"skill": skill, "params": params, "explicit": explicit}


def _summary_line(summary: dict, where: str) -> str:
    chlorine = summary["chlorine_mg_l"]
    parts = [
        f"{summary['samples']} sample(s) for {where}",
        f"{summary['first_sample_date']} to {summary['last_sample_date']}"
        if summary["first_sample_date"]
        else "no dated samples",
        f"free chlorine {chlorine['min']}-{chlorine['max']} mg/L"
        if chlorine["min"] is not None
        else "free chlorine not reported",
        f"max turbidity {summary['turbidity_ntu']['max']} NTU"
        if summary["turbidity_ntu"]["max"] is not None
        else "turbidity not reported",
        f"coliform detections {summary['coliform_detections']}/{summary['coliform_samples']}",
        f"E. coli detections {summary['e_coli_detections']}/{summary['e_coli_samples']}",
    ]
    return "; ".join(parts) + "."


def run_water_quality(params: dict, client: WaterClient) -> dict:
    site = params.get("site")
    days = int(params.get("days", 90))
    rows = client.site_window(site, days=days) if site else client.latest_samples(limit=int(params.get("limit", 50)))
    where = f"site {site}, last {days} days" if site else "all reported sites, most recent samples"
    summary = client.summarize(rows)
    artifact = {
        "dataset": DATASET_WATER,
        "dataset_title": DATASET_WATER_TITLE,
        "freshness": client.freshness(),
        "site": site,
        "window_days": days if site else None,
        "summary": summary,
        "samples": rows[:50],
        "caveat": CAVEAT,
    }
    if not rows:
        return {
            "final_state": "completed",
            "message": (
                f"No drinking water samples are published for {where}. Check the site code — "
                "water-sites lists the codes that exist."
            ),
            "artifact": artifact,
            "watch": None,
        }
    return {
        "final_state": "completed",
        "message": _summary_line(summary, where) + " " + CAVEAT,
        "artifact": artifact,
        "watch": None,
    }


def run_water_sites(params: dict, client: WaterClient) -> dict:
    sites = client.busiest_sites(limit=int(params.get("limit", 20)))
    listing = ", ".join(f"{row.get('sample_site')} ({row.get('samples')} samples, latest {row.get('latest', '')[:10]})" for row in sites[:5])
    return {
        "final_state": "completed",
        "message": (
            f"{len(sites)} monitoring site(s) by sample volume: {listing}. Site codes are monitoring "
            "stations published without coordinates, so I cannot map one to a street address."
        ),
        "artifact": {
            "dataset": DATASET_WATER,
            "dataset_title": DATASET_WATER_TITLE,
            "freshness": client.freshness(),
            "count": len(sites),
            "sites": sites,
        },
        "watch": None,
    }


def run_water_watch(params: dict, client: WaterClient) -> dict:
    site = client.check_site(params["site"])
    rows = client.latest_samples(site=site, limit=1)
    if not rows:
        return {
            "final_state": "completed",
            "message": (
                f"No published samples for site {site}, so there is nothing to watch. Ask for the "
                "site list to see which codes exist."
            ),
            "artifact": {
                "dataset": DATASET_WATER,
                "dataset_title": DATASET_WATER_TITLE,
                "freshness": client.freshness(),
                "found": False,
                "site": site,
            },
            "watch": None,
        }
    latest = rows[0]
    observed = _observation(latest)
    return {
        "final_state": "completed",
        "message": (
            f"Watching site {site}. Latest sample {latest.get('sample_number')} on "
            f"{latest.get('sample_date', '')[:10]} {latest.get('sample_time', '')} — free chlorine "
            f"{latest.get('residual_free_chlorine_mg_l')} mg/L, turbidity {latest.get('turbidity_ntu')} NTU, "
            "coliform " + str(latest.get("coliform_quanti_tray_mpn_100ml")) + ". Point a "
            "pushNotificationConfig at this task and I will POST when a newer sample appears."
        ),
        "artifact": {
            "dataset": DATASET_WATER,
            "dataset_title": DATASET_WATER_TITLE,
            "freshness": client.freshness(),
            "site": site,
            "latest_sample": latest,
            "caveat": CAVEAT,
        },
        "watch": {
            "kind": SKILL_WATER_WATCH,
            "site": site,
            "dataset": DATASET_WATER,
            "observed": observed,
        },
    }


def _observation(sample: dict | None) -> dict:
    if not sample:
        return {"sample_number": "", "sample_date": "", "sample_time": ""}
    return {
        "sample_number": sample.get("sample_number") or "",
        "sample_date": sample.get("sample_date") or "",
        "sample_time": sample.get("sample_time") or "",
    }


class WaterAgent(SkillAgent):
    name = "nycwater"
    card_name = "NYC Drinking Water Agent"
    card_description = (
        "Read-only agent over New York City's drinking water distribution monitoring data "
        "(bkwf-xfky): free chlorine, turbidity, coliform and E. coli results per monitoring site, the "
        "list of site codes, and a watch skill that POSTs to your webhook when a site logs a new "
        "sample. Sites are published as codes without coordinates, so answers are per site, not per "
        "address. Raw monitoring records, not a health verdict."
    )
    env_prefix = "NYC_WATER"
    datasets = (DATASET_WATER,)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_WATER_WATCH,)

    def __init__(self, client: WaterClient | None = None) -> None:
        self.client = client or WaterClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_WATER_WATCH:
            return [] if params.get("site") else ["site"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        return (
            "Which water monitoring site? Site codes look like 55450 or 1S03A — ask for the site "
            "list if you need to see them."
        )

    def run(self, request: dict) -> dict:
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(request["skill"], request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if request["skill"] == SKILL_WATER_SITES:
            return run_water_sites(request["params"], self.client)
        if request["skill"] == SKILL_WATER_WATCH:
            return run_water_watch(request["params"], self.client)
        return run_water_quality(request["params"], self.client)

    def probe_watch(self, watch: dict):
        site = watch.get("site")
        if not site:
            return None
        rows = self.client.latest_samples(site=site, limit=1)
        if not rows:
            return None
        return _observation(rows[0])

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        site = watch.get("site")
        sample = observed or {}
        return (
            f"New drinking water sample at site {site}: number {sample.get('sample_number')} on "
            f"{str(sample.get('sample_date'))[:10]} {sample.get('sample_time')}."
        )


__all__ = [
    "CARD_SKILLS",
    "SKILL_WATER_QUALITY",
    "SKILL_WATER_SITES",
    "SKILL_WATER_WATCH",
    "UpstreamError",
    "WaterAgent",
    "message_data",
    "message_text",
    "parse",
    "run_water_quality",
    "run_water_sites",
    "run_water_watch",
]
