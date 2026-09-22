"""Read-only reader for the interagency wildfire data on NIFC's ArcGIS service.

The layer is keyless and was verified live on 2026-09-22 (450 active incidents):

  WFIGS_Incident_Locations_Current/FeatureServer/0   one point per active incident,
                                                     with size, containment, cause, POO

Quirks handled here:

1. ArcGIS calls SQL "where clauses" a query language of their own, so every string that
   goes into one is escaped (and states are validated against a known list) rather than
   interpolated raw.
2. Timestamps are epoch milliseconds, so they are converted to ISO-8601 UTC here.
3. Points come back in `geometry` as {x: longitude, y: latitude} in the requested spatial
   reference (4326, i.e. degrees), not as attribute fields.
4. The service returns at most 2000 rows per query, so every read is capped and the cap
   is reported back in the artifact instead of silently truncating.
5. `POOState` is written as "US-CA", but callers say "CA", so both are accepted and the
   short form is what the answers use.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from a2a_kit import JsonApiClient

INCIDENTS_PATH = "/WFIGS_Incident_Locations_Current/FeatureServer/0/query"

DATASET = "nifc.gov/wfigs/incident-locations-current"

#: Field names on the layer, and what this server calls them.
FIELDS = (
    "IncidentName",
    "IncidentSize",
    "PercentContained",
    "FireDiscoveryDateTime",
    "IncidentTypeCategory",
    "POOState",
    "POOCounty",
    "FireCause",
    "GACC",
    "IncidentManagementOrganization",
    "UniqueFireIdentifier",
    "FireOutDateTime",
    "ModifiedOnDateTime_dt",
)

#: IncidentTypeCategory codes used by the interagency system.
TYPE_CATEGORIES = {
    "WF": "wildfire",
    "RX": "prescribed fire",
    "CX": "complex",
    "FM": "fuel management",
    "UNK": "unknown type",
}

MAX_ROWS = 2000

#: US cities people ask about, with coordinates, so a "near me" question needs no map.
#: Reference geography shipped with the server, not data from NIFC.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "boston": (42.36, -71.06), "philadelphia": (39.95, -75.17),
    "washington": (38.91, -77.04), "atlanta": (33.75, -84.39), "miami": (25.76, -80.19),
    "orlando": (28.54, -81.38), "chicago": (41.88, -87.63), "detroit": (42.33, -83.05),
    "cleveland": (41.50, -81.69), "minneapolis": (44.98, -93.27), "st louis": (38.63, -90.20),
    "kansas city": (39.10, -94.58), "dallas": (32.78, -96.80), "houston": (29.76, -95.37),
    "austin": (30.27, -97.74), "denver": (39.74, -104.99), "albuquerque": (35.08, -106.65),
    "phoenix": (33.45, -112.07), "las vegas": (36.17, -115.14), "salt lake city": (40.76, -111.89),
    "boise": (43.62, -116.20), "billings": (45.78, -108.50), "missoula": (46.87, -113.99),
    "bozeman": (45.68, -111.04), "spokane": (47.66, -117.43), "seattle": (47.61, -122.33),
    "portland": (45.52, -122.68), "medford": (42.33, -122.87), "sacramento": (38.58, -121.49),
    "san francisco": (37.77, -122.42), "san jose": (37.34, -121.89), "los angeles": (34.05, -118.24),
    "san diego": (32.72, -117.16), "reno": (39.53, -119.81), "flagstaff": (35.20, -111.65),
    "cheyenne": (41.14, -104.82), "rapid city": (44.08, -103.23), "anchorage": (61.22, -149.90),
}

STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
)
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

def escape_literal(value) -> str:
    """A safe single-quoted ArcGIS SQL string: quotes are doubled, not interpolated raw."""
    text = str(value or "").replace("'", "''")
    return f"'{text}'"


def to_iso(epoch_ms) -> str | None:
    """ArcGIS dates are epoch milliseconds; answers use ISO-8601 UTC."""
    if epoch_ms in (None, ""):
        return None
    try:
        seconds = float(epoch_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


class WildfireClient(JsonApiClient):
    """Active wildfire incidents from the interagency WFIGS layer."""

    env_prefix = "FIRE"
    base_url = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_state(value, name: str = "state") -> str:
        text = str(value or "").strip()
        upper = text.upper()
        if upper.startswith("US-"):
            upper = upper[3:]
        if upper in STATE_CODES:
            return upper
        lowered = text.lower()
        if lowered in STATE_NAMES:
            return STATE_NAMES[lowered]
        raise ValueError(f"{name} must be a two-letter US state code such as CA")

    @staticmethod
    def check_acres(value, name: str = "acres") -> float:
        text = str(value).replace(",", "").strip()
        try:
            number = float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if number < 0:
            raise ValueError(f"{name} cannot be negative")
        return number

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = 100) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return number

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
    def check_point(value) -> tuple[float, float]:
        text = re.sub(r"\s+", "", str(value or ""))
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 39.74,-104.99")
        return WildfireClient.check_lat(parts[0]), WildfireClient.check_lon(parts[1])

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        key = " ".join(str(name or "").lower().strip().split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if key and len(key) >= 4 and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    # -- reads -------------------------------------------------------------

    def _query(self, params: dict, ttl: float | None = None) -> dict:
        base = {"f": "json", "outSR": "4326", "returnGeometry": "true", "geometryPrecision": "4"}
        payload = self.get_json(INCIDENTS_PATH, {**base, **params}, ttl=ttl)
        if isinstance(payload, dict) and payload.get("error"):
            raise ValueError(str(payload["error"].get("message") or "ArcGIS rejected the query"))
        if not isinstance(payload, dict) or "features" not in payload:
            raise ValueError("the wildfire service returned an unexpected payload")
        return payload

    @staticmethod
    def _row(feature: dict) -> dict:
        attributes = feature.get("attributes") or {}
        geometry = feature.get("geometry") or {}
        size = attributes.get("IncidentSize")
        contained = attributes.get("PercentContained")
        return {
            "name": attributes.get("IncidentName"),
            "acres": float(size) if size is not None else None,
            "percent_contained": float(contained) if contained is not None else None,
            "discovered": to_iso(attributes.get("FireDiscoveryDateTime")),
            "type": attributes.get("IncidentTypeCategory"),
            "type_name": TYPE_CATEGORIES.get(str(attributes.get("IncidentTypeCategory") or "").upper(),
                                             attributes.get("IncidentTypeCategory")),
            "state": (str(attributes.get("POOState") or "").replace("US-", "") or None),
            "county": attributes.get("POOCounty"),
            "cause": attributes.get("FireCause"),
            "gacc": attributes.get("GACC"),
            "management": attributes.get("IncidentManagementOrganization"),
            "id": attributes.get("UniqueFireIdentifier"),
            "last_updated": to_iso(attributes.get("ModifiedOnDateTime_dt")),
            "latitude": geometry.get("y"),
            "longitude": geometry.get("x"),
            "dataset": DATASET,
        }

    def incidents(self, state: str | None = None, min_acres: float | None = None,
                  limit: int = 10, contained_below: float | None = None,
                  type_category: str | None = None) -> dict:
        """Active incidents, largest first, optionally filtered by state/size/containment."""
        limit = self.check_positive(limit, maximum=100)
        clauses = []
        if state:
            clauses.append(f"POOState = {escape_literal('US-' + self.check_state(state))}")
        if min_acres is not None:
            clauses.append(f"IncidentSize >= {self.check_acres(min_acres)}")
        if contained_below is not None:
            clauses.append(f"PercentContained <= {self.check_acres(contained_below, 'contained_below')}")
        if type_category:
            clauses.append(f"IncidentTypeCategory = {escape_literal(str(type_category).upper())}")
        where = " AND ".join(clauses) or "1=1"
        payload = self._query({
            "where": where,
            "outFields": ",".join(FIELDS),
            "returnGeometry": "true",
            "orderByFields": "IncidentSize DESC",
            "resultRecordCount": str(limit),
        })
        rows = [self._row(feature) for feature in payload.get("features") or []]
        return {
            "dataset": DATASET,
            "where": where,
            "count": len(rows),
            "truncated": len(rows) >= limit,
            "incidents": rows,
            "acres": round(sum(row["acres"] or 0 for row in rows), 1),
        }

    def near(self, lat: float, lon: float, radius_miles: float = 100, limit: int = 10,
             min_acres: float | None = None) -> dict:
        """Active incidents within a radius of a point, nearest first, with real distances."""
        lat = self.check_lat(lat)
        lon = self.check_lon(lon)
        radius = self.check_acres(radius_miles, "radius_miles")
        limit = self.check_positive(limit, maximum=100)
        clauses = []
        if min_acres is not None:
            clauses.append(f"IncidentSize >= {self.check_acres(min_acres)}")
        where = " AND ".join(clauses) or "1=1"
        payload = self._query({
            "where": where,
            "outFields": ",".join(FIELDS),
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "distance": str(radius),
            "units": "esriSRUnit_StatuteMile",
            "spatialRel": "esriSpatialRelIntersects",
            "resultRecordCount": str(min(limit, MAX_ROWS)),
        }, ttl=min(self.cache_ttl, 300))
        rows = []
        for feature in payload.get("features") or []:
            row = self._row(feature)
            if row["latitude"] is None or row["longitude"] is None:
                continue
            row["distance_miles"] = round(haversine_miles(lat, lon, row["latitude"], row["longitude"]), 1)
            rows.append(row)
        rows.sort(key=lambda row: row["distance_miles"])
        rows = rows[:limit]
        return {
            "dataset": DATASET,
            "where": where,
            "origin": {"latitude": lat, "longitude": lon},
            "radius_miles": radius,
            "count": len(rows),
            "incidents": rows,
            "acres": round(sum(row["acres"] or 0 for row in rows), 1),
        }

    def summary(self, state: str | None = None) -> dict:
        """National (or per-state) totals: count, acres, containment, biggest incidents."""
        if state:
            code = self.check_state(state)
            result = self.incidents(state=code, limit=100)
            rows = result["incidents"]
            return {
                "dataset": DATASET,
                "scope": f"{code}",
                "count": result["count"],
                "acres": result["acres"],
                "uncontained": sum(1 for row in rows if (row["percent_contained"] or 0) < 50),
                "biggest": rows[:5],
                "by_state": [],
            }
        # National: one large page, grouped by state in Python.
        payload = self._query({
            "where": "1=1",
            "outFields": ",".join(FIELDS),
            "returnGeometry": "false",
            "orderByFields": "IncidentSize DESC",
            "resultRecordCount": str(MAX_ROWS),
        }, ttl=min(self.cache_ttl, 600))
        rows = [self._row({"attributes": feature.get("attributes") or {}})
                for feature in payload.get("features") or []]
        by_state: dict[str, dict] = {}
        for row in rows:
            key = row["state"] or "??"
            entry = by_state.setdefault(key, {"state": key, "count": 0, "acres": 0.0})
            entry["count"] += 1
            entry["acres"] += row["acres"] or 0
        ranked = sorted(by_state.values(), key=lambda entry: -entry["acres"])
        for entry in ranked:
            entry["acres"] = round(entry["acres"], 1)
        return {
            "dataset": DATASET,
            "scope": "the United States",
            "count": len(rows),
            "acres": round(sum(row["acres"] or 0 for row in rows), 1),
            "uncontained": sum(1 for row in rows if (row["percent_contained"] or 0) < 50),
            "biggest": rows[:5],
            "by_state": ranked[:10],
            "page_limit": MAX_ROWS,
            "page_full": len(rows) >= MAX_ROWS,
        }

    def lookup(self, name: str, limit: int = 5) -> dict:
        """Incidents whose name contains the given text, wildcarded safely."""
        text = " ".join(str(name or "").split())
        if not 2 <= len(text) <= 60:
            raise ValueError("an incident name must be 2-60 characters")
        limit = self.check_positive(limit, maximum=50)
        payload = self._query({
            "where": f"UPPER(IncidentName) LIKE {escape_literal('%' + text.upper() + '%')}",
            "outFields": ",".join(FIELDS),
            "returnGeometry": "true",
            "orderByFields": "IncidentSize DESC",
            "resultRecordCount": str(limit),
        }, ttl=min(self.cache_ttl, 600))
        rows = [self._row(feature) for feature in payload.get("features") or []]
        return {"dataset": DATASET, "query": text, "count": len(rows), "incidents": rows}


__all__ = [
    "CITY_COORDS",
    "DATASET",
    "FIELDS",
    "INCIDENTS_PATH",
    "MAX_ROWS",
    "STATE_CODES",
    "STATE_NAMES",
    "TYPE_CATEGORIES",
    "WildfireClient",
    "escape_literal",
    "haversine_miles",
    "to_iso",
]
