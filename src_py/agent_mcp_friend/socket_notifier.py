"""Push wakeups into the parent Claude Code session's inbox socket.

Claude Code (>= 2.1.224 on macOS/Linux) binds a per-session Unix domain
socket and exports its path to child processes as
``CLAUDE_CODE_MESSAGING_SOCKET`` (auth token in
``CLAUDE_CODE_MESSAGING_TOKEN``). Writing a message frame to that socket
delivers a user-role message into the session: an idle session starts a
new turn with it; a busy session receives it between tool calls.

:class:`SocketNotifier` uses this to wake the host agent the moment
actionable Pluto messages land in the :class:`InboxManager` buffer —
replacing the token-burning watcher-subagent / heartbeat polling pattern
with a zero-model-turn push. The wakeup text is metadata only (count and
senders, never payloads); the woken agent drains via ``pluto_recv`` /
``pluto_pop`` as usual.

Gating: the ``PLUTO_MCP_PUSH`` env var is a tri-state — unset means
auto-on when the socket env var is present; truthy forces on; falsy
forces off. On non-Claude hosts (no socket env) the notifier is inert
and delivery behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

logger = logging.getLogger("pluto_mcp_friend.socket_notifier")

# Env vars exported by Claude Code to its child processes. Treated as the
# ONLY source of the endpoint — the socket directory layout is an
# implementation detail of the host (observed /tmp/cc-socks/<pid>.sock on
# macOS 2.1.236 vs a documented /tmp/cc-socks-<uid> fallback elsewhere),
# so we never construct the path ourselves.
_SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
_TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
_PUSH_ENV = "PLUTO_MCP_PUSH"


def _push_enabled_from_env(socket_present: bool) -> bool:
    """Tri-state PLUTO_MCP_PUSH, mirroring _mcp_inherited's parsing:
    unset → auto (on iff the socket env is present); truthy → on;
    falsy → off; unrecognized → auto."""
    raw = os.environ.get(_PUSH_ENV)
    if raw is not None:
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return socket_present


class SocketNotifier:
    """Debounced, drain-aware wakeup pusher for the Claude Code inbox socket."""

    # Coalesce arrivals within this window into a single wakeup/connection.
    DEBOUNCE_S = 2.0
    # While a wakeup is outstanding (inbox not yet drained), re-wake at
    # most once per this interval even if new messages keep arriving.
    MIN_REWAKE_S = 30.0
    CONNECT_TIMEOUT_S = 5.0
    # After this many consecutive send failures, enter degraded mode and
    # retry at most once per RETRY_COOLDOWN_S. A success fully re-arms.
    DISABLE_AFTER_FAILURES = 3
    RETRY_COOLDOWN_S = 300.0
    # Retry delay after a non-degraded send failure. A failed wakeup is
    # never dropped — pending state is restored and rescheduled so a
    # transient failure can't strand buffered messages wake-less.
    RETRY_BACKOFF_S = 5.0

    def __init__(
        self,
        socket_path: Optional[str],
        token: Optional[str],
        agent_id: str,
        enabled: bool,
        unavailable_reason: Optional[str] = None,
    ):
        self._socket_path = socket_path or None
        self._token = token or None
        self._agent_id = agent_id
        self._enabled = enabled and self._socket_path is not None
        self._unavailable_reason = unavailable_reason
        # Pending (not yet flushed) wakeup state, mutated only between
        # awaits on the event loop — no lock needed.
        self._pending_count = 0
        self._pending_senders: list[str] = []
        self._flush_task: Optional[asyncio.Task] = None
        # True from a sent wakeup until notify_drained() re-arms us.
        self._awaiting_drain = False
        self._last_wakeup_mono: Optional[float] = None
        # Telemetry / failure handling.
        self._wakeups_sent = 0
        self._suppressed = 0
        self._send_failures = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._last_failure_mono: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_wakeup_wall: Optional[float] = None

    @classmethod
    def from_env(cls, agent_id: str) -> "SocketNotifier":
        def _env(name: str) -> Optional[str]:
            # Empty values and unexpanded ${VAR} literals (from an
            # .mcp.json env block on a host without expansion) → unset.
            val = (os.environ.get(name) or "").strip()
            if not val or "${" in val:
                return None
            return val

        socket_path = _env(_SOCKET_ENV)
        token = _env(_TOKEN_ENV)
        if sys.platform == "win32":
            # Claude Code uses a named pipe on native Windows; not
            # implemented here.
            return cls(
                None, None, agent_id, enabled=False,
                unavailable_reason="windows_named_pipe_unsupported",
            )
        enabled = _push_enabled_from_env(socket_present=socket_path is not None)
        reason = None
        if socket_path is None:
            reason = f"{_SOCKET_ENV} not set (not a Claude Code >=2.1.224 host?)"
            forced_on = (os.environ.get(_PUSH_ENV) or "").strip().lower() in (
                "1", "true", "yes", "on",
            )
            if forced_on:
                logger.error(
                    "%s requested but %s is absent — push wakeups unavailable",
                    _PUSH_ENV, _SOCKET_ENV,
                )
        return cls(
            socket_path, token, agent_id,
            enabled=enabled, unavailable_reason=reason,
        )

    @property
    def available(self) -> bool:
        return self._enabled

    # ── Inbox hooks ───────────────────────────────────────────────────────

    async def notify_new_messages(self, messages: list[dict]) -> None:
        """Called by InboxManager._absorb when fresh actionable messages
        land. Schedules a debounced wakeup unless one is already
        outstanding (inbox not drained yet, within MIN_REWAKE_S)."""
        if not self._enabled or not messages:
            return
        if self._awaiting_drain:
            since = (
                time.monotonic() - self._last_wakeup_mono
                if self._last_wakeup_mono is not None else None
            )
            if since is not None and since < self.MIN_REWAKE_S:
                self._suppressed += len(messages)
                return
        self._pending_count += len(messages)
        for m in messages:
            sender = m.get("from")
            if sender and sender not in self._pending_senders:
                self._pending_senders.append(sender)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(
                self._flush_after_debounce(), name="pluto-push-flush",
            )

    def notify_drained(self) -> None:
        """Called by the drain paths (piggyback/drain/pop-to-empty) once
        the agent has consumed the buffer — re-arms the next wakeup and
        drops any pending flush (the agent already has the messages)."""
        self._awaiting_drain = False
        self._pending_count = 0
        self._pending_senders = []
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None

    async def aclose(self) -> None:
        # Disable first so late absorbs during teardown can't schedule a
        # fresh flush after the cancel below.
        self._enabled = False
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        self._flush_task = None

    def summary(self) -> dict:
        return {
            "available": self._enabled,
            "degraded": self._degraded,
            "socket": self._socket_path,
            "unavailable_reason": self._unavailable_reason,
            "wakeups_sent": self._wakeups_sent,
            "suppressed": self._suppressed,
            "send_failures": self._send_failures,
            "last_error": self._last_error,
            "last_wakeup_at": self._last_wakeup_wall,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _restore_pending(self, count: int, senders: list[str]) -> None:
        self._pending_count += count
        for s in senders:
            if s not in self._pending_senders:
                self._pending_senders.append(s)

    def _schedule_flush(self, delay: float) -> None:
        self._flush_task = asyncio.create_task(
            self._flush_after_debounce(delay), name="pluto-push-flush",
        )

    async def _flush_after_debounce(self, delay: Optional[float] = None) -> None:
        try:
            await asyncio.sleep(self.DEBOUNCE_S if delay is None else delay)
        except asyncio.CancelledError:
            raise
        count = self._pending_count
        senders = list(self._pending_senders)
        self._pending_count = 0
        self._pending_senders = []
        if count == 0:
            return
        if self._degraded:
            since_fail = (
                time.monotonic() - self._last_failure_mono
                if self._last_failure_mono is not None else None
            )
            if since_fail is not None and since_fail < self.RETRY_COOLDOWN_S:
                # Inside the cooldown: keep the wakeup pending and come
                # back when the cooldown expires — never drop it.
                self._suppressed += count
                self._restore_pending(count, senders)
                self._schedule_flush(self.RETRY_COOLDOWN_S - since_fail)
                return
        text = (
            f"[pluto:{self._agent_id}] {count} new Pluto message(s) waiting"
            f"{' from ' + ', '.join(senders) if senders else ''}."
            " Call pluto_recv to read them."
        )
        try:
            await self._send(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._send_failures += 1
            self._consecutive_failures += 1
            self._last_failure_mono = time.monotonic()
            self._last_error = str(exc)
            if self._consecutive_failures >= self.DISABLE_AFTER_FAILURES:
                if not self._degraded:
                    logger.warning(
                        "push wakeups degraded after %d consecutive failures "
                        "(last: %s) — retrying at most every %.0fs",
                        self._consecutive_failures, exc, self.RETRY_COOLDOWN_S,
                    )
                self._degraded = True
            # A failed wakeup is retried, not dropped: restore pending
            # state and reschedule so a transient failure can't strand
            # buffered messages with no wake ever arriving.
            self._restore_pending(count, senders)
            self._schedule_flush(
                self.RETRY_COOLDOWN_S if self._degraded
                else self.RETRY_BACKOFF_S
            )
            return
        self._consecutive_failures = 0
        self._degraded = False
        self._wakeups_sent += 1
        self._awaiting_drain = True
        self._last_wakeup_mono = time.monotonic()
        self._last_wakeup_wall = time.time()

    def _encode_frames(self, text: str) -> list[bytes]:
        """The entire wire format lives here.

        Schema (Claude Code 2.1.236, from the CLI's own startup log
        example for the uds-messaging inbox):

            {"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}
            {"type":"user","message":{"role":"user","content":"<text>"}}

        Newline-delimited JSON over a SOCK_STREAM Unix socket. The auth
        line is optional on macOS/Linux and required on Windows; we
        always send it when a token is present because own-child
        (token-verified) messages bypass the receiver's approval gates.
        """
        frames: list[bytes] = []
        if self._token:
            frames.append(
                json.dumps(
                    {"type": "auth", "token": self._token},
                    separators=(",", ":"),
                ).encode() + b"\n"
            )
        frames.append(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": text}},
                separators=(",", ":"),
            ).encode() + b"\n"
        )
        return frames

    async def _send(self, text: str) -> None:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path),
            timeout=self.CONNECT_TIMEOUT_S,
        )
        try:
            for frame in self._encode_frames(text):
                writer.write(frame)
            await asyncio.wait_for(
                writer.drain(), timeout=self.CONNECT_TIMEOUT_S,
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
