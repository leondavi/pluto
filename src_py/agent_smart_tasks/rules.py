"""Rules on a command line: --on MATCH --append FILE TEXT, --idle 60 --stop.

A condition (--on, --where, --idle, --absent) begins a rule; what follows
belongs to it. Several actions after one condition (--append F T --stop) are
several rules on that condition. So a whole supervisor fits into the command
that starts the jobs it supervises.
"""

from __future__ import annotations

import argparse

#: Rule key of each option that begins a rule.
_CONDITIONS = {"on": "match", "idle": "idle_s", "absent": "absent_s"}
_ALIASES = {"match": "on", "w": "where"}
_ACTIONS = {"stop": "cancel", "pause": "pause", "tell": "notify"}
#: What an action leaves of its rule for the next action on that condition.
_DONE = ("then", "file", "text")


class _Rule(argparse.Action):
    """Collects options into a list of rules, in the order given."""

    def __call__(self, parser, ns, value, option=None):
        rules = getattr(ns, self.dest)
        if rules is None:
            rules = []
            setattr(ns, self.dest, rules)
        what = option.lstrip("-")
        what = _ALIASES.get(what, what)
        last = rules[-1] if rules else None
        if what in _CONDITIONS:
            rules.append({_CONDITIONS[what]: value})
            return
        if what == "where":
            if last is None or "then" in last or "idle_s" in last:
                last = {}
                rules.append(last)
            last.setdefault("where", []).append(value)
            return
        if last is None:
            parser.error(f"{option} needs a condition before it: --on, --where or --idle")
        if what == "once":
            last["once"] = True
            return
        if "then" in last:
            last = {k: v for k, v in last.items() if k not in _DONE}
            rules.append(last)
        if what == "append":
            last.update(then="append", file=value[0], text=value[1])
        elif what == "input":
            last.update(then="input", text=value)
        else:
            last["then"] = value if what == "then" else _ACTIONS[what]


def said(rules: list[dict] | None) -> str:
    """Confirmation, so that nobody has to check: "; 2 rules per job: ..."."""
    if not rules:
        return ""

    def one(r):
        when = r.get("match") or ", ".join(r.get("where", ())) or \
            (f"idle {r['idle_s']:g}s" if "idle_s" in r else f"absent {r['absent_s']:g}s")
        do = r.get("then", "notify")
        return f"{when[:50]!r} -> {do}" + (f" {r['file']}" if "file" in r else "")

    return f"; the server holds {len(rules)} rules per job: " + "; ".join(map(one, rules))


def add_options(p: argparse.ArgumentParser, aliases: bool = False) -> None:
    """*aliases*: also the names job_watch uses, where they are free."""
    def option(name, *more, **kw):
        p.add_argument("--" + name, *(more if aliases else ()), action=_Rule,
                       dest="rules", **kw)
    option("on", "--match", metavar="MATCH")
    option("where", "-w", metavar="COND")
    option("idle", metavar="S", type=float)
    option("absent", metavar="S", type=float)
    option("append", metavar=("FILE", "TEXT"), nargs=2)
    option("input", metavar="TEXT")
    if aliases:
        option("then", metavar="ACTION")
    for name in ("stop", "pause", "tell", "once"):
        option(name, nargs=0)
