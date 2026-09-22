"""Server-initiated A2A push notifications.

Cities rarely publish webhooks for their own datasets, so the watcher polls the
dataset on a timer (the A2A spec lets the server choose when to notify) and POSTs
a TaskStatusUpdateEvent to every push-notification config on the task when the
watched value changes. The config token rides in X-A2A-Notification-Token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid

from .errors import SkillAgent, UpstreamError, agent_message
from .socrata import utc_now_iso
from .store import TaskStore

log = logging.getLogger("a2a.watch")


class PushWatcher(threading.Thread):
    def __init__(self, store: TaskStore, agent: SkillAgent, interval: float | None = None, http_post=None) -> None:
        super().__init__(name="a2a-push-watcher", daemon=True)
        raw = interval if interval is not None else float(os.environ.get(f"{agent.env_prefix}_WATCH_INTERVAL", "300"))
        self.interval = max(5.0, float(raw))
        self.store = store
        self.agent = agent
        self._post = http_post or self._http_post
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a watcher crash must never take the server down
                log.exception("push watcher tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> int:
        """One pass over all watched tasks. Returns the number of notifications sent."""
        sent = 0
        for task in self.store.tasks_with_push_configs():
            watch = (task.get("metadata") or {}).get("watch")
            if not watch or watch.get("kind") not in self.agent.watch_kinds:
                continue
            try:
                observed = self.agent.probe_watch(watch)
            except UpstreamError as exc:
                log.warning("watch lookup failed: %s", exc)
                continue
            if observed is None:
                continue
            previous = task["metadata"].get("last_observed")
            if observed == previous:
                continue
            task["metadata"]["last_observed"] = observed
            self.store.save_task(task)
            event = self._event(task, watch, previous, observed)
            for config in self.store.list_push_configs(task["id"]):
                if self._notify(config, event):
                    sent += 1
        return sent

    def _event(self, task: dict, watch: dict, previous, observed) -> dict:
        text = self.agent.describe_watch_change(watch, previous, observed)
        return {
            "kind": "status-update",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "status": {"state": "working", "timestamp": utc_now_iso(), "message": agent_message(text)},
            "final": False,
            "metadata": {
                "watch": watch,
                "previous": previous,
                "observed": observed,
                "summary": text,
            },
        }

    def _notify(self, config: dict, event: dict) -> bool:
        headers = {"Content-Type": "application/json"}
        if config.get("token"):
            headers["X-A2A-Notification-Token"] = config["token"]
        payload = json.dumps(event).encode("utf-8")
        for attempt in range(1, 4):
            try:
                status = self._post(config["url"], payload, headers)
                if 200 <= status < 300:
                    log.info("push notification sent to %s", config["url"])
                    return True
                log.warning("webhook %s answered %s", config["url"], status)
            except Exception as exc:
                log.warning("webhook %s attempt %d failed: %s", config["url"], attempt, exc)
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
        return False

    @staticmethod
    def _http_post(url: str, payload: bytes, headers: dict) -> int:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status


__all__ = ["PushWatcher"]
