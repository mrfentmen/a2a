"""Shared smoke-test helper: talks to a running A2A server over real HTTP.

Each server's tools/smoke.py imports this, adds its own skill-specific checks,
and exits non-zero if anything fails.
"""

from __future__ import annotations

import json
import sys
import uuid
from urllib import error, request


class Smoke:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.passed = 0
        self.failed = 0

    # -- assertions --------------------------------------------------------

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    def finish(self) -> int:
        print(f"\nSMOKE {'PASS' if not self.failed else 'FAIL'} — {self.passed} passed, {self.failed} failed")
        return 1 if self.failed else 0

    # -- transport ---------------------------------------------------------

    def get_json(self, path: str, timeout: float = 30) -> dict:
        with request.urlopen(self.base + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def rpc(self, method: str, params: dict, timeout: float = 45) -> dict:
        """One JSON-RPC call. `timeout` is raised by servers whose upstream is slow (USGS VSC)."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        ).encode()
        req = request.Request(self.base + "/", data=body, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def stream(self, method: str, params: dict, timeout: float = 90) -> list[dict]:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        ).encode()
        req = request.Request(self.base + "/", data=body, headers={"Content-Type": "application/json"})
        events: list[dict] = []
        with request.urlopen(req, timeout=timeout) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:])["result"])
        return events

    @staticmethod
    def user_message(text: str, **extra) -> dict:
        message = {
            "kind": "message",
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        }
        message.update(extra)
        return message

    def card_checks(self, expected_name: str, skill_ids: list[str], timeout: float = 30) -> bool:
        """Standard card checks. Returns False when the server is unreachable."""
        try:
            card = self.get_json("/.well-known/agent-card.json", timeout=timeout)
        except error.URLError as exc:
            print(f"[FAIL] cannot reach {self.base}: {exc}")
            return False
        self.check("agent card served", card.get("name") == expected_name, f"protocol {card.get('protocolVersion')}")
        caps = card.get("capabilities", {})
        self.check("card advertises streaming + push", bool(caps.get("streaming")) and bool(caps.get("pushNotifications")))
        found = [skill["id"] for skill in card.get("skills", [])]
        self.check("card lists the expected skills", found == skill_ids, ", ".join(found))
        return True
