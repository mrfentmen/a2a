"""SQLite-backed A2A task store and push-notification-config store.

Tasks are stored as JSON documents (the A2A Task object shape) with a few
columns lifted out for cheap queries. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}
INTERRUPTED_STATES = {"input-required", "auth-required"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_configs (
    task_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    url TEXT NOT NULL,
    token TEXT,
    json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, config_id)
);
CREATE INDEX IF NOT EXISTS idx_push_configs_task ON push_configs(task_id);
"""


class TaskStore:
    """Task + push-config persistence. Safe to share across threads."""

    def __init__(self, db_path: str | None = None, env_var: str = "A2A_DB",
                 default_path: str | None = None) -> None:
        path = db_path or os.environ.get(env_var) or default_path or ":memory:"
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- tasks -------------------------------------------------------------

    def save_task(self, task: dict) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, context_id, state, updated_at, json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET context_id=excluded.context_id, state=excluded.state, "
                "updated_at=excluded.updated_at, json=excluded.json",
                (task["id"], task["contextId"], task["status"]["state"], now, json.dumps(task)),
            )

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_tasks(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        return int(row["n"])

    def tasks_with_push_configs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT t.json AS json FROM tasks t JOIN push_configs p ON p.task_id = t.id"
            ).fetchall()
        return [json.loads(row["json"]) for row in rows]

    # -- push configs ------------------------------------------------------

    def set_push_config(self, task_id: str, config: dict) -> dict:
        config_id = config["id"]
        with self._lock:
            self._conn.execute(
                "INSERT INTO push_configs (task_id, config_id, url, token, json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id, config_id) DO UPDATE SET url=excluded.url, "
                "token=excluded.token, json=excluded.json",
                (task_id, config_id, config["url"], config.get("token"), json.dumps(config), time.time()),
            )
        return config

    def get_push_config(self, task_id: str, config_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM push_configs WHERE task_id = ? AND config_id = ?", (task_id, config_id)
            ).fetchone()
        return json.loads(row["json"]) if row else None

    def list_push_configs(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM push_configs WHERE task_id = ? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [json.loads(row["json"]) for row in rows]

    def delete_push_config(self, task_id: str, config_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM push_configs WHERE task_id = ? AND config_id = ?", (task_id, config_id)
            )
        return cur.rowcount > 0

    def count_push_configs(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM push_configs").fetchone()
        return int(row["n"])
