"""What an agent needs to take jobs over: what runs, what the server does
for it, what its output looks like. Ready to be put into a file."""

from __future__ import annotations

from agent_smart_tasks import fleet, names, render
from agent_smart_tasks.compact import dumps, short
from agent_smart_tasks.constants import BRIEF_JOBS, BRIEF_SHAPES, BRIEF_TEXT
from pluto_sdk import PlutoError, PlutoJobsClient


def _rules(jobs: PlutoJobsClient, cards: list[dict]) -> list[str]:
    """Rules of the jobs, equal ones together."""
    listed = jobs.watch("list", job_ids=[c["job_id"] for c in cards])["jobs"]
    seen: dict[str, list[int]] = {}
    for found in listed.values():
        for r in found:
            spec = {k: v for k, v in r["spec"].items()
                    if k not in ("name", "cooldown_s", "max_hits", "once", "from_now")
                    and (k, v) != ("count", 1)}
            n = seen.setdefault(dumps(spec), [0, 0, 0])
            n[0] += r["active"]
            n[1] += r["fired"]
            n[2] += r["waiting"]
    return [f"  rule {spec}: active on {a} jobs, fired {f}x"
            + (f", {w} actions waiting" if w else "") for spec, (a, f, w) in seen.items()]


def _shapes(jobs: PlutoJobsClient, cards: list[dict]) -> list[str]:
    """What lines look like, from the output of the first job."""
    try:
        out = jobs.analyze(job_ids=[cards[0]["job_id"]], limit=BRIEF_SHAPES)["out"]
    except PlutoError as exc:
        return [f"  output: {exc}"]
    return ["  output of one:"] + ["    " + x[:160] for x in out.splitlines()[:2 * BRIEF_SHAPES]]


def text(jobs: PlutoJobsClient, cards: list[dict]) -> str:
    groups: dict[tuple, list[dict]] = {}
    for c in cards:
        groups.setdefault((names.group_of(c) or short(c["job_id"]), c.get("owner")),
                          []).append(c)
    out = []
    for (group, owner), cs in groups.items():
        one = jobs.call("open", job_id=cs[0]["job_id"])
        command = one.get("command") or dumps(one.get("argv"))
        out.append(f"{group}: {len(cs)} jobs of {owner}, e.g. {names.label(cs[0])} = "
                   f"{command[:300]}  (in {one.get('cwd', '?')})")
        out += ["  " + r for r in render.rows(cs, owner)]
        out += _rules(jobs, cs[:BRIEF_JOBS])
        out += _shapes(jobs, cs)
    out.append("pst wait NAME; pst each NAME 'prog {log} {key} {state} {exit}' -o 'out/{key}.json' "
               "runs a program per job as it ends; pst stop NAME (owner only). The rules stay.")
    body = "\n".join(out)
    return body if len(body) <= BRIEF_TEXT else body[:BRIEF_TEXT] + "\n(cut; name fewer jobs)"


def give(jobs: PlutoJobsClient, to: str, cards: list[dict]) -> tuple[list, dict]:
    """Transfer all. (cards given, {label: why not})"""
    by_id = {c["job_id"]: c for c in cards}
    r = fleet.each(lambda j: jobs.call("transfer", job_id=j, to=to), list(by_id))
    failed = {names.label(by_id[j]): x["error"] for j, x in r.items() if "error" in x}
    return [c for j, c in by_id.items() if "error" not in r[j]], failed
