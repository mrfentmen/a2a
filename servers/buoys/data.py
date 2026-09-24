"""Read-only reader for NOAA's National Data Buoy Center (NDBC) observations.

Verified live on 2026-09-23: station 41025 (Diamond Shoals, NC) reported 31.1 kt NNE with a
36.9 kt gust, 3.3 m seas and a 25.2 °C sea surface; the station catalogue held 1,354
stations, 890 of which had a current observation.

Three keyless files, all verified on that date:

  /data/latest_obs/latest_obs.txt   every reporting station, one line each (106 KB, fast)
  /data/latest_obs/<id>.txt         the same reading rendered as a human page (per station)
  /activestations.xml               all 1,354 known stations with coordinates and sensors
                                    (note: the root path, not /data/activestations.xml, which 404s)
  /data/realtime2/<id>.txt          one station's 45-day history, newest first (610 KB)

Quirks handled here:

1. The tables are whitespace-separated with the column names in the `#` header line, and the
   line after it is the units. Columns are therefore read from that header by name instead of
   by fixed offsets, which is what keeps `latest_obs.txt` (which starts `#STN LAT LON YYYY MM
   DD hh mm ...`) and `realtime2` (which starts `#YY MM DD hh mm ...`) working through one
   parser.
2. Missing values are the literal `MM`. Note that `MM` is *also* the name of the month column
   in `realtime2`, so rows are kept as positional lists and only the header is keyed by name.
3. `MM` as a missing marker means a sensor is absent, not that a reading was zero: this server
   reports null and says the field is not published.
4. Source units are metric-oceanographic: wind m/s, waves m, temperatures °C, pressure hPa,
   visibility nautical miles, tide feet. `BUOY_UNITS` converts to mph/ft/°F/inHg for
   english (the default) and leaves them alone for metric.
5. `realtime2` rows are newest-first, and a handful of stations report every 10 minutes while
   most report hourly, so the newest row is not necessarily the newest hour.
6. The station catalogue carries stations that do not report (only 890 of 1,354 had a current
   observation), so listing and observation are separate reads and the difference is shown
   rather than papered over.
"""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from urllib import request

from a2a_kit import JsonApiClient, UpstreamError

LATEST_OBS_PATH = "/data/latest_obs/latest_obs.txt"
STATIONS_PATH = "/activestations.xml"

DATASET = "ndbc.noaa.gov/realtime-observations"

#: Columns this server reports, and what it calls them.
FIELDS = (
    "WDIR", "WSPD", "GST", "WVHT", "DPD", "APD", "MWD", "PRES", "PTDY", "ATMP", "WTMP", "DEWP",
    "VIS", "TIDE",
)

LABELS = {
    "wind_speed": "wind speed",
    "wind_gust": "gusts",
    "wave_height": "wave height",
    "dominant_period": "dominant wave period",
    "average_period": "average wave period",
    "pressure": "air pressure",
    "air_temp": "air temperature",
    "water_temp": "water temperature",
    "dew_point": "dew point",
    "visibility": "visibility",
    "tide": "tide",
    "wind_direction": "wind direction",
    "wave_direction": "wave direction",
}

#: The measurements a watch can cross a threshold on, in the client's units.
WATCH_FIELDS = ("wave_height", "wind_speed", "wind_gust", "water_temp", "air_temp")

UNITS = ("english", "metric")
UNIT_LABELS = {"english": "mph / ft / °F / inHg", "metric": "m/s / m / °C / hPa"}

MISSING = "MM"

MPS_TO_MPH = 2.2369362920544
METRES_TO_FEET = 3.280839895013123
HPA_TO_INHG = 0.029529983071445

#: Compass points, since degrees alone are hard to read.
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW",
           "NW", "NNW")

#: Coastal and ocean cities people ask about, with coordinates. Reference geography shipped
#: with the server, not data from NDBC; pass a latitude/longitude for an exact point.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "boston": (42.36, -71.06), "new york": (40.71, -74.01), "atlantic city": (39.36, -74.42),
    "virginia beach": (36.85, -75.98), "cape hatteras": (35.22, -75.53), "wilmington": (34.23, -77.94),
    "charleston": (32.78, -79.93), "savannah": (32.08, -81.09), "jacksonville": (30.33, -81.66),
    "daytona beach": (29.21, -81.02), "miami": (25.76, -80.19), "key west": (24.56, -81.78),
    "fort lauderdale": (26.12, -80.14), "tampa": (27.95, -82.46), "naples": (26.14, -81.79),
    "new orleans": (29.95, -90.07), "gulfport": (30.37, -89.09), "mobile": (30.69, -88.04),
    "galveston": (29.30, -94.80), "corpus christi": (27.80, -97.40), "brownsville": (25.90, -97.50),
    "san diego": (32.72, -117.16), "los angeles": (33.74, -118.27), "santa barbara": (34.42, -119.70),
    "monterey": (36.60, -121.89), "san francisco": (37.77, -122.42), "eureka": (40.80, -124.16),
    "portland": (45.52, -122.68), "seattle": (47.61, -122.33), "astoria": (46.19, -123.83),
    "anchorage": (61.22, -149.90), "juneau": (58.30, -134.42), "kodiak": (57.79, -152.41),
    "dutch harbor": (53.89, -166.54), "nome": (64.50, -165.41), "barrow": (71.29, -156.79),
    "honolulu": (21.31, -157.86), "hilo": (19.72, -155.09), "kahului": (20.89, -156.47),
    "san juan": (18.47, -66.11), "charlotte amalie": (18.34, -64.93), "nassau": (25.05, -77.35),
    "havana": (23.13, -82.38), "kingston": (17.97, -76.79), "cancun": (21.16, -86.85),
    "bermuda": (32.29, -64.78), "halifax": (44.65, -63.58), "st johns": (47.56, -52.71),
    "vancouver": (49.28, -123.12), "victoria": (48.43, -123.37), "tijuana": (32.51, -117.04),
    "veracruz": (19.17, -96.13), "panama city": (8.98, -79.52), "cartagena": (10.39, -75.51),
    "lima": (-12.05, -77.04), "valparaiso": (-33.05, -71.62), "buenos aires": (-34.60, -58.38),
    "rio de janeiro": (-22.91, -43.17), "recife": (-8.05, -34.88), "lisbon": (38.72, -9.14),
    "london": (51.51, -0.13), "dublin": (53.35, -6.26), "reykjavik": (64.15, -21.94),
    "oslo": (59.91, 10.75), "cape town": (-33.92, 18.42), "durban": (-29.86, 31.02),
    "mombasa": (-4.04, 39.67), "dubai": (25.20, 55.27), "mumbai": (19.08, 72.88),
    "chennai": (13.08, 80.27), "singapore": (1.35, 103.82), "hong kong": (22.30, 114.17),
    "taipei": (25.03, 121.57), "tokyo": (35.68, 139.69), "sapporo": (43.06, 141.35),
    "seoul": (37.57, 126.98), "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "auckland": (-36.85, 174.76), "suva": (-18.14, 178.44), "guam": (13.44, 144.79),
}


def compass(degrees) -> str | None:
    """Degrees true -> a 16-point compass name, the way a marine forecast reads."""
    if degrees is None or degrees == "":
        return None
    try:
        value = float(degrees) % 360
    except (TypeError, ValueError):
        return None
    return COMPASS[int((value + 11.25) // 22.5) % 16]


def number(value, digits: int = 1):
    """NDBC writes missing as `MM`; a missing sensor is reported as null, never as zero."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == MISSING:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return round(parsed, digits) if digits is not None else parsed


def parse_table(text: str) -> tuple[list[str], list[list[str]]]:
    """A `#`-headed whitespace table -> (column names, rows as positional lists).

    Column names come from the header so both the latest-observation table and the per-station
    `realtime2` file (which has different leading columns) work through this one parser. The
    units line that follows the header also starts with `#`, so it is skipped with it.
    """
    columns: list[str] = []
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            if not columns:
                columns = [name.lstrip("#") for name in line.split()]
            continue
        if not columns and len(line.split()) < 5:
            continue
        rows.append(line.split())
    return columns, rows


def _column_index(columns: list[str]) -> dict[str, int]:
    """Header name -> position, accepting both year spellings NDBC uses."""
    index: dict[str, int] = {}
    for position, name in enumerate(columns):
        index.setdefault(name, position)
    if "YYYY" in index and "YY" not in index:
        index["YY"] = index["YYYY"]
    return index


def _timestamp(index: dict[str, int], row: list[str]) -> str | None:
    """The observation time, always UTC (NDBC timestamps its tables in GMT)."""
    try:
        year = int(row[index["YY"]])
        month = int(row[index["MM"]])
        day = int(row[index["DD"]])
        hour = int(row[index["hh"]])
        minute = int(row[index["mm"]])
    except (KeyError, IndexError, ValueError):
        return None
    if year < 100:
        year += 2000
    try:
        stamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    return stamp.isoformat(timespec="minutes").replace("+00:00", "Z")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


class BuoyClient(JsonApiClient):
    """NDBC station catalogue, latest observations, and per-station history."""

    env_prefix = "BUOY"
    base_url = "https://www.ndbc.noaa.gov"

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, headers: dict | None = None,
                 units: str | None = None) -> None:
        super().__init__(fetch=fetch, base_url=base_url, cache_ttl=cache_ttl, timeout=timeout,
                         headers=headers)
        self.units = self.check_units(units or os.environ.get(f"{self.env_prefix}_UNITS", "english"))

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_units(value, default: str = "english") -> str:
        units = str(value or default).strip().lower()
        if units not in UNITS:
            raise ValueError(f"units must be one of: {', '.join(UNITS)}")
        return units

    @staticmethod
    def check_station(value, name: str = "station") -> str:
        text = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{4,7}", text):
            raise ValueError(f"{name} must be an NDBC station id such as 41025 or SANF1")
        return text

    @staticmethod
    def check_lat(value, name: str = "latitude") -> float:
        try:
            number_ = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -90 <= number_ <= 90:
            raise ValueError(f"{name} must be between -90 and 90")
        return number_

    @staticmethod
    def check_lon(value, name: str = "longitude") -> float:
        try:
            number_ = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -180 <= number_ <= 180:
            raise ValueError(f"{name} must be between -180 and 180")
        return number_

    @staticmethod
    def check_point(value) -> tuple[float, float]:
        text = re.sub(r"\s+", "", str(value or ""))
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 34.68,-75.36")
        return BuoyClient.check_lat(parts[0]), BuoyClient.check_lon(parts[1])

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = 200) -> int:
        try:
            number_ = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number_ <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return number_

    @staticmethod
    def check_miles(value, name: str = "radius_miles") -> float:
        try:
            number_ = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not 0 < number_ <= 3000:
            raise ValueError(f"{name} must be between 0 and 3000")
        return number_

    @staticmethod
    def check_field(value, name: str = "field") -> str:
        text = str(value or "").strip().lower()
        if text not in WATCH_FIELDS:
            raise ValueError(f"{name} must be one of: {', '.join(WATCH_FIELDS)}")
        return text

    @staticmethod
    def check_threshold(value, name: str = "threshold") -> float:
        try:
            number_ = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -200 <= number_ <= 1000:
            raise ValueError(f"{name} must be between -200 and 1000")
        return number_

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

    # -- unit conversion ---------------------------------------------------

    def wind(self, value_mps):
        if value_mps is None:
            return None
        return round(value_mps * MPS_TO_MPH, 1) if self.units == "english" else round(value_mps, 1)

    def wind_unit(self) -> str:
        return "mph" if self.units == "english" else "m/s"

    def distance(self, value_m):
        if value_m is None:
            return None
        return round(value_m * METRES_TO_FEET, 1) if self.units == "english" else round(value_m, 1)

    def distance_unit(self) -> str:
        return "ft" if self.units == "english" else "m"

    def temperature(self, value_c):
        if value_c is None:
            return None
        return round(value_c * 1.8 + 32, 1) if self.units == "english" else round(value_c, 1)

    def temperature_unit(self) -> str:
        return "°F" if self.units == "english" else "°C"

    def pressure(self, value_hpa):
        if value_hpa is None:
            return None
        return round(value_hpa * HPA_TO_INHG, 2) if self.units == "english" else round(value_hpa, 1)

    def pressure_unit(self) -> str:
        return "inHg" if self.units == "english" else "hPa"

    # -- transport (NDBC serves whitespace tables and XML, not JSON) --------

    def get_text(self, url: str, ttl: float | None = None) -> str:
        """Cached plain-text/XML fetch. `_http_get_text` is the injectable seam for tests."""
        full = self.absolute(url)
        return self.cached(f"text:{full}", ttl if ttl is not None else self.cache_ttl,
                           lambda: self._http_get_text(full, {}, self.headers()))

    def _http_get_text(self, url: str, params: dict, headers: dict) -> str:
        try:
            with request.urlopen(request.Request(url, headers=headers), timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; agents surface one
            raise UpstreamError(f"{self.env_prefix} request failed: {exc}") from exc

    # -- station catalogue -------------------------------------------------

    def stations(self, text: str | None = None, program: str | None = None, owner: str | None = None,
                 type_: str | None = None, met_only: bool = False, reporting_only: bool = False,
                 limit: int = 50) -> dict:
        """Stations from `activestations.xml`, filtered by search text / programme / sensors."""
        limit = self.check_positive(limit, maximum=200)
        rows = self._catalogue()
        reporting = self._latest_index()
        needle = " ".join(str(text).split()).lower() if text else None
        if needle:
            rows = [row for row in rows
                    if needle in " ".join([row["id"] or "", row["name"] or "", row["owner"] or "",
                                           row["program"] or "", row["type"] or ""]).lower()]
        if program:
            wanted = " ".join(str(program).split()).lower()
            rows = [row for row in rows if wanted in str(row["program"] or "").lower()]
        if owner:
            wanted = " ".join(str(owner).split()).lower()
            rows = [row for row in rows if wanted in str(row["owner"] or "").lower()]
        if type_:
            wanted = " ".join(str(type_).split()).lower()
            rows = [row for row in rows if wanted == str(row["type"] or "").lower()]
        if met_only:
            rows = [row for row in rows if row["sensors"].get("met")]
        if reporting_only:
            rows = [row for row in rows if row["id"] in reporting]
        for row in rows:
            row["reporting"] = row["id"] in reporting
        rows.sort(key=lambda row: (not row["reporting"], str(row["id"])))
        return {
            "dataset": DATASET,
            "count": len(rows),
            "reporting": sum(1 for row in rows if row["reporting"]),
            "truncated": len(rows) > limit,
            "stations": rows[:limit],
        }

    def station(self, station_id: str) -> dict | None:
        """One station's catalogue entry, or None when the catalogue has no such id."""
        wanted = self.check_station(station_id)
        for row in self._catalogue():
            if row["id"] == wanted:
                row["reporting"] = wanted in self._latest_index()
                return row
        return None

    def programs(self) -> list[str]:
        """The `pgm` values NDBC actually uses, so answers never invent one."""
        return sorted({str(row["program"]) for row in self._catalogue() if row["program"]})

    def station_types(self) -> list[str]:
        return sorted({str(row["type"]) for row in self._catalogue() if row["type"]})

    def near(self, lat: float, lon: float, radius_miles: float = 100, limit: int = 10,
             reporting_only: bool = True, met_only: bool = False) -> dict:
        """Stations within a radius of a point, nearest first, with real distances."""
        lat = self.check_lat(lat)
        lon = self.check_lon(lon)
        radius = self.check_miles(radius_miles)
        limit = self.check_positive(limit)
        rows = self._catalogue()
        reporting = self._latest_index()
        found = []
        for row in rows:
            if reporting_only and row["id"] not in reporting:
                continue
            if met_only and not row["sensors"].get("met"):
                continue
            if row["latitude"] is None or row["longitude"] is None:
                continue
            row = dict(row)
            row["distance_miles"] = round(haversine_miles(lat, lon, row["latitude"], row["longitude"]), 1)
            if row["distance_miles"] > radius:
                continue
            row["reporting"] = row["id"] in reporting
            found.append(row)
        found.sort(key=lambda row: row["distance_miles"])
        return {
            "dataset": DATASET,
            "origin": {"latitude": lat, "longitude": lon},
            "radius_miles": radius,
            "count": len(found),
            "stations": found[:limit],
            "truncated": len(found) > limit,
        }

    # -- observations ------------------------------------------------------

    def conditions(self, station_id: str) -> dict | None:
        """The station's newest observation from the all-station latest-observation table."""
        wanted = self.check_station(station_id)
        for observation in self.latest()["observations"]:
            if observation["station"] == wanted:
                return observation
        return None

    def latest(self) -> dict:
        """Every station's newest observation, one row each."""
        columns, rows = parse_table(self.get_text(LATEST_OBS_PATH))
        index = _column_index(columns)
        observations = []
        for row in rows:
            station = row[index["STN"]] if "STN" in index and len(row) > index["STN"] else None
            if not station:
                continue
            observations.append(self._observation(index, row, station))
        return {
            "dataset": DATASET,
            "count": len(observations),
            "observations": observations,
        }

    def history(self, station_id: str, hours: int = 24, ttl: float = 900) -> dict:
        """The station's recent observations, newest first (from the per-station table)."""
        wanted = self.check_station(station_id)
        hours = self.check_positive(hours, "hours", maximum=240)
        text = self.get_text(f"/data/realtime2/{wanted}.txt", ttl=ttl)
        columns, rows = parse_table(text)
        index = _column_index(columns)
        series = [self._observation(index, row, wanted) for row in rows[:hours]]
        if not series:
            return {"dataset": DATASET, "station": wanted, "count": 0, "series": [],
                    "first": None, "last": None, "fields": []}
        field_names = [name for name in ("wind_speed", "wind_gust", "wave_height", "dominant_period",
                                         "water_temp", "air_temp", "pressure")
                       if any(row.get(name) is not None for row in series)]
        stats = {}
        for name in field_names:
            values = [row[name] for row in series if row.get(name) is not None]
            stats[name] = {
                "latest": values[0] if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "samples": len(values),
                "unit": self._unit_for(name),
            }
        return {
            "dataset": DATASET,
            "station": wanted,
            "count": len(series),
            "first": series[-1]["time"],
            "last": series[0]["time"],
            "fields": field_names,
            "stats": stats,
            "series": series,
        }

    def watch_state(self, station_id: str, field: str, threshold: float) -> dict:
        """What a watch compares between polls: whether the reading is over the line.

        The reading itself is left out on purpose: buoys report every 10-60 minutes and the
        raw number moves constantly, so a watcher that fired on every revision would be noise.
        A crossing is the news.

        Raises ValueError when the station publishes nothing at all, or when the field the
        caller picked is one of the sensors this station does not report. An established watch
        on a field that later reads `MM` returns None instead (see the agent's probe_watch),
        so a temporary sensor dropout does not fire a notification.
        """
        wanted = self.check_station(station_id)
        field = self.check_field(field)
        threshold = self.check_threshold(threshold)
        observation = self.conditions(wanted)
        if observation is None:
            raise ValueError(f"station {wanted} has no current observation to watch")
        value = observation.get(field)
        if value is None:
            raise ValueError(f"station {wanted} is not publishing {LABELS.get(field, field)} right now")
        return {"above": bool(value >= threshold)}

    def _unit_for(self, field: str) -> str:
        if field in ("wind_speed", "wind_gust"):
            return self.wind_unit()
        if field == "wave_height":
            return self.distance_unit()
        if field in ("water_temp", "air_temp", "dew_point"):
            return self.temperature_unit()
        if field == "pressure":
            return self.pressure_unit()
        if field == "visibility":
            return "nmi"
        if field == "tide":
            return "ft"
        return ""

    def _observation(self, index: dict[str, int], row: list[str], station: str) -> dict:
        def raw(name: str):
            position = index.get(name)
            if position is None or position >= len(row):
                return None
            return row[position]

        wind_direction = number(raw("WDIR"), None)
        wave_direction = number(raw("MWD"), None)
        return {
            "station": station,
            "time": _timestamp(index, row),
            "wind_direction_degrees": wind_direction,
            "wind_direction": compass(wind_direction),
            "wind_speed": self.wind(number(raw("WSPD"))),
            "wind_gust": self.wind(number(raw("GST"))),
            "wave_height": self.distance(number(raw("WVHT"))),
            "dominant_period": number(raw("DPD"), None),
            "average_period": number(raw("APD"), None),
            "wave_direction_degrees": wave_direction,
            "wave_direction": compass(wave_direction),
            "pressure": self.pressure(number(raw("PRES"))),
            "pressure_tendency": self.pressure(number(raw("PTDY"))),
            "air_temp": self.temperature(number(raw("ATMP"))),
            "water_temp": self.temperature(number(raw("WTMP"))),
            "dew_point": self.temperature(number(raw("DEWP"))),
            "visibility": number(raw("VIS")),
            "tide": number(raw("TIDE")),
            "units": {
                "wind_speed": self.wind_unit(),
                "wind_gust": self.wind_unit(),
                "wave_height": self.distance_unit(),
                "pressure": self.pressure_unit(),
                "air_temp": self.temperature_unit(),
                "water_temp": self.temperature_unit(),
                "dew_point": self.temperature_unit(),
                "visibility": "nmi",
                "tide": "ft",
            },
            "dataset": DATASET,
        }

    # -- internals ---------------------------------------------------------

    def _latest_index(self) -> dict[str, dict]:
        """The newest observation per station id, keyed for fast joins."""
        return {row["station"]: row for row in self.latest()["observations"]}

    def _catalogue(self) -> list[dict]:
        """The parsed station catalogue (cached with the rest of the text reads)."""
        text = self.get_text(STATIONS_PATH)
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError(f"the station catalogue was not valid XML: {exc}") from exc
        rows = []
        for element in root.iter("station"):
            rows.append({
                "id": str(element.get("id") or "").upper(),
                "name": element.get("name"),
                "latitude": number(element.get("lat"), 4),
                "longitude": number(element.get("lon"), 4),
                "elevation_m": number(element.get("elev")),
                "owner": element.get("owner"),
                "program": element.get("pgm"),
                "type": element.get("type"),
                "sensors": {
                    "met": element.get("met") == "y",
                    "currents": element.get("currents") == "y",
                    "waterquality": element.get("waterquality") == "y",
                    "dart": element.get("dart") == "y",
                },
                "dataset": DATASET,
            })
        return rows


__all__ = [
    "CITY_COORDS",
    "COMPASS",
    "DATASET",
    "FIELDS",
    "HPA_TO_INHG",
    "LABELS",
    "LATEST_OBS_PATH",
    "METRES_TO_FEET",
    "MPS_TO_MPH",
    "STATIONS_PATH",
    "UNITS",
    "UNIT_LABELS",
    "WATCH_FIELDS",
    "BuoyClient",
    "compass",
    "haversine_miles",
    "number",
    "parse_table",
]
