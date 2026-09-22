#!/usr/bin/env python3
"""End-to-end smoke test for the aurora space-weather A2A server.

    AURORA_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/aurora/server.py --port 8794
    python3 servers/aurora/tools/smoke.py --base http://127.0.0.1:8794
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

DATASET_KP_1M = "swpc.noaa.gov/planetary-k-index-1m"
DATASET_FORECAST = "swpc.noaa.gov/planetary-k-index-forecast"
DATASET_OVATION = "swpc.noaa.gov/ovation-aurora"
DATASET_ALERTS = "swpc.noaa.gov/alerts"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the aurora A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8794")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "Aurora Space Weather Agent",
        ["aurora-now", "aurora-forecast", "aurora-visibility", "aurora-messages", "aurora-watch"],
    ):
        return 1

    # Live: current geomagnetic conditions (the 1-minute Kp product).
    now = smoke.rpc("message/send", {"message": smoke.user_message("how are the geomagnetic conditions right now?")}).get("result", {})
    now_data = artifact_data(now)
    smoke.check("aurora-now completes", now.get("status", {}).get("state") == "completed")
    smoke.check("live Kp returned", isinstance(now_data.get("estimated_kp"), (int, float)), str(now_data.get("estimated_kp")))
    smoke.check("now cites the 1-minute dataset + freshness",
                now_data.get("dataset") == DATASET_KP_1M and bool(now_data.get("freshness")))
    smoke.check("24-hour peak included", now_data.get("peak_kp_24h") is not None)

    # Live: NOAA's predicted Kp for the coming days.
    forecast = smoke.rpc("message/send", {"message": smoke.user_message("what is the aurora forecast for the next 3 days?")}).get("result", {})
    forecast_data = artifact_data(forecast)
    smoke.check("aurora-forecast completes", forecast.get("status", {}).get("state") == "completed")
    smoke.check("forecast cites its dataset", forecast_data.get("dataset") == DATASET_FORECAST)
    smoke.check("forecast has at least one future day", bool(forecast_data.get("days")))
    smoke.check("each day carries a storm flag",
                all("storm" in day for day in forecast_data.get("days", [])))

    # Live: the OVATION model at a real point (Tromso, inside the auroral oval).
    visibility = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("what are the odds of seeing the aurora in Tromso?")},
    ).get("result", {})
    visibility_data = artifact_data(visibility)
    smoke.check("aurora-visibility completes", visibility.get("status", {}).get("state") == "completed")
    smoke.check("visibility cites OVATION", visibility_data.get("dataset") == DATASET_OVATION)
    smoke.check("probability is a percentage",
                0 <= int(visibility_data.get("probability_percent", -1)) <= 100,
                str(visibility_data.get("probability_percent")))
    smoke.check("model run times are reported",
                bool(visibility_data.get("model_observation_time")) and bool(visibility_data.get("model_forecast_time")))

    # Live: SWPC messages, parsed into fields.
    messages = smoke.rpc("message/send", {"message": smoke.user_message("what has NOAA said about storms lately?")}).get("result", {})
    messages_data = artifact_data(messages)
    smoke.check("aurora-messages completes", messages.get("status", {}).get("state") == "completed")
    smoke.check("messages cite the alerts feed", messages_data.get("dataset") == DATASET_ALERTS)
    smoke.check("messages came back", int(messages_data.get("count", 0)) >= 1, str(messages_data.get("count")))
    smoke.check("messages are parsed into fields",
                all(item.get("code") and item.get("issue_datetime") for item in messages_data.get("messages", [])))

    # input-required -> follow-up for a place.
    first = smoke.rpc("message/send", {"message": smoke.user_message("what are the odds of seeing the aurora?")}).get("result", {})
    smoke.check("visibility without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("Fairbanks", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the place",
                follow.get("status", {}).get("state") == "completed" and follow_data.get("place") == "Fairbanks")

    # Live: a storm watch, registered and stored.
    watch = smoke.rpc("message/send", {"message": smoke.user_message("tell me when a geomagnetic storm starts")}).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("aurora-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records a threshold and an observation",
                float(watch_meta.get("threshold_kp", 0)) == 5.0 and "observed" in watch_meta,
                str(watch_meta.get("observed")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("how are the geomagnetic conditions?")})
    kinds = [event.get("kind") for event in events]
    smoke.check("stream starts with task + working status", kinds[:2] == ["task", "status-update"], str(kinds))
    smoke.check("stream includes artifact", "artifact-update" in kinds)
    smoke.check("stream ends final", bool(events[-1].get("final")) and events[-1].get("status", {}).get("state") == "completed")

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
