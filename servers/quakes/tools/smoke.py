#!/usr/bin/env python3
"""End-to-end smoke test for the USGS earthquake A2A server.

    USGS_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/quakes/server.py --port 8792
    python3 servers/quakes/tools/smoke.py --base http://127.0.0.1:8792
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

DATASET = "earthquake.usgs.gov/fdsnws/event/1"
TOKYO = "35.68,139.69"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the USGS earthquake A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8792")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "USGS Earthquake Agent", ["quakes-recent", "quakes-near", "quakes-summary", "quakes-watch"]
    ):
        return 1

    # Live: recent quakes (M2.5+ worldwide is never empty).
    recent = smoke.rpc(
        "message/send", {"message": smoke.user_message("any earthquakes above magnitude 2.5 in the last 24 hours?")}
    ).get("result", {})
    recent_data = artifact_data(recent)
    smoke.check("recent skill completes", recent.get("status", {}).get("state") == "completed")
    smoke.check("live quakes returned", recent_data.get("count", 0) >= 1, str(recent_data.get("count")))
    smoke.check(
        "recent artifact cites dataset + freshness",
        recent_data.get("dataset") == DATASET and bool(recent_data.get("freshness")),
    )

    # Live: quakes near a real seismic region.
    near = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(f"any quakes within 300 km of {TOKYO} in the last 30 days?")},
    ).get("result", {})
    near_data = artifact_data(near)
    smoke.check("near skill completes", near.get("status", {}).get("state") == "completed")
    smoke.check("radius parsed from text", near_data.get("radius_km") == 300.0, str(near_data.get("radius_km")))
    smoke.check("point echoed in artifact", near_data.get("point") == TOKYO, str(near_data.get("point")))
    smoke.check("live quakes near Tokyo", near_data.get("count", 0) >= 1, str(near_data.get("count")))

    # Live: catalog counts from the count endpoint.
    summary = smoke.rpc(
        "message/send", {"message": smoke.user_message("how many earthquakes today?")}
    ).get("result", {})
    summary_data = artifact_data(summary)
    counts = summary_data.get("counts", {})
    smoke.check("summary skill completes", summary.get("status", {}).get("state") == "completed")
    smoke.check("24h M2.5+ count is non-zero", counts.get("last_24h_m2.5", 0) >= 1, str(counts.get("last_24h_m2.5")))
    smoke.check("counts include all three bands", set(counts) == {"last_24h_m2.5", "last_24h_m4.5", "last_7d_m6.0"})

    # input-required -> follow-up for a watch with no place and no threshold.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch for earthquakes")}).get("result", {})
    smoke.check("watch without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("magnitude 5.5", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = artifact_data(follow)
    smoke.check(
        "follow-up starts the global watch",
        follow.get("status", {}).get("state") == "completed" and watch_data.get("min_magnitude") == 5.5,
    )

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("how many earthquakes today?")})
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
