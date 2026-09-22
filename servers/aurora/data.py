"""Read-only reader for NOAA's Space Weather Prediction Center (SWPC) products.

Every endpoint is keyless and was verified live on 2026-09-22:

  /products/noaa-planetary-k-index.json           3-hourly planetary Kp, last ~7 days
  /products/noaa-planetary-k-index-forecast.json  3-hourly Kp, mixing observed and predicted
  /products/alerts.json                           SWPC watches, warnings, alerts and summaries
  /json/planetary_k_index_1m.json                 the 1-minute estimated Kp (right now)
  /json/ovation_aurora_latest.json                aurora probability on a 1-degree global grid

The quirks, all handled here:

1. Four of the five products are bare JSON arrays, not objects with a results wrapper.
2. The forecast file mixes observed and predicted rows in one list; only rows whose
   `observed` field says "predicted" are futures, and only those are forecast.
3. The 1-minute Kp file keeps its latest estimate under `estimated_kp`.
4. OVATION is a ~900 KB grid of [longitude, latitude, probability] triples covering the
   whole planet every degree, so it is cached hard and only the neighbourhood around the
   asked-for point is read.
5. SWPC message text is CRLF-separated with the code on the first line and the headline
   after "Issue Time"; both are parsed out here so callers get fields, not a blob.
"""

from __future__ import annotations

import re

from a2a_kit import JsonApiClient

KP_3H = "/products/noaa-planetary-k-index.json"
KP_FORECAST = "/products/noaa-planetary-k-index-forecast.json"
ALERTS = "/products/alerts.json"
KP_1M = "/json/planetary_k_index_1m.json"
OVATION = "/json/ovation_aurora_latest.json"

DATASET_KP = "swpc.noaa.gov/planetary-k-index"
DATASET_FORECAST = "swpc.noaa.gov/planetary-k-index-forecast"
DATASET_ALERTS = "swpc.noaa.gov/alerts"
DATASET_KP_1M = "swpc.noaa.gov/planetary-k-index-1m"
DATASET_OVATION = "swpc.noaa.gov/ovation-aurora"

#: NOAA's geomagnetic storm scale: Kp 5 and up is a storm, graded G1-G5.
STORM_SCALE = {
    5: "G1 (minor)",
    6: "G2 (moderate)",
    7: "G3 (strong)",
    8: "G4 (severe)",
    9: "G5 (extreme)",
}

#: Message kinds SWPC issues, longest first so "EXTENDED WARNING" wins over "WARNING".
MESSAGE_KINDS = ("EXTENDED WARNING", "WARNING", "WATCH", "ALERT", "SUMMARY", "CANCELLATION", "CANCEL")

_CODE_RE = re.compile(r"Space Weather Message Code:\s*(\S+)")
_SERIAL_RE = re.compile(r"Serial Number:\s*(\d+)")
_ISSUE_RE = re.compile(r"Issue Time:\s*([^\r\n]+)")
_KIND_RE = re.compile(r"\b(" + "|".join(kind.replace(" ", r"\s+") for kind in MESSAGE_KINDS) + r")\b")
_GSCALE_RE = re.compile(r"\bG([1-5])\b")

#: Coordinates for places people ask about, so callers need not know their latitude.
#: This is reference geography shipped with the server, not data from NOAA.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "boston": (42.36, -71.06), "philadelphia": (39.95, -75.17),
    "washington": (38.91, -77.04), "atlanta": (33.75, -84.39), "chicago": (41.88, -87.63),
    "detroit": (42.33, -83.05), "minneapolis": (44.98, -93.27), "st louis": (38.63, -90.20),
    "kansas city": (39.10, -94.58), "cleveland": (41.50, -81.69), "pittsburgh": (40.44, -79.99),
    "buffalo": (42.89, -78.88), "burlington": (44.48, -73.21), "denver": (39.74, -104.99),
    "salt lake city": (40.76, -111.89), "boise": (43.62, -116.20), "billings": (45.78, -108.50),
    "fargo": (46.88, -96.79), "duluth": (46.79, -92.10), "seattle": (47.61, -122.33),
    "portland": (45.52, -122.68), "san francisco": (37.77, -122.42), "los angeles": (34.05, -118.24),
    "phoenix": (33.45, -112.07), "dallas": (32.78, -96.80), "miami": (25.76, -80.19),
    "honolulu": (21.31, -157.86), "anchorage": (61.22, -149.90), "fairbanks": (64.84, -147.72),
    "toronto": (43.65, -79.38), "montreal": (45.50, -73.57), "vancouver": (49.28, -123.12),
    "winnipeg": (49.90, -97.14), "saskatoon": (52.13, -106.67), "yellowknife": (62.45, -114.37),
    "reykjavik": (64.15, -21.94), "tromso": (69.65, -18.96), "oslo": (59.91, 10.75),
    "stockholm": (59.33, 18.07), "helsinki": (60.17, 24.94), "copenhagen": (55.68, 12.57),
    "edinburgh": (55.95, -3.19), "dublin": (53.35, -6.26), "london": (51.51, -0.13),
    "berlin": (52.52, 13.40), "warsaw": (52.23, 21.01), "kyiv": (50.45, 30.52),
    "moscow": (55.76, 37.62), "murmansk": (68.97, 33.08), "tokyo": (35.68, 139.69),
    "beijing": (39.90, 116.41), "sapporo": (43.06, 141.35), "sydney": (-33.87, 151.21),
    "melbourne": (-37.81, 144.96), "auckland": (-36.85, 174.76), "dunedin": (-45.87, 170.50),
    "cape town": (-33.92, 18.42), "buenos aires": (-34.60, -58.38), "ushuaia": (-54.80, -68.30),
}


def kp_band(kp: float | None) -> str:
    """Plain words for a Kp value, using NOAA's own storm grading from 5 up."""
    if kp is None:
        return "unknown"
    if kp >= 5:
        return STORM_SCALE.get(int(min(round(kp), 9)), "G5 (extreme)")
    if kp >= 4:
        return "active"
    if kp >= 3:
        return "unsettled"
    return "quiet"


def is_storm(kp: float | None) -> bool:
    return kp is not None and kp >= 5


class SpaceWeatherClient(JsonApiClient):
    """SWPC products: Kp now and forecast, storm messages, and aurora probability."""

    env_prefix = "AURORA"
    base_url = "https://services.swpc.noaa.gov"

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def check_lat(value, name: str = "latitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -90 <= number <= 90:
            raise ValueError(f"{name} must be between -90 and 90")
        return number

    @staticmethod
    def check_lon(value, name: str = "longitude") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -180 <= number <= 180:
            raise ValueError(f"{name} must be between -180 and 180")
        return number

    @staticmethod
    def check_kp(value, name: str = "kp threshold") -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not 1 <= number <= 9:
            raise ValueError(f"{name} must be between 1 and 9")
        return number

    @staticmethod
    def check_point(value) -> tuple[float, float]:
        text = re.sub(r"\s+", "", str(value or ""))
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 64.84,-147.72")
        return SpaceWeatherClient.check_lat(parts[0]), SpaceWeatherClient.check_lon(parts[1])

    @staticmethod
    def city(name: str) -> tuple[float, float] | None:
        key = " ".join(str(name or "").lower().replace("-", " ").split())
        if key in CITY_COORDS:
            return CITY_COORDS[key]
        for known, coords in CITY_COORDS.items():
            if key and (key in known or known in key):
                return coords
        return None

    # -- reads -------------------------------------------------------------

    def kp_now(self) -> dict:
        """The current 1-minute estimated Kp, with the latest 3-hourly row alongside it."""
        minute = self.get_json(KP_1M, ttl=min(self.cache_ttl, 120))
        latest_minute = minute[-1] if isinstance(minute, list) and minute else {}
        kp = latest_minute.get("estimated_kp")
        kp = float(kp) if kp is not None else None
        rows = self.kp_rows()
        latest_3h = rows[-1] if rows else {}
        return {
            "dataset": DATASET_KP_1M,
            "time_tag": latest_minute.get("time_tag"),
            "estimated_kp": kp,
            "band": kp_band(kp),
            "storm": is_storm(kp),
            "three_hourly": {
                "dataset": DATASET_KP,
                "time_tag": latest_3h.get("time_tag"),
                "kp": latest_3h.get("Kp"),
                "a_running": latest_3h.get("a_running"),
                "station_count": latest_3h.get("station_count"),
            },
        }

    def kp_rows(self, limit: int = 8) -> list[dict]:
        """The most recent 3-hourly Kp rows (8 rows is 24 hours)."""
        payload = self.get_json(KP_3H, ttl=min(self.cache_ttl, 300))
        rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        return rows[-max(1, limit):]

    def recent(self, hours: int = 24) -> dict:
        """Current conditions plus the peak of the last `hours` (3-hourly rows)."""
        rows = self.kp_rows(limit=max(1, int(hours) // 3))
        peak = max((float(row.get("Kp") or 0) for row in rows), default=None)
        now = self.kp_now()
        return {
            "now": now,
            "window_hours": hours,
            "peak_kp": peak,
            "peak_band": kp_band(peak),
            "peak_time_tag": next(
                (row.get("time_tag") for row in reversed(rows) if float(row.get("Kp") or 0) >= (peak or 0)),
                None,
            ),
            "rows": rows,
            "dataset": DATASET_KP,
        }

    def forecast(self, days: int = 3) -> dict:
        """Predicted Kp for the next days, grouped by UTC day, highest first per day."""
        payload = self.get_json(KP_FORECAST, ttl=min(self.cache_ttl, 900))
        rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        predicted = [row for row in rows if str(row.get("observed") or "").lower() == "predicted"]
        by_day: dict[str, dict] = {}
        for row in predicted:
            day = str(row.get("time_tag") or "")[:10]
            if not day:
                continue
            kp = float(row.get("kp") or 0)
            entry = by_day.setdefault(day, {"date": day, "max_kp": kp, "rows": []})
            entry["rows"].append({"time_tag": row.get("time_tag"), "kp": kp})
            if kp > entry["max_kp"]:
                entry["max_kp"] = kp
        ordered = [by_day[day] for day in sorted(by_day)][: max(1, int(days))]
        for entry in ordered:
            entry["band"] = kp_band(entry["max_kp"])
            entry["storm"] = is_storm(entry["max_kp"])
        peak = max((entry["max_kp"] for entry in ordered), default=None)
        return {
            "dataset": DATASET_FORECAST,
            "days": ordered,
            "predicted_rows": len(predicted),
            "observed_rows": len(rows) - len(predicted),
            "peak_kp": peak,
            "peak_band": kp_band(peak),
        }

    def messages(self, limit: int = 5, contains: str | None = None) -> dict:
        """SWPC watches, warnings, alerts and summaries, newest first, parsed into fields."""
        payload = self.get_json(ALERTS, ttl=min(self.cache_ttl, 300))
        rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        parsed = [parse_message(row) for row in rows]
        parsed = [item for item in parsed if item]
        if contains:
            needle = str(contains).lower()
            parsed = [item for item in parsed if needle in item["text"].lower()
                      or needle in (item["headline"] or "").lower()
                      or needle in (item["kind"] or "").lower()]
        # The feed is newest-first today, but do not trust file order: sort on issue time,
        # which is a fixed-width timestamp and therefore sorts correctly as a string.
        parsed.sort(key=lambda item: str(item.get("issue_datetime") or ""), reverse=True)
        keep = max(1, int(limit))
        return {"dataset": DATASET_ALERTS, "count": len(parsed), "messages": parsed[:keep]}

    def aurora_probability(self, lat: float, lon: float, radius: float = 2.0) -> dict:
        """The OVATION model's aurora probability at (or near) a point, right now."""
        lat = self.check_lat(lat)
        lon = self.check_lon(lon)
        grid = self.get_json(OVATION, ttl=min(self.cache_ttl, 900))
        coords = grid.get("coordinates") if isinstance(grid, dict) else None
        if not coords:
            raise ValueError("the OVATION grid was empty")
        lon_norm = lon % 360
        best: tuple[float, int, int, int] | None = None  # distance, lon_step, lat_step, probability
        near: list[int] = []
        for lon_step, lat_step, probability in coords:
            if abs(lat_step - lat) > radius:
                continue
            delta_lon = abs(lon_step - lon_norm)
            delta_lon = min(delta_lon, 360 - delta_lon)
            if delta_lon > radius:
                continue
            near.append(int(probability))
            distance = (lat_step - lat) ** 2 + delta_lon ** 2
            if best is None or distance < best[0]:
                best = (distance, int(lon_step), int(lat_step), int(probability))
        if best is None:
            raise ValueError("the OVATION grid had no cell near that point")
        return {
            "dataset": DATASET_OVATION,
            "observation_time": grid.get("Observation Time"),
            "forecast_time": grid.get("Forecast Time"),
            "probability": best[3],
            "nearest_cell": {"latitude": best[2], "longitude": best[1]},
            "radius_degrees": radius,
            "max_probability_nearby": max(near) if near else best[3],
            "cells_read": len(coords),
        }


def parse_message(row: dict) -> dict | None:
    """One raw SWPC product -> fields. Returns None when the row has no message text."""
    text = str(row.get("message") or "")
    if not text.strip():
        return None
    code_match = _CODE_RE.search(text)
    serial_match = _SERIAL_RE.search(text)
    kind_match = _KIND_RE.search(text)
    issue = _ISSUE_RE.search(text)
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    headline = ""
    for index, line in enumerate(lines):
        if line.lower().startswith("issue time"):
            headline = next((candidate for candidate in lines[index + 1:] if candidate), "")
            break
    scale = _GSCALE_RE.search(text)
    return {
        "dataset": DATASET_ALERTS,
        "product_id": row.get("product_id"),
        "issue_datetime": row.get("issue_datetime"),
        "code": code_match.group(1) if code_match else None,
        "serial": serial_match.group(1) if serial_match else None,
        "kind": kind_match.group(1).upper() if kind_match else None,
        "g_scale": f"G{scale.group(1)}" if scale else None,
        "headline": (headline or lines[0])[:200],
        "text": text,
    }


__all__ = [
    "ALERTS",
    "CITY_COORDS",
    "DATASET_ALERTS",
    "DATASET_FORECAST",
    "DATASET_KP",
    "DATASET_KP_1M",
    "DATASET_OVATION",
    "KP_1M",
    "KP_3H",
    "KP_FORECAST",
    "MESSAGE_KINDS",
    "OVATION",
    "STORM_SCALE",
    "SpaceWeatherClient",
    "is_storm",
    "kp_band",
    "parse_message",
]
