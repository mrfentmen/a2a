"""Read-only reader for the FAA's public airport status feed (fly.faa.gov).

Verified live on 2026-09-24 00:01 UTC: a ground delay program at BOS (runway construction, 2 h
07 m average, 5 h 07 m maximum), departure delays at ORD and TEB, and closures listed for ALO,
LAX and SAN. The whole document is 1.8 KB and comes back in under a second.

Quirks handled here:

1. The document is XML with mixed naming: `<Delay_type>` blocks, an `Arrival_Departure` element
   carrying `Type="Departure"`, and `<Ground_Delay>` inside `<Ground_Delay_List>`. It is parsed
   by walking the tree for the elements that actually exist rather than by a fixed schema.
2. Two `<Delay_type>` blocks can share the same `<Name>` ("Airport Closures" appeared twice in
   the live read), so blocks are concatenated by list, never keyed by name.
3. Durations are English prose ("2 hours and 7 minutes", "16 minutes"). `parse_minutes` turns
   them into numbers, and keeps the original phrase alongside so nothing is lost.
4. `Update_Time` is prose GMT ("Thu Sep 24 00:01:24 2026 GMT") and is normalised to ISO-8601.
5. The feed only carries airports that are affected right now. An airport missing from it means
   "nothing is being reported", which is not the same as "operations are normal".
6. Reasons come in FAA shorthand ("VOL:Multi-taxi", "!ALO 09/021 ALO AD AP CLSD EXC HEL ...")
   and are passed through verbatim, with the plain-language kind (volume, weather, construction)
   derived only when the shorthand is unambiguous.
7. The feed has no city names, only 3-4 letter codes, so this server ships a code -> city table
   for the airports people ask about. A code that is not in that table still works; it just has
   no city name attached.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from urllib import request

from a2a_kit import JsonApiClient, UpstreamError

STATUS_PATH = "/flyfaa/xmlAirportStatus.jsp"

DATASET = "faa.gov/airport-status"

#: The kinds of entry this feed can carry.
KINDS = ("ground-delay", "arrival-delay", "departure-delay", "closure")

#: Plain-language reading of the FAA's shorthand reason codes, where the prefix decides it.
REASON_KINDS = {
    "VOL": "traffic volume",
    "WX": "weather",
    "TM": "traffic management",
    "RWY": "runway",
    "OBST": "obstruction",
    "EQPT": "equipment",
    "OTHER": "other",
}

#: Major US airports by code, with the city people call them: reference geography shipped with
#: the server, not data from the FAA. The feed itself only publishes codes.
AIRPORTS: dict[str, tuple[str, str]] = {
    "ATL": ("Atlanta", "GA"), "AUS": ("Austin", "TX"), "BWI": ("Baltimore", "MD"),
    "BOS": ("Boston", "MA"), "BUF": ("Buffalo", "NY"), "CLT": ("Charlotte", "NC"),
    "MDW": ("Chicago", "IL"), "ORD": ("Chicago", "IL"), "CVG": ("Cincinnati", "OH"),
    "CLE": ("Cleveland", "OH"), "CMH": ("Columbus", "OH"), "DFW": ("Dallas", "TX"),
    "DAL": ("Dallas", "TX"), "DEN": ("Denver", "CO"), "DTW": ("Detroit", "MI"),
    "EWR": ("Newark", "NJ"), "FLL": ("Fort Lauderdale", "FL"), "RSW": ("Fort Myers", "FL"),
    "BDL": ("Hartford", "CT"), "HNL": ("Honolulu", "HI"), "HOU": ("Houston", "TX"),
    "IAH": ("Houston", "TX"), "IND": ("Indianapolis", "IN"), "JAX": ("Jacksonville", "FL"),
    "MCI": ("Kansas City", "MO"), "LAS": ("Las Vegas", "NV"), "LAX": ("Los Angeles", "CA"),
    "SDF": ("Louisville", "KY"), "MEM": ("Memphis", "TN"), "MIA": ("Miami", "FL"),
    "MKE": ("Milwaukee", "WI"), "MSP": ("Minneapolis", "MN"), "BNA": ("Nashville", "TN"),
    "MSY": ("New Orleans", "LA"), "JFK": ("New York", "NY"), "LGA": ("New York", "NY"),
    "OAK": ("Oakland", "CA"), "OKC": ("Oklahoma City", "OK"), "MCO": ("Orlando", "FL"),
    "PHL": ("Philadelphia", "PA"), "PHX": ("Phoenix", "AZ"), "PIT": ("Pittsburgh", "PA"),
    "PDX": ("Portland", "OR"), "RDU": ("Raleigh", "NC"), "RIC": ("Richmond", "VA"),
    "SMF": ("Sacramento", "CA"), "SLC": ("Salt Lake City", "UT"), "SAT": ("San Antonio", "TX"),
    "SAN": ("San Diego", "CA"), "SFO": ("San Francisco", "CA"), "SJC": ("San Jose", "CA"),
    "SJU": ("San Juan", "PR"), "SEA": ("Seattle", "WA"), "STL": ("St. Louis", "MO"),
    "TPA": ("Tampa", "FL"), "DCA": ("Washington", "DC"), "IAD": ("Washington", "DC"),
    "PBI": ("West Palm Beach", "FL"), "ANC": ("Anchorage", "AK"), "FAI": ("Fairbanks", "AK"),
    "OGG": ("Kahului", "HI"), "ABQ": ("Albuquerque", "NM"), "TUS": ("Tucson", "AZ"),
    "BOI": ("Boise", "ID"), "GEG": ("Spokane", "WA"), "ELP": ("El Paso", "TX"),
    "OMA": ("Omaha", "NE"), "DSM": ("Des Moines", "IA"), "TUL": ("Tulsa", "OK"),
    "LIT": ("Little Rock", "AR"), "BHM": ("Birmingham", "AL"), "JAN": ("Jackson", "MS"),
    "PWM": ("Portland", "ME"), "BTV": ("Burlington", "VT"), "SYR": ("Syracuse", "NY"),
    "ROC": ("Rochester", "NY"), "ALB": ("Albany", "NY"), "GSO": ("Greensboro", "NC"),
    "CHS": ("Charleston", "SC"), "SAV": ("Savannah", "GA"), "HSV": ("Huntsville", "AL"),
    "TYS": ("Knoxville", "TN"), "GRR": ("Grand Rapids", "MI"), "DAY": ("Dayton", "OH"),
    "TOL": ("Toledo", "OH"), "MSN": ("Madison", "WI"), "CID": ("Cedar Rapids", "IA"),
    "FSD": ("Sioux Falls", "SD"), "FAR": ("Fargo", "ND"),    "BIL": ("Billings", "MT"),
    "MSO": ("Missoula", "MT"), "RNO": ("Reno", "NV"), "SNA": ("Santa Ana", "CA"),
    "BUR": ("Burbank", "CA"), "PSP": ("Palm Springs", "CA"),
}

MAX_LISTED = 12


def parse_minutes(text) -> int | None:
    """'2 hours and 7 minutes' -> 127, '16 minutes' -> 16, anything else -> None."""
    if text is None:
        return None
    body = str(text).strip().lower()
    if not body:
        return None
    hours = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", body)
    minutes = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", body)
    seconds = re.search(r"(\d+)\s*(?:seconds?|secs?|s)\b", body)
    if not (hours or minutes or seconds):
        digits = re.fullmatch(r"(\d+)", body)
        return int(digits.group(1)) if digits else None
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if minutes:
        total += int(minutes.group(1))
    if seconds:
        total += round(int(seconds.group(1)) / 60)
    return total


def duration_phrase(minutes) -> str | None:
    """127 -> '2 h 7 m', so answers stay readable without inventing precision."""
    if minutes is None:
        return None
    if minutes < 60:
        return f"{minutes} m"
    hours, rest = divmod(int(minutes), 60)
    return f"{hours} h" if not rest else f"{hours} h {rest} m"


def to_iso_utc(text) -> str | None:
    """'Thu Sep 24 00:01:24 2026 GMT' -> '2026-09-24T00:01:24Z'."""
    body = " ".join(str(text or "").replace("GMT", "").split())
    if not body:
        return None
    for pattern in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            stamp = datetime.strptime(body, pattern)
        except ValueError:
            continue
        return stamp.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def reason_kind(reason) -> str | None:
    """'VOL:Multi-taxi' -> 'traffic volume'. None when the shorthand decides nothing."""
    body = str(reason or "").strip()
    prefix = body.split(":", 1)[0].strip().upper()
    return REASON_KINDS.get(prefix)


#: What each kind of entry is, in one phrase, for answers and for watch notifications.
KIND_PHRASES = {
    "ground-delay": "ground delay program",
    "arrival-delay": "arrival delays",
    "departure-delay": "departure delays",
    "closure": "airport closure",
}


def describe_entry(row: dict) -> str:
    """One entry as a sentence fragment: 'ground delay program at BOS (runway construction)'."""
    where = row["airport"]
    if row.get("city"):
        where += f" ({row['city']})"
    phrase = KIND_PHRASES.get(row["kind"], row["kind"].replace("-", " "))
    text = f"{phrase} at {where}"
    if row.get("reason"):
        text += f": {row['reason']}"
    return text


class AirportStatusClient(JsonApiClient):
    """The FAA's national airport status snapshot: delays, delay programs and closures."""

    env_prefix = "FAA"
    base_url = "https://www.fly.faa.gov"

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_airport(value, name: str = "airport") -> str:
        text = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,5}", text):
            raise ValueError(f"{name} must be a 3-4 letter airport code such as SFO")
        return text

    @staticmethod
    def check_positive(value, name: str = "limit", maximum: int = 200) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number") from None
        if not 1 <= count <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return count

    @staticmethod
    def check_kind(value, name: str = "kind") -> str:
        text = str(value or "").strip().lower()
        if text not in KINDS:
            raise ValueError(f"{name} must be one of: {', '.join(KINDS)}")
        return text

    @staticmethod
    def airport_city(code: str) -> tuple[str, str] | None:
        """(city, state) for a code this server knows, else None."""
        return AIRPORTS.get(str(code or "").strip().upper())

    @staticmethod
    def code_for_place(place: str) -> list[str]:
        """Every known code in a city name ('Chicago' -> ['MDW', 'ORD']), best-effort only."""
        wanted = " ".join(str(place or "").lower().split())
        if not wanted:
            return []
        if wanted.upper() in AIRPORTS:
            return [wanted.upper()]
        return sorted(code for code, (city, _) in AIRPORTS.items() if city.lower() == wanted)

    # -- transport (the FAA serves XML, not JSON) --------------------------

    def get_xml(self, url: str, ttl: float | None = None) -> str:
        """Cached XML fetch. `_http_get_text` is the injectable seam for tests."""
        full = self.absolute(url)
        return self.cached(f"xml:{full}", ttl if ttl is not None else self.cache_ttl,
                           lambda: self._http_get_text(full, {}, self.headers()))

    def _http_get_text(self, url: str, params: dict, headers: dict) -> str:
        try:
            with request.urlopen(request.Request(url, headers=headers), timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # urllib raises many types; agents surface one
            raise UpstreamError(f"{self.env_prefix} request failed: {exc}") from exc

    # -- reads -------------------------------------------------------------

    def status(self) -> dict:
        """The whole national snapshot, grouped by the kinds of entry it carries."""
        text = self.get_xml(STATUS_PATH)
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError(f"the FAA status feed was not valid XML: {exc}") from exc
        if root.tag != "AIRPORT_STATUS_INFORMATION":
            raise ValueError("the FAA status feed returned an unexpected document")

        ground = []
        arrivals = []
        departures = []
        closures = []
        for block in root.findall("Delay_type"):
            name = (block.findtext("Name") or "").strip()
            for element in block.iter("Ground_Delay"):
                ground.append(self._delay_entry(element, "ground-delay", name))
            for element in block.iter("Delay"):
                for window in element.findall("Arrival_Departure"):
                    kind = (window.get("Type") or "").strip().lower()
                    target = arrivals if kind == "arrival" else departures
                    target.append(self._delay_entry(element, f"{kind or 'departure'}-delay", name,
                                                    window))
            for element in block.iter("Airport"):
                closures.append(self._closure_entry(element, name))

        for rows in (ground, arrivals, departures, closures):
            rows.sort(key=lambda row: (-(row["max_minutes"] or 0), row["airport"]))
        return {
            "dataset": DATASET,
            "source": "FAA Air Traffic Control System Command Center (fly.faa.gov)",
            "update_time": to_iso_utc(root.findtext("Update_Time")),
            "update_time_text": root.findtext("Update_Time"),
            "counts": {
                "ground-delay": len(ground),
                "arrival-delay": len(arrivals),
                "departure-delay": len(departures),
                "closure": len(closures),
            },
            "ground_delays": ground,
            "arrival_delays": arrivals,
            "departure_delays": departures,
            "closures": closures,
        }

    def airport(self, code: str) -> dict:
        """Everything the current snapshot says about one airport."""
        wanted = self.check_airport(code)
        snapshot = self.status()
        entries = [row for row in self.entries(snapshot) if row["airport"] == wanted]
        city, state = self.airport_city(wanted) or (None, None)
        return {
            "dataset": DATASET,
            "source": snapshot["source"],
            "update_time": snapshot["update_time"],
            "airport": wanted,
            "city": city,
            "state": state,
            "count": len(entries),
            "entries": entries,
            "programs": [row for row in entries if row["kind"] == "ground-delay"],
            "delays": [row for row in entries if row["kind"].endswith("-delay") and row["kind"] != "ground-delay"],
            "closures": [row for row in entries if row["kind"] == "closure"],
        }

    @staticmethod
    def entries(snapshot: dict) -> list[dict]:
        """Every entry in a snapshot as one flat list, worst first."""
        rows = (list(snapshot.get("ground_delays") or []) + list(snapshot.get("arrival_delays") or [])
                + list(snapshot.get("departure_delays") or []) + list(snapshot.get("closures") or []))
        priority = {"ground-delay": 0, "closure": 1, "arrival-delay": 2, "departure-delay": 3}
        rows.sort(key=lambda row: (priority.get(row["kind"], 9), -(row.get("max_minutes") or 0),
                                   row["airport"]))
        return rows

    def watch_state(self, code: str | None = None, kinds: tuple[str, ...] | None = None) -> dict:
        """What a watch compares between polls: which entries exist for the scope.

        Only the set of entries is returned. The reported average and maximum delay are revised
        every few minutes and the snapshot itself is republished continuously, so anything else
        here would fire notifications on ordinary revisions. A ground delay program or closure
        appearing or clearing is the news.
        """
        snapshot = self.status()
        rows = self.entries(snapshot)
        if code:
            wanted = self.check_airport(code)
            rows = [row for row in rows if row["airport"] == wanted]
        for kind in kinds or ():
            checked = self.check_kind(kind)
            rows = [row for row in rows if row["kind"] == checked]
        return {
            "entries": {row["id"]: describe_entry(row) for row in sorted(rows, key=lambda r: r["id"])},
        }

    # -- internals ---------------------------------------------------------

    def _delay_entry(self, element, kind: str, block_name: str, window=None) -> dict:
        """One delay row. A ground delay program publishes an average; the arrival/departure
        blocks publish a min/max window and a trend instead, so the columns differ by kind."""
        airport = (element.findtext("ARPT") or "").strip().upper()
        reason = (element.findtext("Reason") or "").strip() or None
        if window is None:
            average = element.findtext("Avg")
            maximum = element.findtext("Max")
            minimum = None
            trend = None
        else:
            average = None
            maximum = window.findtext("Max")
            minimum = window.findtext("Min")
            trend = (window.findtext("Trend") or "").strip() or None
        city, state = self.airport_city(airport) or (None, None)
        return {
            "id": f"{kind}:{airport}:{reason or ''}",
            "kind": kind,
            "kind_label": block_name or kind,
            "airport": airport,
            "city": city,
            "state": state,
            "reason": reason,
            "reason_kind": reason_kind(reason),
            "average_minutes": parse_minutes(average),
            "average_text": (average or "").strip() or None,
            "max_minutes": parse_minutes(maximum),
            "max_text": (maximum or "").strip() or None,
            "min_minutes": parse_minutes(minimum),
            "trend": trend,
            "dataset": DATASET,
        }

    def _closure_entry(self, element, block_name: str) -> dict:
        airport = (element.findtext("ARPT") or "").strip().upper()
        reason = (element.findtext("Reason") or "").strip() or None
        start = (element.findtext("Start") or "").strip() or None
        reopen = (element.findtext("Reopen") or "").strip() or None
        city, state = self.airport_city(airport) or (None, None)
        return {
            "id": f"closure:{airport}:{reason or ''}",
            "kind": "closure",
            "kind_label": block_name or "Airport Closures",
            "airport": airport,
            "city": city,
            "state": state,
            "reason": reason,
            "reason_kind": reason_kind(reason),
            "average_minutes": None,
            "average_text": None,
            "max_minutes": None,
            "max_text": None,
            "min_minutes": None,
            "trend": None,
            "start": start,
            "reopen": reopen,
            "dataset": DATASET,
        }


__all__ = [
    "AIRPORTS",
    "DATASET",
    "KIND_PHRASES",
    "KINDS",
    "MAX_LISTED",
    "REASON_KINDS",
    "STATUS_PATH",
    "AirportStatusClient",
    "describe_entry",
    "duration_phrase",
    "parse_minutes",
    "reason_kind",
    "to_iso_utc",
]
