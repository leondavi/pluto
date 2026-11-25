"""Many jobs in few lines: what is alike is said once."""

from __future__ import annotations

import re

from agent_smart_streams import names
from agent_smart_streams.compact import brief, short
from pluto_jobs_def import FINAL_STATES

_DIGITS = re.compile(r"[0-9a-fA-F]*\d[0-9a-fA-F]*")


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" or n >= 100 else f"{n:.1f}{unit}"
        n /= 1000


def span(values: list[float], show) -> str:
    """"12s" or "12-15s"."""
    lo, hi = show(min(values)), show(max(values))
    if lo == hi:
        return hi
    unit = re.search(r"[A-Za-z]*\Z", hi)[0]
    return (lo[:-len(unit)] if unit and lo.endswith(unit)
            and not lo[:-len(unit)][-1:].isalpha() else lo) + "-" + hi


def ended(card: dict) -> bool:
    return card["state"] in FINAL_STATES


def status(card: dict) -> str:
    """State of one job, without what is different for every job."""
    code = card.get("exit_code")
    out = card["state"]
    if out != "succeeded" and code not in (None, 0):
        out += f" exit={code}"
    if card.get("reason"):
        out += f" ({card['reason']})"
    if not ended(card) and card.get("activity") == "idle":
        out += " idle"
    return out


def row(card: dict, me: str, last: str = "") -> str:
    """One job on its own line."""
    b = brief(card)
    own = f" {card['owner']}" if card.get("owner") not in (None, me) else ""
    name = names.label(card)
    ident = short(card["job_id"])
    return (f"{name}{'' if name == ident else ' ' + ident} {status(card)} "
            f"{b['elapsed_s']}s {size(b.get('size', 0))}{own}" + (f": {last}" if last else ""))


def rows(cards: list[dict], me: str, last: dict | None = None,
         verbose: bool = False) -> list[str]:
    """Jobs of a batch in the same state share a line, whose key list is
    valid input again. *last*: {short id: last output line}; lines that
    differ only in numbers count as the same."""
    last = last or {}
    if verbose:
        return [row(c, me, last.get(short(c["job_id"]), "")) for c in cards]
    groups: dict[tuple, list[dict]] = {}
    for c in cards:
        text = last.get(short(c["job_id"]), "")
        group = names.group_of(c) or (None if c.get("name") is None else "")
        groups.setdefault((group, status(c), c.get("owner"), _DIGITS.sub("#", text)),
                          []).append(c)
    out = []
    for (group, state, owner, _), cs in groups.items():
        if len(cs) == 1:
            out.append(row(cs[0], me, last.get(short(cs[0]["job_id"]), "")))
            continue
        keys = names.ranges([names.key_of(c) for c in cs])
        own = f" {owner}" if owner not in (None, me) else ""
        took = span([c.get("elapsed_ms", 0) / 1000 for c in cs], lambda v: f"{v:.0f}s")
        bytes_ = span([c.get("size", 0) for c in cs], size)
        text = last.get(short(cs[0]["job_id"]), "")
        out.append(f"{group + '/' if group else ''}{keys} {len(cs)}x {state} {took} {bytes_}"
                   f"{own}" + (f": e.g. {text}" if text else ""))
    return out
