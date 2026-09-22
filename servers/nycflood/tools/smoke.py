#!/usr/bin/env python3
"""End-to-end smoke test for the NYC street-flooding (FloodNet) A2A server.

    NYC_FLOOD_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/nycflood/server.py --port 8788
    python3 servers/nycflood/tools/smoke.py --base http://127.0.0.1:8788
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the NYC flooding A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8788")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "NYC Street Flooding Agent", ["flood-recent", "flood-sensors", "flood-watch"]
    ):
        return 1

    # Live: sensors in a ZIP that is known to have FloodNet coverage.
    sensors = smoke.rpc("message/send", {"message": smoke.user_message("Which flood sensors cover 11211?")}).get("result", {})
    sensor_data = ((sensors.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    smoke.check("sensors skill completes", sensors.get("status", {}).get("state") == "completed")
    smoke.check("sensors found in 11211", sensor_data.get("count", 0) >= 1, str(sensor_data.get("count")))
    smoke.check(
        "sensor artifact cites dataset + freshness",
        sensor_data.get("dataset") == "kb2e-tjy3" and bool(sensor_data.get("freshness")),
    )

    # Live: real flooding events in the last 30 days.
    recent = smoke.rpc("message/send", {"message": smoke.user_message("Which streets flooded in the last 30 days?")}).get("result", {})
    event_data = ((recent.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    smoke.check("recent skill completes", recent.get("status", {}).get("state") == "completed")
    smoke.check("30 day window parsed from text", event_data.get("window_hours") == 720, str(event_data.get("window_hours")))
    smoke.check("real flood events returned", event_data.get("count", 0) >= 1, str(event_data.get("count")))
    smoke.check("event artifact cites dataset", event_data.get("dataset") == "aq7i-eu5q")

    # input-required -> follow-up for a watch with no place.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch for flooding")}).get("result", {})
    smoke.check("watch without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("watch 11211", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = ((follow.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    smoke.check(
        "follow-up starts the watch",
        follow.get("status", {}).get("state") == "completed" and bool(watch_data.get("watching")),
    )

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("flood sensors in 11211")})
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
