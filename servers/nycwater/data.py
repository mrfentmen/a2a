"""Read-only client for NYC drinking water distribution monitoring.

Dataset: bkwf-xfky — "Drinking Water Quality Distribution Monitoring Data".
Verified live 2026-09-22: ~172K samples, updated daily (latest sample 2026-08-31).

Honest limits baked into this client: the dataset identifies monitoring sites by
code only (for example "55450", "1S03A") and carries no coordinates, so it can
answer "how is the water testing at site X", never "how is the water at my house".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from a2a_kit import SocrataClient, UpstreamError, soql_escape

DATASET_WATER = "bkwf-xfky"
DATASET_WATER_TITLE = "Drinking Water Quality Distribution Monitoring Data"

SAMPLE_FIELDS = (
    "sample_number",
    "sample_date",
    "sample_time",
    "sample_site",
    "sample_class",
    "residual_free_chlorine_mg_l",
    "turbidity_ntu",
    "coliform_quanti_tray_mpn_100ml",
    "e_coli_quanti_tray_mpn_100ml",
)

SITE_FIELDS = ("sample_site",)

SITE_RE = re.compile(r"^[A-Za-z0-9]{3,10}$")

__all__ = [
    "DATASET_WATER",
    "DATASET_WATER_TITLE",
    "SAMPLE_FIELDS",
    "UpstreamError",
    "WaterClient",
]


def _mpn(value) -> float | None:
    """Parse a Quanti-Tray MPN value. "<1" means not detected -> 0.0."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.startswith("<"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lstrip("<")
    try:
        return float(text)
    except ValueError:
        return None


class WaterClient(SocrataClient):
    env_prefix = "NYC_WATER"
    dataset = DATASET_WATER

    @staticmethod
    def check_site(site: str) -> str:
        value = str(site).strip()
        if not SITE_RE.match(value):
            raise ValueError("sample site must be a short code such as '55450' or '1S03A'")
        return value

    # -- reads -------------------------------------------------------------

    def latest_samples(self, site: str | None = None, sample_class: str | None = None, limit: int = 20) -> list[dict]:
        where = []
        if site:
            where.append(f"sample_site='{soql_escape(self.check_site(site))}'")
        if sample_class:
            where.append(f"sample_class='{soql_escape(str(sample_class).strip())}'")
        params = {
            "$order": "sample_date DESC, sample_time DESC",
            "$limit": str(max(1, min(int(limit), 100))),
        }
        if where:
            params["$where"] = " AND ".join(where)
        rows = self.rows(DATASET_WATER, params, ttl=min(self.cache_ttl, 120))
        return [self.pick(row, SAMPLE_FIELDS, DATASET_WATER) for row in rows]

    def site_window(self, site: str, days: int = 90, limit: int = 300) -> list[dict]:
        days = max(1, min(int(days), 730))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        params = {
            "$where": f"sample_site='{soql_escape(self.check_site(site))}' AND sample_date > '{cutoff}'",
            "$order": "sample_date DESC, sample_time DESC",
            "$limit": str(max(1, min(int(limit), 1000))),
        }
        rows = self.rows(DATASET_WATER, params, ttl=min(self.cache_ttl, 120))
        return [self.pick(row, SAMPLE_FIELDS, DATASET_WATER) for row in rows]

    def busiest_sites(self, limit: int = 20) -> list[dict]:
        params = {
            "$select": "sample_site,count(*) AS samples,max(sample_date) AS latest",
            "$group": "sample_site",
            "$order": "samples DESC",
            "$limit": str(max(1, min(int(limit), 50))),
        }
        return self.rows(DATASET_WATER, params, ttl=3600)

    def summarize(self, rows: list[dict]) -> dict:
        """Detection + chemistry summary for a set of samples (pure function of rows)."""
        chlorine = [v for v in (_number(r.get("residual_free_chlorine_mg_l")) for r in rows) if v is not None]
        turbidity = [v for v in (_number(r.get("turbidity_ntu")) for r in rows) if v is not None]
        coliform = [v for v in (_mpn(r.get("coliform_quanti_tray_mpn_100ml")) for r in rows) if v is not None]
        e_coli = [v for v in (_mpn(r.get("e_coli_quanti_tray_mpn_100ml")) for r in rows) if v is not None]
        summary = {
            "samples": len(rows),
            "first_sample_date": rows[-1]["sample_date"][:10] if rows and rows[-1].get("sample_date") else None,
            "last_sample_date": rows[0]["sample_date"][:10] if rows and rows[0].get("sample_date") else None,
            "chlorine_mg_l": {
                "min": round(min(chlorine), 3) if chlorine else None,
                "max": round(max(chlorine), 3) if chlorine else None,
                "avg": round(sum(chlorine) / len(chlorine), 3) if chlorine else None,
            },
            "turbidity_ntu": {"max": round(max(turbidity), 3) if turbidity else None},
            "coliform_detections": sum(1 for v in coliform if v > 0),
            "coliform_samples": len(coliform),
            "e_coli_detections": sum(1 for v in e_coli if v > 0),
            "e_coli_samples": len(e_coli),
        }
        return summary

    def freshness(self) -> str | None:
        return self.dataset_freshness(DATASET_WATER)
