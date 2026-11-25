"""A program of the agent's own on live output, one per job.

The program is a job itself and reads the followed job's output from the
beginning, so it may be wrong at first: following again ends it and starts
the corrected one from byte 0, and the followed job never notices. A program
piped into the command instead (``prog | filter``) cannot be replaced, and
takes the output away from everybody else.
"""

from __future__ import annotations

import sys
import time

from agent_smart_streams import each, fleet, names, render, submit
from agent_smart_streams.compact import short
from agent_smart_streams.constants import (
    FOLLOW_CHECK_S, FOLLOW_END_MS, FOLLOW_POLL_S, GROUP_MAX,
)
from pluto_jobs_def import LIVE_STATES
from pluto_sdk import PlutoError, PlutoJobsClient

#: Parameter of a follower that names the job it follows.
_OF = "follows"


def _earlier(jobs: PlutoJobsClient, name: str, followed: set[str]) -> list[dict]:
    """Live followers of these jobs that a new one of this name replaces."""
    found = jobs.call("list", scope="mine", tags=[names.tag(name)], states=list(LIVE_STATES),
                      limit=GROUP_MAX)["jobs"]
    return [c for c in found if (c.get("params") or {}).get(_OF) in followed]


def start(jobs: PlutoJobsClient, cards: list[dict], command: str, name: str,
          cwd: str) -> dict:
    """Followers, named <name>/<key of the job>; those they replace end."""
    keys = [names.key_of(c) for c in cards]
    if len(set(keys)) < len(keys):
        raise PlutoError("two of these jobs have the same key; follow them separately")
    old = _earlier(jobs, name, {short(c["job_id"]) for c in cards})
    if old:
        ids = [c["job_id"] for c in old]
        fleet.each(lambda j: jobs.call("control", job_id=j, action="cancel"), ids)
        jobs.wait(job_ids=ids, until="exit", all=True, timeout_ms=FOLLOW_END_MS)
    batch = [{"name": key, "params": {**(c.get("params") or {}), _OF: short(c["job_id"])},
              "command": f"pss cat -f {short(c['job_id'])} | "
                         f"( {each.fill(command, each.facts(c))} )"}
             for c, key in zip(cards, keys)]
    r = submit.start_all(jobs, batch, {"cwd": cwd}, name)
    return dict(r, replaced=len(old))


def stream(jobs: PlutoJobsClient, card: dict) -> None:
    """Copy the output of a job to stdout until the job has ended."""
    sink = sys.stdout.buffer
    path, asked, over = each.facts(card)["log"], 0.0, render.ended(card)
    while True:
        try:
            src = open(path, "rb", 0)
            break
        except FileNotFoundError:              # queued
            card = jobs.call("status", job_id=card["job_id"])
            if render.ended(card):
                return
            time.sleep(FOLLOW_POLL_S)
    with src:
        while True:
            data = src.read(1 << 18)
            if data:
                sink.write(data)
                sink.flush()
                continue
            if over:
                return
            if time.monotonic() - asked >= FOLLOW_CHECK_S:
                asked = time.monotonic()
                # Asked before the next read, so nothing is left unread.
                over = render.ended(jobs.call("status", job_id=card["job_id"]))
                continue
            time.sleep(FOLLOW_POLL_S)
