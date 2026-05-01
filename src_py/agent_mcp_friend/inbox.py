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
        if messages:
            await self._ack_messages(messages)
        return messages

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
            async with self._lock:
                self._buffered.extend(actionable)
                fresh = list(actionable)

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
