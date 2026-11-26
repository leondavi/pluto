"""What happened to an agent's jobs since its last pss command.

An agent that is busy with something else learns this way that a job ended
or was handed to it, without asking: one line on stderr after whatever pss
command it runs next, so pipes stay clean. It is the durable notices of the
server, each said once. Watch hits are left to pss wait, which reports them;
followers and jobs the agent stopped are no news. Inside a job (a follower,
a script that pss started) nothing is said or acknowledged: that output is
read by a program, and the news belongs to the agent.
"""

from __future__ import annotations

import os
import sys

from agent_smart_streams import names
from agent_smart_streams.lease import state_path
from pluto_jobs_def import EVENT_FINISHED, EVENT_TRANSFERRED
from pluto_sdk import PlutoError, PlutoJobsClient

#: Set by the runner in every job.
IN_JOB_ENV = "PLUTO_JOB_ID"
#: Ended this way, the agent did it itself (or a rule did, which pss wait tells).
_NO_NEWS = ("cancelled",)


def tell(jobs: PlutoJobsClient, quiet: bool = False) -> None:
    """Print what is new; *quiet*: only mark it seen (the command already
    showed the jobs)."""
    if os.environ.get(IN_JOB_ENV):
        return
    path = state_path(jobs.agent_id, ".news")
    try:
        notices = jobs.call("notices", ack=_read(path))["notices"]
    except (PlutoError, OSError):
        return
    if not notices:
        return
    _write(path, max(n["seq"] for n in notices))
    line = summary(notices)
    if line and not quiet:
        print(f"pss: meanwhile {line}", file=sys.stderr)


def summary(notices: list[dict]) -> str:
    """"ended: z/3 failed, z/1-2 succeeded; given to you: x/4"."""
    ended: dict[tuple, list[str]] = {}
    given: list[str] = []
    for n in notices:
        card = {"job_id": n["job_id"], "name": n.get("name"), "tags": n.get("tags")}
        if n["event"] == EVENT_FINISHED and not n.get("follows") \
                and n.get("state") not in _NO_NEWS:
            ended.setdefault((names.group_of(card), n["state"]), []).append(
                names.key_of(card))
        elif n["event"] == EVENT_TRANSFERRED:
            given.append(names.label(card))
    parts = []
    if ended:
        parts.append("ended: " + ", ".join(
            f"{group + '/' if group else ''}{names.ranges(keys)} {state}"
            for (group, state), keys in ended.items()))
    if given:
        parts.append("handed over: " + " ".join(dict.fromkeys(given)))
    return "; ".join(parts)


def _read(path: str) -> int:
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write(path: str, seq: int) -> None:
    tmp = f"{path}.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.write(str(seq))
        os.replace(tmp, path)
    except OSError:
        pass
