#!/usr/bin/env python3
"""End-to-end smoke test for the aviation weather A2A server.

    AVIATION_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/aviation/server.py --port 8802
    python3 servers/aviation/tools/smoke.py --base http://127.0.0.1:8802

Live network: reads the Aviation Weather Center's METAR and TAF services. A station with no
recent observation answers 204 with an empty body, so the checks assert shape and honesty rather
than count > 0 everywhere.
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

DATASET = "aviationweather.gov/api/data"
CATEGORIES = {"VFR", "MVFR", "IFR", "LIFR"}


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def task_text(response: dict) -> str:
    status = response.get("status") or {}
    return ((status.get("message") or {}).get("parts") or [{}])[0].get("text", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the aviation weather A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8802")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "Aviation Weather Agent",
        ["metar", "taf", "aviation-nearby", "aviation-watch"],
    ):
        return 1

    # Live: the current observation.
    metar = smoke.rpc("message/send", {
        "message": smoke.user_message("what is the weather at KDEN right now?")}).get("result", {})
    metar_data = artifact_data(metar)
    report = (metar_data.get("reports") or [{}])[0]
    row = report.get("observation") or {}
    smoke.check("metar completes", metar.get("status", {}).get("state") == "completed")
    smoke.check("answer cites the dataset", metar_data.get("dataset") == DATASET)
    smoke.check("the station resolved to ICAO", report.get("station") == "KDEN"
                and report.get("city") == "Denver", str(report.get("station")))
    smoke.check("a flight category came back", row.get("flight_category") in CATEGORIES,
                str(row.get("flight_category")))
    smoke.check("the raw report is kept", bool(row.get("raw")) and "METAR KDEN" in str(row.get("raw")))
    smoke.check("wind and cloud layers are decoded",
                bool((row.get("wind") or {}).get("phrase")) and isinstance(row.get("clouds"), list),
                str((row.get("wind") or {}).get("phrase")))
    smoke.check("the report time is ISO UTC", str(row.get("report_time") or "").endswith("Z"),
                str(row.get("report_time")))
    smoke.check("the answer says which airfield it describes",
                "not the whole region around it" in task_text(metar))

    # Live: the terminal forecast.
    taf = smoke.rpc("message/send", {"message": smoke.user_message("what is the TAF for KDEN?")}).get("result", {})
    taf_data = artifact_data(taf)
    forecast = (taf_data.get("forecasts") or [{}])[0].get("forecast") or {}
    periods = forecast.get("periods") or []
    smoke.check("taf completes", taf.get("status", {}).get("state") == "completed")
    smoke.check("the forecast carries periods", bool(periods), str(len(periods)))
    smoke.check("every period has a window and a sky description",
                all(period.get("from") and period.get("to") and "clouds_text" in period
                    for period in periods))
    smoke.check("the raw TAF is kept", bool(forecast.get("raw")))

    # Live: stations near a place, one bounding-box request.
    nearby = smoke.rpc("message/send", {
        "message": smoke.user_message("which airports are reporting near Denver?")}).get("result", {})
    nearby_data = artifact_data(nearby)
    stations = nearby_data.get("stations") or []
    miles = [station.get("miles") or 0 for station in stations]
    smoke.check("aviation-nearby completes", nearby.get("status", {}).get("state") == "completed")
    smoke.check("nearby found reporting stations", bool(stations), str(nearby_data.get("found")))
    smoke.check("nearest first", miles == sorted(miles), str(miles[:3]))
    smoke.check("one bbox request", "," in str(nearby_data.get("bbox")), str(nearby_data.get("bbox")))
    smoke.check("every station carries a distance and a category",
                all(station.get("miles") is not None and station.get("flight_category") in CATEGORIES
                    for station in stations))
    smoke.check("the answer is explicit that distances are computed here",
                "straight-line miles computed here" in task_text(nearby))

    # A station the service does not know answers 204: that must read as no report, not an error.
    unknown = smoke.rpc("message/send", {
        "message": smoke.user_message("what is the weather at ZZZZ?")}).get("result", {})
    smoke.check("unknown station is completed, not an error",
                unknown.get("status", {}).get("state") == "completed")
    smoke.check("unknown station is stated honestly",
                "No observation is published for ZZZZ" in task_text(unknown)
                and artifact_data(unknown).get("count") == 0)

    # input-required -> follow-up with a code.
    ask = {"kind": "message", "role": "user", "messageId": "m",
           "parts": [{"kind": "data", "data": {"skill": "metar"}}]}
    first = smoke.rpc("message/send", {"message": ask}).get("result", {})
    smoke.check("metar without a station -> input-required",
                first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("KJFK", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    smoke.check("follow-up answers for the station",
                follow.get("status", {}).get("state") == "completed"
                and artifact_data(follow).get("stations") == ["KJFK"])

    # Live: a watch on the flight category.
    watch = smoke.rpc("message/send", {
        "message": smoke.user_message("tell me when KDEN goes IFR")}).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    observed = (watch_meta.get("observed") or {}).get("stations") or {}
    smoke.check("aviation-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("the watch records the station and its category",
                watch_meta.get("station") == "KDEN"
                and (observed.get("KDEN") or {}).get("flight_category") in CATEGORIES,
                str((observed.get("KDEN") or {}).get("flight_category")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("what is the weather at KBJC?")})
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
