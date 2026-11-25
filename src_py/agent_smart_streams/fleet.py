"""Working with many jobs at once.

An agent that supervises a dozen streams must not pay one tool call per
job, nor be told the same event twice.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from agent_smart_streams import compact
from agent_smart_streams.compact import short
from agent_smart_streams.constants import (
    FLEET_THREADS, GROUP_RE, LAST_LINE_BYTES, MIN_MATCHES_PER_JOB,
    WAIT_TOTAL_MAX_S,
)
from agent_smart_streams.submit import members
from pluto_jobs_def import FINAL_STATES, WAIT_MAX_S
from pluto_sdk import PlutoError, PlutoJobsClient


def ids_of(jobs: PlutoJobsClient, job_id, job_ids) -> list[str]:
    """The distinct ids a call names, groups expanded; raises when none."""
    ids = list(dict.fromkeys(job_ids or ([job_id] if job_id else [])))
    if not ids:
        raise PlutoError("give job_id or job_ids")
    return members(jobs, ids)


def each(fn, keys: list) -> dict:
    """{key: fn(key)} in parallel; one failure does not hide the others."""
    def one(key):
        try:
            return fn(key)
        except PlutoError as exc:
            return {"error": str(exc)}
    with ThreadPoolExecutor(min(FLEET_THREADS, len(keys))) as pool:
        return dict(zip(keys, pool.map(one, keys)))


def one_or_many(jobs: PlutoJobsClient, fn, job_id, job_ids):
    """Plain result for one job, a map for several."""
    ids = ids_of(jobs, job_id, job_ids)
    one = job_id and not job_ids and not re.fullmatch(GROUP_RE, job_id)
    return fn(ids[0]) if one else each(fn, ids)


def control(jobs: PlutoJobsClient, job_id, job_ids, **kw) -> dict:
    """{"ok": [ids], "errors": {id: reason}}, child jobs included."""
    replies = each(lambda j: jobs.call("control", job_id=j, **kw)["jobs"],
                   ids_of(jobs, job_id, job_ids))
    ok, errors = [], {}
    for asked, reply in replies.items():
        for jid, what in reply.items():
            if jid == "error":
                errors[asked] = what
            elif what == "ok":
                ok.append(short(jid))
            else:
                errors[short(jid)] = what
    return {"ok": ok, **({"errors": errors} if errors else {})}


def grep(jobs: PlutoJobsClient, job_id, job_ids, *, max_matches: int, **kw):
    """The match limit covers the whole call."""
    ids = ids_of(jobs, job_id, job_ids)
    share = max(min(MIN_MATCHES_PER_JOB, max_matches), max_matches // len(ids))
    limit = max_matches if len(ids) == 1 else share

    def one(jid):
        return compact.grep(jobs.call("grep", job_id=jid, max_matches=limit, **kw))
    r = one_or_many(jobs, one, job_id, job_ids)
    if "lines" in r or len(ids) == 1:
        return r
    empty = [j for j, g in r.items() if g.get("matches") == 0
             and "resume_at" not in g and "lines_over_regex_limit" not in g]
    out = {j: g for j, g in r.items() if j not in set(empty)}
    return {**out, "no_match": empty} if empty else out


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
             wait_all=False, from_offset=None, timeout_s=0, **kw) -> dict:
        ids = [short(j) for j in ids_of(self.jobs, job_id, job_ids)]
        end = time.monotonic() + max(1, min(int(timeout_s), WAIT_TOTAL_MAX_S))
        while True:
            left = min(end - time.monotonic(), WAIT_MAX_S)
            r = self._once(ids, until, pattern, wait_all, from_offset,
                           timeout_ms=max(1, int(left * 1000)), **kw)
            if not r["timed_out"] or time.monotonic() >= end - 0.5:
                # Only what is returned counts as told.
                self._remember(r, until, pattern)
                return self._with_last_lines(r)

    def _with_last_lines(self, r: dict) -> dict:
        """The last line of a job usually says how it went."""
        def last(jid):
            w = self.jobs.call("window", job_id=jid, tail=1, strip_ansi=True,
                               max_bytes=LAST_LINE_BYTES)
            return w.get("text", "").strip()[:LAST_LINE_BYTES]
        done = [j for j, h in r["hits"].items() if h["reason"] == "exit"]
        for jid, text in (each(last, done) if done else {}).items():
            if isinstance(text, str) and text:
                r["hits"][jid]["last"] = text
        return r

    def _once(self, ids, until, pattern, wait_all, from_offset, **kw) -> dict:
        fresh = [j for j in ids if j not in self._finished]
        # Naming only finished jobs is a question about them, not a wait.
        ask = ids if wait_all or not fresh else fresh
        marks = {} if from_offset is not None else {
            j: self._marks[j, until, pattern] for j in ask
            if (j, until, pattern) in self._marks}
        r = self.jobs.wait(job_ids=ask, until=until, pattern=pattern,
                           all=wait_all or None, from_offset=from_offset,
                           from_offsets=marks or None, **kw)
        return r

    def _remember(self, r: dict, until, pattern) -> None:
        size = {j["job_id"]: j["size"] for j in r["jobs"]}
        for j in r["jobs"]:
            if j["state"] in FINAL_STATES and j["job_id"] in r["hits"] \
                    and r["hits"][j["job_id"]]["reason"] == "exit":
                self._finished.add(short(j["job_id"]))
        for jid, h in r["hits"].items():
            if h["reason"] == "pattern":
                self._marks[short(jid), until, pattern] = h["next"]
            elif h["reason"] in ("idle", "output"):
                self._marks[short(jid), until, pattern] = size.get(jid, 0)
