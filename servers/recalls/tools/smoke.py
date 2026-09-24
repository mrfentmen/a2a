#!/usr/bin/env python3
"""End-to-end smoke test for the openFDA recalls A2A server.

    OPENFDA_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/recalls/server.py --port 8796
    python3 servers/recalls/tools/smoke.py --base http://127.0.0.1:8796
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parents[1]
for path in (str(SERVER_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from a2a_kit.smoke import Smoke  # noqa: E402  (path setup must come first)

DRUG = "api.fda.gov/drug/enforcement"
FOOD = "api.fda.gov/food/enforcement"
DEVICE = "api.fda.gov/device/enforcement"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the openFDA recalls A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8796")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "openFDA Recalls Agent",
        ["recalls-recent", "recall-search", "recalls-summary", "recall-lookup", "recalls-watch"],
    ):
        return 1

    # Live: the newest records across all three registers. The window is wide on purpose:
    # openFDA publishes enforcement reports in batches, so the newest report_date can be a
    # week or more behind today and a 7-day window honestly returns nothing.
    recent = smoke.rpc(
        "message/send", {"message": smoke.user_message("what was recalled in the last 30 days?")}
    ).get("result", {})
    recent_data = artifact_data(recent)
    recalls = recent_data.get("recalls") or []
    smoke.check("recent skill completes", recent.get("status", {}).get("state") == "completed")
    smoke.check("live recalls returned", len(recalls) >= 1, str(recent_data.get("total")))
    smoke.check("recent artifact carries the disclaimer",
                "unvalidated" in (recent_data.get("disclaimer") or ""))
    smoke.check("recent queries all three registers",
                set(recent_data.get("per_scope") or {}) == {"drug", "food", "device"},
                str({scope: value.get("total") for scope, value in (recent_data.get("per_scope") or {}).items()}))
    smoke.check("openFDA dataset stamp present", bool(recent_data.get("dataset_updated")),
                str(recent_data.get("dataset_updated")))
    newest = recalls[0] if recalls else {}
    smoke.check("newest record has a recall number and a date",
                bool(newest.get("recall_number")) and bool(newest.get("report_date")),
                f"{newest.get('recall_number')} {newest.get('report_date')}")

    # Live: keyword search over the food register.
    search = smoke.rpc(
        "message/send", {"message": smoke.user_message("search recalls for romaine")}
    ).get("result", {})
    search_data = artifact_data(search)
    smoke.check("search skill completes", search.get("status", {}).get("state") == "completed")
    smoke.check("romaine search is non-empty", search_data.get("total", 0) >= 1, str(search_data.get("total")))
    smoke.check("search cites the food register", search_data.get("dataset") == FOOD,
                str(search_data.get("dataset")))

    # Live: counts by classification across all three registers.
    summary = smoke.rpc(
        "message/send", {"message": smoke.user_message("how many recalls are there in the last 90 days?")}
    ).get("result", {})
    summary_data = artifact_data(summary)
    by_class = summary_data.get("by_classification") or {}
    smoke.check("summary skill completes", summary.get("status", {}).get("state") == "completed")
    smoke.check("summary totals add up", summary_data.get("total", 0) == sum(by_class.values()))
    smoke.check("summary splits the classes", len(by_class) >= 2, str(by_class))

    # Live: lookup the newest record we just read, so the number is real.
    if newest.get("recall_number"):
        lookup = smoke.rpc(
            "message/send", {"message": smoke.user_message(f"what is recall {newest['recall_number']}?")}
        ).get("result", {})
        lookup_data = artifact_data(lookup)
        smoke.check("lookup skill completes", lookup.get("status", {}).get("state") == "completed")
        smoke.check("lookup returns the same recall",
                    (lookup_data.get("recall") or {}).get("recall_number") == newest["recall_number"],
                    str((lookup_data.get("recall") or {}).get("recall_number")))
        smoke.check("lookup cites that register",
                    lookup_data.get("dataset") in (DRUG, FOOD, DEVICE), str(lookup_data.get("dataset")))

    # input-required -> follow-up: a bare filter completes the pending watch.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch for new recalls")}).get("result", {})
    smoke.check("watch without a filter -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("Class I food recall", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = artifact_data(follow)
    watching = watch_data.get("watching") or {}
    smoke.check("follow-up starts the watch",
                follow.get("status", {}).get("state") == "completed" and watching.get("scope") == "food")
    newest_watched = (watch_data.get("newest_recall") or {}).get("recall_number")
    smoke.check("watch reports the newest matching recall", bool(newest_watched), str(newest_watched))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("how many device recalls in the last 30 days?")})
    kinds = [event.get("kind") for event in events]
    smoke.check("stream starts with task + working status", kinds[:2] == ["task", "status-update"], str(kinds))
    smoke.check("stream includes artifact", "artifact-update" in kinds)
    smoke.check("stream ends final", bool(events[-1].get("final")) and events[-1].get("status", {}).get("state") == "completed")

    # Push config CRUD.
    task_id = follow.get("id")
    config = smoke.rpc(
        "tasks/pushNotificationConfig/set",
        {"taskId": task_id, "pushNotificationConfig": {"url": args.hook, "token": "smoke"}},
    ).get("result", {})
    config_id = config.get("pushNotificationConfig", {}).get("id")
    smoke.check("push config set", bool(config_id))
    listed = smoke.rpc("tasks/pushNotificationConfig/list", {"taskId": task_id}).get("result", {})
    smoke.check("push config listed", len(listed.get("configs", [])) == 1)
    smoke.rpc("tasks/pushNotificationConfig/delete", {"taskId": task_id, "pushNotificationConfigId": config_id})
    after = smoke.rpc("tasks/pushNotificationConfig/list", {"taskId": task_id}).get("result", {})
    smoke.check("push config deleted", after.get("configs") == [])

    return smoke.finish()


if __name__ == "__main__":
    raise SystemExit(main())
