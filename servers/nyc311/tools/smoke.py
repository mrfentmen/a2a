#!/usr/bin/env python3
"""End-to-end smoke test for the NYC 311 A2A server against live data.

    # terminal 1 (local demos need private webhooks enabled)
    NYC311_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/nyc311/server.py
    # terminal 2
    python3 servers/nyc311/tools/smoke.py

Exercises: card, a real 311 lookup, input-required continuation, SSE streaming,
push-config CRUD. Exits non-zero if anything fails.
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

from data import NYC311Client  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the NYC 311 A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks("NYC Open Data Agent", ["complaint-status", "complaints-near"]):
        return 1

    # A real complaint id from the live dataset.
    client = NYC311Client()
    row = None
    for zip_code in ("11235", "10001", "11106"):
        rows = client.recent_complaints(zip_code=zip_code, days=30, limit=1)
        if rows:
            row = rows[0]
            break
    if not row:
        smoke.check("live dataset reachable", False, "no recent complaints found; network down?")
        return 1
    key = row["unique_key"]
    smoke.check("live dataset reachable", True, f"picked complaint {key}")

    # Blocking lookup.
    task = smoke.rpc("message/send", {"message": smoke.user_message(f"Did complaint {key} get fixed?")}).get("result", {})
    artifact = (task.get("artifacts") or [{}])[-1]
    data = (artifact.get("parts") or [{}])[0].get("data", {})
    smoke.check("message/send completes", task.get("status", {}).get("state") == "completed", task.get("status", {}).get("state"))
    smoke.check("artifact carries the complaint", data.get("complaint", {}).get("unique_key") == key)
    smoke.check("artifact cites dataset + freshness", data.get("dataset") == "erm2-nwe9" and bool(data.get("freshness")))

    # input-required -> follow-up inside the same task.
    first = smoke.rpc("message/send", {"message": smoke.user_message("hello there")}).get("result", {})
    smoke.check("ambiguous ask -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(f"it is {key}", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    smoke.check(
        "follow-up completes same task",
        follow.get("id") == first.get("id") and follow.get("status", {}).get("state") == "completed",
    )

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message(f"complaint {key} status please")})
    kinds = [event.get("kind") for event in events]
    smoke.check("stream starts with task + working status", kinds[:2] == ["task", "status-update"], str(kinds))
    smoke.check("stream ends final", bool(events[-1].get("final")) and events[-1].get("status", {}).get("state") == "completed")
    smoke.check("stream includes artifact", "artifact-update" in kinds)

    # Push config CRUD (the watcher firing is covered by unit tests).
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
