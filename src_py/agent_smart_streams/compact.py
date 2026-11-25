"""Token-frugal shapes for tool results.

Everything an agent receives stays in its context and is re-read on every
later turn, so results carry only what the agent can act on. The full
records remain available from job_open and over HTTP.
"""

from __future__ import annotations

import json

FINAL = {"succeeded", "failed", "cancelled", "signaled", "timed_out",
         "output_limit", "lost"}

_BRIEF = ("job_id", "name", "state", "activity", "reason", "exit_code",
          "signal", "size")


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def brief(job: dict) -> dict:
    out = {k: job[k] for k in _BRIEF if job.get(k) is not None}
    out["elapsed_s"] = round(job.get("elapsed_ms", 0) / 1000, 1)
    return out


def started(r: dict) -> dict:
    return {k: r[k] for k in ("job_id", "name", "state") if r.get(k) is not None}


def status(r: dict) -> dict:
    out = brief(r)
    for k in ("owner", "cursor", "unread", "parent"):
        if r.get(k) is not None:
            out[k] = r[k]
    return out


def waited(r: dict) -> dict:
    out = {"jobs": [brief(j) for j in r["jobs"]]}
    hits = {}
    for jid, h in r["hits"].items():
        if h["reason"] == "pattern":
            hits[jid] = f"{h['line']}@{h['offset']}:{h['text']}"
        else:
            hits[jid] = h["reason"]
    if hits:
        out["hits"] = hits
    if r["timed_out"]:
        out["timed_out"] = True
    return out


def listed(r: dict) -> dict:
    return {"total": r["total"],
            "jobs": [dict(brief(j), owner=j.get("owner")) for j in r["jobs"]]}


def read(r: dict, hashes: bool = False) -> dict:
    drop = set() if hashes else {"sha256"}
    drop |= {k for k in ("eof", "ends_mid_line") if not r.get(k)}
    return {k: v for k, v in r.items() if k not in drop}


def grep(r: dict) -> dict:
    """One string per line, grep -n style: 12@345:match, 13-context."""
    groups = ["\n".join(
        f"{e['line']}@{e['offset']}:{e['text']}" if e["match"]
        else f"{e['line']}-{e['text']}" for e in g) for g in r["groups"]]
    context = any(not e["match"] for g in r["groups"] for e in g)
    out = {"matches": r["matches"],
           "lines": ("\n--\n" if context else "\n").join(groups)}
    if not r["complete"]:
        out["resume_at"] = r["resume_at"]
    if r["lines_over_regex_limit"]:
        out["lines_over_regex_limit"] = r["lines_over_regex_limit"]
    if r["state"] not in FINAL:
        out["state"] = r["state"]
    return out


def final_jobs(r: dict) -> set:
    """Ids whose final state this result already tells the agent."""
    jobs = r.get("jobs") if isinstance(r.get("jobs"), list) else [r]
    return {j["job_id"] for j in jobs if isinstance(j, dict)
            and j.get("job_id") and j.get("state") in FINAL}
