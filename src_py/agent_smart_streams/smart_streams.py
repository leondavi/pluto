#!/usr/bin/env python3
"""PlutoSmartStreams entry point: MCP server and user commands.

Run through ``PlutoSmartStreams.sh``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time

_SRC_PY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_friend.frameworks import load_pluto_config  # noqa: E402
from agent_smart_streams import reports  # noqa: E402
from pluto_jobs_def import HISTORY_ALL  # noqa: E402
from pluto_sdk import PlutoError, PlutoHttpClient, PlutoJobsClient  # noqa: E402


def parse(argv):
    p = argparse.ArgumentParser(prog="smart_streams")
    p.add_argument("--agent-id")
    p.add_argument("--host")
    p.add_argument("--http-port", type=int)
    p.add_argument("--framework")
    p.add_argument("--attach", action="store_true",
                   help="another adapter holds this agent's registration")
    p.add_argument("--manage", action="store_true")
    p.add_argument("--history", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--export", metavar="JOB")
    p.add_argument("-o", "--output")
    p.add_argument("--import", dest="import_", metavar="FILE")
    p.add_argument("--prune", action="store_true")
    p.add_argument("--older-than", default="30d")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--agent")
    p.add_argument("--state")
    p.add_argument("--since")
    p.add_argument("--json", action="store_true")
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args(argv)


def history(jobs, a):
    recs = jobs.call("history", limit=HISTORY_ALL)["history"]
    if a.agent:
        recs = [r for r in recs if a.agent in (r.get("owner"), r.get("opener"))]
    if a.state:
        recs = [r for r in recs if r.get("state") == a.state]
    if a.since:
        cut = time.time() * 1000 - reports.parse_age(a.since)
        recs = [r for r in recs if r["at"] >= cut]
    print(json.dumps(recs) if a.json else reports.history_lines(recs))


def export(jobs, a):
    print("[smartstreams] note: an export holds the command and all output, "
          "which may include secrets.", file=sys.stderr)
    if a.output:
        print(jobs.export(a.export, os.path.abspath(a.output))["path"])
        return
    if sys.stdout.isatty():
        print(jobs.export(a.export)["path"])
        return
    tmp = tempfile.mkdtemp(prefix="pjob-")
    try:
        path = jobs.export(a.export, os.path.join(tmp, "x.pjob"))["path"]
        with open(path, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_(jobs, a):
    if a.import_ != "-":
        r = jobs.import_(a.import_)
    else:
        with tempfile.NamedTemporaryFile(suffix=".pjob") as f:
            shutil.copyfileobj(sys.stdin.buffer, f)
            f.flush()
            r = jobs.import_(f.name)
    print(f"{r['job_id']} {r['result']} ({r['state']}, {reports.size(r['size'])})")


def prune(jobs, a):
    age = reports.parse_age(a.older_than)
    ids = jobs.call("prune", older_than_ms=age, dry_run=True)["jobs"]
    if not ids:
        print("Nothing to prune.")
        return
    print("\n".join(ids))
    if not a.yes:
        if not sys.stdin.isatty():
            print("Not deleting without --yes.", file=sys.stderr)
            return
        if input(f"Delete {len(ids)} finished job(s) and their output? [y/N] "
                 ).strip().lower() != "y":
            return
    done = jobs.call("prune", older_than_ms=age, dry_run=False)["jobs"]
    print(f"Pruned {len(done)} job(s).")


def main(argv=None) -> int:
    a = parse(argv)
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), 30),
                        stream=sys.stderr,
                        format="[smartstreams] %(levelname)s %(message)s")
    cfg = load_pluto_config().get("pluto_server", {})
    host = a.host or cfg.get("host_ip", "127.0.0.1")
    port = a.http_port or cfg.get("host_http_port", 9201)
    user_op = (a.manage or a.history or a.stats or a.export or a.import_ or a.prune)
    if not user_op:
        if not a.agent_id:
            print("--agent-id is required", file=sys.stderr)
            return 2
        from agent_smart_streams.server import SmartStreamsServer
        SmartStreamsServer(agent_id=a.agent_id, host=host, http_port=port,
                           attach=a.attach, framework=a.framework).run()
        return 0
    http = PlutoHttpClient(host=host, http_port=port, agent_id="user")
    jobs = PlutoJobsClient(http, as_user=True)
    jobs.agent_id = f"user:{jobs.user}"
    try:
        if a.manage:
            from agent_smart_streams import manage
            return manage.run(jobs)
        if a.history:
            history(jobs, a)
        elif a.stats:
            s = jobs.call("stats")
            print(json.dumps(s) if a.json else reports.stats_lines(s))
        elif a.export:
            export(jobs, a)
        elif a.import_:
            import_(jobs, a)
        else:
            prune(jobs, a)
    except (PlutoError, OSError, ValueError) as exc:
        print(f"[smartstreams] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
