"""Inbox loop and tool-result piggyback for PlutoMCPFriend.

The :class:`InboxManager` runs a background coroutine that calls
``PlutoHttpClient.peek`` on a fixed cadence and buffers actionable
messages in memory. Whenever an MCP tool returns, we attach the buffered
messages to the result under ``_pluto_inbox`` and ack them — so any
Pluto-related tool call doubles as an inbox drain. The role prompt
teaches the agent to look for ``_pluto_inbox`` and process any messages
it finds before continuing.

This delivery model needs no agent-side polling and no PTY trickery; the
agent's normal tool-call cadence is the delivery channel.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any

from pluto_client import PlutoError, PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.inbox")

# Mirror agent_friend.pluto_connection: events the agent must never see.
_NOISE_PAYLOAD_EVENTS = {"delivery_ack", "status_update", "heartbeat"}
_ACTIONABLE_EVENTS = {"message", "broadcast", "task_assigned", "topic_message",
                      "lock_granted", "wait_timeout"}


# Envelope fields the agent actually needs. Server messages also carry
# msg_id and seq (duplicates of seq_token for identity/ordering) — those
# are trimmed at the presentation edge to save tokens. The internal
# buffer keeps full messages (ack accounting reads them).
_ENVELOPE_KEEP = ("event", "from", "payload", "seq_token", "request_id",
                  "topic", "task_id", "wait_ref", "lock_ref", "fencing_token",
                  "resource")


def _slim(msg: dict) -> dict:
    return {k: msg[k] for k in _ENVELOPE_KEEP if k in msg}


def _slim_all(messages: list[dict]) -> list[dict]:
    return [_slim(m) for m in messages]


def _is_noise(msg: dict) -> bool:
    """Context-free noise: events no agent should ever be woken for."""
    if msg.get("event") not in _ACTIONABLE_EVENTS:
        return True
    payload = msg.get("payload")
    if isinstance(payload, dict) and payload.get("event") in _NOISE_PAYLOAD_EVENTS:
        return True
    if _is_server_reminder(msg):
        return True
    return False


def _is_server_reminder(msg: dict) -> bool:
    """The server's periodic "keep pinging" broadcast. It targets TCP
    sessions; servers before v0.5.0 could leak it to HTTP agents, and it
    must never wake an MCP-backed agent."""
    payload = msg.get("payload")
    return (
        msg.get("event") == "broadcast"
        and msg.get("from") == "pluto"
        and isinstance(payload, dict)
        and payload.get("type") == "heartbeat_reminder"
    )


#: Env var selecting which team-wide task broadcasts reach the agent.
#: "relevant" (default): only tasks assigned TO this agent and status
#: updates on tasks assigned BY this agent. "all": every task event.
_TASK_EVENTS_ENV = "PLUTO_MCP_TASK_EVENTS"


def _task_events_mode_from_env() -> str:
    raw = (os.environ.get(_TASK_EVENTS_ENV) or "").strip().lower()
    return "all" if raw == "all" else "relevant"


class InboxManager:
    """Background peek loop + per-tool-result piggyback buffer."""

    PEEK_INTERVAL_S = 1.0
    SESSION_RETRY_BACKOFF_S = 5.0
    # Hard ceiling on a single peek RTT. asyncio.wait_for cancellation does
    # not actually kill the to_thread worker (Python limitation), but it
    # unblocks the peek loop so it can warn, sleep, and retry instead of
    # silently wedging on a hung HTTP socket.
    PEEK_HARD_TIMEOUT_S = 15.0
    # A peek_loop_age_s above this triggers the "stalled" diagnostic in
    # pluto_health. Tracks wall-clock since the last successful peek.
    PEEK_STALL_WARN_S = 10.0
    # After this many consecutive session-lost errors (401/404/
    # session_not_found), give up retrying. PlutoMCPFriend deliberately
    # does NOT auto-re-register (commit 8d0ca13 contract) — an MCP friend
    # is tied to a Claude Code session, so re-registering silently would
    # mask identity loss. Instead we set _unrecoverable and surface it
    # via pluto_health so the agent gets an actionable error.
    SESSION_LOST_GIVE_UP_AFTER = 3
    # Grace window after a watch_durable slice returns, during which the
    # watcher_id stays in active_watchers. Lets a looping subagent re-enter
    # without flapping the count, and lets a *peer* subagent (the bug case
    # we're guarding against) see "already watching" between slices.
    WATCHER_GRACE_S = 5.0

    def __init__(self, client: PlutoHttpClient):
        self._client = client
        # The server announces every task assignment and status change to
        # every agent. Waking each agent for each team-wide task event
        # burns a model turn per event per agent, so by default only the
        # events this agent is party to are actionable; the rest are
        # settled as noise. See _TASK_EVENTS_ENV.
        self._task_events_mode: str = _task_events_mode_from_env()
        self._assigned_by_me: set[str] = set()
        self._buffered: list[dict] = []
        self._seen_seqs: set[int] = set()
        self._last_acked_seq: int = 0
        # Highest seq ever observed from peek (noise or actionable).
        # Combined with the buffer head this defines the safe ack cursor:
        # everything at-or-below it has either been delivered to the
        # agent or classified noise.
        self._max_seen_seq: int = 0
        # Set when an ack attempt failed; the peek loop retries on the
        # next successful cycle so a transient ack failure heals itself.
        self._ack_retry_needed: bool = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._on_new_message: list = []  # callbacks
        # Set whenever a fresh actionable message lands in the buffer.
        # ``wait_for_messages`` waits on this; ``_absorb`` sets it.
        self._new_message_event = asyncio.Event()
        # Currently-running durable watcher keys (inbox_id strings).
        # Dedupe: a second ``watch_durable`` call for an active key returns
        # ``{already_watching: True}`` immediately instead of stacking loops.
        # Membership extends WATCHER_GRACE_S past the slice end so a peer
        # subagent that calls in the gap between a looping subagent's
        # slices still sees the watcher as active and bounces out.
        self._active_watchers: set[str] = set()
        self._watcher_started_at: dict[str, float] = {}
        self._watcher_evict_tasks: dict[str, asyncio.Task] = {}
        # Delivery-latency telemetry: monotonic timestamps keyed by
        # seq_token, set on _absorb and consumed on _ack_messages.
        self._landed_at: dict[int, float] = {}
        # Optional notifier hook (phase-2). Set via :meth:`set_notifier`.
        self._notifier = None
        # Optional Claude Code push-wakeup hook. Set via
        # :meth:`set_push_notifier`. Fired from _absorb on fresh
        # actionable messages; re-armed by the drain paths.
        self._push = None
        # Peek-loop liveness telemetry. Monotonic ts is used for age math;
        # wall-clock ts is what pluto_health surfaces to the agent.
        self._last_peek_ok_mono: float | None = None
        self._last_peek_ok_wall: float | None = None
        self._last_peek_error: str | None = None
        self._peek_attempts: int = 0
        self._peek_ok_count: int = 0
        # Whether a stall warning has already been emitted for the current
        # stall window — re-armed once the loop succeeds again.
        self._stall_warned: bool = False
        # Terminal session-lost state. PlutoMCPFriend does not auto-
        # re-register; this flag stops the peek loop after repeated 401s
        # so we don't spin the HTTP path forever, and surfaces a clear
        # actionable error through pluto_health.
        self._unrecoverable: bool = False
        self._unrecoverable_reason: str | None = None
        self._consecutive_session_lost: int = 0
        # Delivery mode. "batch" (default) drains the buffer en masse on
        # every piggyback / drain — the historical behavior used by
        # turn-driven agents. "single" pops one message at a time so a
        # pipeline-style agent can drive consumption from push
        # notifications (one notification → one pluto_pop → process →
        # next pluto_pop) without surprise bulk drains via piggyback on
        # unrelated Pluto tool calls.
        self._delivery_mode: str = "batch"

    # ── Task-event relevance ──────────────────────────────────────────────

    def note_assigned_task(self, task_id: str) -> None:
        """Record a task this agent assigned, so the status updates the
        assignee broadcasts for it stay actionable here."""
        if task_id:
            self._assigned_by_me.add(str(task_id))

    def _is_irrelevant_task_event(self, msg: dict) -> bool:
        """True for a task broadcast this agent is not party to."""
        if self._task_events_mode == "all" or msg.get("event") != "broadcast":
            return False
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return False
        kind = payload.get("event")
        if kind == "task_assigned":
            return payload.get("assignee") != self._client.agent_id
        if kind == "task_updated":
            return str(payload.get("task_id")) not in self._assigned_by_me
        return False

    def _should_skip(self, msg: dict) -> bool:
        return _is_noise(msg) or self._is_irrelevant_task_event(msg)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="pluto-inbox")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        # Cancel pending watcher-grace evictions so no tasks outlive the
        # manager and leak warnings at event-loop teardown.
        for t in self._watcher_evict_tasks.values():
            if not t.done():
                t.cancel()
        self._watcher_evict_tasks.clear()
        self._active_watchers.clear()
        self._watcher_started_at.clear()

    def set_notifier(self, notifier) -> None:
        """Attach a :class:`Notifier` for phase-2 MCP notifications and
        delivery-latency telemetry. Optional — no-op if unset."""
        self._notifier = notifier

    def set_push_notifier(self, push) -> None:
        """Attach a :class:`SocketNotifier` that wakes the host Claude
        Code session when fresh messages land. Optional — no-op if unset."""
        self._push = push

    def set_delivery_mode(self, mode: str) -> str:
        """Switch delivery mode between ``"batch"`` and ``"single"``.

        Returns the value set. Raises :class:`ValueError` on anything
        else — a silently ignored typo ("singel") would leave the agent
        believing it changed modes when it didn't.

        * ``"batch"`` (default) — every ``piggyback`` / ``drain`` empties
          the buffer; every Pluto tool result carries any pending
          messages under ``_pluto_inbox``.
        * ``"single"`` — ``piggyback`` attaches at most one message (the
          head of the queue) plus a ``_pluto_inbox_remaining`` counter,
          and ``drain`` becomes a thin wrapper over :meth:`pop_one`.
          Agents drive consumption explicitly via ``pluto_pop``, one
          message per call.
        """
        if mode not in ("batch", "single"):
            raise ValueError(f"invalid delivery mode {mode!r}")
        self._delivery_mode = mode
        return mode

    @property
    def delivery_mode(self) -> str:
        return self._delivery_mode

    def on_new_message(self, callback) -> None:
        """Register a coroutine ``callback(messages: list[dict])`` invoked
        when fresh actionable messages arrive in the buffer.

        Used by :class:`PlutoMCPServer` to fire ``notifications/resources/
        updated`` for ``pluto://inbox`` so subscribed clients can refresh.
        """
        self._on_new_message.append(callback)

    # ── Public API used by tools.py ───────────────────────────────────────

    async def piggyback(self, result: Any) -> Any:
        """Wrap *result* with any pending inbox messages and ack them.

        - If *result* is a ``dict``, the messages are added under the
          ``_pluto_inbox`` key (overwriting any existing key by that name).
        - Otherwise the original result is returned wrapped:
          ``{"result": <original>, "_pluto_inbox": [...]}``.

        In ``"single"`` delivery mode the wrap attaches at most one
        message (the head of the queue) and exposes a
        ``_pluto_inbox_remaining`` counter so the agent knows whether to
        loop on ``pluto_pop``. This preserves the one-at-a-time invariant
        across unrelated Pluto tool calls — without it, every
        ``pluto_send`` / ``pluto_lock_*`` etc. would silently bulk-drain
        the buffer behind the pop loop's back.

        Acks fire as soon as messages are placed on the result so even if
        the agent ignores them the server inbox is drained — at-least-once
        delivery is preserved on the wire (peek will resurface them after
        a crash before the ack lands).
        """
        single = self._delivery_mode == "single"
        remaining = 0
        async with self._lock:
            if not self._buffered:
                return result
            if single:
                messages = [self._buffered.pop(0)]
                remaining = len(self._buffered)
                if not self._buffered:
                    self._new_message_event.clear()
            else:
                messages = list(self._buffered)
                self._buffered.clear()
                self._new_message_event.clear()
        if not remaining:
            self._push_drained()

        if isinstance(result, dict):
            wrapped = dict(result)
            wrapped["_pluto_inbox"] = _slim_all(messages)
        else:
            wrapped = {"result": result, "_pluto_inbox": _slim_all(messages)}
        if single:
            wrapped["_pluto_inbox_remaining"] = remaining

        self._record_drain_latency(messages)
        await self._maybe_ack()
        return wrapped

    async def pop_one(self, wait_s: float = 0.0) -> tuple[dict | None, int]:
        """Pop and ack a single message; return ``(message, remaining)``.

        FIFO over the buffer. If the buffer is empty and *wait_s* is
        positive, block on ``_new_message_event`` up to *wait_s* seconds
        for an arrival; otherwise return ``(None, 0)`` immediately.

        Designed for pipeline / event-driven agents that want exactly one
        message per turn (e.g. the agent wakes on an MCP notification and
        calls ``pluto_pop`` once per event). For batch consumption keep
        using :meth:`drain` / :meth:`wait_for_messages`.

        At-least-once guarantees match the existing drain path: a crash
        between the buffer pop and the server ack relies on process
        restart clearing ``_seen_seqs`` so the message can be re-absorbed
        from a subsequent peek.
        """
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            async with self._lock:
                if self._buffered:
                    msg = self._buffered.pop(0)
                    remaining = len(self._buffered)
                    if not self._buffered:
                        self._new_message_event.clear()
                        self._push_drained()
                    break
                # Buffer empty — clear the event under the lock so
                # _absorb cannot fire it between our check and our wait.
                self._new_message_event.clear()
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return None, 0
            try:
                await asyncio.wait_for(
                    self._new_message_event.wait(), timeout=remaining_s,
                )
            except asyncio.TimeoutError:
                return None, 0
        self._record_drain_latency([msg])
        await self._maybe_ack()
        return _slim(msg), remaining

    async def drain(self) -> list[dict]:
        """Return all buffered messages and ack them.  Used by ``pluto_recv``."""
        async with self._lock:
            messages = list(self._buffered)
            self._buffered.clear()
            # Reset the new-message edge so peek-mode waiters
            # (wait_for_messages(drain=False)) re-arm correctly. With the
            # buffer empty again, the next arrival is what should wake them.
            self._new_message_event.clear()
        self._push_drained()
        if messages:
            self._record_drain_latency(messages)
            await self._maybe_ack()
        return _slim_all(messages)

    async def wait_for_messages(
        self,
        timeout_s: float = 300.0,
        drain: bool = True,
    ) -> list[dict]:
        """Block until at least one actionable message arrives, or until
        *timeout_s* seconds elapse.

        When *drain* is ``True`` (default) the call pops messages out of
        the buffer and acks them server-side — the classic consume path
        used by ``pluto_wait_for_messages``.

        When *drain* is ``False`` the call returns a snapshot of the
        currently-buffered messages without popping or acking, and without
        clearing ``_new_message_event``.  This is the peek/signal mode
        used by watcher subagents that share the parent's ``InboxManager``
        (Claude Code inherits the parent's MCP server, so the watcher must
        not consume the parent's inbox).  The parent's ``pluto_recv`` is
        the canonical drain; it resets ``_new_message_event`` so the next
        peek-mode call re-arms correctly.

        Event-driven: the peek loop sets ``_new_message_event`` whenever
        fresh messages land in the buffer.

        Used as a long-poll for agents that want chat-speed responsiveness:
        invoke directly at the tail of a turn, or wrap in a background
        sub-agent / Task so the main agent stays interactive while watching.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            messages: list[dict] = []
            async with self._lock:
                if self._buffered:
                    if drain:
                        messages = list(self._buffered)
                        self._buffered.clear()
                    else:
                        # Peek-mode: snapshot without popping. Leave the
                        # event set — only the actual drain path clears it.
                        return _slim_all(self._buffered)
                elif drain:
                    # Buffer empty — clear the event under the lock so
                    # _absorb can't fire it between our check and our wait
                    # (both code paths acquire _lock).
                    self._new_message_event.clear()
            if messages:
                self._push_drained()
                self._record_drain_latency(messages)
                await self._maybe_ack()
                return _slim_all(messages)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            try:
                await asyncio.wait_for(
                    self._new_message_event.wait(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                return []

    async def watch_durable(
        self,
        *,
        inbox_id: str = "default",
        wait_timeout_s: float = 60.0,
        max_total_s: float = 1800.0,
        drain: bool = True,
    ) -> dict:
        """Server-owned durable long-poll loop.

        Iterates :meth:`wait_for_messages` slices of *wait_timeout_s* until
        either messages arrive, the total *max_total_s* budget is consumed,
        or the error budget trips. Returns a dict with ``messages`` plus
        metadata (``watcher_id``, ``iterations``, ``timeout`` or ``error``).

        *drain* is forwarded to :meth:`wait_for_messages`. With
        ``drain=False`` the watcher returns a non-consuming snapshot of
        currently-buffered messages so it can coexist with a parent that
        drains via ``pluto_recv`` (Claude Code's subagent-inherits-MCP
        model).

        Soft conditions (empty slice): immediately restart, no sleep.
        Hard conditions (raised exception): exponential backoff capped at
        30 s, retry up to 10 consecutive errors before bailing.

        Dedupe: if a watcher for ``inbox_id`` is already active in this
        process, return ``{already_watching: True}`` immediately. The
        existing waiter keeps running; the new caller does not stack
        another loop.
        """
        key = inbox_id or "default"
        evict = self._watcher_evict_tasks.get(key)
        in_grace = evict is not None and not evict.done()
        if key in self._active_watchers and not in_grace:
            # A loop is genuinely live. Grace-window entries (slice
            # returned, eviction pending) are NOT live — bouncing those
            # would lock out the re-entering looper for the whole grace
            # period and make already_watching a lie.
            return {
                "already_watching": True,
                "watcher_id": key,
                "messages": [],
                "count": 0,
            }
        # Claim the slot, or reclaim it from the grace window (cancel the
        # pending eviction — this call's loop owns the slot now).
        if in_grace:
            evict.cancel()
            self._watcher_evict_tasks.pop(key, None)
        self._active_watchers.add(key)
        # Wall clock so age survives across slices and is meaningful to
        # the agent inspecting pluto_session / pluto_health.
        self._watcher_started_at.setdefault(key, time.time())
        # Floor at 0.01 s to prevent a degenerate zero-slice from spinning.
        # Production callers use seconds-scale slices; the low floor is a
        # safety net for tests and pathological configurations.
        slice_s = max(0.01, float(wait_timeout_s))
        total_s = max(slice_s, float(max_total_s))
        max_iters = max(1, math.ceil(total_s / slice_s))
        backoff = 1.0
        consecutive_errors = 0
        iterations_done = 0
        started = time.monotonic()
        deadline = started + total_s
        try:
            while True:
                now = time.monotonic()
                if now >= deadline or iterations_done >= max_iters:
                    return {
                        "messages": [],
                        "count": 0,
                        "timeout": True,
                        "watcher_id": key,
                        "iterations": iterations_done,
                        "max_iterations": max_iters,
                    }
                cur_slice = min(slice_s, deadline - now)
                try:
                    msgs = await self.wait_for_messages(
                        timeout_s=cur_slice, drain=drain,
                    )
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "watch_durable hard error #%d on %s: %s",
                        consecutive_errors, key, exc,
                    )
                    if consecutive_errors > 10:
                        if self._notifier is not None:
                            try:
                                await self._notifier.watcher_error(
                                    watcher_id=key, error=str(exc),
                                )
                            except Exception as nx:
                                logger.debug("notifier.watcher_error failed: %s", nx)
                        return {
                            "messages": [],
                            "count": 0,
                            "error": "too_many_errors",
                            "last_error": str(exc),
                            "watcher_id": key,
                            "iterations": iterations_done,
                        }
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2.0, 30.0)
                    continue
                consecutive_errors = 0
                backoff = 1.0
                iterations_done += 1
                if msgs:
                    return {
                        "messages": msgs,
                        "count": len(msgs),
                        "watcher_id": key,
                        "iterations": iterations_done,
                    }
        finally:
            # Don't discard immediately. Schedule a delayed eviction so a
            # peer subagent that calls in the gap between this caller's
            # slices still sees the watcher as active and bounces out. The
            # next slice from the same caller will cancel this task and
            # re-arm the entry.
            evict = asyncio.create_task(self._evict_watcher_after_grace(key))
            self._watcher_evict_tasks[key] = evict

    async def _evict_watcher_after_grace(self, key: str) -> None:
        try:
            await asyncio.sleep(self.WATCHER_GRACE_S)
        except asyncio.CancelledError:
            return
        self._active_watchers.discard(key)
        self._watcher_started_at.pop(key, None)
        self._watcher_evict_tasks.pop(key, None)

    def active_watchers_snapshot(self) -> dict:
        """Read-only view of currently-active watcher slots, including the
        post-slice grace window. Surfaces enough state for an agent to
        decide whether to spawn another inbox-watcher subagent.
        """
        now = time.time()
        ids = sorted(self._active_watchers)
        ages = [
            now - self._watcher_started_at[i]
            for i in ids
            if i in self._watcher_started_at
        ]
        return {
            "active": len(ids),
            "ids": ids,
            "oldest_age_s": max(ages) if ages else None,
            "grace_s": self.WATCHER_GRACE_S,
        }

    def peek_loop_state(self) -> dict:
        """Snapshot of the background peek loop's liveness.

        Surfaced by ``pluto_health`` so an agent can distinguish "no
        messages arriving" (healthy peek loop, empty inbox) from "peek
        loop is wedged" (no successful peek in N seconds).
        """
        alive = self._task is not None and not self._task.done()
        last_ok_mono = self._last_peek_ok_mono
        age_s = (
            (time.monotonic() - last_ok_mono) if last_ok_mono is not None else None
        )
        stalled = (
            age_s is not None and age_s > self.PEEK_STALL_WARN_S
        ) or (alive and last_ok_mono is None and self._peek_attempts > 0)
        return {
            "alive": alive,
            "age_s": age_s,
            "last_ok_at": self._last_peek_ok_wall,
            "last_error": self._last_peek_error,
            "attempts": self._peek_attempts,
            "ok_count": self._peek_ok_count,
            "stalled": stalled,
            "unrecoverable": self._unrecoverable,
            "unrecoverable_reason": self._unrecoverable_reason,
            "interval_s": self.PEEK_INTERVAL_S,
            "hard_timeout_s": self.PEEK_HARD_TIMEOUT_S,
            "stall_threshold_s": self.PEEK_STALL_WARN_S,
        }

    async def peek_only(self) -> list[dict]:
        """Return buffered messages without acking.  Used by ``pluto://inbox``
        resource reads.
        """
        async with self._lock:
            return _slim_all(self._buffered)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Background loop: peek → filter → buffer → notify.

        Wraps every peek in a hard timeout so a wedged HTTP socket can't
        silently stall delivery. Successful peeks refresh the liveness
        timestamps that ``peek_loop_state`` (and thus ``pluto_health``)
        report; failures land in ``_last_peek_error`` for diagnostics.
        """
        while not self._stop_event.is_set():
            self._peek_attempts += 1
            ok = False
            msgs: list[dict] = []
            try:
                msgs = await asyncio.wait_for(
                    asyncio.to_thread(self._client.peek, self._last_acked_seq),
                    timeout=self.PEEK_HARD_TIMEOUT_S,
                )
                ok = True
            except asyncio.TimeoutError:
                err = f"peek hard-timeout after {self.PEEK_HARD_TIMEOUT_S:.0f}s"
                self._last_peek_error = err
                logger.warning("%s — HTTP listener may be wedged", err)
                if self._notifier is not None:
                    try:
                        await self._notifier.watcher_error(
                            watcher_id="peek_loop", error=err,
                        )
                    except Exception as nx:
                        logger.debug("notifier.watcher_error failed: %s", nx)
            except PlutoError as exc:
                self._last_peek_error = str(exc)
                logger.warning("Pluto peek error: %s", exc)
            except Exception as exc:
                self._last_peek_error = str(exc)
                if self._is_session_lost(exc):
                    self._consecutive_session_lost += 1
                    logger.warning(
                        "Pluto session lost (#%d): %s",
                        self._consecutive_session_lost, exc,
                    )
                    if (
                        self._consecutive_session_lost
                        >= self.SESSION_LOST_GIVE_UP_AFTER
                    ):
                        reason = (
                            f"session_lost x{self._consecutive_session_lost} "
                            f"(last_error={exc}); PlutoMCPFriend does not "
                            f"auto-re-register, restart with --resume"
                        )
                        self._unrecoverable = True
                        self._unrecoverable_reason = reason
                        logger.error(
                            "Peek loop entering unrecoverable state: %s", reason,
                        )
                        if self._notifier is not None:
                            try:
                                await self._notifier.watcher_error(
                                    watcher_id="peek_loop", error=reason,
                                )
                            except Exception as nx:
                                logger.debug(
                                    "notifier.watcher_error failed: %s", nx,
                                )
                        return
                else:
                    logger.warning("Pluto peek error: %s", exc)

            if ok:
                now = time.monotonic()
                self._last_peek_ok_mono = now
                self._last_peek_ok_wall = time.time()
                self._peek_ok_count += 1
                # Stall-warning state is re-armed once a successful peek
                # lands; the next stall window can warn afresh.
                self._stall_warned = False
                # A successful peek means we have a working session again —
                # reset the session-lost streak so a transient 401 doesn't
                # accumulate toward the terminal threshold over time.
                self._consecutive_session_lost = 0
                if msgs:
                    await self._absorb(msgs)
                elif self._ack_retry_needed:
                    # Heal a previously failed ack with zero new traffic.
                    await self._maybe_ack()
                await self._sleep_or_stop(self.PEEK_INTERVAL_S)
            else:
                # Emit a single stall warning per stall window so logs stay
                # readable when the server is down for minutes.
                if not self._stall_warned:
                    last_ok = self._last_peek_ok_mono
                    age = (
                        (time.monotonic() - last_ok) if last_ok is not None
                        else None
                    )
                    if age is None or age > self.PEEK_STALL_WARN_S:
                        logger.warning(
                            "peek loop stalled: no successful peek in %s "
                            "(attempts=%d, last_error=%s)",
                            f"{age:.1f}s" if age is not None else "any window",
                            self._peek_attempts, self._last_peek_error,
                        )
                        self._stall_warned = True
                # On any failure path, back off harder than the normal poll
                # interval so we don't hot-loop against a broken server.
                await self._sleep_or_stop(self.SESSION_RETRY_BACKOFF_S)

    async def _absorb(self, msgs: list[dict]) -> None:
        """Filter noise, dedupe by seq_token, append to buffer, fire callbacks.

        Noise messages are silently acked so they don't keep coming back
        from peek.
        """
        actionable: list[dict] = []
        saw_noise = False
        for m in msgs:
            seq = m.get("seq_token")
            if seq is None:
                continue
            seq_int = int(seq)
            self._max_seen_seq = max(self._max_seen_seq, seq_int)
            if self._should_skip(m):
                saw_noise = True
                continue
            if seq_int in self._seen_seqs:
                continue
            self._seen_seqs.add(seq_int)
            actionable.append(m)

        fresh: list[dict] = []
        if actionable:
            now = time.monotonic()
            async with self._lock:
                self._buffered.extend(actionable)
                fresh = list(actionable)
                for m in actionable:
                    seq = m.get("seq_token")
                    if seq is not None:
                        self._landed_at[int(seq)] = now
                # Wake any waiter blocked in wait_for_messages().
                self._new_message_event.set()
            # Fire phase-2 notifications. Best-effort: notifier internals
            # swallow exceptions so a dead session can't break delivery.
            if self._notifier is not None:
                try:
                    await self._notifier.inbox_message(fresh)
                except Exception as exc:
                    logger.debug("notifier.inbox_message failed: %s", exc)
            # Push wakeup into the host Claude Code session (if attached).
            # Best-effort: SocketNotifier swallows its own send errors.
            if self._push is not None:
                try:
                    await self._push.notify_new_messages(fresh)
                except Exception as exc:
                    logger.debug("push.notify_new_messages failed: %s", exc)

        if saw_noise:
            # Settle noise via the safe cursor — never ack past the
            # lowest undelivered buffered seq (a bare max(noise_seqs)
            # would range-delete still-buffered actionable messages on
            # the server if a noise seq landed above them).
            await self._maybe_ack()

        if fresh:
            for cb in self._on_new_message:
                try:
                    await cb(fresh)
                except Exception as exc:
                    logger.debug("on_new_message callback failed: %s", exc)

    def _record_drain_latency(self, messages: list[dict]) -> None:
        """Drain-latency telemetry: time from _absorb landing a message
        to the drain path handing it to the agent. Reported to the
        notifier (which may be a no-op stub)."""
        if self._notifier is None or not self._landed_at:
            return
        now = time.monotonic()
        for m in messages:
            seq = m.get("seq_token")
            if seq is None:
                continue
            landed = self._landed_at.pop(int(seq), None)
            if landed is not None:
                self._notifier.record_drain_latency_ms((now - landed) * 1000.0)

    def _safe_ack_cursor_locked(self) -> int:
        """Highest seq where everything at-or-below has left the
        server-visible obligation: delivered to the agent or classified
        noise. The buffer is seq-ordered (peek returns ascending), so
        with messages still buffered the cursor stops just below the
        undelivered head; with an empty buffer everything seen so far is
        settled. Callers must hold ``self._lock``."""
        if self._buffered:
            return int(self._buffered[0]["seq_token"]) - 1
        return self._max_seen_seq

    async def _maybe_ack(self) -> None:
        """Advance the server-side ack cursor if it is safe to do so.

        Idempotent and failure-tolerant: on ack failure the cursor
        simply doesn't advance — the messages stay in the server inbox,
        ``_seen_seqs`` keeps deduping their re-peeks, and the peek loop
        retries on its next successful cycle.
        """
        async with self._lock:
            cursor = self._safe_ack_cursor_locked()
        if cursor <= self._last_acked_seq:
            return
        try:
            await asyncio.to_thread(self._client.ack, cursor)
        except Exception as exc:
            self._ack_retry_needed = True
            logger.warning(
                "Pluto ack(up_to=%d) failed (will retry): %s", cursor, exc,
            )
            return
        self._ack_retry_needed = False
        self._last_acked_seq = max(self._last_acked_seq, cursor)
        # Dedupe/telemetry state below the cursor can never be needed
        # again — prune so neither grows for the life of the process.
        self._seen_seqs = {s for s in self._seen_seqs if s > cursor}
        for s in [s for s in self._landed_at if s <= cursor]:
            self._landed_at.pop(s, None)

    def _push_drained(self) -> None:
        """Re-arm the push notifier once the agent has emptied the buffer."""
        if self._push is not None:
            try:
                self._push.notify_drained()
            except Exception as exc:
                logger.debug("push.notify_drained failed: %s", exc)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _is_session_lost(exc: BaseException) -> bool:
        text = str(exc).lower()
        return (
            "session_not_found" in text
            or "404" in text
            or "401" in text
            or "not registered" in text
        )
