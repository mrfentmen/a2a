"""Read-only client for the USGS earthquake catalog (FDSN event web service).

Endpoints (keyless, verified live 2026-09-22):
  - https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson   — event search
  - https://earthquake.usgs.gov/fdsnws/event/1/count?format=geojson   — instant counts

Two things the API insists on and this client handles: every query needs an explicit
time window (otherwise the default window is small), and `orderby` must be one of
time / time-asc / magnitude / magnitude-asc.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from a2a_kit import JsonApiClient, UpstreamError, utc_now_iso

BASE_URL = "https://earthquake.usgs.gov"
DATASET = "earthquake.usgs.gov/fdsnws/event/1"
PRODUCT = "USGS earthquake catalog"

QUERY_PATH = "/fdsnws/event/1/query"
COUNT_PATH = "/fdsnws/event/1/count"
EVENT_PATH = "/fdsnws/event/1/query"

#: Fields kept per event, in artifact order.
QUAKE_FIELDS = (
    "id",
    "magnitude",
    "place",
    "time",
    "updated",
    "depth_km",
    "latitude",
    "longitude",
    "tsunami",
    "alert",
    "significance",
    "felt",
    "type",
    "detail_url",
)

MAGNITUDE_BANDS = (2.5, 4.5, 6.0)
ORDERINGS = ("time", "time-asc", "magnitude", "magnitude-asc")

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,60}$")
_POINT_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

__all__ = [
    "BASE_URL",
    "COUNT_PATH",
    "DATASET",
    "MAGNITUDE_BANDS",
    "ORDERINGS",
    "PRODUCT",
    "QUAKE_FIELDS",
    "QUERY_PATH",
    "USGSQuakeClient",
    "UpstreamError",
]


class USGSQuakeClient(JsonApiClient):
    env_prefix = "USGS"
    base_url = BASE_URL

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_point(point: str) -> tuple[float, float]:
        match = _POINT_RE.match(str(point))
        if not match:
            raise ValueError("a point is 'lat,lon', for example 35.68,139.69")
        lat, lon = float(match.group(1)), float(match.group(2))
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return round(lat, 4), round(lon, 4)

    @staticmethod
    def check_magnitude(value, name: str = "magnitude") -> float:
        try:
            magnitude = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -1.0 <= magnitude <= 10.0:
            raise ValueError(f"{name} must be between -1 and 10")
        return round(magnitude, 1)

    @staticmethod
    def check_hours(value) -> float:
        try:
            hours = float(value)
        except (TypeError, ValueError):
            raise ValueError("hours must be a number") from None
        if not 0.25 <= hours <= 8760:
            raise ValueError("hours must be between 0.25 (15 minutes) and 8760 (one year)")
        return hours

    @staticmethod
    def check_radius_km(value) -> float:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            raise ValueError("radius must be a number of kilometres") from None
        if not 1 <= radius <= 20000:
            raise ValueError("radius must be between 1 and 20000 km")
        return radius

    @staticmethod
    def check_limit(value) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("limit must be a whole number") from None
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return limit

    @staticmethod
    def check_order(value: str) -> str:
        order = str(value).strip().lower()
        if order not in ORDERINGS:
            raise ValueError(f"orderby must be one of: {', '.join(ORDERINGS)}")
        return order

    @staticmethod
    def check_id(quake_id: str) -> str:
        value = str(quake_id).strip()
        if not _ID_RE.match(value):
            raise ValueError("that does not look like a USGS event id (for example us7000abcd)")
        return value

    # -- reads -------------------------------------------------------------

    def _window(self, hours: float) -> str:
        start = datetime.now(timezone.utc) - timedelta(hours=self.check_hours(hours))
        return start.strftime("%Y-%m-%dT%H:%M:%S")

    def recent_quakes(self, min_magnitude: float = 2.5, hours: float = 24.0, limit: int = 10,
                      order: str = "time") -> list[dict]:
        params = {
            "format": "geojson",
            "starttime": self._window(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
            "orderby": self.check_order(order),
            "limit": str(self.check_limit(limit)),
        }
        return self._features(QUERY_PATH, params, ttl=min(self.cache_ttl, 60))

    def quakes_near(self, point: str, radius_km: float = 100.0, min_magnitude: float = 1.0,
                    hours: float = 720.0, limit: int = 10) -> list[dict]:
        latitude, longitude = self.check_point(point)
        params = {
            "format": "geojson",
            "latitude": f"{latitude}",
            "longitude": f"{longitude}",
            "maxradiuskm": f"{self.check_radius_km(radius_km)}",
            "starttime": self._window(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
            "orderby": "time",
            "limit": str(self.check_limit(limit)),
        }
        return self._features(QUERY_PATH, params, ttl=min(self.cache_ttl, 60))

    def quake(self, quake_id: str) -> dict | None:
        params = {"format": "geojson", "eventid": self.check_id(quake_id)}
        payload = self.get_json(QUERY_PATH, params, ttl=300)
        features = payload.get("features") if isinstance(payload, dict) else None
        if not features:
            return None
        return self._one(features[0])

    def count(self, min_magnitude: float = 2.5, hours: float = 24.0) -> int:
        params = {
            "format": "geojson",
            "starttime": self._window(hours),
            "minmagnitude": f"{self.check_magnitude(min_magnitude)}",
        }
        payload = self.get_json(COUNT_PATH, params, ttl=min(self.cache_ttl, 120))
        if not isinstance(payload, dict) or "count" not in payload:
            raise UpstreamError("USGS returned an unexpected count payload")
        return int(payload["count"])

    def counts(self) -> dict:
        """The three counts this agent reports, in one call site so the cache can serve repeats."""
        return {
            "last_24h_m2.5": self.count(2.5, 24),
            "last_24h_m4.5": self.count(4.5, 24),
            "last_7d_m6.0": self.count(6.0, 168),
        }

    # -- payload mapping ---------------------------------------------------

    def _features(self, path: str, params: dict, ttl: float) -> list[dict]:
        payload = self.get_json(path, params, ttl=ttl)
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise UpstreamError("USGS returned an unexpected payload (no features)")
        return [self._one(feature) for feature in features]

    @staticmethod
    def _one(feature: dict) -> dict:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or [None, None, None]
        longitude = coordinates[0] if len(coordinates) > 0 else None
        latitude = coordinates[1] if len(coordinates) > 1 else None
        depth = coordinates[2] if len(coordinates) > 2 else None
        return {
            "id": feature.get("id") or props.get("code") or "",
            "magnitude": props.get("mag"),
            "place": props.get("place"),
            "time": _iso_from_ms(props.get("time")),
            "updated": _iso_from_ms(props.get("updated")),
            "depth_km": depth,
            "latitude": latitude,
            "longitude": longitude,
            "tsunami": props.get("tsunami"),
            "alert": props.get("alert"),
            "significance": props.get("sig"),
            "felt": props.get("felt"),
            "type": props.get("type"),
            "detail_url": props.get("url"),
        }

    @staticmethod
    def freshness() -> str:
        """The catalog is live, so freshness is the moment we read it."""
        return utc_now_iso()


def _iso_from_ms(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return (
            datetime.fromtimestamp(int(value) / 1000, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None
