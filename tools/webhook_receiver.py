#!/usr/bin/env python3
"""Tiny A2A webhook receiver: prints every push notification it receives.

    python3 tools/webhook_receiver.py --port 8799

Point a task's pushNotificationConfig at http://127.0.0.1:8799/hooks and watch
the event JSON land here.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-webhook-receiver/1.0"

        def log_message(self, fmt, *args):
            pass  # keep stdout for the events themselves

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = {"raw": raw.decode("utf-8", errors="replace")}
            print(
                json.dumps(
                    {
                        "path": self.path,
                        "token": self.headers.get("X-A2A-Notification-Token"),
                        "event": event,
                    }
                ),
                flush=True,
            )
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A2A push-notification receiver (demo)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args(argv)
    print(f"listening for A2A push notifications on http://{args.host}:{args.port}/hooks", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), make_handler())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
