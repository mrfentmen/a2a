#!/usr/bin/env python3
"""Boot every A2A server, smoke-test each one over real HTTP, then shut them down.

    python3 tools/smoke_all.py

Each server gets its own port, its own SQLite file (in a temp dir, so a run never
touches committed data) and `<PREFIX>_ALLOW_PRIVATE_WEBHOOKS=1` so the push-config
checks can register a loopback webhook. Exits non-zero if any server fails.

Live network: every check reads the real upstream API (NYC Open Data, NWS, USGS, NOAA
SWPC, NIFC, openFDA).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import error, request

REPO_ROOT = Path(__file__).resolve().parents[1]

#: name, port, env prefix, USER_AGENT variable to set (None when the feed does not ask)
SERVERS = (
    ("nyc311", 8787, "NYC311", None),
    ("nycflood", 8788, "NYC_FLOOD", None),
    ("nycwater", 8789, "NYC_WATER", None),
    ("nws", 8791, "NWS", "NWS_USER_AGENT"),
    ("quakes", 8792, "USGS", "USGS_USER_AGENT"),
    ("tides", 8793, "NOAA_TIDES", "NOAA_TIDES_USER_AGENT"),
    ("aurora", 8794, "AURORA", "AURORA_USER_AGENT"),
    ("fire", 8795, "FIRE", "FIRE_USER_AGENT"),
    ("recalls", 8796, "OPENFDA", "OPENFDA_USER_AGENT"),
)

USER_AGENT = "a2a-smoke-all/1.0 (+https://github.com/mrfentmen/a2a)"


def wait_for_card(base: str, timeout: float = 25.0) -> bool:
    """Poll the agent card until the server answers or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with request.urlopen(f"{base}/.well-known/agent-card.json", timeout=5) as resp:
                return bool(json.loads(resp.read().decode()).get("name"))
        except (error.URLError, OSError, ValueError):
            time.sleep(0.4)
    return False


def start(name: str, port: int, prefix: str, agent_var: str | None, db_dir: Path,
          log_dir: Path) -> tuple[subprocess.Popen, object]:
    env = dict(os.environ)
    env[f"{prefix}_ALLOW_PRIVATE_WEBHOOKS"] = "1"
    env[f"{prefix}_DB"] = str(db_dir / f"{name}.db")
    if agent_var:
        env[agent_var] = USER_AGENT
    log_path = log_dir / f"{name}.log"
    handle = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "servers" / name / "server.py"), "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="smoke-test every A2A server")
    parser.add_argument("--keep-logs", action="store_true", help="leave server logs in a temp dir")
    args = parser.parse_args(argv)

    results: list[tuple[str, int]] = []
    with tempfile.TemporaryDirectory(prefix="a2a-smoke-") as work:
        work_dir = Path(work)
        started: list[tuple[str, subprocess.Popen, object]] = []
        try:
            for name, port, prefix, agent_var in SERVERS:
                process, handle = start(name, port, prefix, agent_var, work_dir, work_dir)
                started.append((name, process, handle))
                base = f"http://127.0.0.1:{port}"
                if not wait_for_card(base):
                    print(f"=== {name}: server did not answer on {base} (see {work_dir / (name + '.log')})")
                    results.append((name, 1))
                    continue
                print(f"\n=== {name} ({base})")
                smoke = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "servers" / name / "tools" / "smoke.py"), "--base", base],
                    cwd=str(REPO_ROOT),
                )
                results.append((name, smoke.returncode))
        finally:
            for name, process, handle in started:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                handle.close()
        if args.keep_logs:
            kept = Path(tempfile.mkdtemp(prefix="a2a-smoke-logs-"))
            for name, _, _ in started:
                source = work_dir / f"{name}.log"
                if source.exists():
                    (kept / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"\nserver logs kept in {kept}")

    print("\n=== summary")
    for name, code in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    failures = sum(1 for _, code in results if code)
    print(f"\nSMOKE ALL {'PASS' if not failures else 'FAIL'} — {len(results) - failures}/{len(results)} servers")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
