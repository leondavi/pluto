#!/usr/bin/env python3
"""ledger.py — the shared "storage" every agent in the experiment talks to.

Both conditions (native Claude Code subagents and Pluto agents) use the
exact same storage CLI, so any difference in correctness comes from the
coordination layer, not from the storage. Every operation appends a
timestamped record to ``events.jsonl`` in the run directory; the harness
derives latencies and correctness verdicts from that file.

Subcommands (run from the run directory):

    hop    --agent A --word W               S1: record receipt of the relay token
    append --agent A --value V              S2: non-atomic read-modify-write
    put    --agent A --value V [--token N]  S3: register write, fencing-aware
    job    --agent A --id K                 S4: run one deterministic job
    stall  --agent A --seconds N            S3: simulated GC pause (blocks N s)

``stall`` exists because Claude Code's Bash tool refuses a bare long
``sleep``; the pause is logged so the validator can see when it ended.

``append`` deliberately sleeps between read and write so that concurrent
writers without mutual exclusion lose updates. ``put`` rejects a write
whose ``--token`` is lower than the highest token already accepted (the
storage-side half of a fencing protocol); writes without a token are
accepted unconditionally, identical in both conditions.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time

# S4 job durations in seconds: uneven on purpose (sum 57 s, LPT on 4 workers
# gives a 15 s makespan, a naive 3-per-worker split gives 30 s).
JOB_SECONDS = {1: 12, 2: 1, 3: 10, 4: 2, 5: 8, 6: 1, 7: 6, 8: 3, 9: 5, 10: 2, 11: 4, 12: 3}

RMW_SLEEP_RANGE = (0.2, 0.5)


def _dir():
    return os.environ.get("LEDGER_DIR", os.getcwd())


def _path(name):
    return os.path.join(_dir(), name)


def record(op, **fields):
    """Append one event; small O_APPEND writes are atomic on POSIX."""
    ev = {"ts": time.time(), "op": op, **fields}
    line = (json.dumps(ev, sort_keys=True) + "\n").encode()
    fd = os.open(_path("events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return ev


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _store(name, obj):
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f)


def cmd_hop(args):
    ev = record("hop", agent=args.agent, word=args.word)
    print(json.dumps({"ok": True, "ts": ev["ts"]}))


def cmd_append(args):
    data = _load("ledger.json", {"count": 0, "entries": []})
    time.sleep(random.uniform(*RMW_SLEEP_RANGE))  # widen the race window
    data["count"] += 1
    data["entries"].append({"agent": args.agent, "value": args.value})
    _store("ledger.json", data)
    record("append", agent=args.agent, value=args.value, count_after=data["count"])
    print(json.dumps({"ok": True, "count": data["count"]}))


def cmd_put(args):
    reg = _load("register.json", {"value": None, "writer": None, "max_token": None})
    token = args.token
    if token is not None and reg["max_token"] is not None and token < reg["max_token"]:
        record("put", agent=args.agent, value=args.value, token=token,
               accepted=False, reason="stale_token", max_token=reg["max_token"])
        print(json.dumps({"ok": False, "error": "stale_token",
                          "max_token": reg["max_token"]}))
        return 3
    reg["value"], reg["writer"] = args.value, args.agent
    if token is not None:
        reg["max_token"] = token if reg["max_token"] is None else max(token, reg["max_token"])
    _store("register.json", reg)
    record("put", agent=args.agent, value=args.value, token=token, accepted=True)
    print(json.dumps({"ok": True}))
    return 0


def cmd_job(args):
    if args.id not in JOB_SECONDS:
        print(json.dumps({"ok": False, "error": f"unknown job {args.id}"}))
        return 2
    record("job_start", agent=args.agent, job=args.id)
    time.sleep(JOB_SECONDS[args.id])
    digest = hashlib.sha256(f"job-{args.id}".encode()).hexdigest()[:12]
    record("job_end", agent=args.agent, job=args.id, result=digest)
    print(json.dumps({"ok": True, "job": args.id, "result": digest}))
    return 0


def cmd_stall(args):
    record("stall_start", agent=args.agent, seconds=args.seconds)
    time.sleep(args.seconds)
    record("stall_end", agent=args.agent)
    print(json.dumps({"ok": True, "stalled_s": args.seconds}))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hop")
    h.add_argument("--agent", required=True)
    h.add_argument("--word", required=True)
    a = sub.add_parser("append")
    a.add_argument("--agent", required=True)
    a.add_argument("--value", required=True)
    u = sub.add_parser("put")
    u.add_argument("--agent", required=True)
    u.add_argument("--value", required=True)
    u.add_argument("--token", type=int, default=None)
    j = sub.add_parser("job")
    j.add_argument("--agent", required=True)
    j.add_argument("--id", type=int, required=True)
    s = sub.add_parser("stall")
    s.add_argument("--agent", required=True)
    s.add_argument("--seconds", type=float, required=True)
    args = p.parse_args(argv)
    handler = {"hop": cmd_hop, "append": cmd_append, "put": cmd_put, "job": cmd_job,
               "stall": cmd_stall}[args.cmd]
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
