#!/usr/bin/env python3
"""End-to-end smoke test for the NDBC buoy A2A server.

    BUOY_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/buoys/server.py --port 8800
    python3 servers/buoys/tools/smoke.py --base http://127.0.0.1:8800

Live network: reads NOAA's latest-observation table, the station catalogue and one station's
per-station history file.
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

DATASET = "ndbc.noaa.gov/realtime-observations"
#: A station that has been reporting continuously for years, so the checks are not flaky.
STATION = "41010"
#: Sensors drop out one at a time, so the watch check picks from whatever this station reports.
WATCH_FIELDS = (
    ("wave_height", f"tell me when the waves at {STATION} pass 6"),
    ("wind_speed", f"tell me when the wind at {STATION} passes 20"),
)


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def task_text(response: dict) -> str:
    status = response.get("status") or {}
    return ((status.get("message") or {}).get("parts") or [{}])[0].get("text", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the NDBC buoy A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8800")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "NDBC Buoy Agent",
        ["buoy-conditions", "buoy-trend", "buoys-near", "buoys-list", "buoy-watch"],
    ):
        return 1

    # Live: the newest reading at a real station.
    conditions = smoke.rpc("message/send", {"message": smoke.user_message(f"what are the conditions at buoy {STATION}?")}).get("result", {})
    data = artifact_data(conditions)
    observation = data.get("observation") or {}
    smoke.check("buoy-conditions completes", conditions.get("status", {}).get("state") == "completed")
    smoke.check("answer cites the dataset", data.get("dataset") == DATASET)
    smoke.check("the station's own row came back", observation.get("station") == STATION,
                str(observation.get("station")))
    smoke.check("the reading is timestamped and aged", bool(observation.get("time"))
                and isinstance(data.get("age_minutes"), int), str(data.get("age_minutes")))
    smoke.check("units are labelled on the fields the station publishes",
                bool(observation.get("units", {}).get("wind_speed"))
                and bool(observation.get("units", {}).get("wave_height")))
    smoke.check("the message says where the values came from",
                "National Data Buoy Center" in task_text(conditions))

    # Live: nearest reporting stations to a coastal city, with distances.
    near = smoke.rpc("message/send", {"message": smoke.user_message("which buoys are near Miami within 700 miles?")}).get("result", {})
    near_data = artifact_data(near)
    stations = near_data.get("stations") or []
    distances = [row.get("distance_miles") for row in stations]
    smoke.check("buoys-near completes", near.get("status", {}).get("state") == "completed")
    smoke.check("stations came back with distances",
                bool(stations) and all(value is not None for value in distances), str(distances[:3]))
    smoke.check("nearest first", distances == sorted(distances), str(distances[:3]))

    # Live: one station's recent history with per-field lows and highs.
    trend = smoke.rpc("message/send", {"message": smoke.user_message(f"how have the waves at {STATION} trended over the last 12 hours?")}).get("result", {})
    trend_data = artifact_data(trend)
    stats = trend_data.get("stats") or {}
    smoke.check("buoy-trend completes", trend.get("status", {}).get("state") == "completed")
    smoke.check("history rows came back", int(trend_data.get("count", 0)) >= 1,
                str(trend_data.get("count")))
    smoke.check("each published field has a low, a high and a sample count",
                all(set(stats[field]) >= {"latest", "min", "max", "samples", "unit"} for field in stats),
                ", ".join(sorted(stats)))

    # Live: catalogue search.
    listing = smoke.rpc("message/send", {"message": smoke.user_message('find NDBC stations with the word "Diamond" in the name')}).get("result", {})
    listing_data = artifact_data(listing)
    smoke.check("buoys-list completes", listing.get("status", {}).get("state") == "completed")
    smoke.check("the search found the station", int(listing_data.get("count", 0)) >= 1,
                str(listing_data.get("count")))
    smoke.check("rows carry coordinates and reporting state",
                all(row.get("latitude") is not None and "reporting" in row
                    for row in listing_data.get("stations") or []))

    # A station that is not in the catalogue must be refused, not guessed.
    unknown = smoke.rpc("message/send", {"message": smoke.user_message("conditions at buoy 99999")}).get("result", {})
    smoke.check("unknown station is refused", "will not guess" in task_text(unknown))

    # input-required -> follow-up for a station.
    first = smoke.rpc("message/send", {"message": smoke.user_message("how are the conditions out there?")}).get("result", {})
    smoke.check("buoy-conditions without a station -> input-required",
                first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(STATION, taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the station",
                follow.get("status", {}).get("state") == "completed"
                and follow_data.get("station") == STATION)

    # Live: a watch, on a field this station is actually publishing today. Buoy sensors drop out
    # (a row reads MM), so a hard-coded wave watch is a coin flip; the conditions above already
    # came from the same feed, so watch whichever of these fields has a reading.
    watch_field, watch_message = next(
        ((field, message) for field, message in WATCH_FIELDS if observation.get(field) is not None),
        (None, None),
    )
    smoke.check(f"station {STATION} is publishing a field a watch can use",
                watch_field is not None, str(observation))
    watch = (smoke.rpc("message/send", {"message": smoke.user_message(watch_message or "watch 41010")})
             .get("result", {}))
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("buoy-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records the station, field, threshold and crossing state",
                watch_meta.get("station") == STATION and watch_meta.get("field") == watch_field
                and set(watch_meta.get("observed") or {}) == {"above"},
                str(watch_meta.get("observed")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message(f"conditions at {STATION}")})
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
