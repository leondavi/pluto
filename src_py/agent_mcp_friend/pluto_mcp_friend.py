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
from utils.snapshot_helper import (  # noqa: E402
    DEFAULT_AUTO_SNAPSHOT_INTERVAL_S,
    DEFAULT_SNAPSHOT_DIR,
    clean_snapshots,
    resolve_resume_path,
)


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
        "--agent-id", required=False, default=None,
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
        "--wait-timeout-s", type=int, default=60, metavar="SECONDS",
        help=(
            "Per-call block duration for pluto_wait_for_messages, in "
            "seconds (default: 60). The watcher subagent loops short "
            "calls of this length so it produces output regularly and "
            "never trips Claude Code's stream watchdog. Keep <=120 to "
            "be safe; a single 300+ s block in a subagent will be "
            "killed at the 600 s silence cutoff."
        ),
    )
    parser.add_argument(
        "--iterations", type=int, default=15, metavar="N",
        help=(
            "How many short-poll iterations the watcher subagent runs "
            "before exiting and letting the parent respawn it (default: "
            "15). Total subagent lifetime ≈ iterations * wait-timeout-s "
            "seconds (default 15 * 60 = 15 minutes). Larger values "
            "reduce respawn-gap latency for orchestrators driving idle "
            "agents; smaller values bound subagent runtime more tightly."
        ),
    )
    parser.add_argument(
        "--roles-dir", default=None,
        help="Override the directory scanned for role prompt files.",
    )
    parser.add_argument(
        "--restore", dest="restore_path", default=None, metavar="PATH",
        help=(
            "Path to a .plut snapshot file. After registering with Pluto, "
            "the launcher applies the snapshot via restore_from_snapshot — "
            "attributes, custom_status, subscriptions, and any reclaimable "
            "locks are reattached, and status flips to recovered_from_file. "
            "Use the agent_id stored inside the .plut as --agent-id to "
            "rejoin the same identity (otherwise locks land in lost_locks)."
        ),
    )
    parser.add_argument(
        "--resume", action="store_true",
        help=(
            "Restore from the default snapshot dir without specifying a "
            "path. With --agent-id, picks "
            f"{DEFAULT_SNAPSHOT_DIR}/<agent_id>.plut; without, picks the "
            "only .plut present. Warns and continues with a fresh register "
            "if no snapshot is found."
        ),
    )
    parser.add_argument(
        "--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR, metavar="DIR",
        help=(
            f"Directory for auto-snapshots and --resume "
            f"(default: {DEFAULT_SNAPSHOT_DIR})."
        ),
    )
    parser.add_argument(
        "--no-auto-snapshot", action="store_true",
        help="Disable the periodic auto-snapshot loop.",
    )
    parser.add_argument(
        "--auto-snapshot-interval", type=int,
        default=DEFAULT_AUTO_SNAPSHOT_INTERVAL_S, metavar="SECONDS",
        help=(
            f"Auto-snapshot interval in seconds (default: "
            f"{DEFAULT_AUTO_SNAPSHOT_INTERVAL_S} = 2 hours). Minimum 60s."
        ),
    )
    parser.add_argument(
        "--clean-snapshots", action="store_true",
        help=(
            "Delete every .plut + recovery.md in --snapshot-dir, then exit. "
            "Combine with --agent-id to clean only one agent's files."
        ),
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

    if args.clean_snapshots:
        removed = clean_snapshots(args.agent_id, args.snapshot_dir)
        print(
            f"[pluto-mcp] removed {len(removed)} file(s) from "
            f"{args.snapshot_dir}",
            file=sys.stderr,
        )
        for p in removed:
            print(f"  - {p}", file=sys.stderr)
        return 0

    if not args.agent_id:
        parser.error("--agent-id is required (unless --clean-snapshots is the only action)")

    restore_path = args.restore_path
    if args.resume and not restore_path:
        restore_path = resolve_resume_path(args.agent_id, args.snapshot_dir)

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
        iterations=args.iterations,
        roles_dir=args.roles_dir,
        restore_path=restore_path,
        snapshot_dir=args.snapshot_dir,
        auto_snapshot_enabled=not args.no_auto_snapshot,
        auto_snapshot_interval_s=args.auto_snapshot_interval,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
