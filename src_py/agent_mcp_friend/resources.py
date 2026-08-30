"""Read-only MCP resources backed by Pluto state.

Resources are addressable URIs an agent (or human user via Claude
Code's ``@``-mention) can fetch on demand. Pluto exposes:

* ``pluto://inbox`` — current pending messages without acking them.
* ``pluto://locks`` — locks held by *this* agent (managed by the
  auto-renewal LockManager).
* ``pluto://agents`` — every connected agent on the server.
* ``pluto://server`` — server health / version info.
* ``pluto://protocol`` — the full shared collaboration protocol text.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from agent_mcp_friend.prompts import default_protocol_path
from pluto_client import PlutoHttpClient


def _dumps(obj) -> str:
    # Compact separators: these payloads land in agent context, where
    # pretty-printing is a ~20-30% token surcharge (mirrors
    # agent_friend/message_formatter's compact JSON).
    return json.dumps(obj, separators=(",", ":"))


def register_resources(
    mcp: FastMCP,
    client: PlutoHttpClient,
    inbox: InboxManager,
    lock_mgr: LockManager,
    protocol_path: str | None = None,
) -> None:
    """Register the canonical Pluto resources on *mcp*."""

    @mcp.resource(
        "pluto://inbox",
        name="Pluto inbox",
        description=(
            "Pending messages addressed to this agent. Reading this "
            "resource does NOT ack the messages — to drain and ack, call "
            "the pluto_recv tool instead."
        ),
        mime_type="application/json",
    )
    async def inbox_resource() -> str:
        msgs = await inbox.peek_only()
        return _dumps({"messages": msgs, "count": len(msgs)})

    @mcp.resource(
        "pluto://locks",
        name="Pluto locks (held by this agent)",
        description=(
            "Locks currently held by this agent that are being "
            "auto-renewed by PlutoMCPFriend."
        ),
        mime_type="application/json",
    )
    async def locks_resource() -> str:
        return _dumps({"locks": lock_mgr.held_locks()})

    @mcp.resource(
        "pluto://agents",
        name="Connected Pluto agents",
        description="Every agent currently registered with the Pluto server.",
        mime_type="application/json",
    )
    async def agents_resource() -> str:
        agents = await asyncio.to_thread(client.list_agents_detailed)
        return _dumps({"agents": agents})

    @mcp.resource(
        "pluto://server",
        name="Pluto server health",
        description="Server version and reachability status.",
        mime_type="application/json",
    )
    async def server_resource() -> str:
        try:
            info = await asyncio.to_thread(client._get, "/health")
            return _dumps(info)
        except Exception as exc:
            return _dumps({"status": "error", "reason": str(exc)})

    @mcp.resource(
        "pluto://protocol",
        name="Pluto coordination protocol",
        description=(
            "Full shared collaboration protocol (library/protocol.md). "
            "Role prompts inline only a digest — fetch this when you "
            "need exact message schemas or injection-frame details."
        ),
        mime_type="text/markdown",
    )
    async def protocol_resource() -> str:
        path = protocol_path or default_protocol_path()
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            return f"(could not read {path}: {exc})"
