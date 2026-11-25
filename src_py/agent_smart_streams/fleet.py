"""Working with many jobs at once.

An agent that supervises a dozen streams must not pay one tool call per
job, nor be told the same event twice.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent_smart_streams import compact
from agent_smart_streams.constants import FLEET_THREADS, MIN_MATCHES_PER_JOB
from pluto_jobs_def import FINAL_STATES
from pluto_sdk import PlutoError, PlutoJobsClient


def ids_of(job_id: str | None, job_ids: list[str] | None) -> list[str]:
    """The distinct ids a call names; raises when it names none."""
    ids = list(dict.fromkeys(job_ids or ([job_id] if job_id else [])))
    if not ids:
        raise PlutoError("give job_id or job_ids")
    return ids


def each(fn, keys: list) -> dict:
    """{key: fn(key)} in parallel; one failure does not hide the others."""
    def one(key):
        try:
            return fn(key)
        except PlutoError as exc:
            return {"error": str(exc)}
    with ThreadPoolExecutor(min(FLEET_THREADS, len(keys))) as pool:
        return dict(zip(keys, pool.map(one, keys)))


def one_or_many(fn, job_id, job_ids):
    """Plain result for one job, a map for several."""
    ids = ids_of(job_id, job_ids)
    return fn(ids[0]) if len(ids) == 1 and not job_ids else each(fn, ids)


def grep(jobs: PlutoJobsClient, job_id, job_ids, *, max_matches: int, **kw):
    """The match limit covers the whole call."""
    ids = ids_of(job_id, job_ids)
    share = max(MIN_MATCHES_PER_JOB, max_matches // len(ids))
    limit = max_matches if len(ids) == 1 else share

    def one(jid):
        return compact.grep(jobs.call("grep", job_id=jid, max_matches=limit, **kw))
    return one_or_many(one, job_id, job_ids)


class Waiter:
    """job_wait that reports each event once per session.

    A pattern hit is not repeated (the next wait looks after it), a finished
    job is reported once and then left out, and an idle job is reported
    again only after it printed something new. from_offset switches this
    memory off for that call.
    """

    def __init__(self, jobs: PlutoJobsClient):
        self.jobs = jobs
        self._marks: dict[tuple, int] = {}
        self._finished: set[str] = set()

    def wait(self, job_id, job_ids, *, until="exit", pattern=None,
             wait_all=False, from_offset=None, **kw) -> dict:
        ids = ids_of(job_id, job_ids)
        fresh = [j for j in ids if j not in self._finished]
        # Naming only finished jobs is a question about them, not a wait.
        ask = ids if wait_all or not fresh else fresh
        marks = {} if from_offset is not None else {
            j: self._marks[j, until, pattern] for j in ask
            if (j, until, pattern) in self._marks}
        r = self.jobs.wait(job_ids=ask, until=until, pattern=pattern,
                           all=wait_all or None, from_offset=from_offset,
                           from_offsets=marks or None, **kw)
        self._remember(r, until, pattern)
        return r

    def _remember(self, r: dict, until, pattern) -> None:
        size = {j["job_id"]: j["size"] for j in r["jobs"]}
        for j in r["jobs"]:
            if j["state"] in FINAL_STATES and j["job_id"] in r["hits"] \
                    and r["hits"][j["job_id"]]["reason"] == "exit":
                self._finished.add(j["job_id"])
        for jid, h in r["hits"].items():
            if h["reason"] == "pattern":
                self._marks[jid, until, pattern] = h["next"]
            elif h["reason"] in ("idle", "output"):
                self._marks[jid, until, pattern] = size.get(jid, 0)
