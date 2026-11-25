"""MCP server for PlutoSmartStreams.

Standalone, it registers with Pluto so peers can see and wake this agent.
With ``attach=True`` another adapter (PlutoMCPFriend) already holds the
registration and the inbox; this server then only makes job calls, which
need no session.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.socket_notifier import SocketNotifier
from agent_smart_streams.constants import (
    DRAIN_HINT, EPOCH_CHECK_S, INBOX_KEY, INSTRUCTIONS, SERVER_NAME,
    SESSION_TTL_MS, WAKEABLE_FRAMEWORKS,
)
from agent_smart_streams.tools import register_tools
from pluto_jobs_def import JOB_EVENTS
from pluto_sdk import PlutoHttpClient, PlutoJobsClient

logger = logging.getLogger("pluto_smart_streams")


class SmartStreamsServer:
    """Wires client, inbox, wakeups and tools into one FastMCP server."""

    def __init__(self, *, agent_id: str, host: str, http_port: int,
                 ttl_ms: int = SESSION_TTL_MS, attach: bool = False,
                 framework: str | None = None):
        self.attach = attach
        self.client = PlutoHttpClient(
            host=host, http_port=http_port, agent_id=agent_id, mode="http",
            ttl_ms=ttl_ms, attributes={"smartstreams": True})
        self.jobs = PlutoJobsClient(self.client, agent_id=agent_id,
                                    framework=framework)
        self.inbox = InboxManager(self.client)
        # Only the host Claude Code session may be woken. Any other CLI
        # started from inside Claude inherits that session's socket, and
        # pushing there would wake a session this agent is not part of.
        self.push = (SocketNotifier.from_env(agent_id)
                     if framework in WAKEABLE_FRAMEWORKS and not attach
                     else SocketNotifier(None, None, agent_id, enabled=False))
        self.push.drain_tool = DRAIN_HINT
        self.inbox.set_push_notifier(self.push)
        self.mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS,
                           lifespan=self._lifespan)
        register_tools(self.mcp, self.jobs,
                       None if attach else self._piggyback)

    async def _piggyback(self, result):
        # Looked up per call: the inbox is replaced after a server restart.
        result = await self.inbox.piggyback(result)
        # Job events are wakeups only; the durable _job_notices carry them.
        mail = [m for m in result.pop(INBOX_KEY, [])
                if m.get("event") not in JOB_EVENTS]
        return dict(result, **{INBOX_KEY: mail}) if mail else result

    def _register(self) -> bool:
        try:
            resp = self.client.register()
        except Exception as exc:
            logger.error("cannot register with Pluto: %s", exc)
            return False
        if resp.get("status") != "ok":
            logger.error("registration failed: %s", resp)
            return False
        # A live holder of the name makes Pluto rename us; own jobs under
        # the id peers can actually reach.
        self.jobs.agent_id = self.client.agent_id
        self.push.set_agent_id(self.client.agent_id)
        return True

    async def _watch_epoch(self) -> None:
        """Re-register after a server restart: job ownership must not be
        tied to one server run."""
        while True:
            await asyncio.sleep(EPOCH_CHECK_S)
            epoch = await asyncio.to_thread(self.client.fetch_server_epoch)
            if not epoch or epoch == self.client.server_epoch:
                continue
            await self.inbox.stop()
            if await asyncio.to_thread(self._register):
                self.inbox = InboxManager(self.client)
                self.inbox.set_push_notifier(self.push)
                self.inbox.start()

    @asynccontextmanager
    async def _lifespan(self, _server):
        watcher = None
        registered = False
        if not self.attach:
            registered = await asyncio.to_thread(self._register)
            if registered:
                self.inbox.start()
            watcher = asyncio.create_task(self._watch_epoch())
        try:
            yield {}
        finally:
            if watcher:
                watcher.cancel()
            if not self.attach:
                await self.inbox.stop()
            await self.push.aclose()
            if self.client.token:
                try:
                    await asyncio.to_thread(self.client.unregister)
                except Exception as exc:
                    logger.debug("unregister failed: %s", exc)

    def run(self) -> None:
        self.mcp.run(transport="stdio")
