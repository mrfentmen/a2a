#!/usr/bin/env python3
"""End-to-end smoke test for the FAA airport status A2A server.

    FAA_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/airports/server.py --port 8801
    python3 servers/airports/tools/smoke.py --base http://127.0.0.1:8801

Live network: reads the FAA's national airspace status snapshot. The snapshot can legitimately
be empty (no delays anywhere), so the checks assert shape and honesty rather than count > 0.
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

DATASET = "faa.gov/airport-status"
KINDS = {"ground-delay", "arrival-delay", "departure-delay", "closure"}


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def task_text(response: dict) -> str:
    status = response.get("status") or {}
    return ((status.get("message") or {}).get("parts") or [{}])[0].get("text", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the FAA airport status A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8801")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "FAA Airport Status Agent",
        ["airport-status", "airports-delays", "airports-closures", "airport-watch"],
    ):
        return 1

    # Live: the national delay picture.
    delays = smoke.rpc("message/send", {"message": smoke.user_message("what are the worst airport delays in the country right now?")}).get("result", {})
    delays_data = artifact_data(delays)
    counts = delays_data.get("counts") or {}
    rows = delays_data.get("delays") or []
    windows = [row.get("max_minutes") or 0 for row in rows]
    smoke.check("airports-delays completes", delays.get("status", {}).get("state") == "completed")
    smoke.check("answer cites the dataset", delays_data.get("dataset") == DATASET)
    smoke.check("the snapshot time is parsed", bool(delays_data.get("update_time")),
                str(delays_data.get("update_time")))
    smoke.check("every kind is counted", set(counts) == KINDS, str(sorted(counts)))
    smoke.check("delays are worst first", windows == sorted(windows, reverse=True), str(windows[:3]))
    smoke.check("each delay row names an airport and a kind",
                all(row.get("airport") and row.get("kind") in KINDS for row in rows))
    smoke.check("no closure leaks into the delay list",
                all(row.get("kind") != "closure" for row in rows))

    # Live: the closure list, which carries its own start and reopen columns.
    closures = smoke.rpc("message/send", {"message": smoke.user_message("are any airports closed?")}).get("result", {})
    closures_data = artifact_data(closures)
    closed = closures_data.get("closures") or []
    smoke.check("airports-closures completes", closures.get("status", {}).get("state") == "completed")
    smoke.check("closure rows carry start and reopen fields",
                all("start" in row and "reopen" in row for row in closed),
                f"{len(closed)} closure row(s)")

    # Live: one airport's slice.
    status = smoke.rpc("message/send", {"message": smoke.user_message("is SFO delayed right now?")}).get("result", {})
    status_data = artifact_data(status)
    report = (status_data.get("reports") or [{}])[0]
    smoke.check("airport-status completes", status.get("status", {}).get("state") == "completed")
    smoke.check("the airport was resolved", report.get("airport") == "SFO"
                and report.get("city") == "San Francisco", str(report.get("airport")))
    smoke.check("the per-airport slice lists only that airport",
                all(row.get("airport") == "SFO" for row in report.get("entries") or []))
    smoke.check("the answer names the snapshot time in UTC", "UTC" in task_text(status))

    # An airport with nothing reported must say so plainly.
    quiet = smoke.rpc("message/send", {"message": smoke.user_message("anything wrong at ATL?")}).get("result", {})
    quiet_data = artifact_data(quiet)
    smoke.check("unaffected airport is reported honestly",
                "not that operations are normal" in task_text(quiet)
                and quiet_data.get("count") == 0)

    # input-required -> follow-up with a code.
    ask = {"kind": "message", "role": "user", "messageId": "m",
           "parts": [{"kind": "data", "data": {"skill": "airport-status"}}]}
    first = smoke.rpc("message/send", {"message": ask}).get("result", {})
    smoke.check("airport-status without an airport -> input-required",
                first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("ORD", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the airport",
                follow.get("status", {}).get("state") == "completed"
                and follow_data.get("airports") == ["ORD"])

    # Live: a watch on the whole system.
    watch = smoke.rpc("message/send", {"message": smoke.user_message("notify me when any ground delay program starts")}).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("airport-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records the scope and the open items",
                watch_meta.get("airport") is None and "filter_kind" in watch_meta
                and isinstance((watch_meta.get("observed") or {}).get("entries"), dict),
                str(len((watch_meta.get("observed") or {}).get("entries") or {})))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("what is happening at ORD?")})
    kinds = [event.get("kind") for event in events]
    smoke.check("stream starts with task + working status", kinds[:2] == ["task", "status-update"], str(kinds))
    smoke.check("stream includes artifact", "artifact-update" in kinds)
    smoke.check("stream ends final",
                bool(events[-1].get("final")) and events[-1].get("status", {}).get("state") == "completed")

    # Push config CRUD.
    task_id = watch.get("id")
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
