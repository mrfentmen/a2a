"""Read-only client for the NYC FloodNet datasets.

  aq7i-eu5q — "FloodNet: Street Flooding Events Measured by FloodNet Sensors"
  kb2e-tjy3 — "FloodNet: Sensor Deployment Metadata"

Verified live 2026-09-22: 491 deployed sensors, 150 street-flooding events in the
previous 30 days. The event dataset only records *completed* events, so every
answer states the window it looked at instead of claiming a live water level.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from a2a_kit import SocrataClient, UpstreamError, soql_escape

DATASET_FLOOD_EVENTS = "aq7i-eu5q"
DATASET_FLOOD_EVENTS_TITLE = "FloodNet: Street Flooding Events Measured by FloodNet Sensors"
DATASET_FLOOD_SENSORS = "kb2e-tjy3"
DATASET_FLOOD_SENSORS_TITLE = "FloodNet: Sensor Deployment Metadata"

# The raw rows also carry long flood_profile_* arrays; summaries never include them.
EVENT_FIELDS = (
    "sensor_id",
    "sensor_name",
    "flood_start_time",
    "flood_end_time",
    "max_depth_inches",
    "onset_time_mins",
    "drain_time_mins",
    "duration_mins",
    "duration_above_4_inches_mins",
    "duration_above_12_inches_mins",
    "duration_above_24_inches_mins",
)

SENSOR_FIELDS = (
    "sensor_id",
    "sensor_name",
    "street_name",
    "borough",
    "zipcode",
    "community_board",
    "council_district",
    "latitude",
    "longitude",
    "tidally_influenced",
    "lowest_point_height_delta_inches",
    "date_installed",
)

BOROUGHS = ("Brooklyn", "Queens", "Bronx", "Manhattan", "Staten Island")

_ZIP_RE = re.compile(r"^\d{5}$")
_SENSOR_ID_RE = re.compile(r"^[A-Za-z]{2}-[A-Za-z0-9-]{4,80}$")

__all__ = [
    "BOROUGHS",
    "DATASET_FLOOD_EVENTS",
    "DATASET_FLOOD_EVENTS_TITLE",
    "DATASET_FLOOD_SENSORS",
    "DATASET_FLOOD_SENSORS_TITLE",
    "EVENT_FIELDS",
    "FloodClient",
    "SENSOR_FIELDS",
    "UpstreamError",
]


class FloodClient(SocrataClient):
    env_prefix = "NYC_FLOOD"
    dataset = DATASET_FLOOD_EVENTS

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_zip(zip_code: str | None) -> str | None:
        if zip_code is None:
            return None
        value = str(zip_code).strip()
        if not _ZIP_RE.match(value):
            raise ValueError("zip code must be 5 digits")
        return value

    @staticmethod
    def check_borough(borough: str | None) -> str | None:
        if borough is None:
            return None
        value = str(borough).strip().title()
        if value not in BOROUGHS:
            raise ValueError(f"borough must be one of: {', '.join(BOROUGHS)}")
        return value

    @staticmethod
    def check_sensor_id(sensor_id: str) -> str:
        value = str(sensor_id).strip()
        if not _SENSOR_ID_RE.match(value):
            raise ValueError("sensor id looks like 'BK-richardson-st-n-11th-st-1x59w1'")
        return value

    # -- sensors -----------------------------------------------------------

    def sensors(self, zip_code: str | None = None, borough: str | None = None, limit: int = 25) -> list[dict]:
        zip_code = self.check_zip(zip_code)
        borough = self.check_borough(borough)
        where = []
        if zip_code:
            where.append(f"zipcode='{soql_escape(zip_code)}'")
        if borough:
            where.append(f"borough='{soql_escape(borough)}'")
        params = {"$order": "sensor_name", "$limit": str(max(1, min(int(limit), 200)))}
        if where:
            params["$where"] = " AND ".join(where)
        rows = self.rows(DATASET_FLOOD_SENSORS, params, ttl=3600)
        return [self.pick(row, SENSOR_FIELDS, DATASET_FLOOD_SENSORS) for row in rows]

    def sensor_ids(self, zip_code: str | None = None, borough: str | None = None, limit: int = 200) -> list[str]:
        return [row["sensor_id"] for row in self.sensors(zip_code, borough, limit) if row.get("sensor_id")]

    def sensor(self, sensor_id: str) -> dict | None:
        key = self.check_sensor_id(sensor_id)
        rows = self.rows(
            DATASET_FLOOD_SENSORS,
            {"$where": f"sensor_id='{soql_escape(key)}'", "$limit": "1"},
            ttl=3600,
        )
        return self.pick(rows[0], SENSOR_FIELDS, DATASET_FLOOD_SENSORS) if rows else None

    # -- events ------------------------------------------------------------

    def recent_events(self, sensor_ids: list[str] | None = None, hours: int = 72, limit: int = 20) -> list[dict]:
        hours = max(1, min(int(hours), 24 * 365))
        where = [f"flood_start_time > '{self._cutoff(hours)}'"]
        if sensor_ids:
            ids = [self.check_sensor_id(sid) for sid in sensor_ids[:200]]
            where.append("sensor_id in (" + ", ".join(f"'{soql_escape(sid)}'" for sid in ids) + ")")
        params = {
            "$where": " AND ".join(where),
            "$order": "flood_start_time DESC",
            "$limit": str(max(1, min(int(limit), 100))),
        }
        rows = self.rows(DATASET_FLOOD_EVENTS, params, ttl=min(self.cache_ttl, 60))
        return [self.pick(row, EVENT_FIELDS, DATASET_FLOOD_EVENTS) for row in rows]

    def latest_event(self, sensor_ids: list[str] | None = None) -> dict | None:
        rows = self.recent_events(sensor_ids, hours=24 * 365, limit=1)
        return rows[0] if rows else None

    def events_for_sensor(self, sensor_id: str, hours: int = 24 * 30, limit: int = 20) -> list[dict]:
        return self.recent_events([sensor_id], hours=hours, limit=limit)

    # -- freshness ---------------------------------------------------------

    @staticmethod
    def _cutoff(hours: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

    def freshness(self, dataset: str | None = None) -> str | None:
        return self.dataset_freshness(dataset or DATASET_FLOOD_EVENTS)
