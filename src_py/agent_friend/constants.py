"""Shared constants for the PlutoAgentFriend package.

Kept dependency-free so every submodule can import from here without
creating cycles.
"""

import os
import pty
import re

# ── File descriptors (match pty module constants) ─────────────────────────────
STDIN_FILENO = pty.STDIN_FILENO   # 0
STDOUT_FILENO = pty.STDOUT_FILENO  # 1

# ── ANSI colour helpers (for stderr messages) ─────────────────────────────────
CYAN = "\033[0;36m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

# ── Terminal escape sequences for focus event handling ────────────────────────
FOCUS_TRACKING_ENABLE  = b"\x1b[?1004h"
FOCUS_TRACKING_DISABLE = b"\x1b[?1004l"
FOCUS_IN_EVENT  = b"\x1b[I"
FOCUS_OUT_EVENT = b"\x1b[O"

# Bracketed paste mode (DEC private mode 2004).
BRACKETED_PASTE_ENABLE  = b"\x1b[?2004h"
BRACKETED_PASTE_DISABLE = b"\x1b[?2004l"
PASTE_START = b"\x1b[200~"
PASTE_END   = b"\x1b[201~"

# ── Agent state constants ─────────────────────────────────────────────────────
AGENT_STATE_BUSY = "BUSY"
AGENT_STATE_ASKING_USER = "ASKING_USER"
AGENT_STATE_READY = "READY"

# ── ANSI escape stripper ─────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text* and return the clean string."""
    return _ANSI_RE.sub("", text)


# ── Default patterns that indicate the agent is asking the user ──────────────
DEFAULT_ASK_PATTERNS = [
    r"\?\s*$",
    r"\[y/n\]",
    r"\[Y/n\]",
    r"\[yes/no\]",
    r"Enter choice",
    r"Select.*:",
    r"Confirm\?",
    r"Continue\?",
    r"Proceed\?",
    r"Press Enter",
    r"Type .* to continue",
]

# How long after last output before considering the agent "idle" (fallback).
SILENCE_TIMEOUT_S = 3.0

# ── Guide-injection timing defaults ──────────────────────────────────────────
GUIDE_STARTUP_DELAY_S = float(os.environ.get("PLUTO_GUIDE_STARTUP_DELAY", 3.0))
GUIDE_READY_GRACE_S = float(os.environ.get("PLUTO_GUIDE_READY_GRACE", 1.5))
GUIDE_MAX_WAIT_S = float(os.environ.get("PLUTO_GUIDE_MAX_WAIT", 60.0))
GUIDE_RETRIES = int(os.environ.get("PLUTO_GUIDE_RETRIES", 2))
GUIDE_RETRY_DELAY_S = float(os.environ.get("PLUTO_GUIDE_RETRY_DELAY", 4.0))
INJECT_SUBMIT_DELAY_S = float(os.environ.get("PLUTO_INJECT_SUBMIT_DELAY", 0.6))
