"""Socrata (SODA) client for NYC Open Data, built on the shared JSON client.

Adds what is Socrata-specific: SOQL escaping, `/resource/<id>.json` queries, the
`count(*)` idiom, and `rowsUpdatedAt` freshness. Everything else (transport,
caching, timeouts, UpstreamError) comes from a2a_kit.jsonapi.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .errors import UpstreamError
from .jsonapi import JsonApiClient

DEFAULT_BASE_URL = "https://data.cityofnewyork.us"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def soql_escape(value) -> str:
    """Escape a string for a SOQL literal (single quotes doubled)."""
    return str(value).replace("'", "''")


class SocrataClient(JsonApiClient):
    env_prefix = "A2A"
    dataset = ""  # primary dataset id, set by subclasses
    base_url = DEFAULT_BASE_URL

    def __init__(self, fetch=None, base_url: str | None = None, app_token: str | None = None,
                 cache_ttl: float | None = None, timeout: float | None = None) -> None:
        self.app_token = app_token if app_token is not None else (os.environ.get(f"{self.env_prefix}_APP_TOKEN") or None)
        super().__init__(fetch=fetch, base_url=base_url or DEFAULT_BASE_URL, cache_ttl=cache_ttl, timeout=timeout)

    def headers(self) -> dict[str, str]:
        headers = super().headers()
        if self.app_token:
            headers["X-App-Token"] = self.app_token
        return headers

    # -- queries -----------------------------------------------------------

    def rows(self, dataset: str, params: dict[str, str], ttl: float | None = None) -> list[dict]:
        """One SODA query with caching. Raises UpstreamError on bad payloads."""
        payload = self.get_json(
            f"/resource/{dataset}.json", params, ttl=ttl if ttl is not None else min(self.cache_ttl, 30)
        )
        if not isinstance(payload, list):
            raise UpstreamError("Socrata returned an unexpected payload")
        return payload

    def count(self, dataset: str, where: str | None = None, ttl: float = 300) -> int:
        params = {"$select": "count(*)"}
        if where:
            params["$where"] = where
        payload = self.get_json(f"/resource/{dataset}.json", params, ttl=ttl)
        if not isinstance(payload, list) or not payload:
            raise UpstreamError("Socrata count query returned an unexpected payload")
        return int(payload[0].get("count", 0))

    def dataset_freshness(self, dataset: str | None = None) -> str | None:
        """rowsUpdatedAt for a dataset as an ISO string (cached 1h)."""
        target = dataset or self.dataset
        payload = self.get_json(f"/api/views/{target}.json", {}, ttl=3600)
        epoch = payload.get("rowsUpdatedAt") if isinstance(payload, dict) else None
        if not epoch:
            return None
        return (
            datetime.fromtimestamp(int(epoch), timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
