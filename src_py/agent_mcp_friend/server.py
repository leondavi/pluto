"""PlutoMCPServer — top-level MCP server orchestrator.

Owns the long-lived ``PlutoHttpClient``, the ``InboxManager`` peek loop,
and the ``LockManager`` auto-renewal tasks. Registers every tool,
prompt, and resource on a single :class:`FastMCP` instance and runs it
over stdio.

The server registers with Pluto on startup and unregisters on shutdown —
the agent never sees the session token, never has to ping or ack.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from agent_mcp_friend.prompts import (
    build_check_prompt_body,
    build_guide_prompt_body,
    build_protocol_prompt_body,
    build_role_prompt_body,
    build_status_prompt_body,
    build_watch_prompt_body,
    role_prompt_specs,
)
from agent_mcp_friend.resources import register_resources
from agent_mcp_friend.tools import register_tools
from pluto_client import PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.server")


class PlutoMCPServer:
    """MCP-server adapter for Pluto.

    Lifecycle:

    1. ``__init__``: build the FastMCP instance and the HTTP client.
    2. ``setup_capabilities()``: register tools, prompts, resources.
    3. ``run()``: register with Pluto, start background loops, run stdio,
       then unregister cleanly on exit.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        host: str = "127.0.0.1",
        http_port: int = 9201,
        ttl_ms: int = 600_000,
        roles_dir: Optional[str] = None,
        protocol_path: Optional[str] = None,
        guide_path: Optional[str] = None,
    ):
        self.agent_id = agent_id
        self.host = host
        self.http_port = http_port
        self.ttl_ms = ttl_ms
        self.roles_dir = roles_dir
        self.protocol_path = protocol_path
        self.guide_path = guide_path

        self.client = PlutoHttpClient(
            host=host,
            http_port=http_port,
            agent_id=agent_id,
            mode="http",
            ttl_ms=ttl_ms,
        )
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)

        self.mcp = FastMCP(
            name="pluto",
            instructions=(
                "Pluto coordination tools. Call pluto_recv at the start of "
                "each turn to drain the inbox; or check the _pluto_inbox "
                "field on any other Pluto tool result. Use pluto_send / "
                "pluto_broadcast for messaging, pluto_lock_* for "
                "concurrency control, pluto_task_* for task coordination."
            ),
            lifespan=self._lifespan,
        )

    # ── Capability registration ───────────────────────────────────────────

    def setup_capabilities(self) -> None:
        register_tools(self.mcp, self.client, self.inbox, self.lock_mgr)
        register_resources(self.mcp, self.client, self.inbox, self.lock_mgr)
        self._register_prompts()

    def _register_prompts(self) -> None:
        host = self.host
        port = self.http_port
        agent_id = self.agent_id
        roles_dir = self.roles_dir
        protocol_path = self.protocol_path
        guide_path = self.guide_path

        # Standalone protocol prompt
        @self.mcp.prompt(
            name="pluto-protocol",
            description=(
                "Inline the shared Pluto coordination protocol "
                "(library/protocol.md) plus live connection info."
            ),
        )
        def pluto_protocol() -> str:
            return build_protocol_prompt_body(
                host=host,
                http_port=port,
                agent_id=agent_id,
                protocol_path=protocol_path,
            )

        # Standalone guide prompt
        @self.mcp.prompt(
            name="pluto-guide",
            description=(
                "Inline the PlutoAgentFriend / PlutoMCPFriend skill guide "
                "for working with the Pluto coordination server."
            ),
        )
        def pluto_guide() -> str:
            return build_guide_prompt_body(
                host=host,
                http_port=port,
                agent_id=agent_id,
                guide_path=guide_path,
            )

        # Action prompts — one-keystroke shortcuts the user can invoke from
        # Claude Code's slash menu without having to phrase a request to the
        # agent in English.

        @self.mcp.prompt(
            name="pluto-check",
            description=(
                "Drain my Pluto inbox right now and summarize whatever "
                "arrived (one-shot, non-blocking)."
            ),
        )
        def pluto_check() -> str:
            return build_check_prompt_body()

        @self.mcp.prompt(
            name="pluto-watch",
            description=(
                "Start watching the Pluto inbox at chat speed: spawn a "
                "background Task on Claude Code, or a foreground "
                "long-poll on Cursor/Aider."
            ),
        )
        def pluto_watch() -> str:
            return build_watch_prompt_body()

        @self.mcp.prompt(
            name="pluto-status",
            description=(
                "Report current Pluto state: my agent_id, connected peers, "
                "inbox depth, and locks I hold."
            ),
        )
        def pluto_status() -> str:
            return build_status_prompt_body()

        # One prompt per role file. Bind via default-arg trick to avoid
        # the late-binding closure pitfall.
        for prompt_name, role_name, description in role_prompt_specs(roles_dir):
            def make_handler(_role: str = role_name):
                def handler() -> str:
                    return build_role_prompt_body(
                        _role,
                        host=host,
                        http_port=port,
                        agent_id=agent_id,
                        roles_dir=roles_dir,
                        protocol_path=protocol_path,
                    )
                return handler

            self.mcp.prompt(
                name=prompt_name,
                description=description,
            )(make_handler())

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, _server):
        """FastMCP lifespan: register, start inbox, then tear down."""
        registered = await asyncio.to_thread(self._register_blocking)
        try:
            if registered:
                self.inbox.start()
            yield {"agent_id": self.agent_id}
        finally:
            await self.inbox.stop()
            await self.lock_mgr.shutdown()
            if self.client.token:
                try:
                    await asyncio.to_thread(self.client.unregister)
                except Exception as exc:
                    logger.debug("Unregister on shutdown failed: %s", exc)

    def _register_blocking(self) -> bool:
        try:
            resp = self.client.register()
        except Exception as exc:
            logger.error(
                "Cannot register with Pluto at %s:%d — %s",
                self.host, self.http_port, exc,
            )
            return False
        if resp.get("status") != "ok":
            logger.error("Pluto registration failed: %s", resp)
            return False
        actual = resp.get("agent_id", self.agent_id)
        if actual != self.agent_id:
            self.agent_id = actual
        logger.info(
            "PlutoMCPFriend registered as %s (token=%s...)",
            self.agent_id,
            (self.client.token or "")[:12],
        )
        return True

    def run(self) -> None:
        """Run the MCP server over stdio. Blocks until the client disconnects."""
        self.setup_capabilities()
        self.mcp.run(transport="stdio")
