"""Read-only client for openFDA enforcement reports (product recalls).

Endpoints (keyless, verified live 2026-09-22):
  - https://api.fda.gov/drug/enforcement.json    17,975 drug recalls
  - https://api.fda.gov/food/enforcement.json    29,415 food recalls
  - https://api.fda.gov/device/enforcement.json  39,969 device recalls

Four behaviours of this API the client has to handle, all confirmed against the
live service rather than taken on trust:

1. A query that matches nothing answers **HTTP 404** with
   `{"error": {"code": "NOT_FOUND", "message": "No matches found!"}}`. That is an
   empty result, not a failure, so it maps to an empty list.
2. Field facets need `.exact`: `count=classification` is a 500, while
   `count=classification.exact` returns the per-class term counts.
3. Date ranges must be written with spaces — `report_date:[20260101 TO 20260922]`.
   Encoding the separator as `+` is a 500.
4. `report_date` is a plain `YYYYMMDD` string, so windows and sorting are string
   comparisons, and an empty `results` array is normal for a sparse window.

openFDA publishes the openFDA disclaimer with every response; it is carried into
each artifact so callers see it instead of the agent paraphrasing it away.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib import error as urlerror
from urllib import parse, request

from a2a_kit import JsonApiClient, UpstreamError, utc_now_iso

BASE_URL = "https://api.fda.gov"
DATASET = "api.fda.gov/{scope}/enforcement"
PRODUCT = "openFDA enforcement reports (product recalls)"

#: Every scope this agent reads, in the order totals are reported.
SCOPES = ("drug", "food", "device")
SCOPE_LABELS = {"drug": "drugs", "food": "food", "device": "medical devices", "all": "all three"}
CLASSIFICATIONS = ("Class I", "Class II", "Class III", "Not Yet Classified")
SORTS = ("report_date:desc", "report_date:asc", "recall_initiation_date:desc", "classification:asc")

#: Fields kept per recall, in artifact order.
RECALL_FIELDS = (
    "recall_number",
    "classification",
    "status",
    "recalling_firm",
    "product_description",
    "reason_for_recall",
    "report_date",
    "recall_initiation_date",
    "center_classification_date",
    "voluntary_mandated",
    "initial_firm_notification",
    "distribution_pattern",
    "state",
    "city",
    "country",
    "product_quantity",
    "product_type",
    "event_id",
)

_RECALL_NUMBER_RE = re.compile(r"^[A-Z]-\d{4}-\d{4}$")
#: "Class I", "class 2", or the bare "I" / "2" a router hands over.
_CLASS_RE = re.compile(r"^(?:class\s+)?(i{1,3}|1|2|3)$", re.IGNORECASE)
_CLASS_ROMAN = {"1": "Class I", "2": "Class II", "3": "Class III",
                "i": "Class I", "ii": "Class II", "iii": "Class III"}
_STATE_RE = re.compile(r"^[A-Z]{2}$")

__all__ = [
    "BASE_URL",
    "CLASSIFICATIONS",
    "DATASET",
    "OpenFdaRecallsClient",
    "PRODUCT",
    "RECALL_FIELDS",
    "SCOPES",
    "SCOPE_LABELS",
    "SORTS",
    "UpstreamError",
    "check_classification",
    "check_recall_number",
    "check_scope",
]


def check_scope(value: str) -> str:
    scope = str(value or "all").strip().lower()
    if scope in ("all", "any", "everything"):
        return "all"
    if scope.endswith("s") and scope[:-1] in SCOPES:
        scope = scope[:-1]
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of: all, {', '.join(SCOPES)}")
    return scope


def check_classification(value: str) -> str:
    text = str(value).strip()
    match = _CLASS_RE.match(text)
    if match:
        return _CLASS_ROMAN[match.group(1).lower()]
    for classification in CLASSIFICATIONS:
        if text.lower() == classification.lower():
            return classification
    raise ValueError(f"classification must be one of: {', '.join(CLASSIFICATIONS)}")


def check_recall_number(value: str) -> str:
    number = str(value).strip().upper()
    if not _RECALL_NUMBER_RE.match(number):
        raise ValueError("a recall number looks like H-1331-2026 (letter, four digits, year)")
    return number


class OpenFdaRecallsClient(JsonApiClient):
    env_prefix = "OPENFDA"
    base_url = BASE_URL

    def __init__(self, fetch=None, base_url=None, cache_ttl=None, timeout=None, headers=None) -> None:
        super().__init__(fetch=fetch, base_url=base_url, cache_ttl=cache_ttl, timeout=timeout, headers=headers)
        # openFDA works without a key; a key only raises the rate limit.
        self.api_key = (os.environ.get(f"{self.env_prefix}_API_KEY") or "").strip() or None

    def _with_key(self, params: dict) -> dict:
        return {**params, "api_key": self.api_key} if self.api_key else dict(params)

    def _scrub(self, text: str) -> str:
        """Never let the API key escape in an error message."""
        return text.replace(self.api_key, "***") if self.api_key else text

    # -- transport ---------------------------------------------------------

    def _http_get(self, url: str, params: dict, headers: dict):
        """404 is 'no matches', not a failure; other statuses carry openFDA's own message."""
        query = parse.urlencode(params) if params else ""
        full = f"{url}?{query}" if query else url
        req = request.Request(full, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # the body is optional; never mask the status itself
                body = ""
            if exc.code == 404:
                return {"meta": {"results": {"total": 0}}, "results": []}
            message = ""
            try:
                payload = json.loads(body)
                error = payload.get("error") or {}
                message = error.get("message") or error.get("details") or ""
            except (ValueError, AttributeError):
                message = body[:200]
            raise UpstreamError(
                self._scrub(f"openFDA answered {exc.code}" + (f": {message}" if message else ""))
            ) from exc
        except Exception as exc:
            raise UpstreamError(self._scrub(f"openFDA request failed: {exc}")) from exc

    # -- validation --------------------------------------------------------

    @staticmethod
    def check_limit(value) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError("limit must be a whole number") from None
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return limit

    @staticmethod
    def check_days(value) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise ValueError("days must be a whole number") from None
        if not 1 <= days <= 3650:
            raise ValueError("days must be between 1 and 3650")
        return days

    @staticmethod
    def check_sort(value) -> str:
        order = str(value).strip().lower()
        if order not in SORTS:
            raise ValueError(f"sort must be one of: {', '.join(SORTS)}")
        return order

    @staticmethod
    def check_state(value) -> str:
        state = str(value).strip().upper()
        if not _STATE_RE.match(state):
            raise ValueError("a state is a two-letter code, for example NY")
        return state

    @staticmethod
    def check_text(value, field: str) -> str:
        """openFDA searches are quoted phrases; keep the caller's words but no quote escapes."""
        text = " ".join(str(value).split())
        if not 3 <= len(text) <= 120:
            raise ValueError(f"{field} must be between 3 and 120 characters")
        return text.replace('"', "")

    # -- query building ----------------------------------------------------

    def build_search(self, classification: str | None = None, state: str | None = None,
                     firm: str | None = None, product: str | None = None, reason: str | None = None,
                     days: int | None = None, date_from: str | None = None,
                     date_to: str | None = None, keywords: str | None = None) -> str:
        clauses = []
        if classification:
            clauses.append(f'classification:"{check_classification(classification)}"')
        if state:
            clauses.append(f'state:"{self.check_state(state)}"')
        if firm:
            clauses.append(f'recalling_firm:"{self.check_text(firm, "firm")}"')
        if product:
            clauses.append(f'product_description:"{self.check_text(product, "product")}"')
        if reason:
            clauses.append(f'reason_for_recall:"{self.check_text(reason, "reason")}"')
        window = self.date_window(days=days, date_from=date_from, date_to=date_to)
        if window:
            clauses.append(window)
        if keywords:
            # Free text is looked up in three fields rather than pasted in as query syntax:
            # openFDA answers 404 for a field it does not know, which would look like "no recalls".
            phrase = self.check_text(keywords, "query")
            clauses.append(
                f'(product_description:"{phrase}" OR reason_for_recall:"{phrase}" '
                f'OR recalling_firm:"{phrase}")'
            )
        return " AND ".join(clauses)

    @staticmethod
    def date_window(days: int | None = None, date_from: str | None = None,
                    date_to: str | None = None) -> str:
        """A report_date window string. Spaces, never plus signs: openFDA 500s on '+TO+'."""
        if not days and not date_from and not date_to:
            return ""
        end = date_to or datetime.now(timezone.utc).strftime("%Y%m%d")
        if days:
            start = (datetime.now(timezone.utc) - timedelta(days=OpenFdaRecallsClient.check_days(days))).strftime("%Y%m%d")
        else:
            start = (date_from or "19000101").replace("-", "")
        return f"report_date:[{start} TO {end}]"

    # -- reads -------------------------------------------------------------

    def search(self, scope: str, search: str = "", limit: int = 10, skip: int = 0,
               sort: str = "report_date:desc", ttl: float | None = None) -> list[dict]:
        payload = self._query(scope, search, limit=limit, skip=skip, sort=sort, ttl=ttl)
        return payload["recalls"]

    def totals(self, scope: str, search: str = "") -> int:
        return self._query(scope, search, limit=1, ttl=min(self.cache_ttl, 900))["total"]

    def counts(self, scope: str, search: str = "") -> dict:
        """Per-classification counts, summed across scopes when scope is 'all'."""
        scopes = SCOPES if scope == "all" else (scope,)
        combined: dict[str, int] = {}
        raw: dict[str, int] = {}
        for one in scopes:
            payload = self._count(one, search)
            for term, count in payload.items():
                combined[term] = combined.get(term, 0) + count
                raw[f"{one}:{term}"] = count
        return {"total": sum(combined.values()), "by_classification": combined, "by_scope": raw}

    def recall(self, recall_number: str) -> dict | None:
        number = check_recall_number(recall_number)
        for scope in SCOPES:
            found = self.search(scope, f'recall_number:"{number}"', limit=1, ttl=min(self.cache_ttl, 3600))
            if found:
                return found[0]
        return None

    def _query(self, scope: str, search: str, limit: int, skip: int = 0,
               sort: str | None = "report_date:desc", ttl: float | None = None) -> dict:
        scope = check_scope(scope)
        if scope == "all":
            raise ValueError("_query needs one scope; use searches() for all")
        params = {"limit": str(self.check_limit(limit))}
        if search:
            params["search"] = search
        if skip:
            params["skip"] = str(max(0, int(skip)))
        if sort:
            params["sort"] = self.check_sort(sort)
        params = self._with_key(params)
        payload = self.get_json(f"/{scope}/enforcement.json", params,
                                ttl=ttl if ttl is not None else min(self.cache_ttl, 900))
        if not isinstance(payload, dict):
            raise UpstreamError("openFDA returned an unexpected payload")
        rows = payload.get("results")
        if rows is None:
            raise UpstreamError("openFDA returned a payload with no results array")
        meta = payload.get("meta") or {}
        return {
            "scope": scope,
            "total": int((meta.get("results") or {}).get("total", len(rows))),
            "last_updated": meta.get("last_updated"),
            "disclaimer": meta.get("disclaimer") or "",
            "recalls": [self._one(row, scope) for row in rows],
        }

    def searches(self, scope: str, search: str = "", limit: int = 10, sort: str = "report_date:desc") -> dict:
        """One or all scopes, merged and re-sorted by report_date (then recall number)."""
        scope = check_scope(scope)
        scopes = SCOPES if scope == "all" else (scope,)
        per_scope, rows, total, updated = {}, [], 0, None
        for one in scopes:
            result = self._query(one, search, limit=limit, sort=sort)
            per_scope[one] = {"total": result["total"], "returned": len(result["recalls"])}
            total += result["total"]
            rows.extend(result["recalls"])
            if result["last_updated"] and (updated is None or result["last_updated"] > updated):
                updated = result["last_updated"]
        reverse = "desc" in sort
        rows.sort(key=lambda row: (row.get("report_date") or "", row.get("recall_number") or ""), reverse=reverse)
        return {
            "scope": scope,
            "search": search,
            "total": total,
            "returned": len(rows),
            "last_updated": updated,
            "per_scope": per_scope,
            "recalls": rows[: limit if scope == "all" else len(rows)],
        }

    def _count(self, scope: str, search: str) -> dict:
        params = self._with_key({"count": "classification.exact"})
        if search:
            params["search"] = search
        payload = self.get_json(f"/{scope}/enforcement.json", params, ttl=min(self.cache_ttl, 900))
        rows = payload.get("results") if isinstance(payload, dict) else None
        if rows is None:
            raise UpstreamError("openFDA returned an unexpected count payload")
        counts = {}
        for row in rows:
            try:
                counts[str(row.get("term"))] = int(row.get("count", 0))
            except (TypeError, ValueError):
                continue
        return counts

    @staticmethod
    def _one(row: dict, scope: str) -> dict:
        recall = {field: row[field] for field in RECALL_FIELDS if field in row}
        recall["scope"] = scope
        recall["dataset"] = DATASET.format(scope=scope)
        return recall

    @staticmethod
    def freshness() -> str:
        """openFDA publishes a dataset timestamp; the read moment is when we fall back to it."""
        return utc_now_iso()

    @staticmethod
    def compliance_note() -> str:
        return (
            "openFDA says: do not rely on openFDA to make decisions regarding medical care; "
            "results are unvalidated. Always check the FDA's own recall notice."
        )
