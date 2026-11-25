"""Token-frugal shapes for tool results.

Everything an agent receives stays in its context and is re-read on every
later turn, so results carry only what the agent can act on. The full
records remain available from job_open and over HTTP.
"""

from __future__ import annotations

import json

from pluto_jobs_def import FINAL_STATES, SHORT_ID_LEN

_FINAL = frozenset(FINAL_STATES)

_BRIEF = ("job_id", "name", "state", "activity", "reason", "exit_code",
          "signal", "size", "output_truncated", "leader_exit_code")


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def short(jid: str) -> str:
    """The form of a job id that agents see; every call accepts it."""
    return jid[-SHORT_ID_LEN:]


def brief(job: dict) -> dict:
    out = {k: job[k] for k in _BRIEF if job.get(k) is not None}
    out["job_id"] = short(job["job_id"])
    if job.get("state") in _FINAL:
        out.pop("activity", None)
        if job["state"] == "succeeded":
            out.pop("exit_code", None)
            out.pop("leader_exit_code", None)
    out["elapsed_s"] = round(job.get("elapsed_ms", 0) / 1000, 1)
    return out


def started(r: dict) -> dict:
    out = {k: r[k] for k in ("name", "state") if r.get(k) is not None}
    return {"job_id": short(r["job_id"]), **out}


def status(r: dict) -> dict:
    out = brief(r)
    for k in ("owner", "cursor", "unread", "parent"):
        if r.get(k) is not None:
            out[k] = r[k]
    return out


def waited(r: dict) -> dict:
    """Only the jobs something happened to, plus a count of the rest."""
    hits, rest = [], {}
    for job in r["jobs"]:
        h = r["hits"].get(job["job_id"])
        if h is None:
            rest[job["state"]] = rest.get(job["state"], 0) + 1
            continue
        out = dict(brief(job), hit=h["reason"])
        if h["reason"] == "exit":
            del out["hit"]      # the state says it
        if h.get("last"):
            out["last"] = h["last"]
        if h["reason"] == "pattern":
            out["hit"] = f"{h['line']}@{h['offset']}:{h['text']}"
        hits.append(out)
    out = {"jobs": hits}
    if rest:
        out["others"] = rest
    if r["timed_out"]:
        out["timed_out"] = True
    return out


def listed(r: dict) -> dict:
    def row(j: dict) -> str:
        b = brief(j)
        return " ".join(str(x) for x in (
            b["job_id"], b.get("name", "-"), b["state"], f"{b['elapsed_s']}s",
            f"{b.get('size', 0)}B", j.get("owner")) if x is not None)
    return {"total": r["total"], "jobs": [row(j) for j in r["jobs"]]}


def read(r: dict, hashes: bool = False) -> dict:
    drop = set() if hashes else {"sha256"}
    drop |= {k for k in ("eof", "ends_mid_line") if not r.get(k)}
    return {k: v for k, v in r.items() if k not in drop}


def grep_lines(groups: list, offsets: bool = True) -> str:
    """grep -n style text: 12@345:match (or 12:match), 13-context."""
    def line(e: dict) -> str:
        if not e["match"]:
            return f"{e['line']}-{e['text']}"
        at = f"@{e['offset']}" if offsets else ""
        return f"{e['line']}{at}:{e['text']}"

    context = any(not e["match"] for g in groups for e in g)
    return ("\n--\n" if context else "\n").join(
        "\n".join(map(line, g)) for g in groups)


def grep(r: dict) -> dict:
    out = {"matches": r["matches"], "lines": grep_lines(r["groups"])}
    if not r["complete"]:
        out["resume_at"] = r["resume_at"]
    if r["lines_over_regex_limit"]:
        out["lines_over_regex_limit"] = r["lines_over_regex_limit"]
    if r["state"] not in _FINAL:
        out["state"] = r["state"]
    return out


def final_jobs(r: dict) -> set:
    """Short ids whose final state this result already tells the agent."""
    jobs = r.get("jobs") if isinstance(r.get("jobs"), list) else [r]
    rows = [j for j in jobs if isinstance(j, dict)]
    waited = r.get("waited")
    if isinstance(waited, dict):
        rows += waited["jobs"]
    return {short(j["job_id"]) for j in rows
            if j.get("job_id") and j.get("state") in _FINAL}
