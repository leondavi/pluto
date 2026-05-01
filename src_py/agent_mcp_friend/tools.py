"""MCP tool registrations for PlutoMCPFriend.

Every tool is a thin wrapper over a ``PlutoHttpClient`` method. The
wrapper injects the session token (so the agent never has to handle it),
runs the underlying blocking HTTP call in a thread, and pipes the result
through :meth:`InboxManager.piggyback` so any pending inbox messages
land on the result as ``_pluto_inbox``.

Tool names follow the ``pluto_*`` convention so they don't collide with
unrelated MCP servers an agent might have configured simultaneously.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from pluto_client import PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.tools")


def register_tools(
    mcp: FastMCP,
    client: PlutoHttpClient,
    inbox: InboxManager,
    lock_mgr: LockManager,
) -> None:
    """Register every Pluto tool on *mcp*.

    Captures *client*, *inbox*, and *lock_mgr* in closures so each tool
    can talk to the same long-lived components.
    """

    async def _run(fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── Messaging ─────────────────────────────────────────────────────────

    @mcp.tool(
        name="pluto_send",
        description=(
            "Send a direct message to another Pluto agent. The recipient "
            "must be registered with the same Pluto server. Returns the "
            "raw server response with status='ok' on success."
        ),
    )
    async def pluto_send(to: str, payload: dict) -> dict:
        resp = await _run(client.send, to, payload)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_broadcast",
        description=(
            "Broadcast a message to every connected Pluto agent. Use "
            "sparingly — direct messages are preferred when the audience "
            "is known."
        ),
    )
    async def pluto_broadcast(payload: dict) -> dict:
        resp = await _run(client.broadcast, payload)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_recv",
        description=(
            "Drain pending Pluto inbox messages addressed to this agent. "
            "Call this at the start of any turn where you have not "
            "already invoked another Pluto tool. Returns "
            "{'messages': [...]} where each message has at least 'event', "
            "'from', 'payload', and 'seq_token' fields. "
            "Returns immediately even if no messages are pending — use "
            "pluto_wait_for_messages if you want to block until one arrives."
        ),
    )
    async def pluto_recv() -> dict:
        messages = await inbox.drain()
        return {"messages": messages, "count": len(messages)}

    @mcp.tool(
        name="pluto_wait_for_messages",
        description=(
            "Block until at least one Pluto message arrives, or until "
            "timeout_s seconds elapse (default 300 = 5 minutes). Returns "
            "the drained-and-acked messages, or an empty list on timeout."
            "\n\nUsage patterns:"
            "\n  • Direct call (Cursor / Aider / any MCP client): call this "
            "with a short timeout (e.g. 30) at the tail of every turn."
            "\n  • Background sub-agent (Claude Code): spawn a background "
            "Task with run_in_background=true whose prompt is 'Call "
            "pluto_wait_for_messages(300) and return its result'. The main "
            "agent stays responsive to the user; when the Task completes, "
            "its result (the messages) appears in the next turn — process "
            "them, then spawn another Task to keep watching."
        ),
    )
    async def pluto_wait_for_messages(timeout_s: int = 300) -> dict:
        messages = await inbox.wait_for_messages(timeout_s=float(timeout_s))
        return {"messages": messages, "count": len(messages)}

    @mcp.tool(
        name="pluto_publish",
        description="Publish a message to a topic channel.",
    )
    async def pluto_publish(topic: str, payload: dict) -> dict:
        if not client.token:
            return {"status": "error", "reason": "not_registered"}
        resp = await _run(
            client._post,
            "/agents/publish",
            {"token": client.token, "topic": topic, "payload": payload},
        )
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_subscribe",
        description="Subscribe to a topic channel.",
    )
    async def pluto_subscribe(topic: str) -> dict:
        resp = await _run(client.subscribe, topic)
        return await inbox.piggyback(resp)

    # ── Agent discovery ───────────────────────────────────────────────────

    @mcp.tool(
        name="pluto_list_agents",
        description=(
            "List all currently connected Pluto agents with their status, "
            "attributes, and subscriptions."
        ),
    )
    async def pluto_list_agents() -> dict:
        agents = await _run(client.list_agents_detailed)
        return await inbox.piggyback({"agents": agents})

    @mcp.tool(
        name="pluto_find_agents",
        description=(
            "Find agents by attribute filter. The filter is a dict of "
            "attribute key/value pairs that registered agents must match."
        ),
    )
    async def pluto_find_agents(filter: Optional[dict] = None) -> dict:
        body = {"filter": filter or {}}
        resp = await _run(client._post, "/agents/find", body)
        return await inbox.piggyback(resp)

    # ── Locks ─────────────────────────────────────────────────────────────

    @mcp.tool(
        name="pluto_lock_acquire",
        description=(
            "Acquire a lock on a resource. mode is 'write' (exclusive) or "
            "'read' (shared). ttl_ms is the lease duration. If "
            "auto_renew=true (default) the wrapper renews the lock at "
            "TTL/2 until pluto_lock_release is called or the session ends."
            "\n\nResponse: status='ok' with lock_ref + fencing_token if "
            "granted; status='wait' with wait_ref if queued (the lock will "
            "arrive later as a Pluto message and appear in _pluto_inbox)."
        ),
    )
    async def pluto_lock_acquire(
        resource: str,
        mode: str = "write",
        ttl_ms: int = 30000,
        max_wait_ms: Optional[int] = None,
        auto_renew: bool = True,
    ) -> dict:
        resp = await _run(client.acquire, resource, mode, ttl_ms, max_wait_ms)
        if (
            auto_renew
            and resp.get("status") == "ok"
            and resp.get("lock_ref")
        ):
            await lock_mgr.register(resp["lock_ref"], resource, ttl_ms)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_lock_release",
        description="Release a lock previously acquired via pluto_lock_acquire.",
    )
    async def pluto_lock_release(lock_ref: str) -> dict:
        await lock_mgr.unregister(lock_ref)
        resp = await _run(client.release, lock_ref)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_lock_renew",
        description=(
            "Manually renew a lock TTL. Usually unnecessary — locks are "
            "auto-renewed when acquired via pluto_lock_acquire(auto_renew=true)."
        ),
    )
    async def pluto_lock_renew(lock_ref: str, ttl_ms: int = 30000) -> dict:
        resp = await _run(client.renew, lock_ref, ttl_ms)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_lock_info",
        description=(
            "Inspect lock state for a resource: current holders, last "
            "holder, queue length, and the FIFO wait queue."
        ),
    )
    async def pluto_lock_info(resource: str) -> dict:
        resp = await _run(client.resource_info, resource)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_list_locks",
        description="List every active lock on the Pluto server.",
    )
    async def pluto_list_locks() -> dict:
        locks = await _run(client.list_locks)
        return await inbox.piggyback({"locks": locks})

    # ── Tasks ─────────────────────────────────────────────────────────────

    @mcp.tool(
        name="pluto_task_assign",
        description=(
            "Assign a task to another agent. The recipient receives a "
            "task_assigned message with description and payload."
        ),
    )
    async def pluto_task_assign(
        assignee: str,
        description: str = "",
        payload: Optional[dict] = None,
    ) -> dict:
        resp = await _run(client.task_assign, assignee, description, payload or {})
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_task_update",
        description=(
            "Update task status. Common transitions: 'in_progress' when "
            "starting work, 'completed' with a result on success, 'failed' "
            "with a reason on error."
        ),
    )
    async def pluto_task_update(
        task_id: str,
        status: str,
        result: Optional[dict] = None,
    ) -> dict:
        resp = await _run(client.task_update, task_id, status, result or {})
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_task_list",
        description="List tasks, optionally filtered by assignee or status.",
    )
    async def pluto_task_list(
        assignee: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        tasks = await _run(client.task_list, assignee, status)
        return await inbox.piggyback({"tasks": tasks})

    # ── Status / introspection ────────────────────────────────────────────

    @mcp.tool(
        name="pluto_set_status",
        description=(
            "Set this agent's custom status string (e.g. 'busy', 'idle', "
            "'reviewing-pr-42'). Visible to other agents via "
            "pluto_list_agents."
        ),
    )
    async def pluto_set_status(custom_status: str) -> dict:
        resp = await _run(client.set_status, custom_status)
        return await inbox.piggyback(resp)
