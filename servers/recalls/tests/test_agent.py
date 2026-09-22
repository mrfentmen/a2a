"""Tests for the openFDA recalls server.

    python3 servers/recalls/tests/test_agent.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit import PushWatcher, TaskStore, UpstreamError  # noqa: E402

from agent import (  # noqa: E402
    CARD_SKILLS,
    RecallsAgent,
    days_from_text,
    parse,
    phrase_from_text,
    scope_from_text,
)
from data import (  # noqa: E402
    DATASET,
    OpenFdaRecallsClient,
    check_classification,
    check_recall_number,
    check_scope,
)

DATING = "2026-09-22T12:00:00Z"

FOOD_CLASS_I = {
    "recall_number": "H-1331-2026",
    "classification": "Class I",
    "status": "Ongoing",
    "recalling_firm": "EURO FOODS GROUP USA NJ INC",
    "product_description": "Crown Farms Dried Suri Cut, 200 gm, in plastic pack",
    "reason_for_recall": "The firm imported and distributed dried ribbon fish that was not properly eviscerated.",
    "report_date": "20260916",
    "recall_initiation_date": "20260801",
    "center_classification_date": "20260915",
    "voluntary_mandated": "Voluntary: Firm initiated",
    "initial_firm_notification": "Letter",
    "distribution_pattern": "The adulterated product was distributed to the following states: VA, NY, NJ, MI",
    "state": "NJ",
    "city": "Totowa",
    "country": "United States",
    "product_quantity": "1,890 cases",
    "product_type": "Food",
    "event_id": "96123",
    "scope": "food",
    "dataset": DATASET.format(scope="food"),
}

DRUG_CLASS_III = {
    "recall_number": "D-0835-2026",
    "classification": "Class III",
    "status": "Ongoing",
    "recalling_firm": "ImprimisRx NJ LLC",
    "product_description": "Povidone Iodine 1.25% / Proparacaine HCl 0.5% Ophthalmic Solution",
    "reason_for_recall": "Subpotent Drug",
    "report_date": "20260916",
    "scope": "drug",
    "dataset": DATASET.format(scope="drug"),
}

RESULT = {
    "scope": "food",
    "search": 'classification:"Class I"',
    "total": 149,
    "returned": 1,
    "last_updated": "2026-09-16",
    "per_scope": {"food": {"total": 149, "returned": 1}},
    "recalls": [FOOD_CLASS_I],
}

COUNTS = {
    "total": 1253,
    "by_classification": {"Class II": 941, "Class I": 270, "Class III": 40},
    "by_scope": {"food:Class I": 149},
}


class FakeRecalls(OpenFdaRecallsClient):
    """Same interface as the real client, no network."""

    def __init__(self, result=RESULT, counts=COUNTS, recall=FOOD_CLASS_I) -> None:
        self.result = result
        self.counts_row = counts
        self.recall_row = recall
        self.calls = []

    def searches(self, scope, search="", limit=10, sort="report_date:desc"):
        self.calls.append(("searches", scope, search, limit))
        return dict(self.result, scope=scope)

    def counts(self, scope, search=""):
        self.calls.append(("counts", scope, search))
        return dict(self.counts_row)

    def recall(self, recall_number):
        self.calls.append(("recall", recall_number))
        return dict(self.recall_row) if self.recall_row else None

    def build_search(self, **kwargs):
        self.calls.append(("build_search", kwargs))
        return " AND ".join(f'{key}:"{value}"' for key, value in sorted(kwargs.items()) if value)


class ValidationTests(unittest.TestCase):
    def test_check_scope(self):
        self.assertEqual(check_scope("all"), "all")
        self.assertEqual(check_scope("Drugs"), "drug")
        with self.assertRaises(ValueError):
            check_scope("toys")

    def test_check_classification(self):
        self.assertEqual(check_classification("class 1"), "Class I")
        self.assertEqual(check_classification("Class III"), "Class III")
        self.assertEqual(check_classification("CLASS ii"), "Class II")
        # The router stores the bare numeral it matched in the text, so it must round-trip.
        self.assertEqual(check_classification("I"), "Class I")
        self.assertEqual(check_classification("2"), "Class II")
        with self.assertRaises(ValueError):
            check_classification("Class IX")

    def test_build_search_accepts_what_the_router_produces(self):
        client = OpenFdaRecallsClient()
        self.assertEqual(client.build_search(classification="I"), 'classification:"Class I"')
        self.assertEqual(client.build_search(classification="iii"), 'classification:"Class III"')

    def test_check_recall_number(self):
        self.assertEqual(check_recall_number("h-1331-2026"), "H-1331-2026")
        with self.assertRaises(ValueError):
            check_recall_number("1331-2026")

    def test_date_window_uses_spaces(self):
        window = OpenFdaRecallsClient.date_window(days=7, date_to="20260922")
        self.assertIn(" TO ", window)
        self.assertNotIn("+", window)
        self.assertTrue(window.startswith("report_date:[202609"))
        self.assertEqual(OpenFdaRecallsClient.date_window(), "")


class ClientBehaviourTests(unittest.TestCase):
    def test_build_search_clauses(self):
        client = OpenFdaRecallsClient()
        search = client.build_search(classification="Class I", state="NY", product="romaine", days=7,
                                     date_to="20260922")
        self.assertIn('classification:"Class I"', search)
        self.assertIn('state:"NY"', search)
        self.assertIn('product_description:"romaine"', search)
        self.assertIn("report_date:[", search)
        self.assertEqual(search.count(" AND "), 3)

    def test_keywords_become_an_or_over_three_fields(self):
        client = OpenFdaRecallsClient()
        search = client.build_search(keywords="undeclared milk")
        self.assertIn('product_description:"undeclared milk"', search)
        self.assertIn('reason_for_recall:"undeclared milk"', search)
        self.assertIn('recalling_firm:"undeclared milk"', search)
        self.assertTrue(search.startswith("(product_description"))
        self.assertIn(" OR ", search)

    def test_malformed_field_name_is_rejected_before_the_api_sees_it(self):
        client = OpenFdaRecallsClient()
        with self.assertRaises(ValueError):
            client.build_search(product="a")  # too short to be a real search
        with self.assertRaises(ValueError):
            client.build_search(state="New York")

    def test_reads_map_fields_and_send_the_right_params(self):
        calls = []

        def fetch(url, params, headers):
            calls.append((url, dict(params), dict(headers)))
            return {"meta": {"results": {"total": 2}, "last_updated": "2026-09-16", "disclaimer": "do not rely"},
                    "results": [FOOD_CLASS_I]}

        client = OpenFdaRecallsClient(fetch=fetch)
        result = client._query("food", 'classification:"Class I"', limit=1)
        url, params, headers = calls[0]
        self.assertTrue(url.endswith("/food/enforcement.json"))
        self.assertEqual(params["limit"], "1")
        self.assertEqual(params["sort"], "report_date:desc")
        self.assertNotIn("api_key", params)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["last_updated"], "2026-09-16")
        self.assertEqual(result["recalls"][0]["scope"], "food")
        self.assertEqual(result["recalls"][0]["dataset"], DATASET.format(scope="food"))
        self.assertIn("Accept", headers)

    def test_404_is_an_empty_result_not_a_failure(self):
        import urllib.error

        error = urllib.error.HTTPError("https://api.fda.gov/food/enforcement.json", 404, "Not Found", {}, None)
        with mock.patch("data.request.urlopen", side_effect=error):
            client = OpenFdaRecallsClient()
            result = client._query("food", 'product_description:"zzzz"', limit=1)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["recalls"], [])

    def test_real_errors_carry_openfda_message_and_hide_the_key(self):
        import urllib.error

        body = json.dumps({"error": {"code": "SERVER_ERROR", "message": "Check your request and try again"}}).encode()
        error = urllib.error.HTTPError("https://api.fda.gov/food/enforcement.json", 500, "Server Error", {},
                                      mock.Mock(read=lambda: body))
        with mock.patch.dict("os.environ", {"OPENFDA_API_KEY": "secret-key"}), \
                mock.patch("data.request.urlopen", side_effect=error):
            client = OpenFdaRecallsClient()
            with self.assertRaises(UpstreamError) as caught:
                client._query("food", "", limit=1)
        message = str(caught.exception)
        self.assertIn("openFDA answered 500", message)
        self.assertIn("Check your request", message)
        self.assertNotIn("secret-key", message)

    def test_counts_sum_across_scopes(self):
        def fetch(url, params, headers):
            self.assertEqual(params["count"], "classification.exact")
            scope = url.split("/")[3]
            rows = {"drug": [{"term": "Class I", "count": 17}, {"term": "Class II", "count": 217}],
                    "food": [{"term": "Class I", "count": 149}],
                    "device": [{"term": "Class II", "count": 636}]}[scope]
            return {"results": rows}

        client = OpenFdaRecallsClient(fetch=fetch)
        counts = client.counts("all", 'report_date:[20260601 TO 20260922]')
        self.assertEqual(counts["total"], 1019)
        self.assertEqual(counts["by_classification"]["Class I"], 166)
        self.assertEqual(counts["by_scope"]["device:Class II"], 636)

    def test_searches_merges_and_sorts_all_scopes(self):
        def fetch(url, params, headers):
            scope = url.split("/")[3]
            row = {"drug": DRUG_CLASS_III, "food": FOOD_CLASS_I,
                   "device": {**FOOD_CLASS_I, "recall_number": "Z-0001-2026", "report_date": "20260920"}}[scope]
            return {"meta": {"results": {"total": 1}, "last_updated": "2026-09-16"}, "results": [row]}

        client = OpenFdaRecallsClient(fetch=fetch)
        merged = client.searches("all", 'classification:"Class I"', limit=2)
        self.assertEqual(merged["total"], 3)
        self.assertEqual(merged["per_scope"]["device"]["total"], 1)
        self.assertEqual([row["recall_number"] for row in merged["recalls"]][0], "Z-0001-2026")
        self.assertEqual(len(merged["recalls"]), 2)


class ParseTests(unittest.TestCase):
    def test_recent_by_default(self):
        request = parse({"parts": [{"kind": "text", "text": "what was recalled recently?"}]})
        self.assertEqual(request["skill"], "recalls-recent")
        self.assertTrue(request["explicit"])

    def test_days_windows(self):
        self.assertEqual(days_from_text("recalls in the last 7 days"), 7)
        self.assertEqual(days_from_text("anything this month?"), 30)
        self.assertIsNone(days_from_text("any recalls?"))
        request = parse({"parts": [{"kind": "text", "text": "what was recalled in the last 7 days?"}]})
        self.assertEqual(request["params"]["days"], 7)

    def test_scope_words(self):
        self.assertEqual(scope_from_text("any food recalls?"), "food")
        self.assertEqual(scope_from_text("pacemaker recalls"), "device")
        self.assertEqual(scope_from_text("drug recalls"), "drug")
        self.assertIsNone(scope_from_text("what was recalled?"))

    def test_class_and_state(self):
        request = parse({"parts": [{"kind": "text", "text": "any Class I device recalls in CA this year?"}]})
        self.assertEqual(request["params"]["classification"], "I")
        self.assertEqual(request["params"]["state"], "CA")
        self.assertEqual(request["params"]["days"], 365)
        self.assertEqual(request["params"]["scope"], "device")

    def test_recall_number_routes_to_lookup(self):
        request = parse({"parts": [{"kind": "text", "text": "what is recall H-1331-2026?"}]})
        self.assertEqual(request["skill"], "recall-lookup")
        self.assertEqual(request["params"]["recall_number"], "H-1331-2026")

    def test_phrase_extraction(self):
        self.assertEqual(phrase_from_text("search recalls for romaine this week"), "romaine")
        self.assertEqual(phrase_from_text('recalls for "undeclared milk"'), "undeclared milk")
        self.assertIsNone(phrase_from_text("what was recalled?"))
        request = parse({"parts": [{"kind": "text", "text": "search recalls for romaine"}]})
        self.assertEqual(request["skill"], "recall-search")
        self.assertEqual(request["params"]["product"], "romaine")

    def test_firm(self):
        request = parse({"parts": [{"kind": "text", "text": "any recalls by firm EURO FOODS GROUP USA NJ INC?"}]})
        self.assertEqual(request["params"]["firm"], "EURO FOODS GROUP USA NJ INC")

    def test_summary_and_watch(self):
        summary = parse({"parts": [{"kind": "text", "text": "how many Class I food recalls this year?"}]})
        self.assertEqual(summary["skill"], "recalls-summary")
        watch = parse({"parts": [{"kind": "text", "text": "tell me when a new Class I food recall appears"}]})
        self.assertEqual(watch["skill"], "recalls-watch")
        self.assertEqual(watch["params"]["classification"], "I")

    def test_explicit_json(self):
        request = parse({"parts": [{"kind": "data", "data": {"skill": "recall-search", "scope": "food",
                                                            "product": "romaine", "days": 365}}]})
        self.assertEqual(request["skill"], "recall-search")
        self.assertEqual(request["params"]["scope"], "food")
        self.assertTrue(request["explicit"])

    def test_help_and_empty(self):
        self.assertEqual(parse({"parts": [{"kind": "text", "text": ""}]})["skill"], "help")
        self.assertEqual(parse({"parts": [{"kind": "text", "text": "what can you do?"}]})["skill"], "help")


class SkillTests(unittest.TestCase):
    def run_skill(self, skill, client=None, **params):
        agent = RecallsAgent(client=client or FakeRecalls())
        request = {"skill": skill, "params": params, "missing": agent.missing(skill, params)}
        return agent.run(request)

    def test_classification_is_spelled_out_in_prose(self):
        result = self.run_skill("recalls-recent", scope="food", classification="I", days=30)
        self.assertTrue(result["message"].startswith("149 Class I recalls in the last 30 days"),
                        result["message"].splitlines()[0])

    def test_recent_cites_dataset_and_disclaimer(self):
        result = self.run_skill("recalls-recent", scope="food", days=30)
        self.assertEqual(result["final_state"], "completed")
        self.assertIn(DATASET.format(scope="food"), result["message"])
        self.assertIn("unvalidated", result["message"])
        self.assertIn("H-1331-2026", result["message"])
        self.assertIn("2026-09-16", result["message"])
        self.assertEqual(result["artifact"]["total"], 149)

    def test_recent_defaults_to_a_30_day_window(self):
        client = FakeRecalls()
        self.run_skill("recalls-recent", client=client)
        build = [call for call in client.calls if call[0] == "build_search"][0]
        self.assertEqual(build[1]["days"], 30)

    def test_recent_with_no_matches_is_honest(self):
        empty = {"scope": "food", "search": "", "total": 0, "returned": 0, "last_updated": "2026-09-16",
                 "per_scope": {"food": {"total": 0, "returned": 0}}, "recalls": []}
        result = self.run_skill("recalls-recent", client=FakeRecalls(result=empty), scope="food")
        self.assertIn("no food recalls", result["message"].lower())
        self.assertIn("404", result["message"])

    def test_search_reports_the_query_it_ran(self):
        result = self.run_skill("recall-search", client=FakeRecalls(), scope="food", product="romaine")
        self.assertIn("149", result["message"])
        self.assertIn("romaine", result["message"])
        self.assertEqual(result["artifact"]["scope"], "food")

    def test_search_without_any_filter_asks(self):
        result = self.run_skill("recall-search", scope="all")
        self.assertIn("Tell me what to look for", result["message"])
        self.assertIsNone(result["watch"])

    def test_summary_breakdown(self):
        result = self.run_skill("recalls-summary", scope="all", days=90)
        self.assertIn("1,253", result["message"])
        self.assertIn("Class II 941", result["message"])
        self.assertEqual(result["artifact"]["by_classification"]["Class I"], 270)

    def test_lookup_found(self):
        result = self.run_skill("recall-lookup", recall_number="H-1331-2026")
        self.assertIn("H-1331-2026", result["message"])
        self.assertIn("EURO FOODS GROUP", result["message"])
        self.assertIn("VA, NY, NJ, MI", result["message"])
        self.assertEqual(result["artifact"]["dataset"], DATASET.format(scope="food"))

    def test_lookup_not_found(self):
        result = self.run_skill("recall-lookup", client=FakeRecalls(recall=None), recall_number="Z-9999-2026")
        self.assertIn("holds no recall", result["message"])
        self.assertIsNone(result["artifact"]["recall"])

    def test_lookup_requires_a_number(self):
        agent = RecallsAgent(client=FakeRecalls())
        self.assertEqual(agent.missing("recall-lookup", {}), ["recall_number"])
        result = agent.run({"skill": "recall-lookup", "params": {}, "missing": ["recall_number"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("H-1331-2026", result["message"])

    def test_watch_needs_a_filter(self):
        agent = RecallsAgent(client=FakeRecalls())
        self.assertEqual(agent.missing("recalls-watch", {"scope": "all"}), ["filter"])
        result = agent.run({"skill": "recalls-watch", "params": {"scope": "all"}, "missing": ["filter"]})
        self.assertEqual(result["final_state"], "input-required")
        self.assertIn("at least one filter", result["message"])

    def test_watch_payload_is_stable_and_names_the_newest(self):
        client = FakeRecalls()
        result = self.run_skill("recalls-watch", client=client, scope="food", classification="I")
        watch = result["watch"]
        self.assertEqual(watch["kind"], "recalls-watch")
        self.assertEqual(watch["scope"], "food")
        self.assertEqual(watch["observed"]["newest"], "H-1331-2026")
        self.assertEqual(watch["observed"]["total"], 149)
        self.assertIn("pushNotificationConfig", result["message"])
        self.assertIn("H-1331-2026", result["message"])

    def test_watch_scope_all_is_not_a_filter(self):
        result = self.run_skill("recalls-watch", scope="all")
        self.assertIsNone(result["watch"])
        self.assertIn("at least one filter", result["message"])

    def test_help_lists_the_skills(self):
        result = self.run_skill("help")
        self.assertIn("openFDA", result["message"])
        self.assertIsNone(result["artifact"])

    def test_card_and_watch_kinds(self):
        agent = RecallsAgent(client=FakeRecalls())
        self.assertEqual([skill["id"] for skill in CARD_SKILLS],
                         ["recalls-recent", "recall-search", "recalls-summary", "recall-lookup", "recalls-watch"])
        self.assertEqual(agent.watch_kinds, ("recalls-watch",))
        self.assertEqual(agent.env_prefix, "OPENFDA")
        self.assertEqual(len(agent.datasets), 3)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(":memory:")
        self.watch = {
            "kind": "recalls-watch",
            "dataset": DATASET.format(scope="food"),
            "scope": "food",
            "search": 'classification:"Class I"',
            "summary": "Class I recalls in the last 30 days",
            "observed": {"scope": "food", "search": 'classification:"Class I"',
                         "newest": "H-1331-2026", "newest_report_date": "20260916", "total": 149},
        }
        self.task = {
            "kind": "task",
            "id": "watched",
            "contextId": "ctx",
            "status": {"state": "completed", "timestamp": DATING},
            "history": [],
            "artifacts": [],
            "metadata": {"watch": self.watch, "last_observed": dict(self.watch["observed"])},
        }
        self.store.save_task(self.task)
        self.store.set_push_config("watched", {"id": "cfg", "url": "https://example.com/hook", "token": "tok"})

    def tearDown(self):
        self.store.close()

    def test_probe_is_stable_when_nothing_new(self):
        client = FakeRecalls()
        agent = RecallsAgent(client=client)
        self.assertEqual(agent.probe_watch(self.watch), agent.probe_watch(self.watch))
        self.assertEqual(agent.probe_watch(self.watch)["newest"], "H-1331-2026")

    def test_watcher_posts_once_when_a_new_recall_appears(self):
        new_recall = {**FOOD_CLASS_I, "recall_number": "H-1400-2026", "report_date": "20260921"}
        updated = dict(RESULT, total=150, recalls=[new_recall])
        sent = []

        def post(url, payload, headers):
            sent.append((url, json.loads(payload), headers))
            return 200

        agent = RecallsAgent(client=FakeRecalls(result=updated))
        watcher = PushWatcher(self.store, agent, interval=5)
        watcher._post = post
        self.assertEqual(watcher.tick(), 1)
        url, event, headers = sent[0]
        self.assertEqual(url, "https://example.com/hook")
        self.assertEqual(headers["X-A2A-Notification-Token"], "tok")
        text = event["status"]["message"]["parts"][0]["text"]
        self.assertIn("New openFDA recall", text)
        self.assertIn("1 more", text)
        self.assertIn("H-1400-2026", text)
        self.assertEqual(event["metadata"]["observed"]["newest"], "H-1400-2026")
        self.assertEqual(watcher.tick(), 0)

    def test_watcher_skips_a_watch_it_cannot_probe(self):
        agent = RecallsAgent(client=FakeRecalls())
        self.assertIsNone(agent.probe_watch({"kind": "recalls-watch"}))
        watcher = PushWatcher(self.store, agent, interval=5)
        watcher._post = lambda *args: 200
        self.assertEqual(watcher.tick(), 0)


if __name__ == "__main__":
    unittest.main()
