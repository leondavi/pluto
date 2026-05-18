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
import os
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from agent_mcp_friend.inbox import InboxManager
from agent_mcp_friend.lock_manager import LockManager
from agent_mcp_friend.notifier import Notifier
from pluto_client import PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.tools")


def _mcp_inherited() -> Optional[bool]:
    """Read the PLUTO_MCP_INHERITED env var as a tri-state.

    Returned values:
      - ``True``  → host has explicitly advertised that subagents inherit
        this MCP server (env var set to a truthy value).
      - ``False`` → host has explicitly advertised that subagents do NOT
        inherit (env var set to a falsy value).
      - ``None``  → unknown (env var not set); fall back to best-effort
        watcher behavior and let the agent observe the outcome.

    Truthy: "1", "true", "yes", "on" (case-insensitive). Falsy: "0",
    "false", "no", "off".
    """
    raw = os.environ.get("PLUTO_MCP_INHERITED")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def register_tools(
    mcp: FastMCP,
    client: PlutoHttpClient,
    inbox: InboxManager,
    lock_mgr: LockManager,
    wait_timeout_s: int = 300,
    notifier: Optional[Notifier] = None,
    server: Optional[Any] = None,
) -> None:
    """Register every Pluto tool on *mcp*.

    Captures *client*, *inbox*, and *lock_mgr* in closures so each tool
    can talk to the same long-lived components. *wait_timeout_s* sets
    the default block duration of ``pluto_wait_for_messages``.
    """

    async def _run(fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _bind_session() -> None:
        """Capture the live MCP ServerSession so background paths (the
        inbox peek loop) can fire notifications without a request
        context. Safe to call from any tool body; re-binds on every
        call so reconnects update the reference. No-op if the notifier
        is absent or the FastMCP request context is unavailable.
        """
        if notifier is None:
            return
        try:
            ctx = mcp.get_context()
            req_ctx = getattr(ctx, "request_context", None)
            session = getattr(req_ctx, "session", None) if req_ctx else None
            if session is not None:
                notifier.bind_session(session)
        except Exception:
            # Outside an active request — fine, nothing to bind.
            pass

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
        _bind_session()
        messages = await inbox.drain()
        return {"messages": messages, "count": len(messages)}

    @mcp.tool(
        name="pluto_pop",
        description=(
            "Pop ONE message from the inbox and ack it. Pipeline / "
            "event-driven counterpart to pluto_recv: pluto_recv drains "
            "the whole buffer, pluto_pop returns a single message plus a "
            "'remaining' count so the caller can drive a one-message-per-"
            "event loop."
            "\n\nUsage pattern: on each inbox notification (or each "
            "pluto_inbox_watch wake), call pluto_pop, process the one "
            "message, then if 'remaining' > 0 call pluto_pop again, "
            "otherwise wait for the next notification."
            "\n\nIf the buffer is empty, returns {message: null, "
            "remaining: 0}. Pass wait_s>0 to block up to that many "
            "seconds for the next arrival before giving up."
            "\n\nFor this tool to be the sole consumer of the inbox you "
            "must first call pluto_set_delivery_mode('single') — "
            "otherwise unrelated Pluto tool calls will bulk-drain the "
            "buffer via the usual piggyback path."
        ),
    )
    async def pluto_pop(wait_s: float = 0.0) -> dict:
        _bind_session()
        msg, remaining = await inbox.pop_one(wait_s=float(wait_s))
        return {
            "message": msg,
            "remaining": remaining,
            "empty": msg is None,
            "delivery_mode": inbox.delivery_mode,
        }

    @mcp.tool(
        name="pluto_set_delivery_mode",
        description=(
            "Switch how the inbox surfaces messages to this agent. Modes:"
            "\n  • 'batch'  (default) — pluto_recv and the _pluto_inbox "
            "piggyback on every Pluto tool result return ALL pending "
            "messages at once. Right for turn-driven / interactive work."
            "\n  • 'single' — piggyback attaches at most one message "
            "(plus _pluto_inbox_remaining) and pluto_pop is the canonical "
            "consumer. Right for pipeline / event-driven work where each "
            "message represents a discrete unit to process."
            "\n\nReturns {delivery_mode: <effective_mode>}; invalid mode "
            "strings are ignored and the current mode is returned."
        ),
    )
    async def pluto_set_delivery_mode(mode: str) -> dict:
        _bind_session()
        effective = inbox.set_delivery_mode(mode)
        return {"delivery_mode": effective}

    _wait_default = int(wait_timeout_s)

    @mcp.tool(
        name="pluto_wait_for_messages",
        description=(
            f"Block until at least one Pluto message arrives, or until "
            f"timeout_s seconds elapse (default {_wait_default}). Returns "
            f"the drained-and-acked messages, or an empty list on timeout."
            f"\n\nRecommended usage in Claude Code: spawn a background Task "
            f"with run_in_background=true and the prompt "
            f"'Call pluto_wait_for_messages({_wait_default}) and return its "
            f"result'. The main agent stays responsive to the user; when "
            f"the Task completes, its result (the messages) appears in the "
            f"next turn — process them, then spawn another Task to keep "
            f"watching."
        ),
    )
    async def pluto_wait_for_messages(timeout_s: int = _wait_default) -> dict:
        _bind_session()
        messages = await inbox.wait_for_messages(timeout_s=float(timeout_s))
        return {"messages": messages, "count": len(messages)}

    @mcp.tool(
        name="pluto_inbox_watch",
        description=(
            "Single-slice inbox watcher. Blocks for up to wait_timeout_s "
            "seconds (default = server wait timeout) waiting for fresh "
            "messages, then returns. By default the tool call returns "
            "inside one slice so the wrapping subagent's tool-call "
            "stream stays visible to Claude Code's 600 s watchdog; pass "
            "max_total_s > wait_timeout_s only on clients without that "
            "watchdog."
            "\n\nIdempotent dedupe: a second concurrent call for the "
            "same inbox_id returns {already_watching: true} without "
            "stacking a second loop — the existing waiter keeps running."
            "\n\nMode: drain=true (default) pops messages off the buffer "
            "and acks them server-side; drain=false returns a "
            "non-consuming snapshot of what's currently buffered, leaving "
            "the messages in place for the parent's pluto_recv to drain. "
            "Watcher subagents on Claude Code (where the subagent inherits "
            "the parent's MCP server) MUST pass drain=false, otherwise "
            "they steal the parent's inbox."
        ),
    )
    async def pluto_inbox_watch(
        inbox_id: str = "default",
        wait_timeout_s: Optional[int] = None,
        max_total_s: Optional[int] = None,
        drain: bool = True,
    ) -> dict:
        _bind_session()
        slice_s = float(wait_timeout_s) if wait_timeout_s else float(_wait_default)
        # Default the total budget to the slice so the tool call returns
        # inside one slice (≤ wait_timeout_s seconds). Callers on clients
        # without a stream-silence watchdog can pass a larger max_total_s
        # explicitly to get the multi-slice durable-loop behavior.
        total_s = float(max_total_s) if max_total_s else slice_s
        resp = await inbox.watch_durable(
            inbox_id=inbox_id,
            wait_timeout_s=slice_s,
            max_total_s=total_s,
            drain=drain,
        )
        resp.setdefault("drain", drain)
        # Skip piggyback (which would drain+ack the buffer) when the
        # caller asked for peek-mode. The snapshot in resp["messages"]
        # already reflects what's currently buffered; the parent's
        # pluto_recv owns the actual drain.
        if not drain:
            return resp
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_heartbeat",
        description=(
            "Cheap liveness probe. Returns immediately with "
            "{ok: true, ts, agent_id, connected, mcp_inherited}. No "
            "network call. Use this between pluto_inbox_watch calls (or "
            "any other long-running work) to demonstrate stream activity "
            "to Claude Code's 600 s stream-silence watchdog without "
            "doing meaningful work."
        ),
    )
    async def pluto_heartbeat() -> dict:
        _bind_session()
        return {
            "ok": True,
            "ts": time.time(),
            "agent_id": client.agent_id,
            "connected": bool(client.token),
            "mcp_inherited": _mcp_inherited(),
            "notifications_enabled": notifier.enabled if notifier else False,
        }

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

    @mcp.tool(
        name="pluto_snapshot_self",
        description=(
            "Capture a self-restorable snapshot of this agent's Pluto state. "
            "Returns {plut: {...}, prompt: '...'} where 'plut' is the "
            "coordination-state JSON the agent should write to "
            "<output_dir>/<agent_id>.plut and 'prompt' is a markdown "
            "recovery prompt for <agent_id>-recovery.md. Pass output_dir "
            "(defaults to /tmp/pluto/snapshots) to also write both files "
            "to disk and have the file paths returned."
        ),
    )
    async def pluto_snapshot_self(
        output_dir: Optional[str] = None,
    ) -> dict:
        if output_dir:
            plut_path, md_path = await _run(client.save_snapshot_files, output_dir)
            resp = {
                "status": "ok",
                "plut_path": plut_path,
                "prompt_path": md_path,
                "message": (
                    f"Snapshot saved. To restore later, run "
                    f"PlutoMCPFriend with --restore {plut_path}"
                ),
            }
        else:
            snap = await _run(client.snapshot_self)
            resp = {"status": "ok", **snap}
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_restore_from_snapshot",
        description=(
            "Restore a previously saved Pluto state from a .plut payload. "
            "Pass either 'plut' (the parsed JSON dict) or 'plut_path' "
            "(file path). Agent must already be registered via the "
            "MCP friend launcher. After restore, status becomes "
            "'recovered_from_file'. Returns reclaimed_locks and lost_locks."
        ),
    )
    async def pluto_restore_from_snapshot(
        plut: Optional[dict] = None,
        plut_path: Optional[str] = None,
    ) -> dict:
        if plut is None and plut_path:
            import json as _json
            with open(plut_path, "r", encoding="utf-8") as f:
                plut = _json.load(f)
        if not isinstance(plut, dict):
            return {"status": "error", "reason": "missing plut or plut_path"}
        resp = await _run(client.restore_from_snapshot, plut)
        return await inbox.piggyback(resp)

    @mcp.tool(
        name="pluto_session",
        description=(
            "Read-only diagnostic. Returns this MCP adapter's "
            "registration state with the Pluto server: agent_id, "
            "host:port, connected (bool), and the buffered inbox "
            "depth. Cheap — no network call. Use as a 'is MCP "
            "alive?' probe; if THIS tool returns an error, the MCP "
            "transport itself is dead and the user must run /mcp in "
            "Claude Code to refresh."
        ),
    )
    async def pluto_session() -> dict:
        _bind_session()
        buffered = await inbox.peek_only()
        inherited = _mcp_inherited()
        watchers = inbox.active_watchers_snapshot()
        out = {
            "agent_id": client.agent_id,
            "host": client.host,
            "http_port": client.http_port,
            "base_url": client.base_url,
            "connected": bool(client.token),
            "buffered_messages": len(buffered),
            "delivery_mode": inbox.delivery_mode,
            "mcp_inherited": inherited,
            "watcher_available": inherited is not False,
            # Legacy list-shaped field, kept for callers that already
            # parse it. New callers should read ``watchers`` instead.
            "active_watchers": watchers["ids"],
            "watchers": watchers,
            "server_epoch": getattr(client, "server_epoch", None),
        }
        if notifier is not None:
            out["notifications"] = notifier.summary()
        return out

    @mcp.tool(
        name="pluto_health",
        description=(
            "End-to-end health probe. Reports MCP-adapter state (always 'ok' "
            "if this tool returns), a live HTTP ping to the Pluto server, "
            "the background peek-loop liveness, and the most recent "
            "auto-snapshot info. Diagnosis: pluto_server != 'ok' → server "
            "unreachable; agent_registered false → session lost (relaunch "
            "with --resume); peek_loop.unrecoverable → terminal session "
            "loss (relaunch); peek_loop.stalled → HTTP listener wedged."
        ),
    )
    async def pluto_health() -> dict:
        import urllib.error
        import urllib.request

        server_status = "unknown"
        server_version: Optional[str] = None
        live_epoch: Optional[str] = None
        url = f"http://{client.host}:{client.http_port}/health"
        try:
            def _probe() -> dict:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    import json as _json
                    body = resp.read().decode("utf-8")
                    try:
                        return _json.loads(body)
                    except Exception:
                        return {"status": "ok", "raw": body[:200]}
            probe = await _run(_probe)
            server_status = "ok"
            if isinstance(probe, dict):
                server_version = probe.get("version")
                live_epoch = probe.get("server_epoch")
        except urllib.error.URLError as exc:
            server_status = f"unreachable: {exc.reason}"
        except Exception as exc:
            server_status = f"error: {exc}"

        cached_epoch = getattr(client, "server_epoch", None)
        epoch_mismatch = (
            cached_epoch is not None
            and live_epoch is not None
            and cached_epoch != live_epoch
        )

        out: dict = {
            "mcp_adapter": "ok",
            "pluto_server": server_status,
            "agent_registered": bool(client.token) and not epoch_mismatch,
            "agent_id": client.agent_id,
            "host": client.host,
            "http_port": client.http_port,
        }
        if server_version:
            out["server_version"] = server_version
        if live_epoch:
            out["server_epoch"] = live_epoch
        if cached_epoch:
            out["session_epoch"] = cached_epoch
        if epoch_mismatch:
            out["server_restarted"] = True
            out["recovery_hint"] = (
                "Server epoch changed — Pluto was restarted/cleaned. "
                "Your token is dead. Relaunch PlutoMCPFriend "
                "(./PlutoMCPFriend.sh --agent-id "
                f"{client.agent_id} --resume) to re-register."
            )
        if server is not None and getattr(server, "autosnap", None) is not None:
            autosnap = server.autosnap
            out["auto_snapshot"] = {
                "enabled": True,
                "interval_s": autosnap.interval_s,
                "last_snapshot_at": autosnap.last_snapshot_at,
                "last_snapshot_path": autosnap.last_snapshot_path,
                "last_error": autosnap.last_error,
            }
        elif server is not None:
            out["auto_snapshot"] = {"enabled": False}

        # Background peek-loop liveness. Lets an agent distinguish
        # "inbox quiet" (loop alive, ok_count growing) from "delivery
        # is wedged" (stalled=true or unrecoverable=true).
        loop_state = inbox.peek_loop_state()
        out["peek_loop"] = loop_state
        # Watcher slot occupancy — feeds the role prompt's "is a watcher
        # already running?" check before spawning a fresh subagent.
        out["watchers"] = inbox.active_watchers_snapshot()
        if loop_state.get("unrecoverable"):
            out["agent_registered"] = False
            out["recovery_hint"] = (
                loop_state.get("unrecoverable_reason")
                or "Peek loop entered unrecoverable state — restart "
                f"./PlutoMCPFriend.sh --agent-id {client.agent_id} --resume"
            )
        elif loop_state.get("stalled"):
            out.setdefault(
                "recovery_hint",
                "Peek loop stalled (no successful peek in >"
                f"{loop_state.get('stall_threshold_s')}s). The Pluto "
                "HTTP listener may be wedged; check server health.",
            )
        return out
