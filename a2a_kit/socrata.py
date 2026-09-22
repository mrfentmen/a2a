"""Socrata (SODA) client base for NYC Open Data.

Read-only, stdlib only, with a small in-memory cache, an optional app token, and
a freshness helper so every answer can say how stale the dataset is.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib import parse, request

from .errors import UpstreamError

DEFAULT_BASE_URL = "https://data.cityofnewyork.us"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def soql_escape(value: str) -> str:
    """Escape a string for a SOQL literal (single quotes doubled)."""
    return str(value).replace("'", "''")


class SocrataClient:
    """Shared transport, caching and freshness logic for one Socrata domain.

    `fetch(url, params, headers) -> parsed JSON` is injectable for tests.
    """

    env_prefix = "A2A"
    dataset = ""  # primary dataset id, set by subclasses

    def __init__(
        self,
        fetch=None,
        base_url: str | None = None,
        app_token: str | None = None,
        cache_ttl: float | None = None,
        timeout: float | None = None,
    ) -> None:
        env = os.environ
        prefix = self.env_prefix
        self.base_url = (base_url or env.get(f"{prefix}_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.app_token = app_token if app_token is not None else (env.get(f"{prefix}_APP_TOKEN") or None)
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get(f"{prefix}_CACHE_TTL", "60"))
        self.timeout = float(timeout if timeout is not None else env.get(f"{prefix}_HTTP_TIMEOUT", "20"))
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict[str, str], headers: dict[str, str]):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers={"Accept": "application/json", **headers})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; surface one
            raise UpstreamError(f"{self.env_prefix} dataset request failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        return {"X-App-Token": self.app_token} if self.app_token else {}

    def cached(self, key: str, ttl: float, producer):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = producer()
        with self._lock:
            self._cache[key] = (now + ttl, value)
        return value

    def rows(self, dataset: str, params: dict[str, str], ttl: float | None = None) -> list[dict]:
        """One SODA query with caching. Raises UpstreamError on bad payloads."""
        key = f"rows:{dataset}:{json.dumps(params, sort_keys=True)}"
        return self.cached(key, ttl if ttl is not None else min(self.cache_ttl, 30),
                           lambda: self._query(dataset, params))

    def _query(self, dataset: str, params: dict[str, str]) -> list[dict]:
        url = f"{self.base_url}/resource/{dataset}.json"
        body = self._fetch(url, params, self._headers())
        if isinstance(body, dict) and "error" in body:  # Socrata error envelope
            raise UpstreamError(f"Socrata error: {body['error']}")
        if not isinstance(body, list):
            raise UpstreamError("Socrata returned an unexpected payload")
        return body

    def count(self, dataset: str, where: str | None = None, ttl: float = 300) -> int:
        params = {"$select": "count(*)"}
        if where:
            params["$where"] = where
        body = self._fetch(f"{self.base_url}/resource/{dataset}.json", params, self._headers())
        if not isinstance(body, list) or not body:
            raise UpstreamError("Socrata count query returned an unexpected payload")
        return int(body[0].get("count", 0))

    def dataset_freshness(self, dataset: str | None = None) -> str | None:
        """rowsUpdatedAt for a dataset as an ISO string (cached 1h)."""
        target = dataset or self.dataset

        def produce():
            url = f"{self.base_url}/api/views/{target}.json"
            body = self._fetch(url, {}, self._headers())
            epoch = body.get("rowsUpdatedAt") if isinstance(body, dict) else None
            if not epoch:
                return None
            return (
                datetime.fromtimestamp(int(epoch), timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

        return self.cached(f"freshness:{target}", 3600, produce)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def pick(row: dict, fields: tuple[str, ...], dataset: str | None = None) -> dict:
        """Keep only the named fields, in dataset order, and stamp the dataset id."""
        summary = {field: row[field] for field in fields if field in row}
        if dataset:
            summary["dataset"] = dataset
        return summary
