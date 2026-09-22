"""Run the NYC 311 A2A server.

    python3 servers/nyc311/server.py                 # http://127.0.0.1:8787
    A2A_PORT=9000 python3 servers/nyc311/server.py

The agent card is served at /.well-known/agent-card.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from a2a_kit import run_agent_server  # noqa: E402  (path setup must come first)

from agent import NYC311Agent  # noqa: E402

AGENT_VERSION = "1.0.0"
DEFAULT_PORT = 8787


def main(argv=None) -> int:
    return run_agent_server(
        NYC311Agent(),
        AGENT_VERSION,
        db_path=str(Path(__file__).resolve().parent / "data" / "tasks.db"),
        default_port=DEFAULT_PORT,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
