#!/usr/bin/env python3
"""End-to-end smoke test for the NOAA tides A2A server.

    NOAA_TIDES_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/tides/server.py --port 8793
    python3 servers/tides/tools/smoke.py --base http://127.0.0.1:8793
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

DATASET = "api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATIONS_DATASET = "api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations"
BATTERY = "8518750"
BATTERY_POINT = "40.7006,-74.0142"
SEATTLE = "9447130"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the NOAA tides A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8793")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "NOAA Tides Agent",
        ["tide-predictions", "tide-next", "water-level", "stations", "tide-watch"],
    ):
        return 1

    # Live: high/low tide table for The Battery.
    table = smoke.rpc(
        "message/send", {"message": smoke.user_message(f"what are the tides at station {BATTERY} today?")}
    ).get("result", {})
    table_data = artifact_data(table)
    smoke.check("predictions skill completes", table.get("status", {}).get("state") == "completed")
    smoke.check("predictions returned live events", len(table_data.get("events") or []) >= 2,
                str(len(table_data.get("events") or [])))
    smoke.check("predictions cite dataset + datum",
                table_data.get("dataset") == DATASET and bool(table_data.get("datum")))
    smoke.check("prediction times are station local",
                " " in str((table_data.get("events") or [{}])[0].get("time", "")),
                str((table_data.get("events") or [{}])[0].get("time")))

    # Live: next high and next low.
    nxt = smoke.rpc(
        "message/send", {"message": smoke.user_message(f"when is the next high tide at {BATTERY}?")}
    ).get("result", {})
    nxt_data = artifact_data(nxt)
    smoke.check("next skill completes", nxt.get("status", {}).get("state") == "completed")
    smoke.check("next_high present with a height", bool((nxt_data.get("next_high") or {}).get("value")),
                str((nxt_data.get("next_high") or {}).get("time")))
    smoke.check("next_low present with a height", bool((nxt_data.get("next_low") or {}).get("value")))

    # Live: observed water level against flood stages.
    level = smoke.rpc(
        "message/send", {"message": smoke.user_message("how high is the water at 8518750 right now?")}
    ).get("result", {})
    level_data = artifact_data(level)
    observation = level_data.get("observation") or {}
    smoke.check("water-level skill completes", level.get("status", {}).get("state") == "completed")
    smoke.check("observation has a value + time", observation.get("value") is not None and bool(observation.get("time")),
                f"{observation.get('value')} at {observation.get('time')}")
    smoke.check("flood stage reported", bool(level_data.get("flood_stage")), str(level_data.get("flood_stage")))
    smoke.check("flood levels published for the station",
                (level_data.get("flood_levels") or {}).get("nos_minor") is not None,
                str((level_data.get("flood_levels") or {}).get("nos_minor")))

    # Live: nearest station to a point, and a second city to prove the search is real.
    near = smoke.rpc(
        "message/send", {"message": smoke.user_message(f"which tide stations are near {BATTERY_POINT}?")}
    ).get("result", {})
    near_data = artifact_data(near)
    matches = near_data.get("station_matches") or []
    smoke.check("stations skill completes", near.get("status", {}).get("state") == "completed")
    smoke.check("nearest station is The Battery", bool(matches) and matches[0].get("id") == BATTERY,
                str([row.get("id") for row in matches[:3]]))
    smoke.check("stations cite the catalogue", near_data.get("dataset") == STATIONS_DATASET)
    seattle = smoke.rpc(
        "message/send", {"message": smoke.user_message("tide table near 47.6026,-122.3393 for 1 day")}
    ).get("result", {})
    seattle_data = artifact_data(seattle)
    smoke.check("point resolution picks the Seattle station", seattle_data.get("station") == SEATTLE,
                str(seattle_data.get("station")))

    # input-required -> follow-up for a watch with no place, then a live watch payload.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch the water for flooding")}).get("result", {})
    smoke.check("watch without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(BATTERY, taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = artifact_data(follow)
    watching = watch_data.get("watching") or {}
    smoke.check("follow-up starts the watch",
                follow.get("status", {}).get("state") == "completed" and watching.get("station") == BATTERY)
    smoke.check("watch defaults to the station flood stage",
                watching.get("flood_stage") in ("action", "minor", "moderate", "major"),
                f"{watching.get('flood_stage')} at {watching.get('threshold')} {watching.get('units')}")

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message(f"next low tide at {BATTERY}")})
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
