"""Submitting many jobs without spelling out each one.

A template and its parameters describe a whole batch; the batch gets one
group handle, and a name if wanted, that stand for all its jobs in later
calls. The server starts a batch in one request.
"""

from __future__ import annotations

import itertools
import os
import re
import secrets

from agent_smart_streams import names
from agent_smart_streams.compact import short
from agent_smart_streams.constants import SUBMIT_MAX
from pluto_sdk import PlutoError, PlutoJobsClient
from pluto_sdk.jobs import smartstreams_home


def expand(template: str, each: dict) -> list[dict]:
    """A job {name, command, params} for every combination of the
    parameters. Only {key} of a given key is replaced, so other braces stay
    as they are."""
    missing = [k for k in each if "{%s}" % k not in template]
    if missing or not each:
        raise PlutoError(f"command has no {{{'}, {'.join(missing)}}}"
                         if missing else "each is empty")
    pattern = re.compile("|".join(re.escape("{%s}" % k) for k in each))
    out = []
    for combo in itertools.product(*(names.values(v, SUBMIT_MAX) for v in each.values())):
        params = dict(zip(each, combo))
        command = pattern.sub(lambda m: params[m[0][1:-1]], template)
        out.append({"name": ":".join(combo), "command": command, "params": params})
        if len(out) > SUBMIT_MAX:
            raise PlutoError(f"more than {SUBMIT_MAX} jobs in one call")
    if not out:
        raise PlutoError("each has a parameter without values")
    return out


def start_all(jobs: PlutoJobsClient, batch: list[dict], spec: dict,
              name: str | None = None) -> dict:
    """Start the jobs under one group handle. Jobs that cannot be started
    do not hide those that were."""
    group = "g" + secrets.token_hex(3)
    tags = [*(spec.get("tags") or []), group] + ([names.tag(name)] if name else [])
    r = jobs.start(batch=batch, **dict(spec, tags=tags))["jobs"]
    started = [(short(x["job_id"]), x.get("name")) for x in r if "job_id" in x]
    out = {"group": group, "n": len(started)}
    if name:
        out["name"] = name
        out["jobs"] = names.ranges([n or j for j, n in started])
    else:
        out["jobs"] = " ".join(f"{n}={j}" if n else j for j, n in started)
    failed = {x.get("name") or str(i): x["error"] for i, x in enumerate(r) if "error" in x}
    if failed:
        out["failed"] = failed
    return out


def start_one(jobs: PlutoJobsClient, spec: dict, name: str | None = None) -> dict:
    if name and names.is_name(name) and "/" not in name:
        spec = dict(spec, tags=[*(spec.get("tags") or []), names.NAME_TAG + name])
    return jobs.start(name=name, **spec)


def output_path(jid: str) -> dict:
    """Where the raw log of a job is, when it is on this machine. A shell
    glob, so that one line serves a whole batch."""
    root = os.path.join(smartstreams_home(), "jobs")
    return {"output": os.path.join(root, f"*{jid}", "output.log")} \
        if os.path.isdir(root) else {}
