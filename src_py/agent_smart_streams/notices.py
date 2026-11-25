"""Delivery of durable job notices on tool results."""

from __future__ import annotations

from agent_smart_streams import compact
from agent_smart_streams.constants import NOTICE_KEY
from agent_smart_streams.compact import short
from pluto_jobs_def import EVENT_FINISHED
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

    def knows(self, event: str, jid: str | None) -> bool:
        """Has some tool result told the agent about this event?"""
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
        fresh = [dict({k: n[k] for k in _FIELDS if n.get(k) is not None},
                      job_id=short(n["job_id"]))
                 for n in notices
                 if not (n["event"] == EVENT_FINISHED and self.knows(n["event"], n["job_id"]))]
        for n in notices:
            self._tell(n["event"], [n["job_id"]])
        return dict(result, **{NOTICE_KEY: fresh}) if fresh else result
