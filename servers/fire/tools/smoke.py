#!/usr/bin/env python3
"""End-to-end smoke test for the wildfire A2A server.

    FIRE_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/fire/server.py --port 8795
    python3 servers/fire/tools/smoke.py --base http://127.0.0.1:8795
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

DATASET = "nifc.gov/wfigs/incident-locations-current"


def artifact_data(response: dict) -> dict:
    return ((response.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test the wildfire A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8795")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks(
        "Wildfire Incident Agent",
        ["fire-active", "fire-near", "fire-summary", "fire-lookup", "fire-watch"],
    ):
        return 1

    # Live: national totals straight off the interagency layer.
    summary = smoke.rpc("message/send", {"message": smoke.user_message("how much fire is burning in the country right now?")}).get("result", {})
    summary_data = artifact_data(summary)
    smoke.check("fire-summary completes", summary.get("status", {}).get("state") == "completed")
    smoke.check("summary cites the layer", summary_data.get("dataset") == DATASET)
    smoke.check("live incident count returned", int(summary_data.get("count", 0)) >= 1, str(summary_data.get("count")))
    smoke.check("acres and uncontained totals are present",
                summary_data.get("acres") is not None and summary_data.get("uncontained") is not None)
    smoke.check("states are ranked", bool(summary_data.get("by_state")))

    # Live: active fires in a state, largest first.
    california = smoke.rpc("message/send", {"message": smoke.user_message("what wildfires are burning in California right now?")}).get("result", {})
    california_data = artifact_data(california)
    smoke.check("fire-active completes", california.get("status", {}).get("state") == "completed")
    smoke.check("state filter is applied", "US-CA" in str(california_data.get("scope")))
    rows = california_data.get("incidents") or []
    smoke.check("incidents carry size and coordinates",
                all(row.get("acres") is not None and row.get("latitude") is not None for row in rows))
    sizes = [row.get("acres") or 0 for row in rows]
    smoke.check("largest first", sizes == sorted(sizes, reverse=True), str(sizes[:3]))

    # Live: fires near a real point (Denver), with computed distances.
    near = smoke.rpc("message/send", {"message": smoke.user_message("any fires within 200 miles of Denver?")}).get("result", {})
    near_data = artifact_data(near)
    smoke.check("fire-near completes", near.get("status", {}).get("state") == "completed")
    distances = [row.get("distance_miles") for row in (near_data.get("incidents") or [])]
    smoke.check("distances are computed and sorted",
                all(distance is not None for distance in distances) and distances == sorted(distances),
                str(distances[:3]))
    smoke.check("near answer names the place", near_data.get("place") == "Denver")

    # Live: lookup by the widest-known incident name is not knowable ahead of time, so search
    # for a fixed word and accept either a match or the honest empty answer.
    lookup = smoke.rpc("message/send", {"message": smoke.user_message("tell me about the fire called Creek")}).get("result", {})
    lookup_data = artifact_data(lookup)
    smoke.check("fire-lookup completes", lookup.get("status", {}).get("state") == "completed")
    smoke.check("lookup reports a count and its dataset",
                lookup_data.get("dataset") == DATASET and isinstance(lookup_data.get("count"), int),
                f"{lookup_data.get('count')} match(es)")

    # input-required -> follow-up for a place.
    first = smoke.rpc("message/send", {"message": smoke.user_message("are there any fires near me?")}).get("result", {})
    smoke.check("fire-near without a place -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message("Missoula", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    follow_data = artifact_data(follow)
    smoke.check("follow-up answers for the place",
                follow.get("status", {}).get("state") == "completed" and follow_data.get("place") == "Missoula")

    # Live: a watch on a real state threshold.
    watch = smoke.rpc("message/send", {"message": smoke.user_message("tell me when a new large fire starts in Oregon")}).get("result", {})
    watch_meta = watch.get("metadata", {}).get("watch", {})
    smoke.check("fire-watch completes", watch.get("status", {}).get("state") == "completed")
    smoke.check("watch records state, threshold and the id set",
                watch_meta.get("state") == "OR" and watch_meta.get("min_acres") == 1000.0
                and isinstance(watch_meta.get("observed", {}).get("ids"), list),
                str(watch_meta.get("observed", {}).get("count")))

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message("how many wildfires are active nationwide?")})
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
