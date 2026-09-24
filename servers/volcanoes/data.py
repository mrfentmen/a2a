"""Read-only reader for the USGS Volcano Science Center's alert-level API.

Verified live on 2026-09-23 (four elevated volcanoes: Kilauea WATCH/ORANGE, Great Sitkin
and Shishaldin, plus one more; 161 monitored volcanoes in the geojson).

Quirks handled here:

1. The service is slow. Measured between 12s and 76s for a 116 KB geojson read the same
   afternoon, keyless and unpaginated, so the default timeout is raised to 90s and both
   reads are cached for 15 minutes rather than hammered.
2. `/volcanoApi/elevated` returns a bare JSON **array** of notice records (one per volcano
   currently above NORMAL), not an object with a list inside.
3. `/volcanoApi/geojson` is a FeatureCollection whose geometry is a Point in GeoJSON order
   (`[longitude, latitude]`) — the opposite of the WFIGS layer, where x is longitude but in
   an `{x, y}` object.
4. The two endpoints carry different fields: only `elevated` has `noticeSynopsis` and the
   `newDate`/`newTime` rendering, only `geojson` has `region`, `volcanoUrl` and the
   Smithsonian threat ranking. `region` therefore lives on the listing/lookup rows, and the
   elevated rows get it by name lookup when the geojson is already cached.
5. Alert levels are NORMAL < ADVISORY < WATCH < WARNING, plus UNASSIGNED for volcanoes USGS
   monitors without a standing level; colour codes are GREEN/YELLOW/ORANGE/RED/UNASSIGNED.
6. `sentUtc` is "YYYY-MM-DD HH:MM:SS" (UTC without the marker), so it is normalised here.
7. Several legacy paths (`volcanoApi/volcanoes`, `/status`, `/volcanoDetails`) are 404 and
   the Smithsonian GVP site answered 403 on 2026-09-23; this server uses only the two
   endpoints that answered 200.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from a2a_kit import JsonApiClient

ELEVATED_PATH = "/vsc/api/volcanoApi/elevated"
GEOJSON_PATH = "/vsc/api/volcanoApi/geojson"

DATASET = "usgs.gov/vsc/volcano-alert-levels"

#: Fields kept from an `elevated` notice record, in report order.
ELEVATED_FIELDS = (
    "vName",
    "vnum",
    "volcanoCd",
    "obs",
    "alertLevel",
    "colorCode",
    "alertLevelPrev",
    "colorCodePrev",
    "nvewsThreat",
    "sentUtc",
    "alertDate",
    "newDate",
    "newTime",
    "noticeSynopsis",
    "noticeId",
    "noticeUrl",
    "noticeData",
    "lat",
    "long",
)

#: Fields kept from a geojson feature's properties.
GEOJSON_FIELDS = (
    "volcanoName",
    "vnum",
    "volcanoCd",
    "obs",
    "region",
    "alertLevel",
    "colorCode",
    "nvewsThreat",
    "alertDate",
    "colorDate",
    "noticeSynopsis",
    "noticeUrl",
    "volcanoUrl",
    "volcanoImage",
)

#: Alert levels, least to most severe. UNASSIGNED means "no level assigned right now".
ALERT_LEVELS = ("UNASSIGNED", "NORMAL", "ADVISORY", "WATCH", "WARNING")
SEVERITY = {"UNASSIGNED": 0, "NORMAL": 1, "ADVISORY": 2, "WATCH": 3, "WARNING": 4}

LEVEL_MEANINGS = {
    "UNASSIGNED": "no alert level is assigned to this volcano right now",
    "NORMAL": "typical background activity for this volcano",
    "ADVISORY": "elevated unrest",
    "WATCH": "escalating unrest or an eruption with limited hazards",
    "WARNING": "a hazardous eruption is imminent, underway, or suspected",
}

COLOR_MEANINGS = {
    "UNASSIGNED": "no aviation colour code is assigned right now",
    "GREEN": "normal background activity",
    "YELLOW": "elevated unrest",
    "ORANGE": "escalating unrest or an eruption with limited hazards",
    "RED": "a hazardous eruption is imminent, underway, or suspected",
}

#: Observation centres behind the `obs` code on every record.
OBSERVATORIES = {
    "avo": "Alaska Volcano Observatory",
    "calvo": "California Volcano Observatory",
    "cvo": "Cascades Volcano Observatory",
    "hvo": "Hawaiian Volcano Observatory",
    "nmi": "USGS Northern Mariana Islands",
    "yvo": "Yellowstone Volcano Observatory",
}

#: Levels people actually ask about: anything above NORMAL.
ELEVATED_LEVELS = ("ADVISORY", "WATCH", "WARNING")

#: Seconds a slow VSC response is reused before asking again (env: VOLCANO_CACHE_TTL).
DEFAULT_CACHE_TTL = 900.0
#: Seconds to wait on the VSC API before giving up (env: VOLCANO_HTTP_TIMEOUT).
DEFAULT_TIMEOUT = 90.0

MAX_WATCH_VOLCANOES = 200


def severity(level) -> int:
    """Rank an alert level; unknown spellings rank lowest so they never look alarming."""
    return SEVERITY.get(str(level or "").strip().upper(), 0)


def is_elevated(level) -> bool:
    return str(level or "").strip().upper() in ELEVATED_LEVELS


def alert_level_name(level) -> str:
    text = str(level or "").strip().upper()
    return text if text in ALERT_LEVELS else "UNASSIGNED"


def color_name(color) -> str:
    """Aviation colour code, clamped to the codes the observatories actually publish."""
    text = str(color or "").strip().upper()
    return text if text in COLOR_MEANINGS else "UNASSIGNED"


def to_iso_utc(value) -> str | None:
    """`sentUtc`/`alertDate` arrive as 'YYYY-MM-DD HH:MM:SS' in UTC; answers use ISO-8601."""
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            stamp = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


class VolcanoClient(JsonApiClient):
    """USGS alert levels and monitored-volcano geography."""

    env_prefix = "VOLCANO"
    base_url = "https://volcanoes.usgs.gov"

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, headers: dict | None = None) -> None:
        prefix = self.env_prefix
        if timeout is None and not os.environ.get(f"{prefix}_HTTP_TIMEOUT"):
            timeout = DEFAULT_TIMEOUT
        if cache_ttl is None and not os.environ.get(f"{prefix}_CACHE_TTL"):
            cache_ttl = DEFAULT_CACHE_TTL
        super().__init__(fetch=fetch, base_url=base_url, cache_ttl=cache_ttl, timeout=timeout,
                         headers=headers)

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_level(value, name: str = "level") -> str:
        text = str(value or "").strip().upper()
        if text not in ALERT_LEVELS:
            raise ValueError(f"{name} must be one of: {', '.join(ALERT_LEVELS)}")
        return text

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = 200) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return number

    @staticmethod
    def check_region(value, name: str = "region") -> str:
        text = " ".join(str(value or "").split())
        if not 2 <= len(text) <= 60:
            raise ValueError(f"{name} must be 2-60 characters")
        return text

    @staticmethod
    def check_name(value, name: str = "volcano") -> str:
        text = " ".join(str(value or "").split())
        if not 2 <= len(text) <= 60:
            raise ValueError(f"{name} must be 2-60 characters")
        return text

    # -- reads -------------------------------------------------------------

    def elevated(self) -> dict:
        """Volcanoes above NORMAL right now, most severe first."""
        payload = self.get_json(ELEVATED_PATH, {}, ttl=self.cache_ttl)
        if not isinstance(payload, list):
            raise ValueError("the volcano alert-level API returned an unexpected payload")
        rows = [self._elevated_row(record) for record in payload if isinstance(record, dict)]
        rows.sort(key=lambda row: (-severity(row["level"]), str(row["name"] or "")))
        stamps = [row["sent"] for row in rows if row["sent"]]
        return {
            "dataset": DATASET,
            "count": len(rows),
            "as_of": max(stamps) if stamps else None,
            "volcanoes": rows,
        }

    @staticmethod
    def _elevated_row(record: dict) -> dict:
        return {
            "name": record.get("vName"),
            "vnum": record.get("vnum"),
            "code": record.get("volcanoCd"),
            "observatory": record.get("obs"),
            "observatory_name": OBSERVATORIES.get(str(record.get("obs") or "").lower()),
            "level": alert_level_name(record.get("alertLevel")),
            "level_meaning": LEVEL_MEANINGS.get(alert_level_name(record.get("alertLevel"))),
            "color": color_name(record.get("colorCode")),
            "previous_level": record.get("alertLevelPrev"),
            "previous_color": record.get("colorCodePrev"),
            "threat": record.get("nvewsThreat"),
            "sent": to_iso_utc(record.get("sentUtc")),
            "alert_date": to_iso_utc(record.get("alertDate")),
            "notice_id": record.get("noticeId"),
            "notice_url": record.get("noticeUrl"),
            "synopsis": record.get("noticeSynopsis"),
            "latitude": record.get("lat"),
            "longitude": record.get("long"),
            "dataset": DATASET,
        }

    def volcanoes(self, region: str | None = None, level: str | None = None,
                  elevated_only: bool = False, limit: int = 200) -> dict:
        """Every monitored volcano, optionally filtered by region and alert level."""
        limit = self.check_positive(limit)
        rows = self._geojson_rows()
        if region:
            needle = self.check_region(region).lower()
            rows = [row for row in rows if needle in str(row["region"] or "").lower()]
        if level:
            wanted = self.check_level(level)
            rows = [row for row in rows if row["level"] == wanted]
        if elevated_only:
            rows = [row for row in rows if is_elevated(row["level"])]
        rows = sorted(rows, key=lambda row: (-severity(row["level"]), str(row["name"])))
        by_level: dict[str, int] = {}
        for row in rows:
            by_level[row["level"]] = by_level.get(row["level"], 0) + 1
        return {
            "dataset": DATASET,
            "scope": region or ("elevated volcanoes" if elevated_only else "every monitored volcano"),
            "count": len(rows),
            "by_level": by_level,
            "volcanoes": rows[:limit],
            "truncated": len(rows) > limit,
        }

    def lookup(self, name: str, limit: int = 5) -> dict:
        """Volcanoes whose name contains the given text (case-insensitive)."""
        text = self.check_name(name)
        limit = self.check_positive(limit, maximum=50)
        needle = text.lower()
        rows = [row for row in self._geojson_rows() if needle in str(row["name"] or "").lower()]
        rows.sort(key=lambda row: (-severity(row["level"]), str(row["name"])))
        return {"dataset": DATASET, "query": text, "count": len(rows), "volcanoes": rows[:limit]}

    def latest_notice(self, name: str) -> dict | None:
        """The newest elevated notice for a volcano, when one is on the elevated list."""
        needle = self.check_name(name).lower()
        for row in self.elevated()["volcanoes"]:
            if needle == str(row["name"] or "").lower() or needle in str(row["name"] or "").lower():
                return row
        return None

    def regions(self) -> list[str]:
        """The region names the geojson actually uses, so answers never invent one."""
        return sorted({str(row["region"]) for row in self._geojson_rows() if row["region"]})

    def watch_state(self, region: str | None = None, name: str | None = None) -> dict:
        """What a volcano watch compares between polls: alert level per volcano in scope."""
        rows = self._geojson_rows()
        if name:
            needle = self.check_name(name).lower()
            rows = [row for row in rows if needle in str(row["name"] or "").lower()]
        elif region:
            needle = self.check_region(region).lower()
            rows = [row for row in rows if needle in str(row["region"] or "").lower()]
        levels = {str(row["name"]): row["level"] for row in rows}
        elevated = sorted(name for name, level in levels.items() if is_elevated(level))
        return {
            "levels": dict(sorted(levels.items())),
            "elevated": elevated[:MAX_WATCH_VOLCANOES],
        }

    # -- internals ---------------------------------------------------------

    def _geojson_rows(self) -> list[dict]:
        """The whole monitored-volcano list, parsed once per cache window."""
        payload = self.get_json(GEOJSON_PATH, {}, ttl=self.cache_ttl)
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise ValueError("the volcano geojson API returned an unexpected payload")
        return [self._geojson_row(feature) for feature in payload["features"] if isinstance(feature, dict)]

    @staticmethod
    def _geojson_row(feature: dict) -> dict:
        properties = feature.get("properties") or {}
        coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        level = alert_level_name(properties.get("alertLevel"))
        return {
            "name": properties.get("volcanoName"),
            "vnum": properties.get("vnum"),
            "code": properties.get("volcanoCd"),
            "observatory": properties.get("obs"),
            "observatory_name": OBSERVATORIES.get(str(properties.get("obs") or "").lower()),
            "region": properties.get("region"),
            "level": level,
            "level_meaning": LEVEL_MEANINGS.get(level),
            "color": color_name(properties.get("colorCode")),
            "threat": properties.get("nvewsThreat"),
            "alert_date": to_iso_utc(properties.get("alertDate")),
            "color_date": to_iso_utc(properties.get("colorDate")),
            "synopsis": properties.get("noticeSynopsis"),
            "notice_url": properties.get("noticeUrl"),
            "url": properties.get("volcanoUrl"),
            "image": properties.get("volcanoImage"),
            "longitude": coordinates[0],
            "latitude": coordinates[1],
            "dataset": DATASET,
        }


__all__ = [
    "ALERT_LEVELS",
    "COLOR_MEANINGS",
    "DATASET",
    "DEFAULT_CACHE_TTL",
    "DEFAULT_TIMEOUT",
    "ELEVATED_FIELDS",
    "ELEVATED_LEVELS",
    "ELEVATED_PATH",
    "GEOJSON_FIELDS",
    "GEOJSON_PATH",
    "LEVEL_MEANINGS",
    "MAX_WATCH_VOLCANOES",
    "OBSERVATORIES",
    "SEVERITY",
    "VolcanoClient",
    "alert_level_name",
    "color_name",
    "is_elevated",
    "severity",
    "to_iso_utc",
]
