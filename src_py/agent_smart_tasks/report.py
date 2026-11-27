"""A record per job, computed on the server, optionally one file each."""

from __future__ import annotations

import json
import re

from agent_smart_tasks import each, names
from pluto_sdk import PlutoError, PlutoJobsClient

#: Columns that are about the job rather than its output.
_ABOUT = {"_state": "state", "_exit": "exit_code", "_signal": "signal"}
_PLAIN = re.compile(r"\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\Z")


def about(card: dict, what: str):
    """Value of a column about the job; KeyError if it is not one."""
    if what == "_key":
        return names.key_of(card)
    if what in _ABOUT:
        return card.get(_ABOUT[what])
    return (card.get("params") or {})[what]


def records(jobs: PlutoJobsClient, cards: list[dict], extract: list, columns: list) -> dict:
    """{"records": {label: (card, record)}, "notes": [..]}"""
    if not columns:
        raise PlutoError("report needs -f NAME=FIELD; see pst help report")
    own, asked = {}, []
    params = set.intersection(*(set(c.get("params") or {}) for c in cards))
    for col in columns:
        m = _PLAIN.match(col)
        if m and (m[2] in _ABOUT or m[2] == "_key" or m[2] in params):
            own[m[1]] = m[2]
        else:
            asked.append(col)
    r = {"records": [{} for _ in cards], "notes": []}
    if asked:
        r = jobs.analyze(job_ids=[c["job_id"] for c in cards], columns=asked,
                         extract=extract or None)
    order = [c.partition("=")[0].strip() for c in columns]
    out = {}
    for c, rec in zip(cards, r["records"]):
        rec = {**rec, **{k: about(c, what) for k, what in own.items()}}
        out[names.label(c)] = (c, {k: rec[k] for k in order})
    return {"records": out, "notes": r["notes"]}


def save(found: dict, path: str) -> str:
    """One JSON file per job; nothing at all unless every path differs."""
    each.check_outputs(path, [c for c, _ in found.values()])
    for c, rec in found.values():
        each.write(each.fill(path, each.facts(c)), json.dumps(rec).encode() + b"\n")
    running = sum(c["state"] not in each.render.FINAL_STATES for c, _ in found.values())
    return f"{len(found)} files written" + (f"; {running} jobs still running" if running else "")
