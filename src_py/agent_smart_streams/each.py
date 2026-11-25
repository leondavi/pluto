"""Run a program of the agent's own on every job.

What an agent would otherwise write around it - waiting for each job, the
path of its log, its exit code, a loop, a file per result - is here.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent_smart_streams import names, render
from agent_smart_streams.compact import short
from agent_smart_streams.constants import (
    EACH_BYTES, EACH_TEXT, EACH_TOTAL, EACH_WORKERS,
)
from pluto_jobs_def import WAIT_MAX_S
from pluto_sdk import PlutoError, PlutoJobsClient
from pluto_sdk.jobs import smartstreams_home

_HOLE = re.compile(r"\{([A-Za-z_]\w*)\}")


def facts(card: dict) -> dict[str, str]:
    """What {placeholders} stand for: parameters first."""
    code = card.get("exit_code")
    return {"log": os.path.join(smartstreams_home(), "jobs", card["job_id"], "output.log"),
            "id": short(card["job_id"]), "key": names.key_of(card), "state": card["state"],
            "exit": "" if code is None else str(code), **(card.get("params") or {})}


def fill(template: str, known: dict[str, str], quote=str) -> str:
    """Other braces stay, so awk and JSON need no escaping."""
    return _HOLE.sub(lambda m: quote(known[m[1]]) if m[1] in known else m[0], template)


def run_one(command: str, out: str | None, card: dict) -> tuple[bool, str]:
    """(success, text): what the program printed, or why it failed."""
    known = facts(card)
    env = dict(os.environ, **{"PSS_" + k.upper(): v for k, v in known.items()})
    try:
        r = subprocess.run(fill(command, known, shlex.quote), shell=True, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if r.returncode:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, f"exit {r.returncode}" + (f": {err[-1][:300]}" if err else "")
    if out is None:
        return True, r.stdout[:4 * EACH_TEXT].decode("utf-8", "replace").strip()
    if len(r.stdout) > EACH_BYTES:
        return False, "printed too much for a file"
    path = fill(out, known)
    if path.endswith(".json"):
        try:
            json.loads(r.stdout)
        except ValueError as exc:
            return False, f"not JSON: {exc}"
    try:
        write(path, r.stdout)
    except OSError as exc:
        return False, f"{path}: {exc.strerror}"
    return True, ""


def write(path: str, data: bytes) -> None:
    """Completely or not at all."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def check_outputs(out: str | None, cards: list[dict]) -> None:
    """Two jobs must not write one file."""
    if out is None:
        return
    paths: dict[str, str] = {}
    for c in cards:
        path, label = fill(out, facts(c)), names.label(c)
        if path in paths:
            raise PlutoError(f"{paths[path]} and {label} would both write {path}; "
                             "put {key} or a parameter into -o")
        paths[path] = label


def finished(jobs: PlutoJobsClient, cards: list[dict], timeout_s: float):
    """The jobs' final cards, each as soon as it ends."""
    end = time.monotonic() + timeout_s
    left = {}
    for c in cards:
        if render.ended(c):
            yield c
        else:
            left[c["job_id"]] = c
    while left and time.monotonic() < end:
        wait_s = max(0.1, min(end - time.monotonic(), WAIT_MAX_S))
        r = jobs.wait(job_ids=list(left), until="exit", timeout_ms=int(wait_s * 1000))
        for c in r["jobs"]:
            if c["job_id"] in r["hits"]:
                del left[c["job_id"]]
                yield c


def run(jobs: PlutoJobsClient, cards: list[dict], command: str, *, out: str | None,
        workers: int = EACH_WORKERS, now: bool = False, timeout_s: float) -> dict:
    """{"done": {label: text}, "failed": {label: why}, "left": [labels]}"""
    check_outputs(out, cards)
    results: queue.Queue = queue.Queue()

    def work(card):
        try:
            results.put((names.label(card), *run_one(command, out, card)))
        except Exception as exc:              # one job never hides the others
            results.put((names.label(card), False, str(exc)))
    started = 0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for c in cards if now else finished(jobs, cards, timeout_s):
            pool.submit(work, c)
            started += 1
    done, failed = {}, {}
    for _ in range(started):
        label, ok, text = results.get()
        (done if ok else failed)[label] = text
    order = [names.label(c) for c in cards]
    return {"done": {k: done[k] for k in order if k in done},
            "failed": {k: failed[k] for k in order if k in failed},
            "left": [k for k in order if k not in done and k not in failed]}


def report(r: dict) -> list[str]:
    """What an agent needs to see of it."""
    out, shown = [], 0
    for label, text in r["done"].items():
        if not text:
            continue
        cut = text[:EACH_TEXT] + (" ...(cut)" if len(text) > EACH_TEXT else "")
        if shown + len(cut) > EACH_TOTAL:
            out.append("(more output not shown; write it to files with -o)")
            break
        shown += len(cut)
        out.append(f"{label}: {cut}" if "\n" not in cut else f"== {label}\n{cut}")
    same: dict[str, list[str]] = {}
    for label, why in r["failed"].items():
        same.setdefault(why, []).append(label)
    out += [f"FAILED {', '.join(labels)}: {why}" for why, labels in same.items()]
    if r["left"]:
        out.append(f"still running: {', '.join(r['left'])}")
    out.append(f"{len(r['done'])} ok, {len(r['failed'])} failed"
               + (f", {len(r['left'])} not finished" if r["left"] else ""))
    return out
