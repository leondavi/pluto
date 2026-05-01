"""Read-only MCP resources backed by Pluto state.

Resources are addressable URIs an agent (or human user via Claude
Code's ``@``-mention) can fetch on demand. Pluto exposes:

* ``pluto://inbox`` — current pending messages without acking them.
* ``pluto://locks`` — locks held by *this* agent (managed by the
  auto-renewal LockManager).
* ``pluto://agents`` — every connected agent on the server.
* ``pluto://server`` — server health / version info.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from pluto_client import PlutoHttpClient


def register_resources(
    mcp: FastMCP,
    client: PlutoHttpClient,
    inbox: InboxManager,
    lock_mgr: LockManager,
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
        return json.dumps({"messages": msgs, "count": len(msgs)}, indent=2)

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
        return json.dumps({"locks": lock_mgr.held_locks()}, indent=2)

    @mcp.resource(
        "pluto://agents",
        name="Connected Pluto agents",
        description="Every agent currently registered with the Pluto server.",
        mime_type="application/json",
    )
    async def agents_resource() -> str:
        agents = await asyncio.to_thread(client.list_agents_detailed)
        return json.dumps({"agents": agents}, indent=2)

    @mcp.resource(
        "pluto://server",
        name="Pluto server health",
        description="Server version and reachability status.",
        mime_type="application/json",
    )
    async def server_resource() -> str:
        try:
            info = await asyncio.to_thread(client._get, "/health")
            return json.dumps(info, indent=2)
        except Exception as exc:
            return json.dumps({"status": "error", "reason": str(exc)}, indent=2)
