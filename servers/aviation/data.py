"""Read-only reader for the Aviation Weather Center's public METAR and TAF services.

Verified live on 2026-09-24 05:00 UTC: KDEN reported 16.1 C, dewpoint 11.7 C, wind 200 at 3 kt,
10+ mi visibility, altimeter 1024.5 hPa, few 8000 ft, broken 15000 ft, overcast 20000 ft, flight
category VFR, with the raw observation alongside; the TAF for the same airport carried 7 forecast
periods; and a Colorado bounding box returned 35 reporting stations with coordinates.

Quirks handled here:

1. An airport code the service does not know is answered with **HTTP 204 and an empty body**, not
   an error and not an empty list. That is read as "no observation for that code" so a real answer
   and a missing one never look alike.
2. The decoded fields are optional by weather: `wgst`, `vertVis`, `wxString`, `slp` and the cloud
   list come and go between stations and between reports, so every field is read defensively and a
   missing one stays missing instead of becoming zero.
3. Visibility is a string when it is capped (`"10+"`) and a number otherwise, and cloud bases are
   hundreds of feet in one place and feet in another, so both are normalised in one place.
4. `fltCat` (VFR/MVFR/IFR/LIFR) is the service's own flight category. This reader reports it and
   never derives one, so no category here can disagree with the aviation weather the service
   publishes. The ceiling it reports is the lowest broken/overcast/vertical-visibility layer.
5. Times arrive as epoch seconds (`obsTime`, `validTimeFrom`) and as ISO strings (`reportTime`,
   `issueTime`), sometimes both in the same record, so both are normalised to ISO UTC.
6. The TAF forecast periods overlap by design (a temporary condition inside a period), so they are
   reported in the order the service gives them rather than merged or truncated.
7. A bounding-box read is the only way to ask "what is near here", and it returns *reporting*
   stations only: an airport with no recent observation simply is not in the answer.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib import request

from a2a_kit import JsonApiClient, UpstreamError

METAR_PATH = "/api/data/metar"
TAF_PATH = "/api/data/taf"

DATASET = "aviationweather.gov/api/data"

#: The service's own flight categories, in order of worsening conditions.
FLIGHT_CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")

#: Cloud cover codes that count as a ceiling.
CEILING_COVERS = ("BKN", "OVC", "VV")

CLOUD_COVERS = {
    "SKC": "clear",
    "CLR": "clear",
    "NSC": "no significant cloud",
    "NCD": "no cloud detected",
    "FEW": "few",
    "SCT": "scattered",
    "BKN": "broken",
    "OVC": "overcast",
    "VV": "vertical visibility",
}

#: Major airports by **ICAO** code, with the city people call them: reference geography shipped
#: with this server, not data from the aviation weather service. The keys are ICAO because that is
#: what the service resolves: a bare FAA code (DEN, JFK, MDW) answers 204 with an empty body, while
#: KDEN, KJFK and KMDW answer with the observation. `resolve_code` accepts either spelling.
AIRPORTS: dict[str, tuple[str, str]] = {
    "KATL": ("Atlanta", "GA"), "KAUS": ("Austin", "TX"), "KBWI": ("Baltimore", "MD"),
    "KBOS": ("Boston", "MA"), "KBUF": ("Buffalo", "NY"), "KCLT": ("Charlotte", "NC"),
    "KMDW": ("Chicago", "IL"), "KORD": ("Chicago", "IL"), "KCVG": ("Cincinnati", "OH"),
    "KCLE": ("Cleveland", "OH"), "KCMH": ("Columbus", "OH"), "KDFW": ("Dallas", "TX"),
    "KDAL": ("Dallas", "TX"), "KDEN": ("Denver", "CO"), "KDTW": ("Detroit", "MI"),
    "KEWR": ("Newark", "NJ"), "KFLL": ("Fort Lauderdale", "FL"), "KRSW": ("Fort Myers", "FL"),
    "KBDL": ("Hartford", "CT"), "PHNL": ("Honolulu", "HI"), "KHOU": ("Houston", "TX"),
    "KIAH": ("Houston", "TX"), "KIND": ("Indianapolis", "IN"), "KJAX": ("Jacksonville", "FL"),
    "KMCI": ("Kansas City", "MO"), "KLAS": ("Las Vegas", "NV"), "KLAX": ("Los Angeles", "CA"),
    "KSDF": ("Louisville", "KY"), "KMEM": ("Memphis", "TN"), "KMIA": ("Miami", "FL"),
    "KMKE": ("Milwaukee", "WI"), "KMSP": ("Minneapolis", "MN"), "KBNA": ("Nashville", "TN"),
    "KMSY": ("New Orleans", "LA"), "KJFK": ("New York", "NY"), "KLGA": ("New York", "NY"),
    "KOAK": ("Oakland", "CA"), "KOKC": ("Oklahoma City", "OK"), "KMCO": ("Orlando", "FL"),
    "KPHL": ("Philadelphia", "PA"), "KPHX": ("Phoenix", "AZ"), "KPIT": ("Pittsburgh", "PA"),
    "KPDX": ("Portland", "OR"), "KRDU": ("Raleigh", "NC"), "KRIC": ("Richmond", "VA"),
    "KSMF": ("Sacramento", "CA"), "KSLC": ("Salt Lake City", "UT"), "KSAT": ("San Antonio", "TX"),
    "KSAN": ("San Diego", "CA"), "KSFO": ("San Francisco", "CA"), "KSJC": ("San Jose", "CA"),
    "TJSJ": ("San Juan", "PR"), "KSEA": ("Seattle", "WA"), "KSTL": ("St. Louis", "MO"),
    "KTPA": ("Tampa", "FL"), "KDCA": ("Washington", "DC"), "KIAD": ("Washington", "DC"),
    "KPBI": ("West Palm Beach", "FL"), "PANC": ("Anchorage", "AK"), "PAFA": ("Fairbanks", "AK"),
    "PHOG": ("Kahului", "HI"), "KABQ": ("Albuquerque", "NM"), "KTUS": ("Tucson", "AZ"),
    "KBOI": ("Boise", "ID"), "KGEG": ("Spokane", "WA"), "KELP": ("El Paso", "TX"),
    "KOMA": ("Omaha", "NE"), "KDSM": ("Des Moines", "IA"), "KTUL": ("Tulsa", "OK"),
    "KBIL": ("Billings", "MT"), "KMSO": ("Missoula", "MT"), "KRNO": ("Reno", "NV"),
    "KCOS": ("Colorado Springs", "CO"), "KBJC": ("Broomfield", "CO"), "KAPA": ("Denver", "CO"),
    "KASE": ("Aspen", "CO"), "KGJT": ("Grand Junction", "CO"), "KEGE": ("Eagle", "CO"),
}

#: Places people ask about for a nearby-airport read, with coordinates: reference geography
#: shipped with this server, not data from the aviation weather service.
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
    "sacramento": (38.58, -121.49), "kansas city": (39.10, -94.58), "atlanta": (33.75, -84.39),
    "miami": (25.76, -80.19), "minneapolis": (44.98, -93.27), "st louis": (38.63, -90.20),
    "salt lake city": (40.76, -111.89), "boise": (43.62, -116.20), "anchorage": (61.22, -149.90),
    "honolulu": (21.31, -157.86), "san francisco": (37.77, -122.42), "washington": (38.91, -77.04),
    "cleveland": (41.50, -81.69), "pittsburgh": (40.44, -80.00), "cincinnati": (39.10, -84.51),
    "orlando": (28.54, -81.38), "new orleans": (29.95, -90.07), "reno": (39.53, -119.81),
    "spokane": (47.66, -117.43), "missoula": (46.87, -113.99), "billings": (45.78, -108.50),
    "colorado springs": (38.83, -104.82), "grand junction": (39.06, -108.55),
    "aspen": (39.19, -106.82), "broomfield": (39.92, -105.09), "eagle": (39.64, -106.92),
    "tampa": (27.95, -82.46), "raleigh": (35.78, -78.64), "richmond": (37.54, -77.44),
    "buffalo": (42.89, -78.88), "louisville": (38.25, -85.76), "oklahoma city": (35.47, -97.52),
    "tulsa": (36.15, -95.99), "omaha": (41.26, -95.93), "des moines": (41.59, -93.62),
    "el paso": (31.76, -106.49), "fort lauderdale": (26.12, -80.14), "west palm beach": (26.71, -80.05),
    "san juan": (18.47, -66.11), "fairbanks": (64.84, -147.72), "kahului": (20.89, -156.47),
    "newark": (40.74, -74.17),
}

MAX_AIRPORTS = 8
MAX_LISTED = 12
DEFAULT_RADIUS_MILES = 40.0
MAX_RADIUS_MILES = 200.0
#: How far back a "current conditions" read may look. The service caps this too.
DEFAULT_HOURS = 2
MAX_HOURS = 12
#: The bbox padding that keeps a station on the edge of a radius inside the box.
BBOX_MARGIN_MILES = 5.0


def to_iso_utc(value) -> str | None:
    """Epoch seconds ('1790225580') or an ISO stamp -> '2026-09-24T04:53:00Z'."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{9,11}", str(value).strip()):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def number_or_none(value):
    """A decoded number, or None. An empty string is missing, never zero."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def visibility_miles(value):
    """(miles, capped) where a capped value ('10+') keeps its cap and stays a number."""
    if value is None or value == "":
        return None, False
    text = str(value).strip()
    capped = text.endswith("+")
    body = text.rstrip("+").strip()
    try:
        return float(body), capped
    except ValueError:
        return None, capped


def cloud_layers(clouds) -> list[dict]:
    """[{cover, cover_text, base_ft}] for the layers the report actually carries."""
    layers = []
    for cloud in clouds or []:
        if not isinstance(cloud, dict):
            continue
        cover = str(cloud.get("cover") or "").strip().upper()
        base = number_or_none(cloud.get("base"))
        if not cover and base is None:
            continue
        layers.append({
            "cover": cover,
            "cover_text": CLOUD_COVERS.get(cover, cover.lower() or "cloud"),
            "base_ft": int(base) if base is not None else None,
        })
    return layers


def ceiling_ft(layers: list[dict]) -> int | None:
    """The lowest broken/overcast/vertical-visibility base, in feet. None when there is none."""
    bases = [layer["base_ft"] for layer in layers
             if layer["cover"] in CEILING_COVERS and layer["base_ft"] is not None]
    return min(bases) if bases else None


def wind_phrase(wdir, wspd, wgst) -> str:
    """'200° at 3 kt', 'calm', 'variable at 5 kt gusting 18 kt'."""
    speed = number_or_none(wspd)
    gust = number_or_none(wgst)
    if speed is None:
        return "wind not reported"
    if speed == 0:
        return "calm"
    if wdir in (None, ""):
        direction = "direction not reported"
    elif str(wdir).strip().upper() in ("VRB", "VAR"):
        direction = "variable"
    else:
        number = number_or_none(wdir)
        direction = f"{int(number):03d}°" if number is not None else str(wdir)
    text = f"{direction} at {int(speed)} kt"
    if gust:
        text += f" gusting {int(gust)} kt"
    return text


def describe_clouds(layers: list[dict]) -> str:
    """'few at 8,000 ft, broken at 15,000 ft, overcast at 20,000 ft'."""
    if not layers:
        return "no cloud layers reported"
    parts = []
    for layer in layers:
        base = f" at {layer['base_ft']:,} ft" if layer["base_ft"] is not None else ""
        parts.append(f"{layer['cover_text']}{base}")
    return ", ".join(parts)


class AviationWeatherClient(JsonApiClient):
    """METAR observations and TAF forecasts for airports, keyless."""

    env_prefix = "AVIATION"
    base_url = "https://aviationweather.gov"
    default_headers = {"Accept": "application/json"}

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict, headers: dict):
        """One request. An unknown code answers 204 with an empty body, which is not an error."""
        from urllib import parse

        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
        except Exception as exc:  # urllib raises many types; agents surface one
            raise UpstreamError(f"{self.env_prefix} request failed: {exc}") from exc
        if not body:
            return []  # HTTP 204: no observation for that code
        try:
            return json.loads(body)
        except ValueError as exc:
            raise UpstreamError(f"{self.env_prefix} sent a payload that is not JSON: {body[:120]}") from exc

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_airport(value, name: str = "airport") -> str:
        text = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,5}", text):
            raise ValueError(f"{name} must be a 3-5 character station code (ICAO, like KDEN)")
        return text

    @staticmethod
    def check_airports(value, name: str = "airports") -> list[str]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            parts = [str(part).strip() for part in value if str(part).strip()]
        else:
            parts = []
        codes = [AviationWeatherClient.check_airport(part) for part in parts]
        if len(codes) > MAX_AIRPORTS:
            raise ValueError(f"at most {MAX_AIRPORTS} airports can be read at once")
        return codes

    @staticmethod
    def check_radius(value, name: str = "radius") -> float:
        try:
            radius = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number of miles") from None
        if not 1 <= radius <= MAX_RADIUS_MILES:
            raise ValueError(f"{name} must be between 1 and {MAX_RADIUS_MILES:g} miles")
        return radius

    @staticmethod
    def check_hours(value, name: str = "hours") -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= hours <= MAX_HOURS:
            raise ValueError(f"{name} must be between 1 and {MAX_HOURS}")
        return hours

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = MAX_LISTED) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= count <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return count

    @staticmethod
    def check_lat(value, name: str = "latitude") -> float:
        try:
            lat = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -90 <= lat <= 90:
            raise ValueError(f"{name} must be between -90 and 90")
        return lat

    @staticmethod
    def check_lon(value, name: str = "longitude") -> float:
        try:
            lon = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not -180 <= lon <= 180:
            raise ValueError(f"{name} must be between -180 and 180")
        return lon

    @staticmethod
    def check_point(value) -> tuple[float, float]:
        text = re.sub(r"\s+", "", str(value or ""))
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError("a point must look like 39.74,-104.99")
        return AviationWeatherClient.check_lat(parts[0]), AviationWeatherClient.check_lon(parts[1])

    @staticmethod
    def resolve_code(code: str) -> str:
        """ICAO spelling for a code people write either way: DEN -> KDEN, KDEN -> KDEN.

        The service only resolves ICAO ids (a bare DEN answers 204), so a three-letter US code
        is promoted where this server knows the ICAO form, and left alone otherwise.
        """
        text = str(code or "").strip().upper()
        if text in AIRPORTS:
            return text
        if len(text) == 3 and f"K{text}" in AIRPORTS:
            return f"K{text}"
        return text

    @staticmethod
    def airport_city(code: str) -> tuple[str, str] | None:
        """(city, state) for a code this server knows, else None."""
        return AIRPORTS.get(AviationWeatherClient.resolve_code(code))

    @staticmethod
    def code_for_place(place: str) -> list[str]:
        """Every known ICAO code in a city name ('Chicago' -> ['KMDW', 'KORD'])."""
        wanted = " ".join(str(place or "").lower().split())
        if not wanted:
            return []
        resolved = AviationWeatherClient.resolve_code(wanted.upper())
        if resolved in AIRPORTS:
            return [resolved]
        return sorted(code for code, (city, _) in AIRPORTS.items() if city.lower() == wanted)

    @staticmethod
    def city(name: str) -> tuple[float, float, str] | None:
        """A known place from this server's own table -> (lat, lon, label)."""
        key = " ".join(str(name or "").lower().strip().split())
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return lat, lon, key.title()
        for known, coords in CITY_COORDS.items():
            if key and len(key) >= 4 and (key in known or known in key):
                return coords[0], coords[1], known.title()
        return None

    @staticmethod
    def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in statute miles (mean Earth radius 3958.8 mi)."""
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = phi2 - phi1
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(a)))

    # -- reads -------------------------------------------------------------

    def _reports(self, path: str, ids: list[str], hours: int | None = None,
                 bbox: str | None = None, ttl: float | None = None) -> list[dict]:
        params: dict = {"format": "json"}
        if ids:
            params["ids"] = ",".join(ids)
        if bbox:
            params["bbox"] = bbox
        if hours is not None:
            params["hours"] = str(hours)
        payload = self.get_json(path, params, ttl=ttl)
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise UpstreamError(f"{self.env_prefix} returned an unexpected payload for {path}")
        return [row for row in payload if isinstance(row, dict)]

    def observations(self, codes, hours: int = DEFAULT_HOURS) -> list[dict]:
        """Current METAR observations for up to MAX_AIRPORTS station codes."""
        wanted = self.check_airports(codes)
        hours = self.check_hours(hours)
        if not wanted:
            return []
        return [self.observation_row(row) for row in self._reports(METAR_PATH, wanted, hours=hours)]

    def observation(self, code: str, hours: int = DEFAULT_HOURS) -> dict:
        """One airport's current observation, with the raw report and the service's category."""
        wanted = self.check_airport(code)
        rows = self.observations([wanted], hours=hours)
        if not rows:
            return {"dataset": DATASET, "endpoint": METAR_PATH, "station": wanted,
                    "city": (self.airport_city(wanted) or (None, None))[0],
                    "state": (self.airport_city(wanted) or (None, None))[1],
                    "found": False, "observation": None}
        city, state = self.airport_city(wanted) or (None, None)
        return {"dataset": DATASET, "endpoint": METAR_PATH, "station": wanted, "city": city,
                "state": state, "found": True, "observation": rows[0]}

    def observation_row(self, row: dict) -> dict:
        """One METAR record, normalised. A missing decoded field stays None."""
        layers = cloud_layers(row.get("clouds"))
        visibility, capped = visibility_miles(row.get("visib"))
        cover = str(row.get("cover") or "").strip().upper()
        category = str(row.get("fltCat") or "").strip().upper()
        city, state = self.airport_city(row.get("icaoId")) or (None, None)
        return {
            "dataset": DATASET,
            "station": str(row.get("icaoId") or "").strip().upper(),
            "name": row.get("name"),
            "city": city,
            "state": state,
            "report_time": to_iso_utc(row.get("reportTime")),
            "observed_at": to_iso_utc(row.get("obsTime")),
            "received_at": to_iso_utc(row.get("receiptTime")),
            "report_type": row.get("metarType"),
            "flight_category": category or None,
            "temperature_c": number_or_none(row.get("temp")),
            "dewpoint_c": number_or_none(row.get("dewp")),
            "wind": {"direction_deg": number_or_none(row.get("wdir")),
                     "speed_kt": number_or_none(row.get("wspd")),
                     "gust_kt": number_or_none(row.get("wgst")),
                     "phrase": wind_phrase(row.get("wdir"), row.get("wspd"), row.get("wgst"))},
            "visibility_miles": visibility,
            "visibility_capped": capped,
            "altimeter_hpa": number_or_none(row.get("altim")),
            "sea_level_pressure_hpa": number_or_none(row.get("slp")),
            "clouds": layers,
            "clouds_text": describe_clouds(layers),
            "ceiling_ft": ceiling_ft(layers),
            "sky_cover": cover or None,
            "weather": (row.get("wxString") or None),
            "latitude": number_or_none(row.get("lat")),
            "longitude": number_or_none(row.get("lon")),
            "elevation_m": number_or_none(row.get("elev")),
            "raw": row.get("rawOb"),
        }

    def forecasts(self, codes) -> list[dict]:
        """TAF forecasts for up to MAX_AIRPORTS station codes."""
        wanted = self.check_airports(codes)
        if not wanted:
            return []
        return [self.forecast_row(row) for row in self._reports(TAF_PATH, wanted)]

    def forecast(self, code: str) -> dict:
        """One airport's forecast: the service's periods plus the raw TAF text."""
        wanted = self.check_airport(code)
        city, state = self.airport_city(wanted) or (None, None)
        rows = self.forecasts([wanted])
        if not rows:
            return {"dataset": DATASET, "endpoint": TAF_PATH, "station": wanted, "city": city,
                    "state": state, "found": False, "forecast": None}
        return {"dataset": DATASET, "endpoint": TAF_PATH, "station": wanted, "city": city,
                "state": state, "found": True, "forecast": rows[0]}

    def forecast_row(self, row: dict) -> dict:
        """One TAF record: overlapping periods in the service's own order."""
        periods = []
        for period in row.get("fcsts") or []:
            if not isinstance(period, dict):
                continue
            layers = cloud_layers(period.get("clouds"))
            visibility, capped = visibility_miles(period.get("visib"))
            periods.append({
                "from": to_iso_utc(period.get("timeFrom")),
                "to": to_iso_utc(period.get("timeTo")),
                "change": period.get("fcstChange"),
                "probability": number_or_none(period.get("probability")),
                "wind": {"direction_deg": number_or_none(period.get("wdir")),
                         "speed_kt": number_or_none(period.get("wspd")),
                         "gust_kt": number_or_none(period.get("wgst")),
                         "phrase": wind_phrase(period.get("wdir"), period.get("wspd"),
                                               period.get("wgst"))},
                "visibility_miles": visibility,
                "visibility_capped": capped,
                "vertical_visibility_ft": number_or_none(period.get("vertVis")),
                "clouds": layers,
                "clouds_text": describe_clouds(layers),
                "ceiling_ft": ceiling_ft(layers),
                "weather": (period.get("wxString") or None),
            })
        city, state = self.airport_city(row.get("icaoId")) or (None, None)
        return {
            "dataset": DATASET,
            "station": str(row.get("icaoId") or "").strip().upper(),
            "name": row.get("name"),
            "city": city,
            "state": state,
            "issued_at": to_iso_utc(row.get("issueTime")),
            "valid_from": to_iso_utc(row.get("validTimeFrom")),
            "valid_to": to_iso_utc(row.get("validTimeTo")),
            "amended": bool(str(row.get("remarks") or "").strip()),
            "periods": periods,
            "period_count": len(periods),
            "latitude": number_or_none(row.get("lat")),
            "longitude": number_or_none(row.get("lon")),
            "elevation_m": number_or_none(row.get("elev")),
            "raw": row.get("rawTAF"),
        }

    def nearby(self, latitude, longitude, radius_miles=DEFAULT_RADIUS_MILES,
               limit: int = MAX_LISTED, hours: int = DEFAULT_HOURS) -> dict:
        """Reporting stations within a radius of a point, nearest first — one bbox request."""
        lat = self.check_lat(latitude)
        lon = self.check_lon(longitude)
        radius = self.check_radius(radius_miles)
        limit = self.check_positive(limit)
        hours = self.check_hours(hours)
        reach = radius + BBOX_MARGIN_MILES
        dlat = reach / 69.0
        dlon = min(180.0, reach / max(0.01, 69.0 * math.cos(math.radians(lat))))
        bbox = f"{lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f}"
        rows = self._reports(METAR_PATH, [], hours=hours, bbox=bbox)
        observations = []
        for row in rows:
            observation = self.observation_row(row)
            if observation["latitude"] is None or observation["longitude"] is None:
                continue
            miles = self.distance_miles(lat, lon, observation["latitude"], observation["longitude"])
            if miles <= radius:
                observations.append({**observation, "miles": round(miles, 1)})
        observations.sort(key=lambda row: row["miles"])
        return {
            "dataset": DATASET,
            "endpoint": METAR_PATH,
            "latitude": lat,
            "longitude": lon,
            "radius_miles": radius,
            "bbox": bbox,
            "in_box": len(rows),
            "found": len(observations),
            "limit": limit,
            "stations": observations[:limit],
        }

    def watch_state(self, code: str) -> dict:
        """What a watch compares between polls: the station's reported flight category.

        Only the reported category (and the raw report it came from) is kept. Temperature, wind
        and visibility are revised within a category constantly — a METAR is issued every hour
        and specials in between — so watching those would page people on ordinary noise. The
        service's own category changing, VFR to MVFR or IFR to LIFR, is the news.
        """
        wanted = self.check_airport(code)
        read = self.observation(wanted)
        row = read["observation"] if read["found"] and read["observation"] else None
        if not row:
            return {"stations": {}}
        return {"stations": {row["station"]: {"flight_category": row["flight_category"],
                                              "report_time": row["report_time"],
                                              "raw": row["raw"]}}}


__all__ = [
    "AIRPORTS",
    "BBOX_MARGIN_MILES",
    "CEILING_COVERS",
    "CITY_COORDS",
    "CLOUD_COVERS",
    "DATASET",
    "DEFAULT_HOURS",
    "DEFAULT_RADIUS_MILES",
    "FLIGHT_CATEGORIES",
    "MAX_AIRPORTS",
    "MAX_HOURS",
    "MAX_LISTED",
    "MAX_RADIUS_MILES",
    "METAR_PATH",
    "TAF_PATH",
    "AviationWeatherClient",
    "ceiling_ft",
    "cloud_layers",
    "describe_clouds",
    "number_or_none",
    "resolve_code",
    "to_iso_utc",
    "visibility_miles",
    "wind_phrase",
]
