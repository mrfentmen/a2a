#!/usr/bin/env python3
"""End-to-end smoke test for the NWS weather-alerts A2A server.

    NWS_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/nws/server.py --port 8791
    python3 servers/nws/tools/smoke.py --base http://127.0.0.1:8791
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

DATASET = "api.weather.gov/alerts/active"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the NWS alerts A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8791")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "NWS Weather Alerts Agent", ["alerts-active", "alerts-summary", "alerts-watch"]
    ):
        return 1

    # Live: active alerts nationwide (always non-empty; the feed covers the whole country).
    active = smoke.rpc("message/send", {"message": smoke.user_message("Any weather alerts right now?")}).get("result", {})
    active_data = artifact_data(active)
    smoke.check("active skill completes", active.get("status", {}).get("state") == "completed")
    smoke.check("live alerts returned", active_data.get("count", 0) >= 1, str(active_data.get("count")))
    smoke.check(
        "active artifact cites dataset + freshness",
        active_data.get("dataset") == DATASET and bool(active_data.get("freshness")),
    )

    # Live: national counts straight from the count endpoint.
    summary = smoke.rpc("message/send", {"message": smoke.user_message("How many weather alerts are active nationwide?")}).get("result", {})
    summary_data = artifact_data(summary)
    smoke.check("summary skill completes", summary.get("status", {}).get("state") == "completed")
    smoke.check("national count is non-zero", summary_data.get("counts", {}).get("total", 0) >= 1, str(summary_data.get("counts", {}).get("total")))
    smoke.check("counts split into land + marine", "land" in summary_data.get("counts", {}))

    # Live: severity filter by point (a US city centre).
    pointed = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("any severe alerts near 40.7128,-74.0060?")},
    ).get("result", {})
    pointed_data = artifact_data(pointed)
    smoke.check("point query completes", pointed.get("status", {}).get("state") == "completed")
    smoke.check("point query labels the place", pointed_data.get("place") == "the point 40.7128,-74.0060", str(pointed_data.get("place")))

    # input-required -> follow-up for a watch with no place.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch for weather alerts")}).get("result", {})
    smoke.check("watch without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("NY", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = artifact_data(follow)
    smoke.check(
        "follow-up starts the watch",
        follow.get("status", {}).get("state") == "completed" and bool(watch_data.get("watching")),
    )

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("how many alerts are active?")})
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
