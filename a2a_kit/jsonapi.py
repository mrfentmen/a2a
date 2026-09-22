"""Generic JSON-over-HTTP client base: caching, polite headers, one error type.

Socrata servers build on this (a2a_kit.socrata), and so do the keyless public APIs
(api.weather.gov, earthquake.usgs.gov, open-meteo): same caching, same timeouts,
same UpstreamError contract the protocol handler already knows how to report.

`fetch(url, params, headers) -> parsed JSON` is injectable for tests.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib import parse, request

from .errors import UpstreamError

DEFAULT_TIMEOUT = 20.0
DEFAULT_CACHE_TTL = 60.0
#: Public APIs ask for a contactable User-Agent; override per agent via <PREFIX>_USER_AGENT.
DEFAULT_USER_AGENT = "a2a-kit/1.0 (+https://github.com/mrfentmen/a2a)"


class JsonApiClient:
    """Base for read-only public-API clients. Thread-safe; caches per URL+params."""

    env_prefix = "A2A"
    base_url = ""
    default_headers: dict[str, str] = {"Accept": "application/json"}
    user_agent = DEFAULT_USER_AGENT

    def __init__(self, fetch=None, base_url: str | None = None, cache_ttl: float | None = None,
                 timeout: float | None = None, headers: dict | None = None) -> None:
        env = os.environ
        prefix = self.env_prefix
        configured = base_url or env.get(f"{prefix}_BASE_URL") or self.base_url
        self.base_url = configured.rstrip("/")
        self.cache_ttl = float(cache_ttl if cache_ttl is not None else env.get(f"{prefix}_CACHE_TTL", str(DEFAULT_CACHE_TTL)))
        self.timeout = float(timeout if timeout is not None else env.get(f"{prefix}_HTTP_TIMEOUT", str(DEFAULT_TIMEOUT)))
        self.user_agent = env.get(f"{prefix}_USER_AGENT") or self.user_agent
        self._extra_headers = dict(headers or {})
        self._fetch = fetch or self._http_get
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    # -- transport ---------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, **self.default_headers, **self._extra_headers}

    def _http_get(self, url: str, params: dict, headers: dict):
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # urllib raises many types; agents surface one
            raise UpstreamError(f"{self.env_prefix} request failed: {exc}") from exc

    def absolute(self, url: str) -> str:
        return url if url.startswith(("http://", "https://")) else f"{self.base_url}{url}"

    # -- caching + reads ---------------------------------------------------

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

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def pick(row: dict, fields: tuple[str, ...], dataset: str | None = None) -> dict:
        """Keep only the named fields, in the order given, and optionally stamp the dataset id."""
        summary = {field: row[field] for field in fields if field in row}
        if dataset:
            summary["dataset"] = dataset
        return summary

    def get_json(self, url: str, params: dict | None = None, ttl: float | None = None) -> Any:
        """GET JSON, cached for `ttl` seconds (defaults to the client's cache_ttl)."""
        params = params or {}
        full = self.absolute(url)
        key = f"get:{full}:{json.dumps({k: str(v) for k, v in sorted(params.items())})}"

        def produce():
            payload = self._fetch(full, params, self.headers())
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                raise UpstreamError(f"{self.env_prefix} error: {payload['error']}")
            return payload

        return self.cached(key, ttl if ttl is not None else self.cache_ttl, produce)
