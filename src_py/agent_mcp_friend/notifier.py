"""MCP notifications wire format + delivery-latency telemetry.

Phase-2 implementation of the ``PLUTO_MCP_NOTIFICATIONS`` seam. Emits
*two* notification frames for each Pluto event, on the theory that
hosts which understand neither will silently drop and hosts which
understand either will benefit:

* **Standard** — ``notifications/resources/updated`` for ``pluto://inbox``
  (broadly supported by MCP clients that subscribe to resources) and
  ``notifications/message`` (logging) with a structured ``data``
  payload that any client surfaces to its log pane.
* **Custom-shaped** — same logging notification but with an explicit
  ``event`` discriminator in ``data`` so Pluto-aware hosts can route
  on it.

The long-poll watcher remains the correctness channel; these
notifications are a one-way nudge that the host *may* convert into a
new model turn or refreshed resource view.

Telemetry: we cannot directly observe whether the host converted a
notification into a new turn, but we can measure the time from a
message landing in the buffer to it being drained out. If that
``drain_after_landed_ms`` distribution shifts when notifications are
enabled, the host is consuming them.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("pluto_mcp_friend.notifier")


class Notifier:
    """One-way notification emitter + drain-latency tracker.

    The MCP session is captured the first time a tool handler calls
    :meth:`bind_session` (re-captured on every call, so reconnects
    update the reference). Background callers — most importantly the
    :class:`InboxManager` peek loop — fire through the cached session
    without needing their own request context.
    """

    _LATENCY_CAP = 1000  # bounded sample buffer

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._session = None
        # event counters
        self.inbox_message_fired = 0
        self.watcher_error_fired = 0
        self.send_failures = 0
        self.first_fired_at: Optional[float] = None
        # latency samples (drain_after_landed in ms, capped)
        self._latencies: list[float] = []

    # ── Session capture ──────────────────────────────────────────────────

    def bind_session(self, session: Any) -> None:
        """Cache the live ``ServerSession``. Safe to call on every tool
        entry; only the most recent session is retained.
        """
        if session is not None:
            self._session = session

    # ── Telemetry ────────────────────────────────────────────────────────

    def record_drain_latency_ms(self, ms: float) -> None:
        self._latencies.append(ms)
        # Bounded ring: drop oldest when over cap.
        if len(self._latencies) > self._LATENCY_CAP:
            del self._latencies[: len(self._latencies) - self._LATENCY_CAP]

    def summary(self) -> dict:
        lat = self._latencies
        mean = (sum(lat) / len(lat)) if lat else None
        p95 = None
        if lat:
            ordered = sorted(lat)
            idx = max(0, int(0.95 * (len(ordered) - 1)))
            p95 = ordered[idx]
        return {
            "enabled": self.enabled,
            "session_bound": self._session is not None,
            "inbox_message_fired": self.inbox_message_fired,
            "watcher_error_fired": self.watcher_error_fired,
            "send_failures": self.send_failures,
            "drain_samples": len(lat),
            "drain_mean_ms": mean,
            "drain_p95_ms": p95,
            "first_fired_at": self.first_fired_at,
        }

    # ── Notification emit ────────────────────────────────────────────────

    async def inbox_message(self, messages: list[dict]) -> None:
        """Fire both standard and structured notifications for new inbox
        messages. No-op when ``enabled=False`` or no session has been
        bound yet.
        """
        if not self.enabled or self._session is None or not messages:
            return
        seq_tokens = [m.get("seq_token") for m in messages if m.get("seq_token") is not None]
        senders = sorted({m.get("from") for m in messages if m.get("from")})
        events = sorted({m.get("event") for m in messages if m.get("event")})
        data = {
            "event": "pluto.inboxMessage",
            "count": len(messages),
            "seq_tokens": seq_tokens,
            "from": senders,
            "event_kinds": events,
            "ts": time.time(),
        }
        sent_any = False
        # Standard channel — resource subscription refresh.
        try:
            from pydantic import AnyUrl

            await self._session.send_resource_updated(AnyUrl("pluto://inbox"))
            sent_any = True
        except Exception as exc:
            self.send_failures += 1
            logger.debug("resource_updated(pluto://inbox) failed: %s", exc)
        # Structured channel — logging notification with discriminated payload.
        try:
            await self._session.send_log_message(
                level="info", data=data, logger="pluto.inbox",
            )
            sent_any = True
        except Exception as exc:
            self.send_failures += 1
            logger.debug("send_log_message(pluto.inbox) failed: %s", exc)
        if sent_any:
            self.inbox_message_fired += 1
            if self.first_fired_at is None:
                self.first_fired_at = time.time()

    async def watcher_error(self, watcher_id: str, error: str) -> None:
        """Fire ``notifications/message`` (warning) for hard watcher
        failures. Same payload shape as ``inbox_message`` for routing.
        """
        if not self.enabled or self._session is None:
            return
        data = {
            "event": "pluto.watcherError",
            "watcher_id": watcher_id,
            "error": error,
            "ts": time.time(),
        }
        try:
            await self._session.send_log_message(
                level="warning", data=data, logger="pluto.watcher",
            )
            self.watcher_error_fired += 1
        except Exception as exc:
            self.send_failures += 1
            logger.debug("send_log_message(pluto.watcher) failed: %s", exc)
