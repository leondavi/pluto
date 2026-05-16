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
import os
from contextlib import asynccontextmanager
from typing import Optional

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from agent_mcp_friend.notifier import Notifier
from agent_mcp_friend.prompts import (
    build_check_prompt_body,
    build_guide_prompt_body,
    build_protocol_prompt_body,
    build_role_prompt_body,
    build_status_prompt_body,
    build_watch_prompt_body,
    build_watch_stop_prompt_body,
    role_prompt_specs,
)
from agent_mcp_friend.resources import register_resources
from agent_mcp_friend.tools import register_tools
from pluto_client import PlutoHttpClient
from utils.snapshot_helper import (
    DEFAULT_AUTO_SNAPSHOT_INTERVAL_S,
    DEFAULT_SNAPSHOT_DIR,
    AutoSnapshotter,
)

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
        wait_timeout_s: int = 300,
        iterations: int = 15,
        roles_dir: Optional[str] = None,
        protocol_path: Optional[str] = None,
        guide_path: Optional[str] = None,
        restore_path: Optional[str] = None,
        snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
        auto_snapshot_enabled: bool = True,
        auto_snapshot_interval_s: int = DEFAULT_AUTO_SNAPSHOT_INTERVAL_S,
    ):
        self.agent_id = agent_id
        self.host = host
        self.http_port = http_port
        self.ttl_ms = ttl_ms
        self.wait_timeout_s = max(1, int(wait_timeout_s))
        self.iterations = max(1, int(iterations))
        self.roles_dir = roles_dir
        self.protocol_path = protocol_path
        self.guide_path = guide_path
        self.restore_path = restore_path
        self.restore_summary = None  # populated after successful restore
        self.snapshot_dir = snapshot_dir
        self.auto_snapshot_enabled = auto_snapshot_enabled
        self.auto_snapshot_interval_s = auto_snapshot_interval_s
        self.autosnap: Optional[AutoSnapshotter] = None

        self.client = PlutoHttpClient(
            host=host,
            http_port=http_port,
            agent_id=agent_id,
            mode="http",
            ttl_ms=ttl_ms,
        )
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)

        # Phase-2 seam: MCP notifications. Gated behind the
        # PLUTO_MCP_NOTIFICATIONS env var; default off. When on, we will
        # emit notifications/inboxMessage on arrival and
        # notifications/watcherError on hard watcher failures. The wire
        # format is not yet implemented — this attribute exists so that
        # downstream hook points can no-op cleanly today and light up
        # later behind a single flag without code churn.
        self.notifications_enabled = (
            os.environ.get("PLUTO_MCP_NOTIFICATIONS", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        self.notifier = Notifier(enabled=self.notifications_enabled)
        self.inbox.set_notifier(self.notifier)
        self.inbox.on_new_message(self._notify_inbox_message)

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
        register_tools(
            self.mcp, self.client, self.inbox, self.lock_mgr,
            wait_timeout_s=self.wait_timeout_s,
            notifier=self.notifier,
            server=self,
        )
        register_resources(self.mcp, self.client, self.inbox, self.lock_mgr)
        self._register_prompts()

    def _register_prompts(self) -> None:
        host = self.host
        port = self.http_port
        agent_id = self.agent_id
        wait_s = self.wait_timeout_s
        iterations = self.iterations
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
                wait_timeout_s=wait_s,
                iterations=iterations,
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
                wait_timeout_s=wait_s,
                iterations=iterations,
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
                "Start watching the Pluto inbox at chat speed by spawning "
                "a background Task that calls pluto_wait_for_messages."
            ),
        )
        def pluto_watch() -> str:
            return build_watch_prompt_body(
                wait_timeout_s=wait_s, iterations=iterations,
            )

        @self.mcp.prompt(
            name="pluto-watch-stop",
            description=(
                "Engage the watcher_stop kill switch: stop auto-respawning "
                "the inbox watcher Task on drain. Resume with /pluto-watch."
            ),
        )
        def pluto_watch_stop() -> str:
            return build_watch_stop_prompt_body()

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
                        wait_timeout_s=wait_s,
                        iterations=iterations,
                        roles_dir=roles_dir,
                        protocol_path=protocol_path,
                    )
                return handler

            self.mcp.prompt(
                name=prompt_name,
                description=description,
            )(make_handler())

    # ── Notification seam (phase-2 stub) ──────────────────────────────────

    async def _notify_inbox_message(self, messages: list) -> None:
        """Legacy seam — phase-2 wire format now lives in
        :class:`Notifier`, which the inbox calls directly via
        ``set_notifier``. This handler stays registered for diagnostic
        symmetry (always-on debug log of fire events).
        """
        logger.debug(
            "_absorb fan-out: %d fresh message(s); notifier.enabled=%s",
            len(messages), self.notifications_enabled,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _lifespan(self, _server):
        """FastMCP lifespan: register, start inbox, then tear down."""
        registered = await asyncio.to_thread(self._register_blocking)
        if registered and self.restore_path:
            await asyncio.to_thread(self._restore_from_file_blocking)
        if registered and self.auto_snapshot_enabled:
            self.autosnap = AutoSnapshotter(
                save_fn=self.client.save_snapshot_files,
                snapshot_dir=self.snapshot_dir,
                interval_s=self.auto_snapshot_interval_s,
            )
            self.autosnap.start()
        try:
            if registered:
                self.inbox.start()
            yield {"agent_id": self.agent_id}
        finally:
            if self.autosnap is not None:
                await asyncio.to_thread(self.autosnap.stop, True)
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

    def _restore_from_file_blocking(self) -> None:
        """Load a .plut snapshot file and apply it to the just-registered session."""
        import json
        try:
            with open(self.restore_path, "r", encoding="utf-8") as f:
                plut = json.load(f)
        except Exception as exc:
            logger.error("Cannot read .plut at %s: %s", self.restore_path, exc)
            return
        snap_agent_id = plut.get("agent_id")
        if snap_agent_id and snap_agent_id != self.agent_id:
            logger.warning(
                "Snapshot agent_id %s differs from registered %s — locks will land in lost_locks",
                snap_agent_id, self.agent_id,
            )
        try:
            result = self.client.restore_from_snapshot(plut)
        except Exception as exc:
            logger.error("restore_from_snapshot failed: %s", exc)
            return
        self.restore_summary = result
        logger.info(
            "Restored %s from %s — reclaimed=%d lost=%d",
            self.agent_id, self.restore_path,
            len(result.get("reclaimed_locks", [])),
            len(result.get("lost_locks", [])),
        )

    def run(self) -> None:
        """Run the MCP server over stdio. Blocks until the client disconnects."""
        self.setup_capabilities()
        self.mcp.run(transport="stdio")
