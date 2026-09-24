#!/usr/bin/env python3
"""End-to-end smoke test for the air-quality A2A server.

    AIR_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/air/server.py --port 8797
    python3 servers/air/tools/smoke.py --base http://127.0.0.1:8797
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

DATASET = "open-meteo.com/air-quality-api"
BANDS = ("Good", "Moderate", "Unhealthy for Sensitive Groups", "Unhealthy", "Very Unhealthy",
         "Hazardous")


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def task_text(response: dict) -> str:
    status = response.get("status") or {}
    return ((status.get("message") or {}).get("parts") or [{}])[0].get("text", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the air-quality A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8797")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "Air Quality Agent",
        ["air-now", "air-forecast", "air-ranking", "air-watch"],
    ):
        return 1

    # Live: current air quality in Delhi, straight from the model.
    now = smoke.rpc("message/send", {"message": smoke.user_message("how bad is the air in Delhi right now?")}).get("result", {})
    data = artifact_data(now)
    smoke.check("air-now completes", now.get("status", {}).get("state") == "completed")
    smoke.check("answer cites the dataset", data.get("dataset") == DATASET)
    smoke.check("an AQI value came back", isinstance(data.get("aqi"), (int, float)), str(data.get("aqi")))
    smoke.check("the EPA band is named", data.get("category") in BANDS, str(data.get("category")))
    smoke.check("pollutant units are reported", bool(data.get("units", {}).get("pm2_5")))
    smoke.check("the message names the band", str(data.get("category")) in task_text(now))

    # Live: an hourly outlook for a real point.
    forecast = smoke.rpc("message/send", {"message": smoke.user_message("what is the air forecast for the next 6 hours at 39.74,-104.99?")}).get("result", {})
    forecast_data = artifact_data(forecast)
    smoke.check("air-forecast completes", forecast.get("status", {}).get("state") == "completed")
    smoke.check("six hours were asked for and returned",
                forecast_data.get("hours") == 6 and len(forecast_data.get("series") or []) == 6,
                f"hours={forecast_data.get('hours')} rows={len(forecast_data.get('series') or [])}")
    smoke.check("the peak hour is reported with a band",
                (forecast_data.get("peak") or {}).get("category") in BANDS,
                str((forecast_data.get("peak") or {}).get("time")))

    # Live: one request, several places, worst first.
    ranking = smoke.rpc("message/send", {"message": smoke.user_message("which of Denver, Phoenix and Los Angeles has the worst air?")}).get("result", {})
    ranking_data = artifact_data(ranking)
    rows = ranking_data.get("places") or []
    values = [row.get("aqi") for row in rows if row.get("aqi") is not None]
    smoke.check("air-ranking completes", ranking.get("status", {}).get("state") == "completed")
    smoke.check("all three places came back", len(rows) == 3, str(len(rows)))
    smoke.check("ranked worst first", values == sorted(values, reverse=True), str(values))

    # An unknown place must be refused, not guessed.
    unknown = smoke.rpc("message/send", {"message": smoke.user_message("how is the air in Atlantis?")}).get("result", {})
    unknown_data = artifact_data(unknown)
    smoke.check("unknown place is refused", "will not guess" in task_text(unknown))
    smoke.check("unknown place is flagged in the artifact", unknown_data.get("known") is False)

    # input-required -> follow-up for a place.
    first = smoke.rpc("message/send", {"message": smoke.user_message("how is the air near me?")}).get("result", {})
    smoke.check("air-now without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("Denver", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the place",
                follow.get("status", {}).get("state") == "completed" and follow_data.get("place") == "Denver")

    # Live: a watch on a real threshold.
    watch = smoke.rpc("message/send", {"message": smoke.user_message("tell me when the air in Denver passes 100")}).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("air-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records place, threshold and the reduced observation",
                watch_meta.get("threshold") == 100.0
                and set(watch_meta.get("observed", {})) == {"band", "above"},
                str(watch_meta.get("observed")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("what is the air quality in Denver?")})
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
