#!/usr/bin/env python3
"""pss: SmartStreams from the shell.

A model pays for every step it takes itself. From the shell, steps combine
with pipes, loops and scripts, and a job may start and follow other jobs.
Standard library only, so that it runs without the adapter's venv.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys

_SRC_PY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_friend.frameworks import load_pluto_config  # noqa: E402
from agent_smart_streams import (  # noqa: E402
    brief, cli_parser, compact, each, fleet, names, render, report, submit,
)
from agent_smart_streams.constants import (  # noqa: E402
    CLI_AGENT_ENV, CLI_HOST_ENV, CLI_PORT_ENV, GROUP_MAX, WAIT_FOREVER_S,
)
from agent_smart_streams.lease import state_path  # noqa: E402
from pluto_sdk import PlutoError, PlutoHttpClient, PlutoJobsClient  # noqa: E402

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
    spec = dict(cwd=cwd, tags=a.tag or None, timeout_s=a.timeout, parent=a.parent,
                stdin=a.stdin or any(r.get("then") == "input" for r in a.rules or ()) or None,
                watch=a.rules or None)
    if a.each:
        params = dict(e.partition("=")[::2] for e in a.each)
        r = submit.start_all(jobs, submit.expand(a.command, params), spec, a.name)
        handle = a.name or r["group"]
        text = f"{handle}/{r['jobs']}: {r['n']} started" if a.name else \
            f"{handle} {r['n']} jobs: {r['jobs']}"
        for name, why in r.get("failed", {}).items():
            print(f"pss: {name}: {why}", file=sys.stderr)
    else:
        r = submit.start_one(jobs, spec | {"command": a.command}, a.name)
        handle = text = a.name or compact.short(r["job_id"])
    print(handle if a.quiet else text, flush=True)
    if a.wait:
        return follow(jobs, waiter, names.cards(jobs, [r.get("group") or r["job_id"]]), a.wait)
    return 1 if a.each and r.get("failed") else 0


def follow(jobs, waiter, cards, timeout, verbose=False) -> int:
    """Until all jobs ended or a rule tells something."""
    ids = [c["job_id"] for c in cards]
    with remembered(waiter, jobs.agent_id):
        r = waiter.follow(ids, timeout)
    cards = current(jobs, cards)
    by_id = {compact.short(c["job_id"]): c for c in cards}
    for jid, hits in r["hits"].items():
        for h in hits:
            print(f"{names.label(by_id[jid])}: {compact.watch_hit(h)}")
    print("\n".join(render.rows(cards, jobs.agent_id, r["ended"], verbose)))
    if r["timed_out"]:
        print("timed out")
        return 124
    return int(any(render.ended(c) and c["state"] != "succeeded" for c in cards))


def current(jobs, cards) -> list:
    """The cards as they are now, in the same order."""
    return jobs.wait(job_ids=[c["job_id"] for c in cards], timeout_ms=1,
                     until="exit", all=True)["jobs"]


def do_wait(jobs, a, waiter):
    cards = names.cards(jobs, a.ids)
    if not (a.any or a.match is not None or a.idle is not None):
        return follow(jobs, waiter, cards, a.timeout, a.v)
    kw = {}
    if a.match is not None:
        kw = dict(until="pattern", pattern=a.match)
    elif a.idle is not None:
        kw = dict(until="idle", idle_ms=int(a.idle * 1000))
    with remembered(waiter, jobs.agent_id):
        r = waiter.wait(None, [c["job_id"] for c in cards], limit_s=WAIT_FOREVER_S,
                        timeout_s=a.timeout, **kw)
    seen = {compact.short(j): str(h.get("text") or h.get("last") or h["reason"])
            for j, h in r["hits"].items()}
    hit = [c for c in r["jobs"] if c["job_id"] in r["hits"]]
    print("\n".join(render.rows(hit, jobs.agent_id, seen, verbose=True)
                    + (["timed out"] if r["timed_out"] else [])))
    return 124 if r["timed_out"] else int(any(
        render.ended(c) and c["state"] != "succeeded" for c in hit))


def do_ps(jobs, a, _w):
    if a.ids:
        rows = names.cards(jobs, a.ids)
    else:
        scope = "all" if a.all or a.owner else "mine"
        rows = jobs.call("list", scope=scope, limit=a.limit)["jobs"]
    rows = fleet.filtered(rows, owner=a.owner, name=a.name, live=a.live)
    for line in render.rows(rows, jobs.agent_id, verbose=a.v):
        print(line)


def do_analyze(jobs, a, _w):
    extract = a.extract[0] if len(a.extract) == 1 else a.extract or None
    r = fleet.analyze(jobs, None, a.ids, extract=extract, where=a.where,
                      by=fields(a.by), agg=fields(a.agg), compare=fields(a.compare),
                      rows=a.rows or None, sort=a.sort, limit=a.limit)
    print(r["out"])
    if r.get("resume"):
        print("resume: " + json.dumps(r["resume"]))


def do_report(jobs, a, _w):
    r = report.records(jobs, names.cards(jobs, a.ids), a.extract, a.field)
    for note in r["notes"]:
        print("note: " + note)
    if a.o:
        print(report.save(r["records"], a.o))
    else:
        for label, (_, rec) in r["records"].items():
            print(f"{label} {compact.dumps(rec)}")


def do_each(jobs, a, _w):
    r = each.run(jobs, names.cards(jobs, a.ids), a.command, out=a.o, workers=a.j,
                 now=a.now, timeout_s=a.timeout)
    print("\n".join(each.report(r)))
    return 1 if r["failed"] else 124 if r["left"] else 0


def do_watch(jobs, a, _w):
    more = dict(by=fields(a.by), extract=a.extract, count=a.count, window_s=a.window,
                stalled=a.stalled, stalled_n=a.stalled_n, name=a.name)
    more = {k: v for k, v in more.items() if v}
    if a.remove is not None:
        r = [fleet.watch(jobs, "remove", None, a.ids, rule=a.remove or None, where=None, by=None)]
    elif a.list or not (a.rules or more):
        r = [fleet.watch(jobs, "list", None, a.ids, where=None, by=None)]
    else:
        first, *others = a.rules or [{}]
        r = [fleet.watch(jobs, "add", None, a.ids, from_now=a.from_now or None,
                         **{"where": None, "by": None, **rule})
             for rule in ({**more, **first}, *others)]
    print("\n".join(compact.dumps(x) for x in r))


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
    cards = names.cards(jobs, a.ids)
    for c in cards:
        if len(cards) > 1:
            print(f"==> {names.label(c)}")
        print(jobs.call("window", job_id=c["job_id"], tail=a.n, strip_ansi=True)["text"], end="")


def do_log(jobs, a, _w):
    print("\n".join(each.facts(c)["log"] for c in names.cards(jobs, a.ids)))


def do_cat(jobs, a, _w):
    for c in names.cards(jobs, a.ids):
        with open(each.facts(c)["log"], "rb") as f:
            while data := f.read(1 << 20):
                sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def do_control(jobs, a, _w):
    cards = names.cards(jobs, a.ids, strict=jobs.as_user)
    label = {compact.short(c["job_id"]): names.label(c) for c in cards}
    r = fleet.control(jobs, None, list(label), action=a.op, tree=a.tree or None)
    for k in ("ok", "finished"):
        if r.get(k):
            print(f"{k}: {' '.join(label.get(x, x) for x in r[k])}")
    for jid, why in r.get("errors", {}).items():
        print(f"{label.get(jid, jid)}: {why}", file=sys.stderr)
    return int("errors" in r)


def do_give(jobs, a, _w):
    given, failed = brief.give(jobs, a.to, names.cards(jobs, a.ids, strict=jobs.as_user))
    print(f"{len(given)} given to {a.to}")
    for name, why in failed.items():
        print(f"{name}: {why}", file=sys.stderr)
    return int(bool(failed))


def do_brief(jobs, a, _w):
    cards = names.cards(jobs, a.ids) if a.ids else jobs.call(
        "list", scope="mine", limit=GROUP_MAX)["jobs"]
    if not cards:
        raise PlutoError("you have no jobs; name some (pss ps --all)")
    print(brief.text(jobs, cards))


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
      "resume": do_control, "give": do_give, "brief": do_brief, "each": do_each,
      "report": do_report, "call": do_call}


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
    if not argv or argv[0] in ("help", "-h", "--help") or argv[-1] in ("-h", "--help"):
        topic = next((x for x in argv if x in cli_parser.MORE), None)
        print(cli_parser.MORE[topic] if topic else cli_parser.HELP)
        return 0
    a = cli_parser.build().parse_args(argv)
    try:
        jobs = connect()
        code = DO[a.op](jobs, a, fleet.Waiter(jobs)) or 0
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # The reader (head, grep -q) has seen enough.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (PlutoError, OSError) as exc:
        print(f"pss: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
