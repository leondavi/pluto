"""Wakeups for job events, without waking an agent that already knows."""

from __future__ import annotations

import asyncio

from agent_mcp_friend.socket_notifier import SocketNotifier
from agent_smart_streams.compact import short
from agent_smart_streams.constants import WAKE_GRACE_S, WAKE_POLL_S
from agent_smart_streams.notices import NoticeBoard
from pluto_jobs_def import EVENT_FINISHED, EVENT_WATCH, JOB_EVENTS


class JobNotifier(SocketNotifier):
    """Holds a job event back while a job_* call runs and for a moment
    after, and drops it once some tool result has told the agent."""

    board: NoticeBoard | None = None

    async def notify_new_messages(self, messages: list[dict]) -> None:
        events = [m for m in messages if m.get("event") in JOB_EVENTS]
        mail = [m for m in messages if m.get("event") not in JOB_EVENTS]
        if mail:
            await super().notify_new_messages(mail)
        if events and self._enabled:
            task = asyncio.create_task(self._hold(events))
            self._held.add(task)
            task.add_done_callback(self._held.discard)

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._held: set[asyncio.Task] = set()
        self._news: dict[str, str] = {}

    async def _hold(self, events: list[dict]) -> None:
        await asyncio.sleep(WAKE_GRACE_S)
        while self.board and self.board.busy:
            await asyncio.sleep(WAKE_POLL_S)
        news = [e for e in events
                if not (self.board and self.board.knows(
                    e["event"], e.get("job_id"), e.get("h", 0)))]
        for e in news:
            if e["event"] == EVENT_WATCH:
                name = " ".join(str(e.get("watch", "")).split())[:60]
                self._news[short(e.get("job_id") or "?") + " watch"] = name
                continue
            self._news[short(e.get("job_id") or "?")] = (
                e.get("state", "") if e["event"] == EVENT_FINISHED
                else "transferred to you")
        if news:
            await super().notify_new_messages(news)

    def _wake_text(self, count: int, senders: list[str]) -> str:
        news, self._news = self._news, {}
        if not news or senders != ["smartstreams"]:
            return super()._wake_text(count, senders)
        return f"[smartstreams:{self._agent_id}] " + ", ".join(
            f"{j} {st}" for j, st in news.items())

    async def aclose(self) -> None:
        for task in list(self._held):
            task.cancel()
        await super().aclose()
