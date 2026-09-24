#!/usr/bin/env python3
"""End-to-end smoke test for the volcano alert-level A2A server.

    VOLCANO_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/volcanoes/server.py --port 8798
    python3 servers/volcanoes/tools/smoke.py --base http://127.0.0.1:8798

The USGS VSC API is slow (12-76s per read on 2026-09-23), so this script gives live calls a
long client timeout instead of the shared 45s default. The server caches for 15 minutes, so
only the first call in each scope actually waits.
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

DATASET = "usgs.gov/vsc/volcano-alert-levels"
ELEVATED = ("ADVISORY", "WATCH", "WARNING")
LIVE_TIMEOUT = 240.0


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def task_text(response: dict) -> str:
    status = response.get("status") or {}
    return ((status.get("message") or {}).get("parts") or [{}])[0].get("text", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the volcano A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8798")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "Volcano Alert-Level Agent",
        ["volcano-alerts", "volcano-list", "volcano-lookup", "volcano-watch"],
        timeout=LIVE_TIMEOUT,
    ):
        return 1

    # Live: whatever is above NORMAL right now (may legitimately be empty).
    alerts = smoke.rpc("message/send", {"message": smoke.user_message("which volcanoes are elevated right now?")},
                       timeout=LIVE_TIMEOUT).get("result", {})
    alerts_data = artifact_data(alerts)
    levels = [row.get("level") for row in alerts_data.get("volcanoes") or []]
    smoke.check("volcano-alerts completes", alerts.get("status", {}).get("state") == "completed")
    smoke.check("answer cites the dataset", alerts_data.get("dataset") == DATASET)
    smoke.check("every listed volcano is really elevated",
                all(level in ELEVATED for level in levels), str(levels))
    smoke.check("elevated rows carry a notice synopsis and coordinates",
                all(row.get("synopsis") and row.get("latitude") is not None
                    for row in alerts_data.get("volcanoes") or []))
    smoke.check("the message names a level and a colour code",
                "/" in task_text(alerts) and DATASET in task_text(alerts))

    # Live: the full monitored list for one region.
    listing = smoke.rpc("message/send", {"message": smoke.user_message("what volcanoes does USGS monitor in Hawaii?")},
                        timeout=LIVE_TIMEOUT).get("result", {})
    listing_data = artifact_data(listing)
    smoke.check("volcano-list completes", listing.get("status", {}).get("state") == "completed")
    smoke.check("the region filter found volcanoes", int(listing_data.get("count", 0)) >= 5,
                str(listing_data.get("count")))
    smoke.check("levels are counted", bool(listing_data.get("by_level")))
    smoke.check("the live region list is reported", "Hawaii" in (listing_data.get("regions_available") or []))

    # Live: lookup by name, with the elevation notice joined in.
    lookup = smoke.rpc("message/send", {"message": smoke.user_message("what is Kilauea doing?")},
                       timeout=LIVE_TIMEOUT).get("result", {})
    lookup_data = artifact_data(lookup)
    row = (lookup_data.get("volcanoes") or [{}])[0]
    smoke.check("volcano-lookup completes", lookup.get("status", {}).get("state") == "completed")
    smoke.check("the name matched", lookup_data.get("count", 0) >= 1 and row.get("name") == "Kilauea")
    smoke.check("geojson coordinates are lat/lon, not lon/lat",
                isinstance(row.get("latitude"), (int, float)) and 18 <= row["latitude"] <= 23
                and -161 <= (row.get("longitude") or 0) <= -154,
                f"{row.get('latitude')},{row.get('longitude')}")
    smoke.check("the observatory is named", bool(row.get("observatory_name")), str(row.get("observatory_name")))

    # An unknown name must say what is monitored instead of inventing a volcano.
    unknown = smoke.rpc("message/send", {"message": smoke.user_message("tell me about the Atlantis volcano")},
                        timeout=LIVE_TIMEOUT).get("result", {})
    smoke.check("unknown volcano is refused",
                "No monitored volcano's name contains" in task_text(unknown))

    # input-required -> follow-up for a name.
    first = smoke.rpc("message/send", {"message": smoke.user_message("give me the details on it")},
                      timeout=LIVE_TIMEOUT).get("result", {})
    smoke.check("lookup without a name -> input-required",
                first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("Kilauea", taskId=first.get("id"), contextId=first.get("contextId"))},
        timeout=LIVE_TIMEOUT,
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the name",
                follow.get("status", {}).get("state") == "completed"
                and follow_data.get("query") == "Kilauea")

    # Live: a watch on one volcano, which compares alert levels per volcano.
    watch = smoke.rpc("message/send", {"message": smoke.user_message("tell me when Kilauea changes alert level")},
                      timeout=LIVE_TIMEOUT).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("volcano-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records the scope and the current levels",
                watch_meta.get("name") == "Kilauea"
                and list((watch_meta.get("observed") or {}).get("levels", {})) == ["Kilauea"],
                str(watch_meta.get("observed")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("which volcanoes are elevated?")},
                          timeout=LIVE_TIMEOUT)
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
