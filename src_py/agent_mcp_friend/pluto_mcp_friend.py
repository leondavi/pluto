#!/usr/bin/env python3
"""PlutoMCPFriend CLI — launch the MCP-server adapter for Pluto.

Spoken to over stdio by the agent CLI (Claude Code, Cursor, Aider, ...)
that has it registered in its ``.mcp.json`` config. Not intended to be
run interactively by humans — use ``PlutoMCPFriend.sh`` instead, which
generates the ``.mcp.json`` and launches the framework with it.

Usage (typical, via .mcp.json)::

    {
      "mcpServers": {
        "pluto": {
          "command": "/path/to/.venv/bin/python",
          "args": [
            "/path/to/src_py/agent_mcp_friend/pluto_mcp_friend.py",
            "--agent-id", "coder-1",
            "--host", "localhost",
            "--http-port", "9201"
          ]
        }
      }
    }
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Allow running as a script:  python3 pluto_mcp_friend.py
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_PY = os.path.normpath(os.path.join(_THIS_DIR, ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_friend.frameworks import load_pluto_config  # noqa: E402
from agent_mcp_friend import __version__  # noqa: E402
from agent_mcp_friend.server import PlutoMCPServer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pluto_mcp_friend",
        description=(
            "PlutoMCPFriend — MCP server adapter for the Pluto coordination "
            "server. Run by an MCP-capable agent CLI via stdio. Use "
            "PlutoMCPFriend.sh for interactive setup."
        ),
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent ID to register with Pluto (e.g. coder-1).",
    )
    parser.add_argument(
        "--host", default=None,
        help="Pluto server host (default: from config / localhost).",
    )
    parser.add_argument(
        "--http-port", type=int, default=None,
        help="Pluto HTTP port (default: from config / 9201).",
    )
    parser.add_argument(
        "--ttl-ms", type=int, default=600_000,
        help=(
            "Session TTL in milliseconds (default: 600000 = 10 min). The "
            "background inbox loop renews the session implicitly."
        ),
    )
    parser.add_argument(
        "--wait-timeout-s", type=int, default=300, metavar="SECONDS",
        help=(
            "Default block duration for pluto_wait_for_messages, in seconds "
            "(default: 300 = 5 min). Used as the tool's argument default and "
            "as the value embedded in the role connection block / "
            "/pluto-watch slash prompt. Higher = fewer Task respawns; lower "
            "= faster recovery if a watcher is wedged."
        ),
    )
    parser.add_argument(
        "--roles-dir", default=None,
        help="Override the directory scanned for role prompt files.",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=(
            "Log level for stderr diagnostics. Logs go to stderr, never "
            "stdout — stdout is reserved for MCP JSON-RPC framing."
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=f"PlutoMCPFriend {__version__}",
    )

    args = parser.parse_args(argv)

    # Logging must go to stderr only — stdout is the MCP transport.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="[pluto-mcp] %(levelname)s %(name)s: %(message)s",
    )

    # Resolve host / port from config when not given explicitly.
    config = load_pluto_config()
    server_cfg = config.get("pluto_server", {})
    host = args.host or server_cfg.get("host_ip", "127.0.0.1")
    http_port = args.http_port or server_cfg.get("host_http_port", 9201)

    server = PlutoMCPServer(
        agent_id=args.agent_id,
        host=host,
        http_port=http_port,
        ttl_ms=args.ttl_ms,
        wait_timeout_s=args.wait_timeout_s,
        roles_dir=args.roles_dir,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
