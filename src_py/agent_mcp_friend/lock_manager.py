"""Lock auto-renewal manager for PlutoMCPFriend.

When an agent calls ``pluto_lock_acquire``, we register the granted lock
with this manager. A background task renews the lock at TTL/2 until the
agent releases it or the MCP server shuts down. This removes the
"remember to renew" burden from the agent — long edits no longer need
manual renewal logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from pluto_client import PlutoHttpClient

logger = logging.getLogger("pluto_mcp_friend.lock_manager")


@dataclass
class _Tracked:
    resource: str
    ttl_ms: int
    task: asyncio.Task


class LockManager:
    """Tracks held locks and renews each at TTL/2 until released."""

    MIN_RENEW_INTERVAL_S = 1.0

    def __init__(self, client: PlutoHttpClient):
        self._client = client
        self._tracked: dict[str, _Tracked] = {}
        self._lock = asyncio.Lock()
        # Locks whose auto-renew failed. The agent still believes it
        # holds them, so this MUST reach it: tools.py piggybacks
        # take_lost() onto the next tool result as _pluto_lock_lost, and
        # pluto_health surfaces lost_summary().
        self._lost_events: list[dict] = []
        self._lost_total: int = 0
        self._last_lost: Optional[dict] = None

    async def register(self, lock_ref: str, resource: str, ttl_ms: int) -> None:
        """Begin auto-renewing *lock_ref* every TTL/2 until release."""
        async with self._lock:
            existing = self._tracked.pop(lock_ref, None)
        if existing is not None:
            existing.task.cancel()

        task = asyncio.create_task(
            self._renew_loop(lock_ref, ttl_ms),
            name=f"pluto-renew-{lock_ref}",
        )
        async with self._lock:
            self._tracked[lock_ref] = _Tracked(resource, ttl_ms, task)

    @staticmethod
    async def _reap(task: asyncio.Task) -> None:
        """Await a just-cancelled renew task, swallowing its errors but
        re-raising the *caller's own* cancellation (the bare
        ``except (CancelledError, Exception)`` this replaces silently
        ate it, breaking cooperative shutdown)."""
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:
            pass

    async def unregister(self, lock_ref: str) -> None:
        """Stop renewing *lock_ref* (e.g. after release)."""
        async with self._lock:
            tracked = self._tracked.pop(lock_ref, None)
        if tracked is not None:
            tracked.task.cancel()
            await self._reap(tracked.task)

    async def shutdown(self) -> None:
        """Cancel every renewal task on server shutdown."""
        async with self._lock:
            tasks = [t.task for t in self._tracked.values()]
            self._tracked.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            await self._reap(t)

    def held_locks(self) -> list[dict]:
        """Snapshot of currently auto-renewed locks (for the locks resource)."""
        return [
            {"lock_ref": ref, "resource": t.resource, "ttl_ms": t.ttl_ms}
            for ref, t in self._tracked.items()
        ]

    def take_lost(self) -> list[dict]:
        """Drain-once list of locks whose auto-renew failed since the
        last call. Piggybacked onto tool results as _pluto_lock_lost."""
        lost, self._lost_events = self._lost_events, []
        return lost

    def lost_summary(self) -> dict:
        """Cumulative lost-lock telemetry for pluto_health."""
        return {"lost_total": self._lost_total, "last_lost": self._last_lost}

    def _record_lost(self, lock_ref: str, resource: str, reason: str) -> None:
        event = {
            "lock_ref": lock_ref,
            "resource": resource,
            "reason": reason,
            "ts": time.time(),
        }
        self._lost_events.append(event)
        self._lost_total += 1
        self._last_lost = event

    async def _renew_loop(self, lock_ref: str, ttl_ms: int) -> None:
        interval = max(self.MIN_RENEW_INTERVAL_S, ttl_ms / 2000.0)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    resp = await asyncio.to_thread(
                        self._client.renew, lock_ref, ttl_ms
                    )
                except Exception as exc:
                    logger.warning(
                        "Auto-renew of %s failed: %s — stopping",
                        lock_ref, exc,
                    )
                    # Fire-and-forget cleanup; can't await self.unregister here
                    # because that would re-await this task.
                    async with self._lock:
                        tracked = self._tracked.pop(lock_ref, None)
                    self._record_lost(
                        lock_ref,
                        tracked.resource if tracked else "",
                        f"renew_error: {exc}",
                    )
                    return
                if resp.get("status") != "ok":
                    logger.warning(
                        "Auto-renew of %s returned %s — stopping",
                        lock_ref, resp,
                    )
                    async with self._lock:
                        tracked = self._tracked.pop(lock_ref, None)
                    self._record_lost(
                        lock_ref,
                        tracked.resource if tracked else "",
                        f"renew_denied: {resp.get('status')}",
                    )
                    return
        except asyncio.CancelledError:
            raise
