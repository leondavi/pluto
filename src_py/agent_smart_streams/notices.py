"""Delivery of durable job notices on tool results."""

from __future__ import annotations

from agent_smart_streams import compact
from agent_smart_streams.constants import NOTICE_KEY
from agent_smart_streams.compact import short
from pluto_jobs_def import EVENT_FINISHED, EVENT_WATCH
from pluto_sdk import PlutoError, PlutoJobsClient

_FIELDS = ("event", "job_id", "name", "state")


class NoticeBoard:
    """Attaches notices to results. A notice is acknowledged by the call
    *after* the one that carried it, so a result lost on the way to the
    model shows it again."""

    def __init__(self, jobs: PlutoJobsClient):
        self.jobs = jobs
        self._ack = 0
        self._told: set[tuple[str, str]] = set()
        self.busy = 0              # job_* calls in progress
        self.watch_seen: dict[str, int] = {}     # shared with job_wait

    def knows(self, event: str, jid: str | None, h: int = 0) -> bool:
        """Has some tool result told the agent about this event?"""
        if event == EVENT_WATCH:
            return h <= self.watch_seen.get(short(jid or ""), 0)
        return (event, short(jid or "")) in self._told

    def _tell(self, event: str, jids) -> None:
        self._told |= {(event, short(j)) for j in jids}

    def expect_finish(self, jids) -> None:
        """The agent ended these jobs itself; that is no news to it."""
        self._tell(EVENT_FINISHED, jids)

    def attach(self, result: dict) -> dict:
        told = compact.final_jobs(result)
        self._tell(EVENT_FINISHED, told)
        try:
            notices = self.jobs.call("notices", ack=self._ack)["notices"]
        except (PlutoError, OSError):
            return result
        if not notices:
            return result
        self._ack = max(n["seq"] for n in notices)
        fresh = [self._shape(n) for n in notices
                 if not (n["event"] in (EVENT_FINISHED, EVENT_WATCH)
                         and self.knows(n["event"], n["job_id"], n.get("h", 0)))]
        for n in notices:
            if n["event"] == EVENT_WATCH:
                jid = short(n["job_id"])
                self.watch_seen[jid] = max(n["h"], self.watch_seen.get(jid, 0))
            else:
                self._tell(n["event"], [n["job_id"]])
        return dict(result, **{NOTICE_KEY: fresh}) if fresh else result

    @staticmethod
    def _shape(n: dict) -> dict:
        out = {k: n[k] for k in _FIELDS if n.get(k) is not None}
        out["job_id"] = short(n["job_id"])
        if n["event"] == EVENT_WATCH:
            out["hit"] = compact.watch_hit(n)
            if n.get("did"):
                out["did"] = n["did"]
            out.pop("state")
        return out
