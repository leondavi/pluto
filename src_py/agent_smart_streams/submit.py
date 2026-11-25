"""Submitting many jobs without spelling out each one.

A template and its parameters describe a whole batch; the batch gets one
group handle that stands for all its jobs in later calls.
"""

from __future__ import annotations

import itertools
import os
import re
import secrets

from agent_smart_streams.compact import short
from agent_smart_streams.constants import GROUP_RE, GROUP_MAX, SUBMIT_MAX
from pluto_sdk import PlutoError, PlutoJobsClient
from pluto_sdk.jobs import smartstreams_home

_RANGE = re.compile(r"(-?\d+)-(-?\d+)")


def values(spec) -> list[str]:
    """"1-4,8,x" or a list of such items: inclusive integer ranges and
    literals, in order. A leading zero keeps its width ("01-12")."""
    items = spec if isinstance(spec, list) else str(spec).split(",")
    out: list[str] = []
    for item in map(str, items):
        m = _RANGE.fullmatch(item.strip()) if isinstance(spec, str) else None
        if not m:
            out.append(item.strip() if isinstance(spec, str) else item)
            continue
        lo, hi = int(m[1]), int(m[2])
        if hi < lo or hi - lo >= SUBMIT_MAX:
            raise PlutoError(f"bad range {item!r} (at most {SUBMIT_MAX} values)")
        width = len(m[1]) if m[1].startswith("0") else 0
        out += [str(n).zfill(width) for n in range(lo, hi + 1)]
    return out


def expand(template: str, each: dict) -> list[tuple[str, str]]:
    """[(name, command)] for every combination of the parameters. Only
    {key} of a given key is replaced, so other braces stay as they are."""
    missing = [k for k in each if "{%s}" % k not in template]
    if missing or not each:
        raise PlutoError(f"command has no {{{'}, {'.join(missing)}}}"
                         if missing else "each is empty")
    pattern = re.compile("|".join(re.escape("{%s}" % k) for k in each))
    out = []
    for combo in itertools.product(*(values(v) for v in each.values())):
        at = {"{%s}" % k: v for k, v in zip(each, combo)}
        out.append(("-".join(combo), pattern.sub(lambda m: at[m[0]], template)))
        if len(out) > SUBMIT_MAX:
            raise PlutoError(f"more than {SUBMIT_MAX} jobs in one call")
    if not out:
        raise PlutoError("each has a parameter without values")
    return out


def start_all(jobs: PlutoJobsClient, batch: list, spec: dict) -> dict:
    """Start (name, command) pairs under one group tag. Jobs started before
    a failure are still reported: they are running."""
    group = "g" + secrets.token_hex(3)
    spec = dict(spec, tags=[*(spec.get("tags") or []), group])
    started, failed = [], {}
    for i, (name, command) in enumerate(batch):
        try:
            r = jobs.start(command=command, name=name, **spec)
            started.append((short(r["job_id"]), name))
        except PlutoError as exc:
            if not started and not failed:
                raise      # most likely wrong for all of them
            failed[name or str(i)] = str(exc)
    named = any(n for _, n in started)
    out = {"group": group, "n": len(started),
           "jobs": " ".join(f"{n}={j}" if named else j for j, n in started)}
    if failed:
        out["failed"] = failed
    return out


def output_path(jid: str) -> dict:
    """Where the raw log of a job is, when it is on this machine. A shell
    glob, so that one line serves a whole batch."""
    root = os.path.join(smartstreams_home(), "jobs")
    return {"output": os.path.join(root, f"*{jid}", "output.log")} \
        if os.path.isdir(root) else {}


def members(jobs: PlutoJobsClient, handles: list[str]) -> list[str]:
    """Job ids with group handles replaced by their jobs, oldest first."""
    out: list[str] = []
    for h in handles:
        if not re.fullmatch(GROUP_RE, h):
            out.append(h)
            continue
        found = jobs.call("list", scope="all", tags=[h], limit=GROUP_MAX)["jobs"]
        if not found:
            raise PlutoError(f"no such group: {h}")
        out += [short(j["job_id"]) for j in
                sorted(found, key=lambda j: j["opened_at"])]
    return list(dict.fromkeys(out))
