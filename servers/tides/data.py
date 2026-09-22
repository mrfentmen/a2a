"""Read-only client for the NOAA CO-OPS tides and currents API.

Endpoints (keyless, verified live 2026-09-22):
  - /api/prod/datagetter                        tide predictions, observed water level
  - /mdapi/prod/webapi/stations.json            every tide station (3499 of them)
  - /mdapi/prod/webapi/stations/{id}.json       one station's metadata
  - /mdapi/prod/webapi/stations/{id}/floodlevels.json   flood stages for that station

Three things about this API shape the client:

1. Every datagetter call needs `application` (the caller's identity), `units`,
   `time_zone`, `datum` and `format` — missing any of them is an HTTP 400.
2. `state=NY` on the station list is silently ignored: NOAA returns all 3499
   stations (about 2 MB). So the list is cached for a day and filtered here.
3. Station records carry `lng`, not `lon`, and flood levels arrive in feet
   referenced to MLLW whatever `units` says, so comparisons convert first.

Times are requested in `lst_ldt` (station local time, daylight saving aware),
which is what a tide table shows. That also gives us the station's own clock:
the latest observation is used as "now" for every time comparison, so no offset
maths is needed anywhere.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone

from a2a_kit import JsonApiClient, UpstreamError, utc_now_iso

BASE_URL = "https://api.tidesandcurrents.noaa.gov"
DATAGETTER_PATH = "/api/prod/datagetter"
STATIONS_PATH = "/mdapi/prod/webapi/stations.json"
STATION_PATH = "/mdapi/prod/webapi/stations/{station}.json"
FLOODLEVELS_PATH = "/mdapi/prod/webapi/stations/{station}/floodlevels.json"

DATASET = "api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATIONS_DATASET = "api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations"
PRODUCT = "NOAA CO-OPS tide predictions and observed water levels"

#: NOAA requires an `application` parameter identifying the caller.
APPLICATION = "a2a-tides"

TIME_ZONE = "lst_ldt"
UNITS = ("english", "metric")
UNIT_LABELS = {"english": "ft", "metric": "m"}
DATUMS = ("MLLW", "MSL", "NAVD", "STND", "MHHW")
FLOOD_STAGES = ("action", "minor", "moderate", "major")
#: Flood levels come back in feet, referenced to MLLW.
FLOOD_LEVEL_UNITS = "english"
#: NOAA's quality flag on an observation: preliminary, verified, or missing.
QUALITY = {"p": "preliminary", "v": "verified", "": "not reported"}

STATION_FIELDS = ("id", "name", "state", "lat", "lng", "tideType", "timezonecorr", "affiliations")

_STATION_RE = re.compile(r"^\d{7}$")
_POINT_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'’\-/()]{1,70}$")

__all__ = [
    "APPLICATION",
    "BASE_URL",
    "DATAGETTER_PATH",
    "DATASET",
    "DATUMS",
    "FLOODLEVELS_PATH",
    "FLOOD_LEVEL_UNITS",
    "FLOOD_STAGES",
    "NoaaTidesClient",
    "PRODUCT",
    "STATIONS_DATASET",
    "STATIONS_PATH",
    "STATION_FIELDS",
    "STATION_PATH",
    "TIME_ZONE",
    "UNIT_LABELS",
    "UNITS",
    "UpstreamError",
    "convert",
    "haversine_km",
    "to_feet",
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def convert(value: float, from_units: str, to_units: str) -> float:
    """Feet and metres, nothing else, because that is all NOAA returns."""
    if from_units == to_units:
        return float(value)
    return float(value) * 0.3048 if from_units == "english" else float(value) / 0.3048


def to_feet(value: float, units: str) -> float:
    """NOAA flood stages are always feet, so metric readings convert before comparing."""
    return convert(value, units, FLOOD_LEVEL_UNITS)


class NoaaTidesClient(JsonApiClient):
    env_prefix = "NOAA_TIDES"
    base_url = BASE_URL

    def __init__(self, fetch=None, base_url=None, cache_ttl=None, timeout=None, headers=None) -> None:
        super().__init__(fetch=fetch, base_url=base_url, cache_ttl=cache_ttl, timeout=timeout, headers=headers)
        self.units = os.environ.get(f"{self.env_prefix}_UNITS", "english").strip().lower() or "english"
        if self.units not in UNITS:
            raise ValueError(f"{self.env_prefix}_UNITS must be one of: {', '.join(UNITS)}")
        self.datum = os.environ.get(f"{self.env_prefix}_DATUM", "MLLW").strip().upper() or "MLLW"
        if self.datum not in DATUMS:
            raise ValueError(f"{self.env_prefix}_DATUM must be one of: {', '.join(DATUMS)}")
        self.station_ttl = float(os.environ.get(f"{self.env_prefix}_STATION_TTL", "86400"))

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_station(value) -> str:
        station = str(value).strip()
        if not _STATION_RE.match(station):
            raise ValueError("a NOAA station id is seven digits, for example 8518750 (The Battery)")
        return station

    @staticmethod
    def check_units(value, default: str = "english") -> str:
        units = str(value or default).strip().lower()
        if units not in UNITS:
            raise ValueError(f"units must be one of: {', '.join(UNITS)}")
        return units

    @staticmethod
    def check_datum(value) -> str:
        datum = str(value).strip().upper()
        if datum not in DATUMS:
            raise ValueError(f"datum must be one of: {', '.join(DATUMS)}")
        return datum

    @staticmethod
    def check_days(value) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise ValueError("days must be a whole number") from None
        if not 1 <= days <= 7:
            raise ValueError("days must be between 1 and 7")
        return days

    @staticmethod
    def check_point(point) -> tuple[float, float]:
        match = _POINT_RE.match(str(point))
        if not match:
            raise ValueError("a point is 'lat,lon', for example 40.7006,-74.0142")
        lat, lon = float(match.group(1)), float(match.group(2))
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return round(lat, 4), round(lon, 4)

    @staticmethod
    def check_radius_km(value) -> float:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            raise ValueError("radius must be a number of kilometres") from None
        if not 1 <= radius <= 2000:
            raise ValueError("radius must be between 1 and 2000 km")
        return radius

    @staticmethod
    def check_threshold(value) -> float:
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            raise ValueError("a threshold must be a number, in the station's units") from None
        if not -50 <= threshold <= 1000:
            raise ValueError("a threshold must be between -50 and 1000")
        return round(threshold, 3)

    @staticmethod
    def check_flood_stage(value) -> str:
        stage = str(value).strip().lower()
        if stage not in FLOOD_STAGES:
            raise ValueError(f"flood stage must be one of: {', '.join(FLOOD_STAGES)}")
        return stage

    @staticmethod
    def check_limit(value) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("limit must be a whole number") from None
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return limit

    @staticmethod
    def check_name(value) -> str:
        name = str(value).strip()
        if not _NAME_RE.match(name):
            raise ValueError("that does not look like a station name")
        return name

    @staticmethod
    def place_label(station_id: str, name: str | None = None, state: str | None = None) -> str:
        if name:
            return f"{name} ({station_id})" + (f", {state}" if state else "")
        return f"station {station_id}"

    # -- transport ---------------------------------------------------------

    def _datagetter(self, product: str, params: dict, ttl: float) -> dict:
        query = {
            "product": product,
            "application": APPLICATION,
            "format": "json",
            "units": self.units,
            "time_zone": TIME_ZONE,
            "datum": self.datum,
        }
        query.update(params)
        payload = self.get_json(DATAGETTER_PATH, query, ttl=ttl)
        if not isinstance(payload, dict):
            raise UpstreamError("NOAA CO-OPS returned an unexpected payload")
        # The datagetter reports failures in the body, with HTTP 400.
        problem = payload.get("error")
        if isinstance(problem, dict):
            raise UpstreamError(f"NOAA CO-OPS error: {problem.get('message') or 'unknown error'}")
        if isinstance(problem, str):
            raise UpstreamError(f"NOAA CO-OPS error: {problem}")
        return payload

    # -- reads -------------------------------------------------------------

    def latest_observation(self, station: str) -> dict | None:
        """The most recent observed water level, plus the station's own clock."""
        station = self.check_station(station)
        payload = self._datagetter(
            "water_level", {"station": station, "date": "latest"}, ttl=min(self.cache_ttl, 120)
        )
        rows = payload.get("data") or []
        if not rows:
            return None
        row = rows[0]
        metadata = payload.get("metadata") or {}
        try:
            value = float(row.get("v"))
        except (TypeError, ValueError):
            return None
        return {
            "station": station,
            "name": metadata.get("name") or "",
            "latitude": _to_float(metadata.get("lat")),
            "longitude": _to_float(metadata.get("lon")),
            "time": row.get("t"),
            "value": value,
            "units": self.units,
            "unit_label": UNIT_LABELS[self.units],
            "sigma": _to_float(row.get("s")),
            "flags": row.get("f"),
            "quality": row.get("q"),
            "quality_label": QUALITY.get(str(row.get("q")), str(row.get("q") or "unknown")),
            "datum": self.datum,
            "dataset": DATASET,
        }

    def predictions(self, station: str, days: int = 2, interval: str = "hilo") -> list[dict]:
        """High/low tide predictions for the next `days` in station local time.

        The window is padded by a day on each side: `begin_date` is interpreted in
        station local time, and our clock is UTC, so padding is what keeps the
        boundary events (the ones a caller actually wants) inside the response.
        """
        station = self.check_station(station)
        days = self.check_days(days)
        today = datetime.now(timezone.utc).date()
        params = {
            "station": station,
            "begin_date": (today - timedelta(days=1)).strftime("%Y%m%d"),
            "end_date": (today + timedelta(days=days + 1)).strftime("%Y%m%d"),
            "interval": interval,
        }
        payload = self._datagetter("predictions", params, ttl=min(self.cache_ttl, 900))
        rows = payload.get("predictions")
        if rows is None:
            raise UpstreamError("NOAA CO-OPS returned an unexpected predictions payload")
        events = []
        for row in rows:
            value = _to_float(row.get("v"))
            if value is None:
                continue
            kind = row.get("type")
            events.append({
                "time": row.get("t"),
                "value": value,
                "units": self.units,
                "unit_label": UNIT_LABELS[self.units],
                "type": {"H": "high", "L": "low"}.get(kind, kind or ""),
            })
        return events

    def upcoming_tides(self, station: str, days: int = 2, limit: int = 12) -> dict:
        """High/low events from the station's current local time forward."""
        observation = self.latest_observation(station)
        events = self.predictions(station, days=days)
        now = (observation or {}).get("time") or ""
        upcoming = [event for event in events if not now or (event.get("time") or "") > now]
        label = self.place_label(station, (observation or {}).get("name"))
        return {
            "station": station,
            "name": (observation or {}).get("name") or "",
            "place": label,
            "observed_at": now,
            "units": self.units,
            "unit_label": UNIT_LABELS[self.units],
            "datum": self.datum,
            "days": days,
            "events": upcoming[: self.check_limit(limit)],
            "total_events": len(upcoming),
        }

    def next_tides(self, station: str) -> dict:
        """The next high and the next low from the station's clock forward."""
        table = self.upcoming_tides(station, days=2, limit=50)
        next_high = next((event for event in table["events"] if event.get("type") == "high"), None)
        next_low = next((event for event in table["events"] if event.get("type") == "low"), None)
        table["events"] = [event for event in (next_high, next_low) if event]
        table["next_high"] = next_high
        table["next_low"] = next_low
        return table

    def station_info(self, station: str) -> dict | None:
        station = self.check_station(station)
        payload = self.get_json(STATION_PATH.format(station=station), {}, ttl=self.station_ttl)
        rows = payload.get("stations") if isinstance(payload, dict) else None
        if not rows:
            return None
        return self.pick(rows[0], STATION_FIELDS)

    def flood_levels(self, station: str) -> dict:
        """Flood stages for a station, in feet over MLLW. Empty dict when NOAA has none."""
        station = self.check_station(station)
        payload = self.get_json(FLOODLEVELS_PATH.format(station=station), {}, ttl=self.station_ttl)
        if not isinstance(payload, dict):
            return {}
        levels = {}
        for source in ("nos", "nws"):
            for stage in FLOOD_STAGES:
                value = _to_float(payload.get(f"{source}_{stage}"))
                if value is not None:
                    levels[f"{source}_{stage}"] = value
        action = _to_float(payload.get("action"))
        if action is not None:
            levels["action"] = action
        levels["units"] = FLOOD_LEVEL_UNITS
        return levels

    def threshold_for_stage(self, station: str, stage: str, units: str | None = None) -> float | None:
        """Resolve a named flood stage to a number in `units` (default: this client's).

        NOAA publishes flood stages in feet, so anything else converts from feet.
        """
        stage = self.check_flood_stage(stage)
        target = self.check_units(units, default=self.units)
        levels = self.flood_levels(station)
        value = levels.get(f"nos_{stage}")
        if value is None:
            value = levels.get(f"nws_{stage}")
        if value is None and stage == "action":
            value = levels.get("action")
        if value is None:
            return None
        return round(convert(value, FLOOD_LEVEL_UNITS, target), 3)

    def flood_stage_for_value(self, station: str, value: float) -> str:
        """Which named stage a water level is at, comparing in feet."""
        levels = self.flood_levels(station)
        if not levels:
            return "unknown"
        value_ft = to_feet(value, self.units)
        reached = [
            stage for stage in FLOOD_STAGES
            if (levels.get(f"nos_{stage}") or levels.get(f"nws_{stage}")) is not None
            and value_ft >= (levels.get(f"nos_{stage}") or levels.get(f"nws_{stage}"))
        ]
        if not reached:
            return "normal"
        return {"action": "action stage", "minor": "minor flooding",
                "moderate": "moderate flooding", "major": "major flooding"}[reached[-1]]

    def stations(self, name: str | None = None, state: str | None = None, point: str | None = None,
                 radius_km: float = 50.0, limit: int = 10) -> list[dict]:
        """Find tide stations. Named stations match on a case-insensitive substring."""
        limit = self.check_limit(limit)
        needle = self.check_name(name).lower() if name else None
        wanted_state = str(state).strip().upper() if state else None
        origin = self.check_point(point) if point else None
        radius = self.check_radius_km(radius_km) if origin else None

        rows = self._station_list()
        matches = []
        for row in rows:
            if needle and needle not in (row.get("name") or "").lower():
                continue
            if wanted_state and (row.get("state") or "").upper() != wanted_state:
                continue
            station = self.pick(row, STATION_FIELDS)
            latitude, longitude = _to_float(row.get("lat")), _to_float(row.get("lng"))
            if origin and (latitude is None or longitude is None):
                continue
            if origin:
                station["distance_km"] = round(haversine_km(origin[0], origin[1], latitude, longitude), 1)
            matches.append(station)
        if origin:
            matches.sort(key=lambda station: station["distance_km"])
        else:
            matches.sort(key=lambda station: (station.get("state") or "", station.get("name") or ""))
        return matches[:limit]

    def nearest_station(self, point: str, radius_km: float = 50.0) -> dict | None:
        found = self.stations(point=point, radius_km=radius_km, limit=1)
        if not found:
            return None
        if found[0].get("distance_km", 0) > self.check_radius_km(radius_km):
            return None
        return found[0]

    def _station_list(self) -> list[dict]:
        """Every tide station (about 2 MB). Cached for a day by get_json."""
        payload = self.get_json(STATIONS_PATH, {"type": "tidepredictions"}, ttl=self.station_ttl)
        rows = payload.get("stations") if isinstance(payload, dict) else None
        if rows is None:
            raise UpstreamError("NOAA CO-OPS returned an unexpected station list payload")
        return rows

    @staticmethod
    def freshness() -> str:
        """Both feeds are live, so freshness is the moment we read them."""
        return utc_now_iso()


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
