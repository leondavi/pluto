"""Submitting many jobs without spelling out each one.

A template and its parameters describe a whole batch; the batch gets one
group handle, and a name if wanted, that stand for all its jobs in later
calls. The server starts a batch in one request.
"""

from __future__ import annotations

import itertools
import math
import os
import re
import secrets

from agent_smart_tasks import names
from agent_smart_tasks.compact import short
from agent_smart_tasks.constants import SUBMIT_MAX
from pluto_sdk import PlutoError, PlutoJobsClient
from pluto_sdk.jobs import smarttasks_home


def expand(template: str, each: dict) -> list[dict]:
    """A job {name, command, params} for every combination of the
    parameters. Only {key} of a given key is replaced, so other braces stay
    as they are. A key the command does not use names the job (and is
    there for rules), but then there must be one job only: identical jobs
    are a mistake."""
    if not each:
        raise PlutoError("each is empty")
    lists = [names.values(v, SUBMIT_MAX) for v in each.values()]
    if math.prod(map(len, lists)) > SUBMIT_MAX:
        raise PlutoError(f"more than {SUBMIT_MAX} jobs in one call")
    combos = list(itertools.product(*lists))
    missing = [k for k in each if "{%s}" % k not in template]
    if missing and len(combos) > 1:
        raise PlutoError(f"command has no {{{'}, {'.join(missing)}}}, so all "
                         f"{len(combos)} jobs would be the same")
    out = []
    for combo in combos:
        params = dict(zip(each, combo))
        out.append({"name": ":".join(combo), "command": fill(template, params),
                    "params": params})
    if not out:
        raise PlutoError("each has a parameter without values")
    return out


def fill(template: str, params: dict) -> str:
    """{key} of the given keys replaced; other braces stay."""
    if not params:
        return template
    pattern = re.compile("|".join(re.escape("{%s}" % k) for k in params))
    return pattern.sub(lambda m: params[m[0][1:-1]], template)


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
    root = os.path.join(smarttasks_home(), "jobs")
    return {"output": os.path.join(root, f"*{jid}", "output.log")} \
        if os.path.isdir(root) else {}
