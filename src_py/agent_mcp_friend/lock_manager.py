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
from dataclasses import dataclass

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

    async def unregister(self, lock_ref: str) -> None:
        """Stop renewing *lock_ref* (e.g. after release)."""
        async with self._lock:
            tracked = self._tracked.pop(lock_ref, None)
        if tracked is not None:
            tracked.task.cancel()
            try:
                await tracked.task
            except (asyncio.CancelledError, Exception):
                pass

    async def shutdown(self) -> None:
        """Cancel every renewal task on server shutdown."""
        async with self._lock:
            tasks = [t.task for t in self._tracked.values()]
            self._tracked.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def held_locks(self) -> list[dict]:
        """Snapshot of currently auto-renewed locks (for the locks resource)."""
        return [
            {"lock_ref": ref, "resource": t.resource, "ttl_ms": t.ttl_ms}
            for ref, t in self._tracked.items()
        ]

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
                        self._tracked.pop(lock_ref, None)
                    return
                if resp.get("status") != "ok":
                    logger.warning(
                        "Auto-renew of %s returned %s — stopping",
                        lock_ref, resp,
                    )
                    async with self._lock:
                        self._tracked.pop(lock_ref, None)
                    return
        except asyncio.CancelledError:
            raise
