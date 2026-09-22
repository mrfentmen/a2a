"""One CLI for every A2A server in this repo.

    python3 servers/nyc311/server.py --port 8787 --public-url https://example.org
"""

from __future__ import annotations

import argparse
import logging
import os

from .errors import SkillAgent
from .httpd import PROTOCOL_VERSION, AgentServerApp, serve

log = logging.getLogger("a2a.cli")


def run_agent_server(agent: SkillAgent, version: str, db_path: str, default_port: int,
                     protocol_version: str = PROTOCOL_VERSION, argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{agent.card_name} — A2A {protocol_version} server")
    parser.add_argument("--host", default=os.environ.get("A2A_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("A2A_PORT", str(default_port))))
    parser.add_argument("--public-url", default=None, help="URL clients use to reach this server (embedded in the card)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("A2A_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    public_url = args.public_url or os.environ.get("A2A_PUBLIC_URL") or f"http://{args.host}:{args.port}"
    if args.host == "0.0.0.0":
        log.warning("listening on 0.0.0.0; set A2A_PUBLIC_URL so the card advertises the real host")

    app = AgentServerApp(agent, public_url, version, db_path, protocol_version)
    app.start()
    serve(app, args.host, args.port)
    return 0


__all__ = ["run_agent_server"]
