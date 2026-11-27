"""Options and help of pst. The help is short on purpose: an agent that
reads it pays for it on every later turn."""

from __future__ import annotations

import argparse

from agent_smart_tasks import rules
from agent_smart_tasks.constants import CLI_WAIT_S, EACH_WORKERS, GROUP_MAX

HELP = """pst - run and follow many durable jobs. Example, complete:

  pst run -n soak -e k=1-12 'bin/soak --job {k}' \\
      --on 'CKPT request token={t}' --append 'control/{k}.ack' '{t}' \\
      --idle 60 --stop
  pst wait soak -t 540
  pst each soak 'python3 parse.py {log} {exit}' -o 'out/{k}.json'

The server keeps the rules and runs everything: no watcher script, ids, log
paths or nohup needed, and all of it outlives your session.
JOBS: soak  soak/3  soak/1-6,9  r1,r2  or an id. Running a name again adds or
replaces its keys.

  run [-n NAME] [-e k=1-12].. [RULES] [-w S] [--timeout S] CMD
        {k} in CMD (and in rules) is the value; several -e: every combination
        --slot NAME:N   at most N running jobs of this slot, all agents together
        --after JOBS    start once they ended (--after-ok: and succeeded);
                        per job: -e k=1-8 --after 'tb/{k}'
        --lock RES[:read]  hold a Pluto lock while running ($PLUTO_LOCK_TOKEN)
  RULES  a condition, then actions (several allowed):
        --on 'text {field}' | --where 'field > 3' | --idle S | --absent S
        --append FILE TEXT | --input TEXT | --stop | --pause | --tell
        --tell (the default) ends pst wait with the line. Later: watch JOBS RULES
  wait JOBS [-t S] [--any] [--match RE]   until all ended or a rule told; at
        -t it prints where every job is, so wait instead of sleep and ps
  ps [JOBS] [-v] [--all]     head|tail [-n N] JOBS     cat|log JOBS
  grep [-C|-A|-B N] [-i -v -c -F -m N] RE JOBS
  each JOBS CMD [-o FILE]    your program per job as it ends; {log} {k} {key}
        {state} {exit} are filled in; -o keeps its output
  follow JOBS CMD            your program per job, live: its stdin is the whole
        output, closed when the job ends. Following again replaces it
  report JOBS -x 'E{e:int} p={p:int}'.. -f 'bad=first:e where p > 9'.. [-o FILE]
        a record per job without a program: pst help report
  analyze JOBS [...]         outline of any output: pst help analyze
  stop|pause|resume JOBS     give AGENT JOBS       brief [JOBS]  (handover)
wait exits 1 if a job failed, 124 on timeout."""

MORE = {
    "analyze": """pst analyze JOBS            outline: line shapes, counts, likely errors, rare lines
  -x EXTRACT..  auto | kv | json | csv | 'E{e:int} loss={loss:float}' | 're:(?P<x>..)'
  -w COND..     'status >= 500', 'msg contains x', 'x ~ re', 'has err', 'dur > 2s',
                'loss > prev*1.5', 'p > last.limit', 'abs(v - prev.v) > 3'
  -b FIELDS -a 'count,max:v,p95:v'    group and aggregate (also _job _name _level)
  -c FIELDS     compare jobs, aligned by -b      --rows  list the lines
  -s SORT -l N""",
    "report": """pst report JOBS -x TEMPLATE.. -f COLUMN.. [-o 'out/{key}.json']
  COLUMN  NAME=FIELD | NAME=FN:FIELD[,FIELD] [where COND]
  FN      first (default) last list count sum min max mean; two fields make an object
  FIELD   of a template ({x:int} {x:float} are numbers), or _state _exit _key, parameters
  COND    'p > last.limit' (latest earlier value), 'abs(v - prev.v) > 3' (previous match)
Several -x: the first that matches a line. Without -o the records are printed.""",
    "run": """pst run [-n NAME] [-e k=1-4,8 | k=a,b].. [RULES] [-C DIR] [-w S] [--timeout S]
        [--slot NAME:N] [--after JOBS] [--after-ok] [--lock RES[:read]]
        [--tag T] [--parent JOB] [--stdin] [-q] CMD
Several -e give every combination (soak/1:a). RULES in order: a condition, then
its actions; {field} is what --on captured, or a parameter. --once: first time
only. -w S waits like pst wait. A slot is a semaphore, --after a barrier ({k}
in it: each job waits for its own), a
lock is Pluto's (agents can hold it too: GET /locks shows the holder).""",
}


class _Parser(argparse.ArgumentParser):
    """Says what is wrong and how it goes, not every option there is."""

    def error(self, message):
        op = self.prog.partition(" ")[2]
        self.exit(2, f"pst {op}: {message}\n" + topic(op) + "\n")


def topic(op: str) -> str:
    """Help on one command: its entries in the list of commands."""
    if op in MORE:
        return MORE[op]
    found, take = [], False
    for line in HELP[HELP.index("\n  run "):].splitlines():
        if line.startswith("  ") and not line.startswith("   "):
            take = any(op in w.split("|") for w in line.split()
                       if not w.startswith(("-", "[", "'")))
        elif not line.startswith(" "):
            take = False
        if take:
            found.append(line)
    return "\n".join(found) or "no such command; see pst help"


def build() -> argparse.ArgumentParser:
    p = _Parser(prog="pst", add_help=False)
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
             (["--tag"], many), (["--parent"], {}), (["--stdin"], yes),
             (["--slot"], {}), (["--after"], many), (["--after-ok"], yes),
             (["--lock"], {}), ids=None)
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
    op("report", (["-x", "--extract"], many),
       (["-f", "--field"], dict(action="extend", nargs="+", default=[])), (["-o"], {}))
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
              (["-A"], dict(type=int)), (["-B"], dict(type=int)),
              (["-m", "--max"], dict(type=int, default=20)), (["-i"], yes),
              (["-v", "--invert"], yes), (["-c", "--count"], yes),
              (["-F", "--fixed"], yes),
              # Habits from grep that change nothing here: always numbered, ERE.
              (["-n", "-E", "-H"], dict(action="store_true", dest="habit")), ids=None)
    grep.add_argument("pattern")
    grep.add_argument("ids", nargs="+")
    follow = op("follow", (["-n", "--name"], {}), ids=None)
    follow.add_argument("ids", nargs="+")
    follow.add_argument("command")
    for name in ("head", "tail"):
        op(name, (["-n"], dict(type=int, default=10)))
    op("cat", (["-f", "--follow"], yes))
    op("log")
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
