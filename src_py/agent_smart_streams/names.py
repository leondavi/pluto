"""Jobs by the names an agent gave them.

``soak`` is a batch, ``soak/3`` one of its jobs, ``soak/1-6,9`` some of
them: the key of a job is the parameter it was started with, so an agent
never needs a table of ids. A batch without a name goes by its handle
(``g3f09ac/3``). Ids are accepted as before, and so are lists with commas
(``r1,r2,soak/3``).
"""

from __future__ import annotations

import os
import re
import sys

from agent_smart_streams.compact import short
from agent_smart_streams.constants import (
    GROUP_MAX, GROUP_RE, NAME_RE, NAME_TAG, SHORT_RE,
)
from pluto_sdk import PlutoError, PlutoJobsClient

_RANGE = re.compile(r"(-?\d+)-(-?\d+)")


def check_name(name: str) -> str:
    """*name*, if jobs can be found by it."""
    if not re.fullmatch(NAME_RE, name) or re.fullmatch(f"{SHORT_RE}|{GROUP_RE}", name):
        raise PlutoError(f"name {name!r}: use letters, digits, _ . -, starting with a "
                         "letter and unlike an id")
    return name


def tag(name: str) -> str:
    return NAME_TAG + check_name(name)


def group_of(card: dict) -> str | None:
    """What the batch of a job is called: its name, or its handle."""
    tags = card.get("tags") or ()
    return next((t[len(NAME_TAG):] for t in tags if t.startswith(NAME_TAG)),
                next((t for t in tags if re.fullmatch(GROUP_RE, t)), None))


def label(card: dict) -> str:
    """How an agent names this job: soak/3, or its id if it has no name."""
    group, key = group_of(card), card.get("name")
    if group is None:
        return short(card["job_id"])
    return group if key in (None, group) else f"{group}/{key}"


def key_of(card: dict) -> str:
    """The part of the label that differs within a batch."""
    return card.get("name") if group_of(card) and card.get("name") else short(card["job_id"])


def values(spec, limit: int) -> list[str]:
    """"1-4,8,x" or a list of such items: inclusive integer ranges and
    literals, in order. A leading zero keeps its width ("01-12")."""
    items = spec if isinstance(spec, list) else str(spec).split(",")
    out: list[str] = []
    for item in map(str, items):
        m = _RANGE.fullmatch(item.strip()) if isinstance(spec, str) else None
        if not m:
            out.append(item.strip() if isinstance(spec, str) else item)
            continue
        lo, hi = int(m[1]), int(m[2])
        if hi < lo or hi - lo >= limit:
            raise PlutoError(f"bad range {item!r} (at most {limit} values)")
        width = len(m[1]) if m[1].startswith("0") else 0
        out += [str(n).zfill(width) for n in range(lo, hi + 1)]
    return out


def ranges(keys: list[str]) -> str:
    """The inverse of values(): "1-4,8,x"."""
    numbers = sorted({int(k) for k in keys if re.fullmatch(r"0|[1-9]\d*", k)})
    out, i = [], 0
    while i < len(numbers):
        j = i
        while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
            j += 1
        first, last = str(numbers[i]), str(numbers[j])
        out += [first] if i == j else [first, last] if j == i + 1 else [f"{first}-{last}"]
        i = j + 1
    plain = {str(n) for n in numbers}
    return ",".join(out + [k for k in keys if k not in plain])


#: Every job named in this process, for the news line: what a command just
#: showed is no news.
NAMED: set[str] = set()


# ── Finding ───────────────────────────────────────────────────────────────────

def _listed(jobs: PlutoJobsClient, wanted: str) -> list[dict]:
    r = jobs.call("list", scope="all", tags=[wanted], limit=GROUP_MAX)
    if r["total"] > len(r["jobs"]):
        raise PlutoError(f"{r['total']} jobs are called {wanted.removeprefix(NAME_TAG)}; "
                         "use group handles or ids")
    return r["jobs"]


def _here(card: dict) -> bool:
    cwd = card.get("cwd")
    return bool(cwd) and os.path.realpath(cwd) == os.path.realpath(os.getcwd())


def _current(cards: list[dict]) -> list[dict]:
    """Of one key the newest job. A key started again under the same name
    replaces its earlier run; a key started later joins the others, so a
    name can grow one job at a time. A round with other parameters (seed=
    after n=) is another experiment: the newest round's kind replaces it."""
    cards = sorted(cards, key=lambda c: c["opened_at"], reverse=True)
    kind = sorted((cards[0].get("params") or {})) if cards else None
    out: dict[str, dict] = {}
    for c in cards:
        if sorted(c.get("params") or {}) == kind:
            out.setdefault(c.get("name") or c["job_id"], c)
    return sorted(out.values(), key=lambda c: c["opened_at"])


def _batch(jobs: PlutoJobsClient, group: str, strict: bool, own: bool = False) -> list[dict]:
    if re.fullmatch(GROUP_RE, group):
        cards = sorted(_listed(jobs, group), key=lambda c: c["opened_at"])
    else:
        cards = _listed(jobs, tag(group))
        # Agents of a team often share a directory and pick the same names:
        # an agent's own jobs come first, others' only when it has none.
        mine = [] if strict else [c for c in cards if c["owner"] == jobs.agent_id]
        if own and not strict and not mine and cards:
            raise PlutoError(f"no job of yours is called {group!r} (others' are: "
                             "name them by id, see pss ps -v)")
        theirs = not mine and not strict and cards
        near = mine or [c for c in cards if _here(c)]
        cards = _current(near or ([] if strict else cards))
        if theirs and cards:
            print(f"pss: none of {group} is yours; these are "
                  f"{', '.join(sorted({c['owner'] for c in cards}))}'s", file=sys.stderr)
    if not cards:
        raise PlutoError(f"no job is called {group!r}" + (" in this directory" if strict else ""))
    return cards


def _named(jobs: PlutoJobsClient, handle: str, strict: bool, own: bool = False) -> list[dict]:
    group, _, keys = handle.partition("/")
    cards = _batch(jobs, group, strict, own)
    if not keys:
        return cards
    found = {c.get("name"): c for c in cards}
    want = values(keys, GROUP_MAX)
    missing = [k for k in want if k not in found]
    if missing:
        raise PlutoError(f"{group} has no {', '.join(missing[:5])}; it has "
                         + ranges([c.get("name") or "-" for c in cards]))
    return [found[k] for k in want]


def is_name(handle: str) -> bool:
    """Is it more than the id of one job?"""
    return not re.fullmatch(f"{SHORT_RE}|J-.*", handle)


def cards(jobs: PlutoJobsClient, handles: list[str], *, strict: bool = False,
          own: bool = False) -> list[dict]:
    """Cards of the jobs the handles stand for, oldest first per handle.
    *strict*: for changes made as the user, who owns every job; then a name
    means jobs of this directory only. *own*: a name must be the agent's
    own (to follow or wait for: others' jobs only by id)."""
    out: dict[str, dict] = {}
    for h in handles:
        for c in _resolve(jobs, h, strict, own):
            out.setdefault(c["job_id"], c)
    NAMED.update(out)
    return list(out.values())


def _resolve(jobs: PlutoJobsClient, handle: str, strict: bool, own: bool = False) -> list[dict]:
    """``soak/1,4`` names keys; ``r1,r2`` or ``a/1-3,b`` that are no keys are
    read as several handles, as people write them."""
    def one(h):
        return _named(jobs, h, strict, own) if is_name(h) else [jobs.call("status", job_id=h)]
    try:
        return one(handle)
    except PlutoError:
        parts = [p for p in handle.split(",") if p]
        if len(parts) < 2:
            raise
    out, group = [], None
    for p in parts:                     # "s/1-3,l2/7,8": 8 is l2/8
        if "/" in p:
            group = p.partition("/")[0]
            out += one(p)
            continue
        try:
            out += one(p)
        except PlutoError:
            if group is None:
                raise
            out += one(f"{group}/{p}")
    return out


def members(jobs: PlutoJobsClient, handles: list[str], *, strict: bool = False,
            own: bool = False) -> list[str]:
    """Short ids of those jobs; plain ids are not looked up."""
    out: list[str] = []
    for h in handles:
        out += [short(c["job_id"]) for c in cards(jobs, [h], strict=strict, own=own)] \
            if is_name(h) else [short(h)]
    return list(dict.fromkeys(out))
