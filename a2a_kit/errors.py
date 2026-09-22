"""A2A protocol error codes, shared helpers, and the base agent contract.

Error codes mirror the official a2a-python SDK map:
  -32001 task not found, -32002 not cancelable, -32003 push not supported,
  -32004 unsupported operation, -32005 content type, -32006 invalid response,
  -32600/-32601/-32602/-32603 standard JSON-RPC codes.
"""

from __future__ import annotations

import ipaddress
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

ERROR_CODES = {
    "task_not_found": -32001,
    "task_not_cancelable": -32002,
    "push_not_supported": -32003,
    "unsupported_operation": -32004,
    "content_type_not_supported": -32005,
    "invalid_agent_response": -32006,
    "invalid_request": -32600,
    "method_not_found": -32601,
    "invalid_params": -32602,
    "internal": -32603,
}


class A2AError(Exception):
    """A JSON-RPC error that is safe to hand back to the caller."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UpstreamError(RuntimeError):
    """The public dataset behind a skill could not be read."""


def error(name: str, message: str) -> A2AError:
    return A2AError(ERROR_CODES[name], message)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def agent_message(text: str) -> dict:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": new_id(),
        "parts": [{"kind": "text", "text": text}],
    }


def validate_webhook_url(url: str, allow_private: bool, allow_env: str = "A2A_ALLOW_PRIVATE_WEBHOOKS") -> None:
    """Reject non-http(s) URLs, and private/loopback hosts unless explicitly allowed."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise error("invalid_params", "pushNotificationConfig.url must be an http(s) URL")
    host = parsed.hostname
    loopback = host == "localhost"
    private = False
    try:
        ip = ipaddress.ip_address(host)
        loopback = loopback or ip.is_loopback
        private = ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        pass  # hostname; DNS names are allowed (SSRF caveat is documented in each README)
    if (loopback or private) and not allow_private:
        raise error(
            "invalid_params",
            f"webhook URL points at a private address; set {allow_env}=1 to allow local demos",
        )


class SkillAgent:
    """What a domain agent must provide to be served by the A2A kit.

    A subclass owns the dataset work and the wording; the kit owns the protocol:
    task lifecycle, streaming, cancellation, push configs, HTTP, and storage.
    """

    #: short name used in logs
    name = "agent"
    #: card display name / description
    card_name = "A2A agent"
    card_description = ""
    #: dataset ids this agent reads (reported on /healthz)
    datasets: tuple[str, ...] = ()
    #: env prefix for per-agent settings, e.g. "NYC311"
    env_prefix = "A2A"
    #: card skills (A2A AgentSkill objects)
    card_skills: list[dict] = []
    #: watch kinds this agent knows how to probe (see probe_watch)
    watch_kinds: tuple[str, ...] = ()

    def parse(self, message: dict) -> dict:
        """Incoming message -> {"skill": str, "params": dict, "explicit": bool}.

        "explicit" says whether the caller actually named this skill. It lets the
        protocol keep a pending input-required skill when the follow-up is just a
        bare parameter ("site 55450"). Must not raise.
        """
        raise NotImplementedError

    def missing(self, skill: str, params: dict) -> list[str]:
        """Required parameters still absent for this skill."""
        return []

    def input_prompt(self, skill: str, missing: list[str]) -> str:
        """What to ask the caller when parameters are missing."""
        raise NotImplementedError

    def run(self, request: dict) -> dict:
        """Execute a request -> {"final_state", "message", "artifact", "watch"}."""
        raise NotImplementedError

    def probe_watch(self, watch: dict):
        """Current observation for a watch spec, or None if it cannot be read."""
        return None

    def describe_watch_change(self, watch: dict, previous, observed) -> str:
        """Human sentence for a push notification."""
        return "Watched value changed."

    @property
    def allow_private_webhooks(self) -> bool:
        return os.environ.get(f"{self.env_prefix}_ALLOW_PRIVATE_WEBHOOKS", "0") == "1"
