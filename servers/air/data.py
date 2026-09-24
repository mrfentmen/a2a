"""Read-only reader for Open-Meteo's air-quality model (CAMS-driven, keyless).

Verified live on 2026-09-23: Delhi read US AQI 162 (PM2.5 58.3 µg/m³) while New York
read 39 (PM2.5 8.5 µg/m³) — same request shape, one value per field.

Quirks handled here:

1. Everything is one endpoint with a comma-separated `current` / `hourly` list, so a
   missing field is a 400, not an empty column: every field this server asks for is
   named in CURRENT_FIELDS / HOURLY_FIELDS and validated against the response.
2. An out-of-range latitude or a bad `forecast_days` comes back as HTTP 400 with
   `{"error": true, "reason": "..."}` — the reason is extracted and re-raised as an
   UpstreamError so callers hear the model's own words instead of "HTTP Error 400".
3. Pollen only exists over Europe; everywhere else the six pollen fields are `null`,
   and a null here means "this model does not publish it for this grid cell", not
   "zero pollen".
4. `hourly` always starts at 00:00 of today in the requested timezone, so a "next N
   hours" read has to find the current hour in the series rather than slicing from 0.
5. Passing several comma-separated latitudes returns a JSON *array* (one object per
   location) instead of a single object, which is what `ranking()` relies on.
6. Forecast values are hourly model output and repeat on a stepped grid; they are an
   outlook, not a measurement.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse, request

from a2a_kit import JsonApiClient, UpstreamError

CURRENT_PATH = "/v1/air-quality"

DATASET = "open-meteo.com/air-quality-api"

#: Model fields this server requests for a "right now" answer, in report order.
CURRENT_FIELDS = (
    "us_aqi",
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "carbon_monoxide",
    "uv_index",
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
)

#: The three pollutant fields worth plotting hour by hour.
HOURLY_FIELDS = ("us_aqi", "pm2_5", "pm10", "ozone")

POLLUTANTS = ("pm2_5", "pm10", "ozone", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide")
POLLEN = ("alder_pollen", "birch_pollen", "grass_pollen", "mugwort_pollen", "olive_pollen", "ragweed_pollen")

LABELS = {
    "us_aqi": "US AQI",
    "pm2_5": "PM2.5",
    "pm10": "PM10",
    "ozone": "ozone",
    "nitrogen_dioxide": "nitrogen dioxide",
    "sulphur_dioxide": "sulphur dioxide",
    "carbon_monoxide": "carbon monoxide",
    "uv_index": "UV index",
    "alder_pollen": "alder pollen",
    "birch_pollen": "birch pollen",
    "grass_pollen": "grass pollen",
    "mugwort_pollen": "mugwort pollen",
    "olive_pollen": "olive pollen",
    "ragweed_pollen": "ragweed pollen",
}

#: US EPA AQI bands: inclusive upper bound, name, and what the band means for people.
AQI_BANDS = (
    (50, "Good", "air quality is satisfactory and poses little or no risk"),
    (100, "Moderate", "acceptable for most people; unusually sensitive people should "
                      "consider limiting long or heavy outdoor exertion"),
    (150, "Unhealthy for Sensitive Groups",
     "sensitive groups (asthma, heart or lung disease, older adults, children) should "
     "limit prolonged outdoor exertion; everyone else can stay active"),
    (200, "Unhealthy", "everyone may begin to feel effects; sensitive groups should avoid "
                       "prolonged outdoor exertion"),
    (300, "Very Unhealthy", "health alert: everyone should avoid prolonged outdoor exertion"),
    (500, "Hazardous", "emergency conditions: everyone should stay indoors and reduce activity"),
)

#: The API's own ceiling for `forecast_days` (0-7); anything larger is a 400.
MAX_FORECAST_DAYS = 7
MAX_HOURS = 72

#: US cities people ask about, with coordinates. Reference geography shipped with the
#: server, not data from Open-Meteo; pass a latitude/longitude for an exact point.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "los angeles": (34.05, -118.24), "chicago": (41.88, -87.63),
    "houston": (29.76, -95.37), "phoenix": (33.45, -112.07), "philadelphia": (39.95, -75.17),
    "san antonio": (29.42, -98.49), "san diego": (32.72, -117.16), "dallas": (32.78, -96.80),
    "san jose": (37.34, -121.89), "austin": (30.27, -97.74), "jacksonville": (30.33, -81.66),
    "columbus": (39.96, -83.00), "charlotte": (35.23, -80.84), "indianapolis": (39.77, -86.16),
    "seattle": (47.61, -122.33), "denver": (39.74, -104.99), "boston": (42.36, -71.06),
    "nashville": (36.16, -86.78), "detroit": (42.33, -83.05), "portland": (45.52, -122.68),
    "las vegas": (36.17, -115.14), "memphis": (35.15, -90.05), "baltimore": (39.29, -76.61),
    "milwaukee": (43.04, -87.91), "albuquerque": (35.08, -106.65), "tucson": (32.22, -110.97),
    "fresno": (36.74, -119.79), "sacramento": (38.58, -121.49), "kansas city": (39.10, -94.58),
    "atlanta": (33.75, -84.39), "miami": (25.76, -80.19), "minneapolis": (44.98, -93.27),
    "st louis": (38.63, -90.20), "salt lake city": (40.76, -111.89), "boise": (43.62, -116.20),
    "anchorage": (61.22, -149.90), "honolulu": (21.31, -157.86), "san francisco": (37.77, -122.42),
    "washington": (38.91, -77.04), "cleveland": (41.50, -81.69), "pittsburgh": (40.44, -80.00),
    "cincinnati": (39.10, -84.51), "orlando": (28.54, -81.38), "new orleans": (29.95, -90.07),
    "reno": (39.53, -119.81), "spokane": (47.66, -117.43), "missoula": (46.87, -113.99),
    "billings": (45.78, -108.50), "flagstaff": (35.20, -111.65), "cheyenne": (41.14, -104.82),
    "rapid city": (44.08, -103.23), "bozeman": (45.68, -111.04), "medford": (42.33, -122.87),
    "beaumont": (30.08, -94.13),
    "delhi": (28.61, 77.21), "mumbai": (19.08, 72.88), "kolkata": (22.57, 88.36),
    "chennai": (13.08, 80.27), "bangalore": (12.97, 77.59), "hyderabad": (17.39, 78.49),
    "pune": (18.52, 73.86), "ahmedabad": (23.02, 72.57), "karachi": (24.86, 67.01),
    "lahore": (31.55, 74.34), "dhaka": (23.81, 90.41), "kathmandu": (27.72, 85.32),
    "beijing": (39.90, 116.41), "shanghai": (31.23, 121.47), "chengdu": (30.57, 104.07),
    "seoul": (37.57, 126.98), "tokyo": (35.68, 139.69), "osaka": (34.69, 135.50),
    "taipei": (25.03, 121.57), "bangkok": (13.76, 100.50), "hanoi": (21.03, 105.85),
    "ho chi minh city": (10.82, 106.63), "jakarta": (-6.21, 106.85), "singapore": (1.35, 103.82),
    "kuala lumpur": (3.14, 101.69), "manila": (14.60, 120.98), "sydney": (-33.87, 151.21),
    "melbourne": (-37.81, 144.96), "auckland": (-36.85, 174.76), "london": (51.51, -0.13),
    "paris": (48.86, 2.35), "berlin": (52.52, 13.41), "madrid": (40.42, -3.70),
    "rome": (41.90, 12.50), "milan": (45.46, 9.19), "amsterdam": (52.37, 4.90),
    "brussels": (50.85, 4.35), "vienna": (48.21, 16.37), "prague": (50.08, 14.44),
    "warsaw": (52.23, 21.01), "budapest": (47.50, 19.04), "athens": (37.98, 23.73),
    "stockholm": (59.33, 18.07), "oslo": (59.91, 10.75), "copenhagen": (55.68, 12.57),
    "helsinki": (60.17, 24.94), "dublin": (53.35, -6.26), "lisbon": (38.72, -9.14),
    "barcelona": (41.39, 2.17), "istanbul": (41.01, 28.98), "moscow": (55.76, 37.62),
    "kyiv": (50.45, 30.52), "dubai": (25.20, 55.27), "doha": (25.29, 51.53),
    "riyadh": (24.71, 46.68), "tehran": (35.69, 51.39), "baghdad": (33.31, 44.36),
    "cairo": (30.04, 31.24), "lagos": (6.52, 3.38), "nairobi": (-1.29, 36.82),
    "johannesburg": (-26.20, 28.05), "cape town": (-33.92, 18.42), "accra": (5.60, -0.19),
    "mexico city": (19.43, -99.13), "guadalajara": (20.67, -103.35), "monterrey": (25.69, -100.32),
    "bogota": (4.71, -74.07), "lima": (-12.05, -77.04), "santiago": (-33.45, -70.67),
    "buenos aires": (-34.60, -58.38), "sao paulo": (-23.55, -46.63), "rio de janeiro": (-22.91, -43.17),
    "brasilia": (-15.79, -47.88), "toronto": (43.65, -79.38), "montreal": (45.50, -73.57),
    "vancouver": (49.28, -123.12), "calgary": (51.05, -114.07), "edmonton": (53.55, -113.49),
    "winnipeg": (49.90, -97.14), "ottawa": (45.42, -75.70),
}

#: What `air-ranking` compares when the caller names no places.
DEFAULT_RANKING = (
    "new york", "los angeles", "chicago", "houston", "denver", "seattle", "miami",
    "phoenix", "beijing", "delhi", "london", "sao paulo", "mexico city", "seoul", "tokyo",
)

MAX_PLACES = 25


def aqi_band(aqi) -> tuple[str, str]:
    """US EPA band name and meaning for an AQI value. Unknown values read as 'unavailable'."""
    if aqi is None:
        return "unavailable", "no value was published for this location"
    value = float(aqi)
    for ceiling, name, meaning in AQI_BANDS:
        if value <= ceiling:
            return name, meaning
    return AQI_BANDS[-1][1], AQI_BANDS[-1][2]


def aqi_category(aqi) -> str:
    return aqi_band(aqi)[0]


def round_or_none(value, digits: int = 1):
    """Model output arrives as floats; nulls stay null instead of becoming 0."""
    if value is None or value == "":
        return None
    return round(float(value), digits)


def current_hour_utc() -> str:
    """The model's own time format, e.g. 2026-09-24T00."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")


class AirQualityClient(JsonApiClient):
    """Current and forecast air quality for any point on the model grid."""

    env_prefix = "AIR"
    base_url = "https://air-quality-api.open-meteo.com"

    # -- validation --------------------------------------------------------

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
        return AirQualityClient.check_lat(parts[0]), AirQualityClient.check_lon(parts[1])

    @staticmethod
    def check_hours(value, name: str = "hours") -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= number <= MAX_HOURS:
            raise ValueError(f"{name} must be between 1 and {MAX_HOURS}")
        return number

    @staticmethod
    def check_threshold(value, name: str = "threshold") -> float:
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not 0 <= number <= 500:
            raise ValueError(f"{name} must be between 0 and 500 US AQI")
        return number

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        """A known place from the server's own list -> (lat, lon, label)."""
        key = " ".join(str(name or "").lower().strip().split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if key and len(key) >= 4 and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict, headers: dict):
        """The kit's GET, plus Open-Meteo's 400 body: it carries the real reason."""
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            raise UpstreamError(f"{self.env_prefix} request rejected: {self._reason(exc)}") from exc
        except Exception as exc:  # urllib raises many types; agents surface one
            raise UpstreamError(f"{self.env_prefix} request failed: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise UpstreamError(f"{self.env_prefix} error: {payload.get('reason') or 'unknown'}")
        return payload

    @staticmethod
    def _reason(exc) -> str:
        """The model's own explanation from a 400 body, falling back to the HTTP message."""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict) and body.get("reason"):
                return str(body["reason"])
        except Exception:
            pass
        return str(exc) or "no reason given"

    # -- reads -------------------------------------------------------------

    def _current_payload(self, latitude, longitude) -> dict:
        payload = self.get_json(CURRENT_PATH, {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "current": ",".join(CURRENT_FIELDS),
            "timezone": "UTC",
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("current"), dict):
            raise ValueError("the air-quality API returned an unexpected payload")
        return payload

    def _observation(self, latitude, longitude) -> dict:
        """One location's current values, units, and AQI band."""
        payload = self._current_payload(latitude, longitude)
        current = payload["current"]
        units = payload.get("current_units") or {}
        values = {field: round_or_none(current.get(field)) for field in CURRENT_FIELDS}
        aqi = values.get("us_aqi")
        name, meaning = aqi_band(aqi)
        pollen = {field: values[field] for field in POLLEN}
        return {
            "dataset": DATASET,
            "time": current.get("time"),
            "latitude": payload.get("latitude", latitude),
            "longitude": payload.get("longitude", longitude),
            "elevation_m": payload.get("elevation"),
            "aqi": aqi,
            "category": name,
            "guidance": meaning,
            "pollutants": {field: values[field] for field in POLLUTANTS},
            "pollen": pollen if any(value is not None for value in pollen.values()) else None,
            "uv_index": values.get("uv_index"),
            "units": {field: units[field] for field in CURRENT_FIELDS if field in units},
        }

    def now(self, latitude, longitude) -> dict:
        """Current air quality at one point."""
        latitude = self.check_lat(latitude)
        longitude = self.check_lon(longitude)
        return self._observation(latitude, longitude)

    def forecast(self, latitude, longitude, hours: int = 24) -> dict:
        """Hourly US AQI and PM2.5 from the current hour forward, plus the window's peak."""
        latitude = self.check_lat(latitude)
        longitude = self.check_lon(longitude)
        hours = self.check_hours(hours)
        days = min(MAX_FORECAST_DAYS, max(1, math.ceil((hours + 23) / 24)))
        payload = self.get_json(CURRENT_PATH, {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "hourly": ",".join(HOURLY_FIELDS),
            "forecast_days": str(days),
            "timezone": "UTC",
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
            raise ValueError("the air-quality API returned an unexpected forecast payload")
        hourly = payload["hourly"]
        times = list(hourly.get("time") or [])
        start = next((index for index, stamp in enumerate(times) if str(stamp) >= current_hour_utc()), len(times))
        series = []
        for index in range(start, min(start + hours, len(times))):
            series.append({
                "time": times[index],
                "aqi": round_or_none((hourly.get("us_aqi") or [None])[index]),
                "pm2_5": round_or_none((hourly.get("pm2_5") or [None])[index]),
            })
        if not series:
            raise ValueError("the air-quality API returned no hours for this forecast window")
        peak = max(series, key=lambda row: row["aqi"] if row["aqi"] is not None else -1)
        cleanest = min(series, key=lambda row: row["aqi"] if row["aqi"] is not None else 10_000)
        units = payload.get("hourly_units") or {}
        return {
            "dataset": DATASET,
            "latitude": payload.get("latitude", latitude),
            "longitude": payload.get("longitude", longitude),
            "hours": len(series),
            "first_hour": series[0]["time"],
            "last_hour": series[-1]["time"],
            "peak": {"aqi": peak["aqi"], "time": peak["time"], "category": aqi_category(peak["aqi"])},
            "cleanest": {"aqi": cleanest["aqi"], "time": cleanest["time"]},
            "series": series,
            "units": {field: units[field] for field in HOURLY_FIELDS if field in units},
        }

    def ranking(self, places: list[tuple[str, float, float]]) -> dict:
        """One multi-location request -> the places ranked by current US AQI, worst first."""
        if not places:
            raise ValueError("at least one place is needed to rank air quality")
        if len(places) > MAX_PLACES:
            raise ValueError(f"at most {MAX_PLACES} places can be compared at once")
        checked = []
        for label, latitude, longitude in places:
            checked.append((str(label), self.check_lat(latitude), self.check_lon(longitude)))
        payload = self.get_json(CURRENT_PATH, {
            "latitude": ",".join(f"{latitude:.4f}" for _, latitude, _ in checked),
            "longitude": ",".join(f"{longitude:.4f}" for _, _, longitude in checked),
            "current": "us_aqi,pm2_5,pm10,ozone",
            "timezone": "UTC",
        })
        items = payload if isinstance(payload, list) else [payload]
        if len(items) != len(checked):
            raise ValueError("the air-quality API returned a different number of locations than asked")
        rows = []
        for (label, latitude, longitude), item in zip(checked, items):
            if not isinstance(item, dict) or not isinstance(item.get("current"), dict):
                raise ValueError("the air-quality API returned an unexpected payload for a location")
            current = item["current"]
            aqi = round_or_none(current.get("us_aqi"))
            rows.append({
                "place": label,
                "latitude": item.get("latitude", latitude),
                "longitude": item.get("longitude", longitude),
                "aqi": aqi,
                "category": aqi_category(aqi),
                "pm2_5": round_or_none(current.get("pm2_5")),
                "pm10": round_or_none(current.get("pm10")),
                "ozone": round_or_none(current.get("ozone")),
                "time": current.get("time"),
            })
        rows.sort(key=lambda row: row["aqi"] if row["aqi"] is not None else -1, reverse=True)
        units = {}
        if isinstance(items[0], dict):
            units = items[0].get("current_units") or {}
        return {
            "dataset": DATASET,
            "count": len(rows),
            "units": {field: units[field] for field in ("us_aqi", "pm2_5", "pm10", "ozone") if field in units},
            "worst": rows[0],
            "best": rows[-1],
            "places": rows,
        }

    def watch_state(self, latitude, longitude, threshold: float) -> dict:
        """What a watch compares between polls: the AQI band and whether it is over the line.

        The raw AQI and its timestamp are deliberately left out: both move every hour,
        and a watcher that fired on every hourly revision would be noise. Band changes
        and threshold crossings are the news, so those are the only two keys compared.
        """
        threshold = self.check_threshold(threshold)
        observation = self._observation(self.check_lat(latitude), self.check_lon(longitude))
        aqi = observation["aqi"]
        return {
            "band": observation["category"],
            "above": bool(aqi is not None and aqi >= threshold),
        }


__all__ = [
    "AQI_BANDS",
    "CITY_COORDS",
    "CURRENT_FIELDS",
    "CURRENT_PATH",
    "DATASET",
    "DEFAULT_RANKING",
    "HOURLY_FIELDS",
    "LABELS",
    "MAX_FORECAST_DAYS",
    "MAX_HOURS",
    "MAX_PLACES",
    "POLLEN",
    "POLLUTANTS",
    "AirQualityClient",
    "aqi_band",
    "aqi_category",
    "current_hour_utc",
    "round_or_none",
]
