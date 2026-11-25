"""Delivery of durable job notices on tool results."""

from __future__ import annotations

from agent_smart_streams import compact
from agent_smart_streams.constants import NOTICE_KEY
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

    def attach(self, result: dict) -> dict:
        try:
            notices = self.jobs.call("notices", ack=self._ack)["notices"]
        except (PlutoError, OSError):
            return result
        if not notices:
            return result
        self._ack = max(n["seq"] for n in notices)
        told = compact.final_jobs(result)
        fresh = [{k: n[k] for k in _FIELDS if n.get(k) is not None}
                 for n in notices
                 if not (n["event"] == EVENT_FINISHED and n["job_id"] in told)]
        return dict(result, **{NOTICE_KEY: fresh}) if fresh else result
