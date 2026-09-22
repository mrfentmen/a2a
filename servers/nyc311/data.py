"""Read-only client for NYC Open Data 311 Service Requests.

Dataset: erm2-nwe9 — "311 Service Requests from 2020 to Present".
Verified live 2026-09-22: ~22.5M rows, updated daily, no key required for light use.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from a2a_kit import SocrataClient, UpstreamError, soql_escape

DATASET_311 = "erm2-nwe9"
DATASET_311_TITLE = "311 Service Requests from 2020 to Present"

COMPLAINT_FIELDS = (
    "unique_key",
    "created_date",
    "closed_date",
    "complaint_type",
    "descriptor",
    "status",
    "resolution_description",
    "agency_name",
    "street_name",
    "incident_zip",
    "borough",
    "latitude",
    "longitude",
)

_ZIP_RE = re.compile(r"^\d{5}$")
_KEY_RE = re.compile(r"^\d{6,9}$")
_TEXT_RE = re.compile(r"^[A-Za-z0-9 .'&-]{2,50}$")

# Re-exported so callers only need one import for the failure type.
__all__ = ["DATASET_311", "DATASET_311_TITLE", "COMPLAINT_FIELDS", "NYC311Client", "UpstreamError"]


class NYC311Client(SocrataClient):
    env_prefix = "NYC311"
    dataset = DATASET_311

    # -- reads -------------------------------------------------------------

    def get_complaint(self, unique_key: str) -> dict | None:
        key = str(unique_key).strip()
        if not _KEY_RE.match(key):
            raise ValueError("complaint id must be 6-9 digits")
        rows = self.rows(
            DATASET_311,
            {"$where": f"unique_key='{soql_escape(key)}'", "$limit": "1"},
            ttl=min(self.cache_ttl, 30),
        )
        return self.pick(rows[0], COMPLAINT_FIELDS, DATASET_311) if rows else None

    def recent_complaints(
        self,
        zip_code: str | None = None,
        street: str | None = None,
        borough: str | None = None,
        complaint_type_contains: str | None = None,
        days: int = 30,
        limit: int = 10,
    ) -> list[dict]:
        where: list[str] = []
        if zip_code is not None:
            if not _ZIP_RE.match(str(zip_code)):
                raise ValueError("zip code must be 5 digits")
            where.append(f"incident_zip='{soql_escape(str(zip_code))}'")
        if street is not None:
            street = str(street).strip()
            if not _TEXT_RE.match(street):
                raise ValueError("street name contains unsupported characters")
            where.append(f"upper(street_name) like upper('%{soql_escape(street)}%')")
        if borough is not None:
            borough = str(borough).strip()
            if not _TEXT_RE.match(borough):
                raise ValueError("borough contains unsupported characters")
            where.append(f"upper(borough)=upper('{soql_escape(borough)}')")
        if complaint_type_contains:
            token = str(complaint_type_contains).strip()
            if not _TEXT_RE.match(token):
                raise ValueError("complaint type contains unsupported characters")
            where.append(f"upper(complaint_type) like upper('%{soql_escape(token)}%')")
        if not where:
            raise ValueError("at least one of zip_code, street, borough is required")

        days = max(1, min(int(days), 365))
        limit = max(1, min(int(limit), 50))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        where.append(f"created_date > '{cutoff}'")
        params = {
            "$where": " AND ".join(where),
            "$order": "created_date DESC",
            "$limit": str(limit),
        }
        rows = self.rows(DATASET_311, params, ttl=min(self.cache_ttl, 30))
        return [self.pick(row, COMPLAINT_FIELDS, DATASET_311) for row in rows]

    def freshness(self) -> str | None:
        return self.dataset_freshness(DATASET_311)
