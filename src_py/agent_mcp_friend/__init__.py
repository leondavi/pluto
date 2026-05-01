"""PlutoMCPFriend — MCP-server adapter for the Pluto coordination server.

Exposes Pluto's HTTP coordination API (locks, messaging, tasks) as native
MCP tools, prompts, and resources so MCP-capable agent CLIs (Claude Code,
Cursor, Aider, ...) can call ``pluto_send`` / ``pluto_lock_acquire`` /
etc. as ordinary tool calls — no PTY, no curl, no copy-pasted tokens.

The adapter is a thin wrapper around the existing ``PlutoHttpClient``;
the Erlang server is unchanged.

See ``docs/guide/pluto-mcp-friend.md`` for end-user documentation.
"""

__all__ = ["__version__"]

import os as _os


def _read_version() -> str:
    here = _os.path.dirname(_os.path.abspath(__file__))
    candidate = _os.path.normpath(_os.path.join(here, "..", "..", "VERSION.md"))
    try:
        with open(candidate) as f:
            v = f.readline().strip()
            if v.startswith("v") or v.startswith("V"):
                v = v[1:]
            return v or "unknown"
    except OSError:
        return "unknown"


__version__: str = _read_version()
