#!/usr/bin/env python3
"""pss: SmartStreams from the shell.

A model pays for every step it takes itself. From the shell, steps combine
with pipes, loops and scripts, and a job may start and follow other jobs.
Standard library only, so that it runs without the adapter's venv.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys

_SRC_PY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_friend.frameworks import load_pluto_config  # noqa: E402
from agent_smart_streams import compact, fleet, submit  # noqa: E402
from agent_smart_streams.constants import (  # noqa: E402
    CLI_AGENT_ENV, CLI_HOST_ENV, CLI_PORT_ENV, CLI_WAIT_S,
    GROUP_MAX, WAIT_FOREVER_S,
)
from agent_smart_streams.lease import state_path  # noqa: E402
from pluto_jobs_def import FINAL_STATES  # noqa: E402
from pluto_sdk import PlutoError, PlutoHttpClient, PlutoJobsClient  # noqa: E402
from pluto_sdk.jobs import smartstreams_home  # noqa: E402

HELP = """pss - run and follow many durable jobs. IDS are job ids or group handles.

  pss run [-e k=1-12]... [-n NAME] [-C DIR] [-w S] 'cmd {k}'   start one job, or
        one per combination (k=1-4,8 or k=a,b); prints the id or group. -q: only that
  pss wait IDS... [-t S] [--any]      until all (or any) exit: state, code, last line
  pss wait IDS... --match RE | --idle S | --watch     until that happens to one
  pss ps [IDS...] [--all] [--live] [--owner A] [--name TEXT]
  pss analyze IDS... [-x EXTRACT] [-w COND]... [-b FIELDS] [-a AGGS] [-c FIELDS]
        [--rows] [-s SORT] [-l N]     alone: an outline of the output, no pattern needed
  pss watch IDS... [--match M] [-w COND]... [--idle S] [--stalled FIELD] [--then
        notify|cancel|pause] [--once] | --list | --remove [RULE]
  pss grep [-C N] [-m N] [-i] RE IDS...       pss tail [-n N] IDS...    pss cat ID
  pss log IDS...                      paths of the raw logs (read only)
  pss stop|pause|resume IDS... [--tree]       pss give AGENT IDS...
  pss call OP ['{json}']              any other server operation
Exit status: wait is 1 if a job did not succeed, 124 on timeout."""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pss", add_help=False)
    sub = p.add_subparsers(dest="op", metavar="OP")

    def op(name, *flags, ids="+"):
        s = sub.add_parser(name)
        if ids:
            s.add_argument("ids", nargs=ids)
        for f, kw in flags:
            s.add_argument(*f, **kw)
        return s

    many = dict(action="append", default=[])
    yes = dict(action="store_true")
    run = op("run", (["-e", "--each"], many), (["-n", "--name"], {}),
             (["-C", "--cwd"], {}), (["-w", "--wait"], dict(type=int, default=0)),
             (["-q", "--quiet"], yes), (["--timeout"], dict(type=float)),
             (["--tag"], many), (["--parent"], {}), (["--stdin"], yes), ids=None)
    run.add_argument("command")
    op("wait", (["-t", "--timeout"], dict(type=int, default=CLI_WAIT_S)),
       (["--any"], yes), (["--match"], {}), (["--idle"], dict(type=float)),
       (["--watch"], yes))
    op("ps", (["--all"], yes), (["--live"], yes), (["--owner"], {}),
       (["--name"], {}), (["-l", "--limit"], dict(type=int, default=GROUP_MAX)), ids="*")
    op("analyze", (["-x", "--extract"], {}), (["-w", "--where"], many),
       (["-b", "--by"], {}), (["-a", "--agg"], {}), (["-c", "--compare"], {}),
       (["--rows"], yes), (["-s", "--sort"], {}), (["-l", "--limit"], dict(type=int)))
    op("watch", (["--match"], {}), (["-w", "--where"], many), (["-b", "--by"], {}),
       (["-x", "--extract"], {}), (["--idle"], dict(type=float)),
       (["--absent"], dict(type=float)), (["--stalled"], {}),
       (["--stalled-n"], dict(type=int)), (["--count"], dict(type=int)),
       (["--window"], dict(type=float)), (["--then"], dict(default="notify")),
       (["--once"], yes), (["--from-now"], yes), (["-n", "--name"], {}),
       (["--list"], yes), (["--remove"], dict(nargs="?", const="")))
    g = op("grep", (["-C", "--context"], dict(type=int, default=0)),
           (["-m", "--max"], dict(type=int, default=20)), (["-i"], yes),
           (["-F", "--fixed"], yes), ids=None)
    g.add_argument("pattern")
    g.add_argument("ids", nargs="+")
    op("tail", (["-n"], dict(type=int, default=10)))
    for name in ("cat", "log"):
        op(name)
    for name in ("stop", "pause", "resume"):
        op(name, (["--tree"], yes))
    give = op("give", ids=None)
    give.add_argument("to")
    give.add_argument("ids", nargs="+")
    call = op("call", ids=None)
    call.add_argument("what")
    call.add_argument("args", nargs="?", default="{}")
    sub.add_parser("help")
    return p


# ── Once-only events across invocations ───────────────────────────────────────

@contextlib.contextmanager
def remembered(waiter: fleet.Waiter, agent: str):
    """The events this agent was told about, kept in a file of its own."""
    path = state_path(agent, ".json")
    waiter.restore(_load(path))
    yield
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)     # another wait may have saved meanwhile
        waiter.restore(_load(path))
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(waiter.state(), f)
        os.replace(tmp, path)


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ── Commands ──────────────────────────────────────────────────────────────────

def fields(text):
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def do_run(jobs, a, waiter):
    cwd = os.path.abspath(a.cwd or os.getcwd())
    spec = dict(cwd=cwd, tags=a.tag or None, timeout_s=a.timeout,
                parent=a.parent, stdin=a.stdin or None)
    if a.each:
        each = dict(e.partition("=")[::2] for e in a.each)
        r = submit.start_all(jobs, submit.expand(a.command, each), spec)
        handle, text = r["group"], f"{r['group']} {r['n']} jobs: {r['jobs']}"
        for name, why in r.get("failed", {}).items():
            print(f"pss: {name}: {why}", file=sys.stderr)
    else:
        r = jobs.start(command=a.command, name=a.name, **spec)
        handle = text = compact.short(r["job_id"])
    print(handle if a.quiet else text, flush=True)
    if a.wait:
        return wait_for(jobs, waiter, [handle], a.wait, wait_all=True)
    return 1 if a.each and r.get("failed") else 0


def job_line(j: dict) -> str:
    code = f" exit={j['exit_code']}" if j.get("exit_code") not in (None, 0) else ""
    return (f"{j['job_id']} {j.get('name', '-')} {j['state']}{code} "
            f"{j['elapsed_s']}s {j.get('size', 0)}B")


def wait_for(jobs, waiter, ids, timeout, *, wait_all=False, forever=True, **kw) -> int:
    with remembered(waiter, jobs.agent_id):
        r = compact.waited(waiter.wait(None, ids, wait_all=wait_all, limit_s=WAIT_FOREVER_S,
                                       timeout_s=timeout, **kw))
    for j in r["jobs"]:
        extra = [x for x in (j.get("hit"), j.get("last")) if x]
        print(job_line(j) + (": " + " | ".join(map(str, extra)) if extra else ""))
    if r.get("others"):
        print("others: " + ", ".join(f"{n} {s}" for s, n in r["others"].items()))
    if r.get("timed_out"):
        print("timed out")
        return 124
    return int(any(j["state"] in FINAL_STATES and j["state"] != "succeeded"
                   for j in r["jobs"]))


def do_wait(jobs, a, waiter):
    kw = {}
    if a.match is not None:
        kw = dict(until="pattern", pattern=a.match)
    elif a.idle is not None:
        kw = dict(until="idle", idle_ms=int(a.idle * 1000))
    elif a.watch:
        kw = dict(until="watch")
    return wait_for(jobs, waiter, a.ids, a.timeout, wait_all=not (a.any or kw), **kw)


def do_ps(jobs, a, _w):
    if a.ids:
        rows = [jobs.call("status", job_id=j) for j in submit.members(jobs, a.ids)]
    else:
        scope = "all" if a.all or a.owner else "mine"
        rows = jobs.call("list", scope=scope, limit=a.limit)["jobs"]
    rows = fleet.filtered(rows, owner=a.owner, name=a.name, live=a.live)
    for r in rows:
        own = f" {r['owner']}" if r.get("owner") != jobs.agent_id else ""
        print(job_line(compact.brief(r)) + own
              + (" idle" if r.get("activity") == "idle" else ""))


def do_analyze(jobs, a, _w):
    r = fleet.analyze(jobs, None, a.ids, extract=a.extract, where=a.where,
                      by=fields(a.by), agg=fields(a.agg), compare=fields(a.compare),
                      rows=a.rows or None, sort=a.sort, limit=a.limit)
    print(r["out"])
    if r.get("resume"):
        print("resume: " + json.dumps(r["resume"]))


def do_watch(jobs, a, _w):
    action = "list" if a.list else "remove" if a.remove is not None else "add"
    r = fleet.watch(
        jobs, action, None, a.ids, name=a.name, match=a.match, where=a.where,
        by=fields(a.by), extract=a.extract, count=a.count, window_s=a.window,
        idle_s=a.idle, absent_s=a.absent, stalled=a.stalled,
        stalled_n=a.stalled_n, then=a.then if action == "add" else None,
        once=a.once or None, from_now=a.from_now or None, rule=a.remove or None)
    print(compact.dumps(r))


def do_grep(jobs, a, _w):
    r = fleet.grep(jobs, None, a.ids, max_matches=a.max, pattern=a.pattern,
                   before=a.context or None, after=a.context or None,
                   ignore_case=a.i or None, fixed=a.fixed or None)
    found = 0
    for jid, g in r.items():
        if jid == "no_match":
            continue
        if "error" in g:
            print(f"{jid}: {g['error']}", file=sys.stderr)
            continue
        found += g["matches"]
        for line in g["lines"].splitlines():
            print(f"{jid}:{line}")
        if "resume_at" in g:
            print(f"{jid}: more (first {g['matches']} shown)")
    return 0 if found else 1


def do_tail(jobs, a, _w):
    ids = submit.members(jobs, a.ids)
    for jid in ids:
        if len(ids) > 1:
            print(f"==> {jid}")
        print(jobs.call("window", job_id=jid, tail=a.n, strip_ansi=True)["text"], end="")


def paths(jobs, ids):
    for jid in submit.members(jobs, ids):
        full = jobs.call("status", job_id=jid)["job_id"]
        yield os.path.join(smartstreams_home(), "jobs", full, "output.log")


def do_log(jobs, a, _w):
    print("\n".join(paths(jobs, a.ids)))


def do_cat(jobs, a, _w):
    for path in paths(jobs, a.ids):
        with open(path, "rb") as f:
            while data := f.read(1 << 20):
                sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def do_control(jobs, a, _w):
    r = fleet.control(jobs, None, a.ids, action=a.op, tree=a.tree or None)
    for k in ("ok", "finished"):
        if r.get(k):
            print(f"{k}: {' '.join(r[k])}")
    for jid, why in r.get("errors", {}).items():
        print(f"{jid}: {why}", file=sys.stderr)
    return int("errors" in r)


def do_give(jobs, a, _w):
    r = fleet.each(lambda j: jobs.call("transfer", job_id=j, to=a.to),
                   submit.members(jobs, a.ids))
    bad = {j: x["error"] for j, x in r.items() if "error" in x}
    print(f"{len(r) - len(bad)} given to {a.to}")
    for j, why in bad.items():
        print(f"{j}: {why}", file=sys.stderr)
    return int(bool(bad))


def do_call(jobs, a, _w):
    try:
        args = json.loads(a.args)
    except ValueError as exc:
        raise PlutoError(f"arguments are not JSON: {exc}")
    if not isinstance(args, dict):
        raise PlutoError("arguments must be a JSON object")
    print(compact.dumps(jobs.call(a.what, **args)))


DO = {"run": do_run, "wait": do_wait, "ps": do_ps, "analyze": do_analyze,
      "watch": do_watch, "grep": do_grep, "tail": do_tail, "cat": do_cat,
      "log": do_log, "stop": do_control, "pause": do_control,
      "resume": do_control, "give": do_give, "call": do_call}


def connect() -> PlutoJobsClient:
    cfg = load_pluto_config().get("pluto_server", {})
    agent = os.environ.get(CLI_AGENT_ENV)
    http = PlutoHttpClient(
        host=os.environ.get(CLI_HOST_ENV) or cfg.get("host_ip", "127.0.0.1"),
        http_port=int(os.environ.get(CLI_PORT_ENV) or cfg.get("host_http_port", 9201)),
        agent_id=agent or "user")
    jobs = PlutoJobsClient(http, as_user=not agent)
    if not agent:
        jobs.agent_id = f"user:{jobs.user}"
    return jobs


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return 0
    a = parser().parse_args(argv)
    try:
        jobs = connect()
        return DO[a.op](jobs, a, fleet.Waiter(jobs)) or 0
    except (PlutoError, OSError) as exc:
        print(f"pss: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
