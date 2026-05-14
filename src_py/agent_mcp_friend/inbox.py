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
import time
from typing import Any

from pluto_client import PlutoError, PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.inbox")

# Mirror agent_friend.pluto_connection: events the agent must never see.
_NOISE_PAYLOAD_EVENTS = {"delivery_ack", "status_update", "heartbeat"}
_ACTIONABLE_EVENTS = {"message", "broadcast", "task_assigned", "topic_message"}


def _is_noise(msg: dict) -> bool:
    if msg.get("event") not in _ACTIONABLE_EVENTS:
        return True
    payload = msg.get("payload")
    if isinstance(payload, dict) and payload.get("event") in _NOISE_PAYLOAD_EVENTS:
        return True
    return False


class InboxManager:
    """Background peek loop + per-tool-result piggyback buffer."""

    PEEK_INTERVAL_S = 1.0
    SESSION_RETRY_BACKOFF_S = 5.0

    def __init__(self, client: PlutoHttpClient):
        self._client = client
        self._buffered: list[dict] = []
        self._seen_seqs: set[int] = set()
        self._last_acked_seq: int = 0
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
        self._active_watchers: set[str] = set()
        # Delivery-latency telemetry: monotonic timestamps keyed by
        # seq_token, set on _absorb and consumed on _ack_messages.
        self._landed_at: dict[int, float] = {}
        # Optional notifier hook (phase-2). Set via :meth:`set_notifier`.
        self._notifier = None

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

    def set_notifier(self, notifier) -> None:
        """Attach a :class:`Notifier` for phase-2 MCP notifications and
        delivery-latency telemetry. Optional — no-op if unset."""
        self._notifier = notifier

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

        Acks fire as soon as messages are placed on the result so even if
        the agent ignores them the server inbox is drained — at-least-once
        delivery is preserved on the wire (peek will resurface them after
        a crash before the ack lands).
        """
        async with self._lock:
            if not self._buffered:
                return result
            messages = list(self._buffered)
            self._buffered.clear()

        if isinstance(result, dict):
            wrapped = dict(result)
            wrapped["_pluto_inbox"] = messages
        else:
            wrapped = {"result": result, "_pluto_inbox": messages}

        await self._ack_messages(messages)
        return wrapped

    async def drain(self) -> list[dict]:
        """Return all buffered messages and ack them.  Used by ``pluto_recv``."""
        async with self._lock:
            messages = list(self._buffered)
            self._buffered.clear()
            # Reset the new-message edge so peek-mode waiters
            # (wait_for_messages(drain=False)) re-arm correctly. With the
            # buffer empty again, the next arrival is what should wake them.
            self._new_message_event.clear()
        if messages:
            await self._ack_messages(messages)
        return messages

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
                        return list(self._buffered)
                elif drain:
                    # Buffer empty — clear the event under the lock so
                    # _absorb can't fire it between our check and our wait
                    # (both code paths acquire _lock).
                    self._new_message_event.clear()
            if messages:
                await self._ack_messages(messages)
                return messages

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
        if key in self._active_watchers:
            return {
                "already_watching": True,
                "watcher_id": key,
                "messages": [],
                "count": 0,
            }
        self._active_watchers.add(key)
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
            self._active_watchers.discard(key)

    async def peek_only(self) -> list[dict]:
        """Return buffered messages without acking.  Used by ``pluto://inbox``
        resource reads.
        """
        async with self._lock:
            return list(self._buffered)

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Background loop: peek → filter → buffer → notify."""
        while not self._stop_event.is_set():
            try:
                msgs = await asyncio.to_thread(
                    self._client.peek, self._last_acked_seq
                )
            except PlutoError as exc:
                logger.warning("Pluto peek error: %s", exc)
                await self._sleep_or_stop(self.SESSION_RETRY_BACKOFF_S)
                continue
            except Exception as exc:
                if self._is_session_lost(exc):
                    logger.warning("Pluto session lost; backing off")
                    await self._sleep_or_stop(self.SESSION_RETRY_BACKOFF_S)
                    continue
                logger.warning("Pluto peek error: %s", exc)
                await self._sleep_or_stop(self.SESSION_RETRY_BACKOFF_S)
                continue

            if msgs:
                await self._absorb(msgs)
            await self._sleep_or_stop(self.PEEK_INTERVAL_S)

    async def _absorb(self, msgs: list[dict]) -> None:
        """Filter noise, dedupe by seq_token, append to buffer, fire callbacks.

        Noise messages are silently acked so they don't keep coming back
        from peek.
        """
        actionable: list[dict] = []
        noise_seqs: list[int] = []
        for m in msgs:
            seq = m.get("seq_token")
            if seq is None:
                continue
            seq_int = int(seq)
            if _is_noise(m):
                noise_seqs.append(seq_int)
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

        if noise_seqs:
            try:
                await asyncio.to_thread(self._client.ack, max(noise_seqs))
            except Exception as exc:
                logger.debug("Noise-ack failed: %s", exc)

        if fresh:
            for cb in self._on_new_message:
                try:
                    await cb(fresh)
                except Exception as exc:
                    logger.debug("on_new_message callback failed: %s", exc)

    async def _ack_messages(self, messages: list[dict]) -> None:
        seqs = [int(m["seq_token"]) for m in messages if "seq_token" in m]
        if not seqs:
            return
        up_to = max(seqs)
        # Drain-latency telemetry: time from _absorb landing the message to
        # the drain path firing the ack. Reported to the notifier (which
        # may be a no-op stub).
        if self._notifier is not None and self._landed_at:
            now = time.monotonic()
            for seq in seqs:
                landed = self._landed_at.pop(seq, None)
                if landed is not None:
                    self._notifier.record_drain_latency_ms((now - landed) * 1000.0)
            # Compact: discard any landing timestamps we'll never settle.
            stale = [s for s in self._landed_at if s <= up_to]
            for s in stale:
                self._landed_at.pop(s, None)
        try:
            await asyncio.to_thread(self._client.ack, up_to)
            self._last_acked_seq = max(self._last_acked_seq, up_to)
        except Exception as exc:
            logger.warning("Pluto ack(up_to=%d) failed: %s", up_to, exc)

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
