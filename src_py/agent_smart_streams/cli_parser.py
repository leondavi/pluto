"""Options and help of pss. The help is short on purpose: an agent that
reads it pays for it on every later turn."""

from __future__ import annotations

import argparse

from agent_smart_streams import rules
from agent_smart_streams.constants import CLI_WAIT_S, EACH_WORKERS, GROUP_MAX

HELP = """pss - run and follow many durable jobs. Example, complete:

  pss run -n soak -e k=1-12 'bin/soak --job {k}' \\
      --on 'CKPT request token={t}' --append 'control/{k}.ack' '{t}' \\
      --idle 60 --stop
  pss wait soak -t 540
  pss each soak 'python3 parse.py {log} {exit}' -o 'out/{k}.json'

The server keeps the rules: no watcher script, ids or log paths needed, and
all of it outlives your session. JOBS: soak  soak/3  soak/1-6,9  or an id.

  run [-n NAME] [-e k=1-12].. [RULES] [-w S] CMD      start one or a batch
  RULES, also: pss watch JOBS RULES    --on 'text {field}' | --where COND |
        --idle S | --absent S, then --append FILE TEXT | --input TEXT | --stop |
        --pause | --tell (default; ends pss wait)    list: pss watch JOBS
  wait JOBS [-t S] [--any]   until all ended, or a rule tells
  ps [JOBS] [-v] [--all]     tail|cat|log JOBS     grep RE JOBS
  each JOBS CMD [-o FILE]    your program per job as it ends; {log} {key}
        {state} {exit} and parameters are filled in; -o keeps its output
  report JOBS -x 'E{e:int} p={p:int}'.. -f 'bad=first:e where p > 9'.. [-o FILE]
        a record per job without a program; last.F is the value in force
  analyze JOBS [...]         outline of any output; queries: pss help analyze
  stop|pause|resume JOBS     give AGENT JOBS       brief [JOBS]  (handover)
wait exits 1 if a job failed, 124 on timeout. More: pss help COMMAND"""

MORE = {
    "analyze": """pss analyze JOBS            outline: line shapes, counts, likely errors, rare lines
  -x EXTRACT..  auto | kv | json | csv | 'E{e:int} loss={loss:float}' | 're:(?P<x>..)'
  -w COND..     'status >= 500', 'msg contains x', 'x ~ re', 'has err', 'dur > 2s',
                'loss > prev*1.5', 'p > last.limit', 'abs(v - prev.v) > 3'
  -b FIELDS -a 'count,max:v,p95:v'    group and aggregate (also _job _name _level)
  -c FIELDS     compare jobs, aligned by -b      --rows  list the lines
  -s SORT -l N""",
    "report": """pss report JOBS -x TEMPLATE.. -f COLUMN.. [-o 'out/{key}.json']
  COLUMN  NAME=FIELD | NAME=FN:FIELD[,FIELD] [where COND]
  FN      first (default) last list count sum min max mean; two fields make an object
  FIELD   of a template ({x:int} {x:float} are numbers), or _state _exit _key, parameters
  COND    'p > last.limit' (latest earlier value), 'abs(v - prev.v) > 3' (previous match)
Several -x: the first that matches a line. Without -o the records are printed.""",
    "run": """pss run [-n NAME] [-e k=1-4,8 | k=a,b].. [RULES] [-C DIR] [-w S] [--timeout S]
        [--tag T] [--parent JOB] [--stdin] [-q] CMD
Several -e give every combination (soak/1:a). RULES in order: a condition, then
its action; {field} is what --on captured, or a parameter. --once: first time only.""",
}


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pss", add_help=False)
    sub = p.add_subparsers(dest="op", metavar="OP")
    many: dict = dict(action="append", default=[])
    yes: dict = dict(action="store_true")

    def op(name, *flags, ids="+"):
        s = sub.add_parser(name)
        if ids:
            s.add_argument("ids", nargs=ids)
        for f, kw in flags:
            s.add_argument(*f, **kw)
        return s

    run = op("run", (["-e", "--each"], many), (["-n", "--name"], {}),
             (["-C", "--cwd"], {}), (["-w", "--wait"], dict(type=int, default=0)),
             (["-q", "--quiet"], yes), (["--timeout"], dict(type=float)),
             (["--tag"], many), (["--parent"], {}), (["--stdin"], yes), ids=None)
    run.add_argument("command")
    rules.add_options(run)
    op("wait", (["-t", "--timeout"], dict(type=int, default=CLI_WAIT_S)),
       (["--any"], yes), (["--match"], {}), (["--idle"], dict(type=float)),
       (["-v"], yes))
    op("ps", (["--all"], yes), (["--live"], yes), (["--owner"], {}), (["--name"], {}),
       (["-v"], yes), (["-l", "--limit"], dict(type=int, default=GROUP_MAX)), ids="*")
    op("analyze", (["-x", "--extract"], many), (["-w", "--where"], many),
       (["-b", "--by"], {}), (["-a", "--agg"], {}), (["-c", "--compare"], {}),
       (["--rows"], yes), (["-s", "--sort"], {}), (["-l", "--limit"], dict(type=int)))
    op("report", (["-x", "--extract"], many), (["-f", "--field"], many), (["-o"], {}))
    each = op("each", (["-o"], {}), (["-j"], dict(type=int, default=EACH_WORKERS)),
              (["--now"], yes), (["-t", "--timeout"], dict(type=int, default=CLI_WAIT_S)),
              ids=None)
    each.add_argument("ids", nargs="+")
    each.add_argument("command")
    watch = op("watch", (["-b", "--by"], {}),
               (["-x", "--extract"], {}), (["--stalled"], {}),
               (["--stalled-n"], dict(type=int)), (["--count"], dict(type=int)),
               (["--window"], dict(type=float)),
               (["--from-now"], yes), (["-n", "--name"], {}),
               (["--list"], yes), (["--remove"], dict(nargs="?", const="")))
    rules.add_options(watch, aliases=True)
    grep = op("grep", (["-C", "--context"], dict(type=int, default=0)),
              (["-m", "--max"], dict(type=int, default=20)), (["-i"], yes),
              (["-F", "--fixed"], yes), ids=None)
    grep.add_argument("pattern")
    grep.add_argument("ids", nargs="+")
    op("tail", (["-n"], dict(type=int, default=10)))
    for name in ("cat", "log"):
        op(name)
    for name in ("stop", "pause", "resume"):
        op(name, (["--tree"], yes))
    give = op("give", ids=None)
    give.add_argument("to")
    give.add_argument("ids", nargs="+")
    op("brief", ids="*")
    call = op("call", ids=None)
    call.add_argument("what")
    call.add_argument("args", nargs="?", default="{}")
    return p
