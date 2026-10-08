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
    push: Optional[Any] = None,
) -> None:
    """Register every Pluto tool on *mcp*.

    Captures *client*, *inbox*, and *lock_mgr* in closures so each tool
    can talk to the same long-lived components. *wait_timeout_s* sets
    the default block duration of ``pluto_wait_for_messages``.
    """

    async def _run(fn, *args, **kwargs) -> Any:
        # Every tool's first HTTP hop routes through here, so binding the
        # MCP session at this choke point (plus the explicit calls in the
        # network-free inbox tools) guarantees notifications work no
        # matter which tool the agent happens to call first.
        _bind_session()
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _attach_lost(result: Any) -> Any:
        """Attach any lock-loss events the agent hasn't seen yet as
        ``_pluto_lock_lost`` — a lock whose auto-renew failed is gone,
        and the agent must stop writing to that resource.

        ``take_lost()`` is drain-once, so this MUST run on every tool the
        agent might call in a loop. The inbox tools (``pluto_recv``,
        ``pluto_pop``, ``pluto_wait_for_messages``, ``pluto_heartbeat``)
        deliberately call this *instead of* :func:`_finish`: they own the
        drain themselves, so piggybacking on top of them would pull an
        extra message off the buffer (and in single mode break the
        one-message-per-pop invariant outright).
        """
        lost = lock_mgr.take_lost()
        identity = _identity_notice_once()
        if not lost and identity is None:
            return result
        if isinstance(result, dict):
            result = dict(result)
        else:
            result = {"result": result}
        if lost:
            result["_pluto_lock_lost"] = lost
        if identity is not None:
            result["_pluto_identity"] = identity
        return result

    _identity_announced = [False]

    def _requested_agent_id() -> Optional[str]:
        return getattr(server, "requested_agent_id", None) if server else None

    def _identity_notice_once() -> Optional[str]:
        """One-shot warning when the server renamed this agent at
        registration (requested name held by a live agent). Without it
        the model believes it is the id from the role prompt and peers
        keep messaging a name that routes elsewhere."""
        requested = _requested_agent_id()
        if requested is None or _identity_announced[0]:
            return None
        _identity_announced[0] = True
        return (
            f"IMPORTANT: this session is registered with Pluto as "
            f"'{client.agent_id}', NOT '{requested}' (that name was already "
            f"taken by a live agent). Messages sent to '{requested}' go to "
            f"the other agent. Introduce yourself to peers as "
            f"'{client.agent_id}', or relaunch with a different --agent-id."
        )

    async def _finish(result: Any) -> Any:
        """Terminal wrapper for non-inbox tool results: piggyback pending
        inbox messages, then attach lock-loss events."""
        return _attach_lost(await inbox.piggyback(result))

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
        return await _finish(resp)

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
        return await _finish(resp)

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
        return _attach_lost({"messages": messages, "count": len(messages)})

    @mcp.tool(
        name="pluto_pop",
        description=(
            "Pop and ack ONE inbox message; returns {message, remaining, "
            "empty, delivery_mode}. Pair with delivery mode 'single' so "
            "unrelated tool calls don't bulk-drain the buffer. wait_s>0 "
            "blocks up to that many seconds for the next arrival."
        ),
    )
    async def pluto_pop(wait_s: float = 0.0) -> dict:
        _bind_session()
        msg, remaining = await inbox.pop_one(wait_s=float(wait_s))
        return _attach_lost({
            "message": msg,
            "remaining": remaining,
            "empty": msg is None,
            "delivery_mode": inbox.delivery_mode,
        })

    @mcp.tool(
        name="pluto_set_delivery_mode",
        description=(
            "Set inbox delivery mode: 'batch' (default; drains everything "
            "per pluto_recv/piggyback — turn-driven work) or 'single' "
            "(one message per pluto_pop/piggyback — pipeline work). "
            "Invalid modes return {status: 'error', reason: 'invalid_mode'}."
        ),
    )
    async def pluto_set_delivery_mode(mode: str) -> dict:
        _bind_session()
        try:
            effective = inbox.set_delivery_mode(mode)
        except ValueError:
            return {
                "status": "error",
                "reason": "invalid_mode",
                "valid_modes": ["batch", "single"],
                "delivery_mode": inbox.delivery_mode,
            }
        return {"status": "ok", "delivery_mode": effective}

    _wait_default = int(wait_timeout_s)

    # NOTE: descriptions and schemas must stay config-independent —
    # interpolating _wait_default (in the text OR as a signature default)
    # makes tools/list vary per deployment, breaking prompt-prefix
    # caching across agents. TestCacheStability pins this.
    @mcp.tool(
        name="pluto_wait_for_messages",
        description=(
            "Block until at least one Pluto message arrives or timeout_s "
            "elapses (default: the launcher's --wait-timeout-s). Returns "
            "the drained-and-acked messages; [] on timeout."
        ),
    )
    async def pluto_wait_for_messages(timeout_s: Optional[int] = None) -> dict:
        _bind_session()
        effective = _wait_default if timeout_s is None else int(timeout_s)
        messages = await inbox.wait_for_messages(timeout_s=float(effective))
        return _attach_lost({"messages": messages, "count": len(messages)})

    @mcp.tool(
        name="pluto_inbox_watch",
        description=(
            "Single-slice inbox watcher: blocks up to wait_timeout_s "
            "(default: the launcher's --wait-timeout-s) for fresh "
            "messages, then returns. A concurrent call for the same "
            "inbox_id returns {already_watching: true} instead of "
            "stacking a loop. Watcher subagents sharing the parent's MCP "
            "server MUST pass drain=false (non-consuming snapshot) — "
            "drain=true pops and acks the parent's inbox."
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
        return await _finish(resp)

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
        return _attach_lost({
            "ok": True,
            "ts": time.time(),
            "agent_id": client.agent_id,
            "connected": bool(client.token),
            "mcp_inherited": _mcp_inherited(),
            "notifications_enabled": notifier.enabled if notifier else False,
        })

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
        return await _finish(resp)

    @mcp.tool(
        name="pluto_subscribe",
        description="Subscribe to a topic channel.",
    )
    async def pluto_subscribe(topic: str) -> dict:
        resp = await _run(client.subscribe, topic)
        return await _finish(resp)

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
        return await _finish({"agents": agents})

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
        return await _finish(resp)

    # ── Locks ─────────────────────────────────────────────────────────────

    # wait_ref -> (resource, ttl_ms) for queued acquires with auto_renew, so
    # the lock is put under auto-renewal the moment its grant arrives.
    _pending_waits: dict[str, tuple[str, int]] = {}

    async def _on_grant(messages: list[dict]) -> None:
        for m in messages:
            pending = _pending_waits.pop(str(m.get("wait_ref")), None)
            if pending is None:
                continue
            if m.get("event") == "lock_granted" and m.get("lock_ref"):
                await lock_mgr.register(m["lock_ref"], *pending)

    inbox.on_new_message(_on_grant)

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
        if auto_renew and resp.get("status") == "ok" and resp.get("lock_ref"):
            await lock_mgr.register(resp["lock_ref"], resource, ttl_ms)
        elif auto_renew and resp.get("status") == "wait" and resp.get("wait_ref"):
            # Granted later via a lock_granted inbox event; see _on_grant.
            _pending_waits[resp["wait_ref"]] = (resource, ttl_ms)
        return await _finish(resp)

    @mcp.tool(
        name="pluto_lock_release",
        description="Release a lock previously acquired via pluto_lock_acquire.",
    )
    async def pluto_lock_release(lock_ref: str) -> dict:
        await lock_mgr.unregister(lock_ref)
        resp = await _run(client.release, lock_ref)
        return await _finish(resp)

    @mcp.tool(
        name="pluto_lock_renew",
        description=(
            "Manually renew a lock TTL. Usually unnecessary — locks are "
            "auto-renewed when acquired via pluto_lock_acquire(auto_renew=true)."
        ),
    )
    async def pluto_lock_renew(lock_ref: str, ttl_ms: int = 30000) -> dict:
        resp = await _run(client.renew, lock_ref, ttl_ms)
        return await _finish(resp)

    @mcp.tool(
        name="pluto_lock_info",
        description=(
            "Inspect lock state for a resource: current holders, last "
            "holder, queue length, and the FIFO wait queue."
        ),
    )
    async def pluto_lock_info(resource: str) -> dict:
        resp = await _run(client.resource_info, resource)
        return await _finish(resp)

    @mcp.tool(
        name="pluto_list_locks",
        description="List every active lock on the Pluto server.",
    )
    async def pluto_list_locks() -> dict:
        locks = await _run(client.list_locks)
        return await _finish({"locks": locks})

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
        # Keep the assignee's status updates for this task actionable here.
        if isinstance(resp, dict) and resp.get("task_id"):
            inbox.note_assigned_task(resp["task_id"])
        return await _finish(resp)

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
        return await _finish(resp)

    @mcp.tool(
        name="pluto_task_list",
        description="List tasks, optionally filtered by assignee or status.",
    )
    async def pluto_task_list(
        assignee: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        tasks = await _run(client.task_list, assignee, status)
        return await _finish({"tasks": tasks})

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
        return await _finish(resp)

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
        return await _finish(resp)

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
        return await _finish(resp)

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
            "requested_agent_id": _requested_agent_id(),
            "renamed_by_server": _requested_agent_id() is not None,
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
        if push is not None:
            out["push"] = push.summary()
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
            "requested_agent_id": _requested_agent_id(),
            "renamed_by_server": _requested_agent_id() is not None,
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
        if push is not None:
            out["push"] = push.summary()
        # Held vs lost auto-renewed locks. lost_total > 0 means an
        # auto-renew failed at some point — the affected lock_refs were
        # (or will be) delivered via _pluto_lock_lost on tool results.
        out["locks"] = {
            "held": lock_mgr.held_locks(),
            **lock_mgr.lost_summary(),
        }
        # Watcher slot occupancy — feeds the role prompt's "is a watcher
        # already running?" check before spawning a fresh subagent.
        out["watchers"] = inbox.active_watchers_snapshot()
        if loop_state.get("unrecoverable"):
            out["agent_registered"] = False
            # setdefault, not assignment: an epoch mismatch is the *cause*
            # of the 401s that trip the unrecoverable threshold, so its
            # "server was restarted/cleaned" hint is the more actionable
            # of the two and must not be clobbered here.
            out.setdefault(
                "recovery_hint",
                loop_state.get("unrecoverable_reason")
                or "Peek loop entered unrecoverable state — restart "
                f"./PlutoMCPFriend.sh --agent-id {client.agent_id} --resume",
            )
        elif loop_state.get("stalled"):
            out.setdefault(
                "recovery_hint",
                "Peek loop stalled (no successful peek in >"
                f"{loop_state.get('stall_threshold_s')}s). The Pluto "
                "HTTP listener may be wedged; check server health.",
            )
        return out
