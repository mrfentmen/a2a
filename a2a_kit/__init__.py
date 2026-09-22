"""a2a_kit — a stdlib-only Agent2Agent (A2A) server kit.

Build an A2A server by subclassing SkillAgent and handing it to run_agent_server:
you write the skills, the kit speaks the protocol (cards, JSON-RPC, SSE streams,
task lifecycle, webhook push notifications, SQLite persistence).
"""

from .cli import run_agent_server
from .errors import A2AError, ERROR_CODES, SkillAgent, UpstreamError, agent_message, error, new_id, now_iso
from .httpd import PROTOCOL_VERSION, AgentServerApp, build_agent_card, make_handler, serve
from .jsonapi import DEFAULT_USER_AGENT, JsonApiClient
from .protocol import A2AHandler
from .push import PushWatcher
from .socrata import SocrataClient, soql_escape, utc_now_iso
from .store import INTERRUPTED_STATES, TERMINAL_STATES, TaskStore

__all__ = [
    "A2AError",
    "A2AHandler",
    "AgentServerApp",
    "DEFAULT_USER_AGENT",
    "ERROR_CODES",
    "INTERRUPTED_STATES",
    "JsonApiClient",
    "PROTOCOL_VERSION",
    "PushWatcher",
    "SocrataClient",
    "SkillAgent",
    "TERMINAL_STATES",
    "TaskStore",
    "UpstreamError",
    "agent_message",
    "build_agent_card",
    "error",
    "make_handler",
    "new_id",
    "now_iso",
    "run_agent_server",
    "serve",
    "soql_escape",
    "utc_now_iso",
]

__version__ = "1.0.0"
