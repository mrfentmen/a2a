"""Read-only client for the National Weather Service alerts API.

Endpoint: https://api.weather.gov/alerts/active (verified live 2026-09-22: 386 active
alerts nationwide, 4 in New York State). Keyless; NWS asks for a contactable
User-Agent, so override <PREFIX>_USER_AGENT in production deployments.

Notice the API rejects unknown query parameters (a `limit` parameter is a 400), so
paging and sorting happen here, not on the server.
"""

from __future__ import annotations

import re

from a2a_kit import JsonApiClient, UpstreamError, utc_now_iso

BASE_URL = "https://api.weather.gov"
DATASET = "api.weather.gov/alerts/active"
PRODUCT = "NWS active weather alerts"

ALERT_FIELDS = (
    "id",
    "areaDesc",
    "event",
    "severity",
    "certainty",
    "urgency",
    "onset",
    "ends",
    "effective",
    "headline",
    "instruction",
    "senderName",
    "messageType",
    "category",
    "response",
)

SEVERITY_ORDER = ("Extreme", "Severe", "Moderate", "Minor", "Unknown")

#: Two-letter NWS area codes (states, DC, PR) plus the land/marine regions NWS uses.
STATE_CODES = {
    code: name
    for code, name in (
        ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
        ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
        ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
        ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
        ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
        ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
        ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
        ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
        ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
        ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
        ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
        ("PR", "Puerto Rico"), ("VI", "Virgin Islands"), ("GU", "Guam"), ("AS", "American Samoa"),
        ("MP", "Northern Mariana Islands"),
    )
}
STATE_NAMES = {name.lower(): code for code, name in STATE_CODES.items()}

_AREA_RE = re.compile(r"^[A-Z]{2}$")
_ZONE_RE = re.compile(r"^[A-Z]{3}\d{3}$")
_POINT_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_NATIONAL = "the United States"

__all__ = [
    "ALERT_FIELDS",
    "BASE_URL",
    "DATASET",
    "NWSAlertsClient",
    "PRODUCT",
    "SEVERITY_ORDER",
    "STATE_CODES",
    "STATE_NAMES",
    "UpstreamError",
]


class NWSAlertsClient(JsonApiClient):
    env_prefix = "NWS"
    base_url = BASE_URL

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_area(area: str) -> str:
        value = str(area).strip().upper()
        if not _AREA_RE.match(value):
            raise ValueError("an NWS area code is two letters, for example NY or TX")
        return value

    @staticmethod
    def check_zone(zone: str) -> str:
        value = str(zone).strip().upper()
        if not _ZONE_RE.match(value):
            raise ValueError("an NWS zone id looks like NYZ072")
        return value

    @staticmethod
    def check_point(point: str) -> str:
        value = str(point).strip()
        match = _POINT_RE.match(value)
        if not match:
            raise ValueError("an NWS point is 'lat,lon', for example 40.71,-74.01")
        lat, lon = float(match.group(1)), float(match.group(2))
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        return f"{lat:.4f},{lon:.4f}"

    @staticmethod
    def place_label(area: str | None = None, zone: str | None = None, point: str | None = None) -> str:
        if area:
            return STATE_CODES.get(area, area)
        if zone:
            return f"zone {zone}"
        if point:
            return f"the point {point}"
        return _NATIONAL

    # -- reads -------------------------------------------------------------

    def active_alerts(self, area: str | None = None, zone: str | None = None, point: str | None = None,
                      severity: str | None = None, event_contains: str | None = None,
                      limit: int = 20) -> list[dict]:
        """Active alerts, most severe first. `limit` is applied here (the API has no limit)."""
        params: dict[str, str] = {}
        if area:
            params["area"] = self.check_area(area)
        if zone:
            params["zone"] = self.check_zone(zone)
        if point:
            params["point"] = self.check_point(point)
        if severity:
            value = str(severity).strip().title()
            if value not in SEVERITY_ORDER:
                raise ValueError(f"severity must be one of: {', '.join(SEVERITY_ORDER)}")
            params["severity"] = value

        payload = self.get_json("/alerts/active", params, ttl=min(self.cache_ttl, 60))
        features = payload.get("features") if isinstance(payload, dict) else None
        if features is None:
            raise UpstreamError("NWS returned an unexpected payload (no features)")

        alerts = [self.pick(feature.get("properties") or {}, ALERT_FIELDS, DATASET) for feature in features]
        if event_contains:
            needle = str(event_contains).strip().lower()
            alerts = [alert for alert in alerts if needle in (alert.get("event") or "").lower()]
        alerts.sort(key=lambda alert: (SEVERITY_ORDER.index(alert.get("severity", "Unknown"))
                                       if alert.get("severity") in SEVERITY_ORDER else len(SEVERITY_ORDER),
                                       alert.get("onset") or ""))
        return alerts[: max(1, min(int(limit), 100))]

    def alert(self, alert_id: str) -> dict | None:
        value = str(alert_id).strip()
        if not value:
            raise ValueError("an alert id is required")
        if not re.fullmatch(r"[A-Za-z0-9:.\-]{8,120}", value):
            raise ValueError("that does not look like an NWS alert id")
        payload = self.get_json(f"/alerts/{value}", {}, ttl=300)
        properties = payload.get("properties") if isinstance(payload, dict) else None
        if not properties:
            return None
        return self.pick(properties, ALERT_FIELDS, DATASET)

    def counts(self) -> dict:
        """Live national counts: total / land / marine, plus a per-area breakdown."""
        payload = self.get_json("/alerts/active/count", {}, ttl=min(self.cache_ttl, 60))
        if not isinstance(payload, dict) or "total" not in payload:
            raise UpstreamError("NWS returned an unexpected count payload")
        return {
            "total": int(payload.get("total", 0)),
            "land": int(payload.get("land", 0)),
            "marine": int(payload.get("marine", 0)),
            "areas": payload.get("areas") or {},
            "regions": payload.get("regions") or {},
        }

    def freshness(self) -> str:
        """NWS alerts are a live feed, so freshness is the moment we read them."""
        return utc_now_iso()
