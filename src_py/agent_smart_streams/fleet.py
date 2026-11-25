"""Working with many jobs at once.

An agent that supervises a dozen streams must not pay one tool call per
job, nor be told the same event twice.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from agent_smart_streams import compact
from agent_smart_streams.compact import short
from agent_smart_streams.constants import (
    ACTION_ALIASES, FLEET_THREADS, GREP_MATCHES_CALL, GREP_MATCHES_MANY,
    LAST_LINE_BYTES, WAIT_TOTAL_MAX_S,
)
from agent_smart_streams.names import is_name, members
from pluto_jobs_def import ERR_FINISHED, FINAL_STATES, GREP_MATCHES, WAIT_MAX_S
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
    one = job_id and not job_ids and not is_name(job_id)
    return fn(ids[0]) if one else each(fn, ids)


def control(jobs: PlutoJobsClient, job_id, job_ids, *, action, **kw) -> dict:
    """{"ok": [ids], "finished": [ids], "errors": {id: reason}}, child jobs
    included. Ending a job that already ended is not an error."""
    action = ACTION_ALIASES.get(action, action)
    ends = action in ("cancel", "halt")
    replies = each(lambda j: jobs.call("control", job_id=j, action=action, **kw)["jobs"],
                   ids_of(jobs, job_id, job_ids))
    ok, finished, errors = [], [], {}
    for asked, reply in replies.items():
        for jid, what in reply.items():
            if ends and what == ERR_FINISHED:
                finished.append(asked if jid == "error" else short(jid))
            elif jid == "error":
                errors[asked] = what
            elif what == "ok":
                ok.append(short(jid))
            else:
                errors[short(jid)] = what
    return {"ok": ok, **({"finished": finished} if finished else {}),
            **({"errors": errors} if errors else {})}


def filtered(rows: list, owner=None, name=None, live=False) -> list:
    return [r for r in rows
            if (not owner or r.get("owner") == owner)
            and (not name or name in (r.get("name") or ""))
            and (not live or r["state"] not in FINAL_STATES)]


def grep(jobs: PlutoJobsClient, job_id, job_ids, *, max_matches=None, **kw):
    """The match limit is per job: sharing one limit among jobs would return
    partial answers that have to be asked again."""
    ids = ids_of(jobs, job_id, job_ids)
    if len(ids) > 1 and (kw.get("from_offset") or kw.get("to_offset")):
        raise PlutoError("offsets differ per job; grep one job, or use lines")
    limit = max_matches or (GREP_MATCHES if len(ids) == 1 else GREP_MATCHES_MANY)
    if limit * len(ids) > GREP_MATCHES_CALL:
        raise PlutoError(f"max_matches x jobs exceeds {GREP_MATCHES_CALL}; "
                         "lower it, or let job_analyze summarise")

    def one(jid):
        return compact.grep(jobs.call("grep", job_id=jid, max_matches=limit, **kw))
    r = one_or_many(jobs, one, job_id, job_ids)
    if "lines" in r or len(ids) == 1:
        return r
    empty = [j for j, g in r.items() if g.get("matches") == 0
             and "resume_at" not in g and "lines_over_regex_limit" not in g]
    out = {j: g for j, g in r.items() if j not in set(empty)}
    return {**out, "no_match": empty} if empty else out


def analyze(jobs: PlutoJobsClient, job_id, job_ids, *, resume=None, **kw) -> dict:
    """One query over all the jobs; positions are per job."""
    ids = ids_of(jobs, job_id, job_ids)
    lists = {k: _list(kw.pop(k)) for k in ("where", "by", "agg", "compare")}
    r = jobs.analyze(job_ids=ids, from_offsets=resume, **lists, **kw)
    return {k: r[k] for k in ("out", "resume") if k in r}


def watch(jobs: PlutoJobsClient, action, job_id, job_ids, **kw) -> dict:
    ids = ids_of(jobs, job_id, job_ids)
    lists = {k: _list(kw.pop(k)) for k in ("where", "by")}
    r = jobs.watch(action, job_ids=ids, **lists, **kw)
    if action == "list":
        return {short(j): compact.watch_rules(v) for j, v in r["jobs"].items()}
    # The same rule has the same id almost everywhere: say it once.
    by_rule: dict[str, list] = {}
    for jid, rule in r["jobs"].items():
        key = rule if isinstance(rule, str) else ",".join(rule)
        by_rule.setdefault(key, []).append(short(jid))
    out = {"removed" if action == "remove" else "added":
           {k: v if len(v) < 5 else f"{len(v)} jobs" for k, v in by_rule.items()}}
    if r["errors"]:
        out["errors"] = {short(j): e for j, e in r["errors"].items()}
    return out


def _list(v):
    return [v] if isinstance(v, str) else v or None


class Waiter:
    """job_wait that reports each event once per session.

    A pattern hit is not repeated (the next wait looks after it), a finished
    job is reported once and then left out, and an idle job is reported
    again only after it printed something new. from_offset rewinds one job.
    """

    def __init__(self, jobs: PlutoJobsClient):
        self.jobs = jobs
        self._marks: dict[tuple, int] = {}
        self._finished: set[str] = set()
        self.watch_seen: dict[str, int] = {}     # job -> last hit told

    def wait(self, job_id, job_ids, *, until="exit", pattern=None,
             wait_all=False, from_offset=None, timeout_s=0,
             limit_s=WAIT_TOTAL_MAX_S, **kw) -> dict:
        ids = [short(j) for j in ids_of(self.jobs, job_id, job_ids)]
        if from_offset is not None and len(ids) > 1:
            raise PlutoError("from_offset is for one job; without it every "
                             "event of every job is told once")
        end = time.monotonic() + max(1, min(int(timeout_s), limit_s))
        while True:
            left = min(end - time.monotonic(), WAIT_MAX_S)
            r = self._once(ids, until, pattern, wait_all, from_offset,
                           timeout_ms=max(1, int(left * 1000)), **kw)
            if not r["timed_out"] or time.monotonic() >= end - 0.5:
                # Only what is returned counts as told.
                self._remember(r, until, pattern)
                return self._with_last_lines(r)

    def follow(self, ids: list, timeout_s: float) -> dict:
        """Until all jobs ended, or a rule wants attention: {"ended": {short
        id: last line}, "hits": {short id: [hit]}, "timed_out": bool}. Jobs
        that end one by one do not end the wait."""
        end = time.monotonic() + timeout_s
        left, ended, hits = [short(j) for j in ids], {}, {}
        while left and not hits and time.monotonic() < end - 0.5:
            wait_s = max(0.001, min(end - time.monotonic(), WAIT_MAX_S))
            r = self._once(left, "follow", None, False, None, timeout_ms=int(wait_s * 1000))
            self._remember(r, "follow", None)
            for jid, h in self._with_last_lines(r)["hits"].items():
                if h["reason"] != "watch":
                    ended[short(jid)] = h.get("last", "")
                else:
                    hits[short(jid)] = h["watch"]
            left = [j for j in left if j not in ended]
        return {"ended": ended, "hits": hits, "timed_out": bool(left) and not hits}

    def state(self) -> dict:
        """What was told so far, for a waiter that outlives this process."""
        return {"marks": [[*k, v] for k, v in self._marks.items()],
                "finished": sorted(self._finished), "watch": self.watch_seen}

    def restore(self, state: dict) -> None:
        for jid, until, pattern, v in state.get("marks", []):
            k = (jid, until, pattern)
            self._marks[k] = max(v, self._marks.get(k, 0))
        self._finished |= set(state.get("finished", []))
        for jid, v in state.get("watch", {}).items():
            self.watch_seen[jid] = max(v, self.watch_seen.get(jid, 0))

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
        if until in ("watch", "follow"):
            # Hits have their own numbering; offsets do not apply to it.
            return self.jobs.wait(
                job_ids=ask, until=until, all=wait_all or None, **kw,
                from_offsets={j: self.watch_seen[j] for j in ask
                              if j in self.watch_seen} or None)
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
            elif h["reason"] == "watch":
                self.watch_seen[short(jid)] = h["next"]
            elif h["reason"] in ("idle", "output"):
                self._marks[short(jid), until, pattern] = size.get(jid, 0)
