#!/usr/bin/env python3
"""End-to-end smoke test for the NYC drinking water A2A server.

    NYC_WATER_ALLOW_PRIVATE_WEBHOOKS=1 python3 servers/nycwater/server.py --port 8789
    python3 servers/nycwater/tools/smoke.py --base http://127.0.0.1:8789
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
    parser = argparse.ArgumentParser(description="smoke-test the NYC drinking water A2A server")
    parser.add_argument("--base", default="http://127.0.0.1:8789")
    parser.add_argument("--hook", default="http://127.0.0.1:8799/hooks")
    parser.add_argument("--site", default="55450")
    args = parser.parse_args(argv)

    smoke = Smoke(args.base)
    if not smoke.card_checks("NYC Drinking Water Agent", ["water-quality", "water-sites", "water-watch"]):
        return 1

    # Live: monitoring site codes.
    sites = smoke.rpc("message/send", {"message": smoke.user_message("which water monitoring sites report the most?")}).get("result", {})
    site_data = ((sites.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    smoke.check("sites skill completes", sites.get("status", {}).get("state") == "completed")
    smoke.check("real site codes returned", site_data.get("count", 0) >= 1, str(site_data.get("count")))
    smoke.check("site artifact cites dataset + freshness", site_data.get("dataset") == "bkwf-xfky" and bool(site_data.get("freshness")))

    # Live: sample chemistry for one site.
    quality = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(f"how is the water testing at site {args.site} in the last 365 days?")},
    ).get("result", {})
    quality_data = ((quality.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    summary = quality_data.get("summary", {})
    smoke.check("quality skill completes", quality.get("status", {}).get("state") == "completed")
    smoke.check("site code parsed from text", quality_data.get("site") == args.site, str(quality_data.get("site")))
    smoke.check("samples found for the site", summary.get("samples", 0) >= 1, str(summary.get("samples")))
    smoke.check("chemistry summarized", summary.get("chlorine_mg_l", {}).get("avg") is not None)
    smoke.check("detections counted", "e_coli_detections" in summary and "coliform_detections" in summary)

    # input-required -> follow-up for a watch with no site.
    first = smoke.rpc("message/send", {"message": smoke.user_message("watch the water")}).get("result", {})
    smoke.check("watch without a site -> input-required", first.get("status", {}).get("state") == "input-required")
    follow = smoke.rpc(
        "message/send",
        {"message": smoke.user_message(f"site {args.site}", taskId=first.get("id"), contextId=first.get("contextId"))},
    ).get("result", {})
    watch_data = ((follow.get("artifacts") or [{}])[-1].get("parts") or [{}])[0].get("data", {})
    smoke.check(
        "follow-up starts the watch",
        follow.get("status", {}).get("state") == "completed" and watch_data.get("site") == args.site,
    )

    # Streaming.
    events = smoke.stream("message/stream", {"message": smoke.user_message(f"water quality at {args.site}")})
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
