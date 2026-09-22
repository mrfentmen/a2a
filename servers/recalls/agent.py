"""The recalls agent: five skills over openFDA enforcement reports.

  - recalls-recent: newest drug, food and device recalls, filterable
  - recall-search:  keyword / firm / state / class search across the three registers
  - recalls-summary: how many recalls, broken down by classification
  - recall-lookup:  one recall by its number (H-1331-2026)
  - recalls-watch:  get POSTed when a new recall matches your filters

Watch-capable: recalls-watch stores the filters plus the newest recall number it saw,
so the notification fires when a *new* recall appears, not on every poll.

Every answer carries openFDA's own disclaimer, because these records are unvalidated
and are about medicines, food and medical devices.
"""

from __future__ import annotations

import re

from a2a_kit import SkillAgent

from data import (
    DATASET,
    PRODUCT,
    SCOPES,
    SCOPE_LABELS,
    OpenFdaRecallsClient,
    UpstreamError,
    check_classification,
    check_recall_number,
    check_scope,
)

SKILL_RECENT = "recalls-recent"
SKILL_SEARCH = "recall-search"
SKILL_SUMMARY = "recalls-summary"
SKILL_LOOKUP = "recall-lookup"
SKILL_WATCH = "recalls-watch"

SOURCE = "openFDA (US Food and Drug Administration)"
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 10

CARD_SKILLS = [
    {
        "id": SKILL_RECENT,
        "name": "Recent recalls",
        "description": (
            "The newest recall records from openFDA enforcement reports, across drugs, food and "
            "medical devices, newest report date first. Filter by scope, classification, state or "
            "date window."
        ),
        "tags": ["recalls", "openfda", "fda", "food-safety", "drugs", "devices"],
        "examples": [
            "what was recalled in the last 7 days?",
            "any Class I food recalls this month?",
            '{"skill": "recalls-recent", "scope": "device", "classification": "Class I"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_SEARCH,
        "name": "Search recalls",
        "description": (
            "Search the recall registers for a product, a firm, a reason or a state — for example "
            "romaine, a company name, or 'undeclared milk'. Counts and dates are openFDA's."
        ),
        "tags": ["recalls", "openfda", "search", "product", "firm"],
        "examples": [
            "search recalls for romaine",
            "any recalls by firm Crown Farms?",
            "recalls mentioning undeclared milk in CA",
            '{"skill": "recall-search", "scope": "food", "product": "romaine", "days": 365}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_SUMMARY,
        "name": "Recall counts",
        "description": (
            "How many recalls openFDA holds, broken down by classification (Class I / II / III), "
            "per scope or across all three, optionally within a date window."
        ),
        "tags": ["recalls", "openfda", "counts", "summary", "classification"],
        "examples": [
            "how many recalls are there this year?",
            "how many Class I drug recalls?",
            '{"skill": "recalls-summary", "scope": "all", "days": 90}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_LOOKUP,
        "name": "Look up a recall",
        "description": (
            "One recall record by its recall number, for example H-1331-2026 or D-0835-2026. "
            "Returns the firm, product, reason, classification, status and distribution."
        ),
        "tags": ["recalls", "openfda", "lookup", "recall-number"],
        "examples": [
            "what is recall H-1331-2026?",
            "look up D-0835-2026",
            '{"skill": "recall-lookup", "recall_number": "H-1331-2026"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
    {
        "id": SKILL_WATCH,
        "name": "Watch for new recalls",
        "description": (
            "Watch the registers and have the server call your webhook when a new recall matching "
            "your filters appears. Needs at least one filter (scope, classification, state, product, "
            "firm or reason) so the watch stays quiet."
        ),
        "tags": ["recalls", "openfda", "watch", "webhook", "alerts"],
        "examples": [
            "tell me when a new Class I food recall appears",
            "notify me about new romaine recalls",
            '{"skill": "recalls-watch", "scope": "food", "classification": "Class I"}',
        ],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json", "text/plain"],
    },
]

_RECALL_NUMBER_RE = re.compile(r"\b([A-Z]-\d{4}-\d{4})\b")
_CLASS_RE = re.compile(r"\bclass\s+(i{1,3}|1|2|3)\b", re.IGNORECASE)
_STATE_RE = re.compile(r"\b(?:state\s+([A-Z]{2})\b|\bin\s+([A-Z]{2})\b)")
_DAYS_RE = re.compile(r"\b(?:last|past|previous)\s+(\d{1,4})\s*(day|days|week|weeks|month|months|year|years)\b",
                      re.IGNORECASE)
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{3,80})[\"“”']")
_FOR_RE = re.compile(
    r"\b(?:search\s+)?(?:recalls?|recalled)\s+(?:for|of|about|containing|with|mentioning)\s+"
    r"([a-zA-Z0-9][a-zA-Z0-9 '\-/]{2,80})",
    re.IGNORECASE,
)
_FIRM_RE = re.compile(r"\b(?:by|from)\s+(?:firm|company|maker|manufacturer)\s+([A-Za-z0-9][A-Za-z0-9 .,'&\-]{2,60})",
                      re.IGNORECASE)
_WATCH_WORDS = re.compile(r"\b(watch|notify|alert me|tell me when|let me know|subscribe|ping)\b", re.IGNORECASE)
_SUMMARY_WORDS = re.compile(r"\b(how many|count|counts|summary|total|tally)\b", re.IGNORECASE)
_RECENT_WORDS = re.compile(r"\b(recent|recently|newest|latest|this week|this month|today|yesterday)\b", re.IGNORECASE)
_SEARCH_WORDS = re.compile(r"\b(search|find|look for|any recalls? (?:for|of|about|mentioning))\b", re.IGNORECASE)
_SCOPE_WORDS = {
    "drug": ("drug", "drugs", "medicine", "medicines", "medication", "medications", "pill", "pills",
             "tablet", "tablets", "injection", "ophthalmic", "prescription"),
    "food": ("food", "foods", "lettuce", "romaine", "spinach", "salad", "snack", "snacks", "candy",
             "cheese", "milk", "formula", "fish", "tuna", "granola", "nut", "nuts", "peanut",
             "blueberr", "fruit", "vegetable", "supplement", "supplements", "dietary"),
    "device": ("device", "devices", "pacemaker", "catheter", "infusion", "pump", "ventilator",
               "implant", "stent", "monitor", "scanner", "prosthesis", "surgical", "syringe"),
}
_TIME_WINDOWS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30,
                 "year": 365, "years": 365}
_NAMED_WINDOWS = {"today": 1, "yesterday": 2, "this week": 7, "last week": 7,
                  "this month": 30, "last month": 30, "this year": 365, "last year": 365}
#: Words that end a captured product phrase such as "recalls for romaine this week".
_STOPWORDS = frozenset(
    {"this", "last", "past", "next", "week", "weeks", "month", "months", "year", "years", "day", "days",
     "today", "yesterday", "in", "from", "and", "or", "the", "a", "an", "recalls", "recall", "please",
     "with", "by", "for", "of"}
)

HELP = (
    "I read openFDA enforcement reports: drug, food and medical device recalls. Ask me:\n"
    "  • what was recalled in the last 7 days?\n"
    "  • any Class I food recalls this month?\n"
    "  • search recalls for romaine\n"
    "  • how many recalls are there this year?\n"
    "  • what is recall H-1331-2026?\n"
    "  • tell me when a new Class I food recall appears\n"
    "These records are openFDA's; they are unvalidated and I repeat their disclaimer."
)


def message_text(message: dict) -> str:
    parts = message.get("parts") or []
    return " ".join(part.get("text", "") for part in parts if part.get("kind") == "text").strip()


def message_data(message: dict) -> dict:
    merged: dict = {}
    for part in message.get("parts") or []:
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            merged.update(part["data"])
    return merged


def _first(params: dict, *names, default=None):
    for name in names:
        if name in params and params[name] not in (None, ""):
            return params[name]
    return default


def scope_from_text(text: str) -> str | None:
    """A scope only when the words clearly name one."""
    lowered = text.lower()
    for scope, words in _SCOPE_WORDS.items():
        for word in words:
            if re.search(rf"\b{word}s?\b", lowered):
                return scope
    return None


def phrase_from_text(text: str) -> str | None:
    """"recalls for romaine this week" -> 'romaine'."""
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    match = _FOR_RE.search(text)
    if not match:
        return None
    words = []
    for word in re.split(r"\s+", match.group(1).strip()):
        cleaned = word.strip(".,;:")
        if cleaned.lower() in _STOPWORDS:
            break
        if _CLASS_RE.match(cleaned) or _RECALL_NUMBER_RE.match(cleaned.upper()):
            break
        words.append(cleaned)
        if len(words) == 5:
            break
    phrase = " ".join(words).strip()
    return phrase if len(phrase) >= 3 else None


def days_from_text(text: str) -> int | None:
    lowered = text.lower()
    match = _DAYS_RE.search(lowered)
    if match:
        return int(match.group(1)) * _TIME_WINDOWS[match.group(2).lower()]
    for phrase, days in _NAMED_WINDOWS.items():
        if phrase in lowered:
            return days
    if re.search(r"\btoday\b", lowered):
        return 1
    return None


def parse(message: dict) -> dict:
    """Incoming message -> {skill, params, explicit}. Never raises."""
    text = message_text(message)
    data = message_data(message)
    requested = _first(data, "skill", "skill_id")

    recall_number = _first(data, "recall_number", "recall")
    if recall_number is None:
        match = _RECALL_NUMBER_RE.search(text.upper())
        if match:
            recall_number = match.group(1)
    scope = _first(data, "scope", "dataset")
    if scope is None:
        scope = scope_from_text(text)
    classification = _first(data, "classification", "class")
    if classification is None:
        match = _CLASS_RE.search(text)
        if match:
            classification = match.group(1)
    state = _first(data, "state")
    if state is None:
        match = _STATE_RE.search(text)
        if match:
            state = match.group(1) or match.group(2)
    product = _first(data, "product", "product_description")
    reason = _first(data, "reason", "reason_for_recall")
    firm = _first(data, "firm", "recalling_firm")
    query = _first(data, "query", "q")
    if query is None and not product and not reason:
        # "recalls for romaine" is a product search; explicit fields always win.
        product = phrase_from_text(text)
    if firm is None:
        match = _FIRM_RE.search(text)
        if match:
            firm = match.group(1).strip()
    days = _first(data, "days")
    if days is None:
        days = days_from_text(text)
    limit = _first(data, "limit", default=DEFAULT_LIMIT)
    sort = _first(data, "sort")

    asked_for_watch = bool(_WATCH_WORDS.search(text))
    if requested in (SKILL_RECENT, SKILL_SEARCH, SKILL_SUMMARY, SKILL_LOOKUP, SKILL_WATCH):
        skill = requested
    elif asked_for_watch:
        skill = SKILL_WATCH
    elif recall_number:
        skill = SKILL_LOOKUP
    elif _SUMMARY_WORDS.search(text):
        skill = SKILL_SUMMARY
    elif _SEARCH_WORDS.search(text) or query or firm or product or reason:
        skill = SKILL_SEARCH
    else:
        skill = SKILL_RECENT

    params: dict = {"limit": limit}
    for key, value in (("scope", scope), ("classification", classification), ("state", state),
                       ("product", product), ("reason", reason), ("firm", firm), ("query", query),
                       ("days", days), ("sort", sort), ("recall_number", recall_number)):
        if value is not None:
            params[key] = value
    if not requested and (not text.strip() or re.search(r"\b(help|what can you do|commands?)\b", text, re.IGNORECASE)):
        skill, params = "help", {}
    explicit = bool(requested or asked_for_watch or _SUMMARY_WORDS.search(text) or _SEARCH_WORDS.search(text)
                    or _RECENT_WORDS.search(text))
    return {"skill": skill, "params": params, "explicit": explicit}


# -- shared shape ---------------------------------------------------------


def _search_string(params: dict, client: OpenFdaRecallsClient) -> str:
    return client.build_search(
        classification=params.get("classification"),
        state=params.get("state"),
        firm=params.get("firm"),
        product=params.get("product"),
        reason=params.get("reason"),
        days=params.get("days"),
        keywords=params.get("query"),
    )


def _artifact(client: OpenFdaRecallsClient, **extra) -> dict:
    artifact = {
        "dataset": DATASET.format(scope=extra.get("scope") or "all"),
        "product": PRODUCT,
        "source": SOURCE,
        "freshness": client.freshness(),
        "disclaimer": client.compliance_note(),
    }
    artifact.update(extra)
    return artifact


def _scope_label(scope: str) -> str:
    return SCOPE_LABELS.get(scope, scope)


def _missing_filters() -> dict:
    return {
        "final_state": "input-required",
        "message": (
            "I need at least one filter so the watch stays quiet. Which of these should I watch: "
            "a scope (drug, food, device), a classification (Class I, II, III), a state, a product "
            "word, or a firm?"
        ),
        "artifact": None,
        "watch": None,
    }


def _describe_search(params: dict) -> str:
    parts = []
    if params.get("classification"):
        parts.append(f"{params['classification']} recalls")
    elif params.get("scope"):
        parts.append(f"{_scope_label(params['scope'])} recalls")
    else:
        parts.append("recalls")
    if params.get("product"):
        parts.append(f"mentioning “{params['product']}”")
    if params.get("reason"):
        parts.append(f"with reason “{params['reason']}”")
    if params.get("firm"):
        parts.append(f"from firm “{params['firm']}”")
    if params.get("state"):
        parts.append(f"in {params['state']}")
    if params.get("days"):
        parts.append(f"in the last {params['days']} days")
    return " ".join(parts)


# -- skills ---------------------------------------------------------------


def _recall_line(recall: dict) -> str:
    date = recall.get("report_date") or ""
    if len(str(date)) == 8:
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return (
        f"  • [{recall.get('classification')}] {date} {recall.get('scope')} "
        f"{recall.get('recall_number')} — {recall.get('product_description')}\n"
        f"      {recall.get('recalling_firm')} — {recall.get('reason_for_recall')}"
    )


def _recall_detail(recall: dict) -> str:
    lines = [
        f"Recall {recall.get('recall_number')} ({recall.get('scope')}) — {recall.get('classification')}, status {recall.get('status')}",
        f"  Firm: {recall.get('recalling_firm')}"
        + (f" ({recall.get('city')}, {recall.get('state')})" if recall.get("city") or recall.get("state") else ""),
        f"  Product: {recall.get('product_description')}",
        f"  Reason: {recall.get('reason_for_recall')}",
        f"  Quantity: {recall.get('product_quantity') or 'not reported'}",
        f"  Distribution: {recall.get('distribution_pattern') or 'not reported'}",
        f"  Initiated {recall.get('recall_initiation_date') or 'unknown'}, "
        f"reported {recall.get('report_date') or 'unknown'}, "
        f"classified {recall.get('center_classification_date') or 'unknown'}",
        f"  {recall.get('voluntary_mandated') or 'Initiation not reported'}"
        + (f"; first notice: {recall['initial_firm_notification']}" if recall.get("initial_firm_notification") else ""),
    ]
    return "\n".join(lines)


def run_recall_lookup(params: dict, client: OpenFdaRecallsClient) -> dict:
    number = check_recall_number(params["recall_number"])
    recall = client.recall(number)
    if not recall:
        return {
            "final_state": "completed",
            "message": (
                f"openFDA holds no recall with the number {number}. Recall numbers look like "
                "H-1331-2026 (food), D-0835-2026 (drug) or Z-1234-2026 (device)."
            ),
            "artifact": _artifact(client, scope="all", recall_number=number, recall=None),
            "watch": None,
        }
    artifact = _artifact(client, scope=recall.get("scope"), recall_number=number, recall=recall)
    artifact["dataset"] = recall.get("dataset")
    return {
        "final_state": "completed",
        "message": (
            f"{_recall_detail(recall)}\n\nSource: {recall.get('dataset')} (read live). "
            f"{client.compliance_note()}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_recalls_recent(params: dict, client: OpenFdaRecallsClient) -> dict:
    scope = check_scope(params.get("scope", "all"))
    days = params.get("days") if params.get("days") is not None else DEFAULT_DAYS
    search = _search_string({**params, "days": days}, client)
    result = client.searches(scope, search, limit=int(params.get("limit", DEFAULT_LIMIT)))
    recalls = result["recalls"][: int(params.get("limit", DEFAULT_LIMIT))]
    described = _describe_search({**params, "scope": scope, "days": days})
    artifact = _artifact(
        client, scope=scope, search=search, total=result["total"], returned=len(recalls),
        per_scope=result["per_scope"], dataset_updated=result["last_updated"], recalls=recalls,
    )
    if not recalls:
        return {
            "final_state": "completed",
            "message": (
                f"No {described} found in openFDA. An empty result is a real answer here: openFDA "
                f"answers 404 when a search matches nothing (and for a query it cannot parse, so "
                f"double-check unusual wording). Read live from {DATASET.format(scope=scope)}."
            ),
            "artifact": artifact,
            "watch": None,
        }
    listing = "\n".join(_recall_line(recall) for recall in recalls[:6])
    return {
        "final_state": "completed",
        "message": (
            f"{result['total']} {described} (showing {len(recalls)}), newest report date first:\n{listing}\n\n"
            f"Source: {DATASET.format(scope=scope)} (read live; openFDA's own dataset stamp "
            f"{result['last_updated'] or 'not published'}). {client.compliance_note()}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_recall_search(params: dict, client: OpenFdaRecallsClient) -> dict:
    scope = check_scope(params.get("scope", "all"))
    search = _search_string(params, client)
    if not search:
        return {
            "final_state": "completed",
            "message": (
                "Tell me what to look for: a product word (“romaine”), a firm, a state, a "
                "classification, or a date window."
            ),
            "artifact": _artifact(client, scope=scope, search=""),
            "watch": None,
        }
    result = client.searches(scope, search, limit=int(params.get("limit", DEFAULT_LIMIT)))
    recalls = result["recalls"][: int(params.get("limit", DEFAULT_LIMIT))]
    artifact = _artifact(
        client, scope=scope, search=search, total=result["total"], returned=len(recalls),
        per_scope=result["per_scope"], dataset_updated=result["last_updated"], recalls=recalls,
    )
    described = _describe_search({**params, "scope": scope})
    if not recalls:
        return {
            "final_state": "completed",
            "message": (
                f"No recalls match {search.strip() or described} in openFDA ({scope}). openFDA answers "
                "404 when a search matches nothing — and also for a query it cannot parse, so if you "
                "pasted unusual wording, try plainer keywords."
            ),
            "artifact": artifact,
            "watch": None,
        }
    listing = "\n".join(_recall_line(recall) for recall in recalls[:6])
    return {
        "final_state": "completed",
        "message": (
            f"{result['total']} {described} match `{search}` (showing {len(recalls)}):\n{listing}\n\n"
            f"Source: {DATASET.format(scope=scope)} (read live). {client.compliance_note()}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_recalls_summary(params: dict, client: OpenFdaRecallsClient) -> dict:
    scope = check_scope(params.get("scope", "all"))
    search = _search_string(params, client)
    counts = client.counts(scope, search)
    dataset = DATASET.format(scope=scope)
    artifact = _artifact(
        client, scope=scope, search=search, dataset=dataset, total=counts["total"],
        by_classification=counts["by_classification"], by_scope=counts["by_scope"],
    )
    if not counts["total"]:
        return {
            "final_state": "completed",
            "message": f"No recalls match {search or 'that filter'}. (Read live from {dataset}.)",
            "artifact": artifact,
            "watch": None,
        }
    ranked = sorted(counts["by_classification"].items(), key=lambda item: -item[1])
    breakdown = ", ".join(f"{term} {count:,}" for term, count in ranked)
    return {
        "final_state": "completed",
        "message": (
            f"openFDA holds {counts['total']:,} recalls for {_scope_label(scope)}"
            + (f" matching `{search}`" if search else "")
            + f": {breakdown}.\n\nSource: {dataset} (read live). {client.compliance_note()}"
        ),
        "artifact": artifact,
        "watch": None,
    }


def run_recalls_watch(params: dict, client: OpenFdaRecallsClient) -> dict:
    has_filter = any(params.get(key) for key in ("classification", "state", "product", "reason", "firm", "query")) \
        or (params.get("scope") and check_scope(params["scope"]) != "all")
    if not has_filter:
        return _missing_filters()
    scope = check_scope(params.get("scope", "all"))
    days = params.get("days")
    search = _search_string(params, client)
    result = client.searches(scope, search, limit=1)
    newest = (result["recalls"] or [{}])[0]
    observed = {
        "scope": scope,
        "search": search,
        "newest": newest.get("recall_number") or "",
        "newest_report_date": newest.get("report_date") or "",
        "total": result["total"],
    }
    watch = {
        "kind": SKILL_WATCH,
        "dataset": DATASET.format(scope=scope),
        "scope": scope,
        "search": search,
        "summary": _describe_search({**params, "scope": scope}),
        "observed": observed,
    }
    artifact = _artifact(
        client, scope=scope, search=search, total=result["total"],
        watching={key: value for key, value in watch.items() if key != "observed"},
        newest_recall=newest or None,
    )
    latest = (
        f" The newest match right now is {newest.get('recall_number')} "
        f"({newest.get('classification')}, {newest.get('product_description')}, reported "
        f"{newest.get('report_date')})."
        if newest.get("recall_number") else " Nothing matches those filters right now."
    )
    return {
        "final_state": "completed",
        "message": (
            f"Watching openFDA for {_describe_search({**params, 'scope': scope})}.{latest} "
            "Point a pushNotificationConfig at this task and I will POST when a new recall number "
            "appears. openFDA publishes on business days, so quiet weekends are normal."
        ),
        "artifact": artifact,
        "watch": watch,
    }


class RecallsAgent(SkillAgent):
    name = "openfda-recalls"
    card_name = "openFDA Recalls Agent"
    card_description = (
        "Read-only agent over openFDA enforcement reports: recent drug, food and medical device "
        "recalls, keyword/firm/state/class search, counts by classification, lookup by recall "
        "number, and a watch skill that POSTs when a new recall matches your filters. Every answer "
        "is read live from openFDA and carries openFDA's own unvalidated-data disclaimer."
    )
    env_prefix = "OPENFDA"
    datasets = tuple(DATASET.format(scope=scope) for scope in SCOPES)
    card_skills = CARD_SKILLS
    watch_kinds = (SKILL_WATCH,)

    def __init__(self, client: OpenFdaRecallsClient | None = None) -> None:
        self.client = client or OpenFdaRecallsClient()

    def parse(self, message: dict) -> dict:
        return parse(message)

    def missing(self, skill: str, params: dict) -> list[str]:
        if skill == SKILL_LOOKUP:
            return [] if params.get("recall_number") else ["recall_number"]
        if skill == SKILL_WATCH:
            has_filter = any(params.get(key) for key in ("classification", "state", "product", "reason", "firm", "query")) \
                or (params.get("scope") and check_scope(params["scope"]) != "all")
            return [] if has_filter else ["filter"]
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        if skill == SKILL_LOOKUP:
            return (
                "Which recall number? They look like H-1331-2026 (food), D-0835-2026 (drug) or "
                "Z-1234-2026 (device)."
            )
        return _missing_filters()["message"]

    def run(self, request: dict) -> dict:
        skill = request["skill"]
        params = request["params"]
        if request["missing"]:
            return {
                "final_state": "input-required",
                "message": self.input_prompt(skill, request["missing"]),
                "artifact": None,
                "watch": None,
            }
        if skill == SKILL_LOOKUP:
            return run_recall_lookup(params, self.client)
        if skill == SKILL_SEARCH:
            return run_recall_search(params, self.client)
        if skill == SKILL_SUMMARY:
            return run_recalls_summary(params, self.client)
        if skill == SKILL_WATCH:
            return run_recalls_watch(params, self.client)
        if skill == SKILL_RECENT:
            return run_recalls_recent(params, self.client)
        return {"final_state": "completed", "message": HELP, "artifact": None, "watch": None}

    def probe_watch(self, watch: dict) -> dict | None:
        """Only the newest recall number and total, so a notification means a new record."""
        scope = watch.get("scope")
        if not scope:
            return None
        result = self.client.searches(scope, watch.get("search") or "", limit=1)
        newest = (result["recalls"] or [{}])[0]
        return {
            "scope": scope,
            "search": watch.get("search") or "",
            "newest": newest.get("recall_number") or "",
            "newest_report_date": newest.get("report_date") or "",
            "total": result["total"],
        }

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        scope = watch.get("scope") or "all"
        described = watch.get("summary") or _scope_label(scope)
        before = (previous or {}).get("total")
        after = (observed or {}).get("total")
        line = f"New openFDA recall matching {described}"
        if after is not None and before is not None and after > before:
            line += f" ({after - before} more; {after} total)"
        detail = ""
        try:
            detail = self._latest_detail(watch)
        except UpstreamError:
            detail = ""
        return f"{line}. {detail}".strip()

    def _latest_detail(self, watch: dict) -> str:
        result = self.client.searches(watch.get("scope") or "all", watch.get("search") or "", limit=1)
        newest = (result["recalls"] or [{}])[0]
        if not newest.get("recall_number"):
            return ""
        return (
            f"Newest: {newest['recall_number']} [{newest.get('classification')}] "
            f"{newest.get('product_description')} — {newest.get('recalling_firm')}, reported "
            f"{newest.get('report_date')}."
        )


__all__ = [
    "CARD_SKILLS",
    "DEFAULT_DAYS",
    "HELP",
    "RecallsAgent",
    "SKILL_LOOKUP",
    "SKILL_RECENT",
    "SKILL_SEARCH",
    "SKILL_SUMMARY",
    "SKILL_WATCH",
    "UpstreamError",
    "days_from_text",
    "message_data",
    "message_text",
    "parse",
    "phrase_from_text",
    "run_recall_lookup",
    "run_recall_search",
    "run_recalls_recent",
    "run_recalls_summary",
    "run_recalls_watch",
    "scope_from_text",
]
