"""HTTP binding for the A2A kit: agent card, JSON-RPC endpoint, SSE streaming.

Routes:
  GET  /.well-known/agent-card.json   public agent card
  GET  /.well-known/agent.json        same card (legacy path, served for compat)
  GET  /healthz                       liveness + counters
  POST /                              JSON-RPC (message/send, message/stream, tasks/*)
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import A2AError, SkillAgent
from .protocol import A2AHandler
from .push import PushWatcher
from .store import TaskStore

PROTOCOL_VERSION = "0.3.0"
MAX_BODY = 1_000_000

log = logging.getLogger("a2a.http")


def build_agent_card(agent: SkillAgent, public_url: str, version: str,
                     protocol_version: str = PROTOCOL_VERSION) -> dict:
    base = public_url.rstrip("/")
    card = {
        "protocolVersion": protocol_version,
        "name": agent.card_name,
        "description": agent.card_description,
        "url": base + "/",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": base + "/", "transport": "JSONRPC"}],
        "version": version,
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": agent.card_skills,
    }
    org = os.environ.get("A2A_PROVIDER_ORG")
    provider_url = os.environ.get("A2A_PROVIDER_URL")
    if org and provider_url:
        card["provider"] = {"organization": org, "url": provider_url}
    return card


class AgentServerApp:
    """Owns the store, the protocol handler, the watcher and the card."""

    def __init__(self, agent: SkillAgent, public_url: str, version: str, db_path: str,
                 protocol_version: str = PROTOCOL_VERSION) -> None:
        self.agent = agent
        self.version = version
        self.public_url = public_url.rstrip("/")
        self.store = TaskStore(env_var=f"{agent.env_prefix}_DB", default_path=db_path)
        self.rpc = A2AHandler(self.store, agent)
        self.watcher = PushWatcher(self.store, agent)
        self.card = build_agent_card(agent, public_url, version, protocol_version)

    def start(self) -> None:
        self.watcher.start()

    def stop(self) -> None:
        self.watcher.stop()
        self.store.close()

    def health(self) -> dict:
        return {
            "status": "ok",
            "agent": self.agent.name,
            "version": self.version,
            "protocolVersion": self.card["protocolVersion"],
            "tasks": self.store.count_tasks(),
            "push_configs": self.store.count_push_configs(),
            "watch_interval_s": self.watcher.interval,
            "datasets": list(self.agent.datasets),
            "card": self.public_url + "/.well-known/agent-card.json",
        }


def make_handler(app: AgentServerApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-kit/" + app.version

        def log_message(self, fmt, *args):  # route through logging, not stderr
            logging.getLogger("a2a.http").debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------

        def _send_json(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_rpc_error(self, request_id, exc: A2AError) -> None:
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}},
            )

        def _write_event(self, request_id, event: dict) -> None:
            line = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": event})
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        # -- routes --------------------------------------------------------

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                self._send_json(200, app.card)
            elif path == "/healthz":
                self._send_json(200, app.health())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send_rpc_error(None, A2AError(-32600, "empty request body"))
                return
            if length > MAX_BODY:
                self._send_rpc_error(None, A2AError(-32600, "request body too large"))
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_rpc_error(None, A2AError(-32700, "invalid JSON"))
                return
            if not isinstance(payload, dict):
                self._send_rpc_error(None, A2AError(-32600, "request must be a JSON-RPC object"))
                return
            request_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params") or {}
            if not isinstance(method, str):
                self._send_rpc_error(request_id, A2AError(-32600, "method is required"))
                return
            try:
                result = app.rpc.handle(method, params)
            except A2AError as exc:
                self._send_rpc_error(request_id, exc)
                return
            except Exception as exc:  # unexpected: report internal error, keep serving
                log.exception("unhandled error in %s", method)
                self._send_rpc_error(request_id, A2AError(-32603, f"internal error: {exc}"))
                return
            if hasattr(result, "__next__"):
                self._stream(request_id, result)
                return
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

        def _stream(self, request_id, events) -> None:
            # Surface early errors (before any event) as plain JSON-RPC errors.
            try:
                first = next(events)
            except StopIteration:
                self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": None})
                return
            except A2AError as exc:
                self._send_rpc_error(request_id, exc)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self._write_event(request_id, first)
                for event in events:
                    self._write_event(request_id, event)
            except (BrokenPipeError, ConnectionResetError):
                log.info("stream client disconnected (task event stream closed early)")

    return Handler


def serve(app: AgentServerApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    log.info("agent card: %s/.well-known/agent-card.json", app.public_url)
    log.info("skills: %s", ", ".join(skill["id"] for skill in app.card["skills"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
        app.stop()
    return server


__all__ = ["AgentServerApp", "MAX_BODY", "PROTOCOL_VERSION", "build_agent_card", "make_handler", "serve"]
