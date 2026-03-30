"""
pluto_client_def.py — Definitions, constants, and type aliases for pluto_client.

Import this module in pluto_client.py. Do not add logic here.
"""

from typing import Callable, Dict, List, Optional

# ── ASCII Logo ───────────────────────────────────────────────────────────────

PLUTO_LOGO = (
    "\n"
    "  ██████╗ ██╗     ██╗   ██╗████████╗ ██████╗ \n"
    "  ██╔══██╗██║     ██║   ██║╚══██╔══╝██╔═══██╗\n"
    "  ██████╔╝██║     ██║   ██║   ██║   ██║   ██║\n"
    "  ██╔═══╝ ██║     ██║   ██║   ██║   ██║   ██║\n"
    "  ██║     ███████╗╚██████╔╝   ██║   ╚██████╔╝\n"
    "  ╚═╝     ╚══════╝ ╚═════╝    ╚═╝    ╚═════╝ \n"
    "\n"
    "  · · ·  Agent Coordination Server  · · ·\n"
)

# ── Network defaults ─────────────────────────────────────────────────────────

DEFAULT_HOST: str = "localhost"
DEFAULT_PORT: int = 9000
DEFAULT_TIMEOUT: float = 10.0  # seconds
DEFAULT_AGENT_ID: str = "pluto-cli"

# ── Agent guide ──────────────────────────────────────────────────────────────

DEFAULT_GUIDE_OUTPUT_PATH: str = "/tmp/pluto/agent_guide.md"
# Relative path from src_py/ to the template file
GUIDE_TEMPLATE_RELATIVE: str = "../agent/agent_guide_template.md"

# ── Protocol operations ──────────────────────────────────────────────────────

OP_REGISTER    = "register"
OP_ACQUIRE     = "acquire"
OP_RELEASE     = "release"
OP_RENEW       = "renew"
OP_SEND        = "send"
OP_BROADCAST   = "broadcast"
OP_LIST_AGENTS = "list_agents"
OP_PING        = "ping"
OP_STATS       = "stats"

# ── Lock modes ───────────────────────────────────────────────────────────────

MODE_WRITE = "write"
MODE_READ  = "read"
LOCK_MODES = [MODE_WRITE, MODE_READ]

# ── Response statuses ────────────────────────────────────────────────────────

STATUS_OK    = "ok"
STATUS_WAIT  = "wait"
STATUS_ERROR = "error"
STATUS_PONG  = "pong"

# ── Event types ──────────────────────────────────────────────────────────────

EVENT_MESSAGE           = "message"
EVENT_BROADCAST         = "broadcast"
EVENT_LOCK_GRANTED      = "lock_granted"
EVENT_LOCK_EXPIRED      = "lock_expired"
EVENT_LOCK_RELEASED     = "lock_released"
EVENT_WAIT_TIMEOUT      = "wait_timeout"
EVENT_DEADLOCK_DETECTED = "deadlock_detected"
EVENT_AGENT_JOINED      = "agent_joined"
EVENT_AGENT_LEFT        = "agent_left"

ALL_EVENT_TYPES = [
    EVENT_MESSAGE,
    EVENT_BROADCAST,
    EVENT_LOCK_GRANTED,
    EVENT_LOCK_EXPIRED,
    EVENT_LOCK_RELEASED,
    EVENT_WAIT_TIMEOUT,
    EVENT_DEADLOCK_DETECTED,
    EVENT_AGENT_JOINED,
    EVENT_AGENT_LEFT,
]

# ── Error reasons ─────────────────────────────────────────────────────────────

ERR_BAD_REQUEST        = "bad_request"
ERR_UNKNOWN_OP         = "unknown_op"
ERR_UNKNOWN_TARGET     = "unknown_target"
ERR_CONFLICT           = "conflict"
ERR_NOT_FOUND          = "not_found"
ERR_EXPIRED            = "expired"
ERR_WAIT_TIMEOUT       = "wait_timeout"
ERR_DEADLOCK           = "deadlock"
ERR_ALREADY_REGISTERED = "already_registered"
ERR_UNAUTHORIZED       = "unauthorized"
ERR_INTERNAL_ERROR     = "internal_error"

# ── CLI ───────────────────────────────────────────────────────────────────────

CLI_OPS = ["ping", "list", "guide", "stats"]

CLI_DESCRIPTION = """\
Pluto client — interact with a running Pluto coordination server.

Subcommands:
  ping    Verify connectivity: register and confirm the server is reachable.
  list    List all agent IDs currently connected to the server.
  stats   Query server statistics (locks, messages, deadlocks, per-agent).
  guide   Generate the Pluto agent guide to a file (and print it to stdout).
"""

CLI_EPILOG = (
    "Examples:\n"
    "  python pluto_client.py ping\n"
    "  python pluto_client.py list --host 10.0.0.5 --port 9000\n"
    "  python pluto_client.py stats\n"
    "  python pluto_client.py guide\n"
    "  python pluto_client.py guide --output /home/agent/guide.md\n"
    "  python pluto_client.py guide --output ./guide.md --host myhost --port 9001\n"
)

# ── Type aliases ──────────────────────────────────────────────────────────────

LockRef        = str
WaitRef        = str
AgentId        = str
Resource       = str
EventHandler   = Callable[[Dict], None]
EventHandlerMap = Dict[str, List[EventHandler]]
