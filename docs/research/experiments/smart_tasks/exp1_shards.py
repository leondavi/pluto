"""Experiment 1 (v2): plan, workspace builder, ground truth and grader."""

import os
import random

import pst_build
import wl_shards
from wl_shards import cell_ck

NAME = "e1_shard_fleet"
KINDS = ["BALANCE", "SIZE", "ORDER", "STALE", "PERSIST", "VERSION", "PATH"]
N_SHARDS, GROUP, N_CELLS = 48, 12, 6
OPS, DURATION, TIMEOUT = 9000, 200.0, 1200
BARRIER_S, REACT_S, LIVELOCK_S = 120, 90, 1000
ANSWER = "answers/final.json"


def owner(rec):
    """Agent that owns a ledger record."""
    return f"w{(rec['shard'] - 1) // GROUP + 1}"


def plan(seed, ops=OPS, duration=DURATION, transient_s=None):
    """Return ({shard: params}, [corrupted cells])."""
    rng = random.Random(f"plan1v2:{seed}")
    shards = {}
    ids = list(range(1, N_SHARDS + 1))
    n_faults = [0] * 10 + [1] * 20 + [2] * 18
    rng.shuffle(n_faults)
    live, trans = set(), set()
    for g in range(N_SHARDS // GROUP):              # 2 livelocks and 3 transients per group
        pick = rng.sample(ids[g * GROUP:(g + 1) * GROUP], 5)
        live |= set(pick[:2])
        trans |= set(pick[2:])
    for s in ids:
        g = (s - 1) // GROUP
        hi = 0.46 if s in live else 0.90
        spin = "LIVELOCK" if s in live else "TRANSIENT" if s in trans else None
        shards[s] = {
            "seed": rng.randrange(2**40), "shard": s, "ops": ops,
            "duration": duration * rng.uniform(0.9, 1.05), "barrier_s": BARRIER_S,
            "group": ids[g * GROUP:(g + 1) * GROUP], "cells": [],
            "faults": sorted((k, int(ops * rng.uniform(0.15, hi)))
                             for k in rng.sample(KINDS, n_faults[s - 1])),
            "spin": spin, "spin_op": int(ops * rng.uniform(0.5, 0.85)) if spin else 0,
            "spin_s": LIVELOCK_S if spin == "LIVELOCK" else
            transient_s if transient_s is not None else rng.uniform(6, 13)}

    def cell(s, op, text):
        shards[s]["cells"].append((min(max(op, 1), ops), text))

    def txt(verb, key, val, bad=False, retry=0, peer=""):
        ck = cell_ck(key, val) ^ (0x40 << rng.randrange(8) if bad else 0)
        return f"{verb} cell={key:06d} val={val:06d} ck={ck:04x}{peer} retry={retry}"

    pool = rng.sample(range(500000, 600000), 900)
    poison, pool = pool[:N_CELLS], pool[N_CELLS:]
    scale = ops / OPS
    for _ in range(int(14000 * scale)):                 # healthy cell traffic
        k, v = rng.choice(pool), rng.randrange(10**6)
        cell(rng.choice(ids), rng.randrange(1, ops),
             txt("CELL", k, v, bad=(b := rng.random() < 0.012), retry=int(b)))
    for _ in range(int(9000 * scale)):                  # healthy migrations
        _xfer(rng, cell, txt, ops, rng.choice(pool), rng.randrange(10**6),
              *rng.sample(ids, 2), rng.randrange(1, int(ops * .97)), False)
    ok = [s for s in ids if s not in live]              # a livelocked shard stops migrating
    truth_cells = []
    for c in poison:
        path = rng.sample(ok, rng.randint(4, 7))
        o0, val = int(ops * rng.uniform(0.26, 0.40)), rng.randrange(10**6)
        for frac in (0.06, 0.13, 0.20):                 # the same cell while healthy
            _xfer(rng, cell, txt, ops, c, rng.randrange(10**6), *rng.sample(ids, 2),
                  int(ops * frac), False)
        cell(path[0], o0, txt("CELL", c, val, bad=True))
        o = o0
        for a, b in zip(path, path[1:]):
            o = _xfer(rng, cell, txt, ops, c, val, a, b,
                      o + int(ops * rng.uniform(0.03, 0.07)), True)
        truth_cells.append({"cell": c, "shard": path[0], "op": o0, "path": path})
    for p in shards.values():
        p["cells"].sort(key=lambda x: x[0])
    return shards, sorted(truth_cells, key=lambda x: x["cell"])


def _xfer(rng, cell, txt, ops, k, v, a, b, o, bad):
    o2 = o + rng.randrange(8, 50)
    if rng.random() < 0.02 and not bad:
        cell(a, o, txt("XOUT", k, v, bad=True, retry=1, peer=f" to={b:02d}"))
    cell(a, o, txt("XOUT", k, v, bad=bad, peer=f" to={b:02d}"))
    cell(b, o2, txt("XIN", k, v, bad=bad, peer=f" from={a:02d}"))
    return o2


def path_ok(path, key):
    """Every node's later nodes lie on the same side as the searched key."""
    for i, x in enumerate(path):
        if x == key and i != len(path) - 1:
            return False
        if any((y < x) != (key < x) or y == x for y in path[i + 1:]):
            return False
    return True


def check_lines(lines):
    """Independent checker over log text: ({kind: {op, key}}, bad cell lines)."""
    snaps, first, bad = {}, {}, []
    latest, last_ver, rebuild = {}, 0, False
    for line in lines:
        t = line.split()
        if line.startswith("#") or len(t) < 3:
            continue
        op = int(t[0])
        f = dict(x.split("=", 1) for x in t[1:] if "=" in x)
        hits = []
        if t[1] == "REBUILD":
            rebuild = t[2] == "begin"
        elif t[1] == "AUDIT" and not rebuild:
            k = int(f["node"])
            if not int(f["lo"]) < k < int(f["hi"]):
                hits.append(("ORDER", k))
            if abs(int(f["bf"])) > 1:
                hits.append(("BALANCE", k))
            if int(f["sz"]) != int(f["lsz"]) + int(f["rsz"]) + 1:
                hits.append(("SIZE", k))
        elif t[1] == "SNAP":
            snaps[f["id"]] = f["dg"]
        elif t[1] == "RECHK" and snaps[f["id"]] != f["dg"]:
            hits.append(("PERSIST", int(f["pin"])))
        elif len(t) > 3 and t[3] in ("GET", "PUT", "DEL"):
            k = int(f["key"])
            if t[3] == "GET" and f["repl"] == "0" and int(f["ver"]) != latest.get(k, 0):
                hits.append(("STALE", k))
            if t[3] == "PUT":
                v = int(f["ver"])
                if v != last_ver + 1:
                    hits.append(("VERSION", k))
                last_ver = latest[k] = v
            if t[3] == "DEL":
                latest.pop(k, None)
            if not path_ok([int(x) for x in f["path"].split("/")], k):
                hits.append(("PATH", k))
        elif t[2] in ("CELL", "XOUT", "XIN") and f["retry"] == "0":
            k, v = int(f["cell"]), int(f["val"])
            if int(f["ck"], 16) != cell_ck(k, v):
                bad.append({"op": op, "lt": int(f["lt"]), "verb": t[2], "cell": k,
                            "peer": f.get("to") or f.get("from")})
        for kind, k in hits:
            first.setdefault(kind, {"op": op, "key": k})
    return first, bad


def truth(seed, **kw):
    shards, cells = plan(seed, **kw)
    return {"shards": {str(s): check_lines(wl_shards.text_lines(p))[0]
                       for s, p in shards.items()}, "cells": cells,
            "livelocked": [s for s, p in shards.items() if p["spin"] == "LIVELOCK"]}


def build(ws, seed, **kw):
    shards, _ = plan(seed, **kw)
    pst_build.install(ws, "shard_stress", "wl_shards",
                     {f"shard{s}.img": p for s, p in shards.items()})
    pst_build.write(ws, "docs/LOGFORMAT.md", LOGFORMAT)


def _eq(a, b):
    if isinstance(a, bool) or a is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def _d(x):
    return x if isinstance(x, dict) else {}


def shard_score(got, want):
    """Kind 0.3 + op 0.4 + key 0.3 per violated kind, over max(true, claimed)."""
    if not isinstance(got, dict):
        return 0.0
    g = {str(k).upper(): _d(v) for k, v in got.items() if v is not None}
    if not want:
        return float(not g)
    x = sum(.3 + .4 * _eq(g[k].get("op"), w["op"]) + .3 * _eq(g[k].get("key"), w["key"])
            for k, w in want.items() if k in g)
    return x / max(len(want), len(g))


def reactions(ws, livelocked):
    """(livelocks stopped within REACT_S of onset, other shards killed)."""
    hits, done, killed = set(), set(), set()
    for r in pst_build.launches(ws):
        s = r.get("shard")
        if s in livelocked:
            if "onset" in r and r["t_last"] - r["onset"] <= REACT_S:
                hits.add(s)
        elif r["done"]:
            done.add(s)
        elif "began" in r:
            killed.add(s)
    return len(hits), len(killed - done)


def grade(ws, tr, meta=None):
    """Shards 0.50, corrupted cells 0.25, livelock reactions 0.25."""
    ans = pst_build.read_json(os.path.join(ws, ANSWER))
    got, xs = _d(_d(ans).get("shards")), []
    for s, want in tr["shards"].items():
        g = got.get(s)
        if not isinstance(g, dict):
            g = pst_build.read_json(os.path.join(ws, f"answers/shard_{s}.json"))
        xs.append(shard_score(g, want))
    cl = _d(ans).get("cells")
    by = {}
    for c in cl if isinstance(cl, list) else []:
        if isinstance(c, dict) and str(c.get("cell")).isdigit():
            by[int(c["cell"])] = c
    y = 0.0
    for w in tr["cells"]:
        c = by.get(w["cell"], {})
        p = c.get("path") if isinstance(c.get("path"), list) else []
        if c:
            y += (.2 + .15 * _eq(c.get("shard"), w["shard"]) + .25 * _eq(c.get("op"), w["op"])
                  + .4 * sum(map(_eq, p, w["path"])) / max(len(p), len(w["path"])))
    y /= max(len(tr["cells"]), len(by))
    hit, false = reactions(ws, set(tr["livelocked"]))
    z = max(0, hit - false) / len(tr["livelocked"])
    parts = {"shards": round(sum(xs) / len(xs), 4), "cells": round(y, 4), "reactions": round(z, 4)}
    return {"score": round(.5 * sum(xs) / len(xs) + .25 * y + .25 * z, 4), "parts": parts,
            "livelocks_stopped": hit, "false_kills": false, "final_valid": isinstance(ans, dict)}


LOGFORMAT = """# Shard stress log format

`bin/shard_stress --shard K` (K = 1..48) stresses one shard of a persistent AVL
map and prints about 2 MB over about 200 s. Shards form groups of twelve (1-12,
13-24, 25-36, 37-48). A shard starts working only when all twelve of its group
are running at the same time; it gives up after 120 s of waiting (exit 75), and
then the group must be launched again. Shards that have already finished count
as present. A rerun of a shard prints the same trace.

Every data line starts with a 6-digit op index. `lt` is a Lamport time that is
comparable across all 48 shards.

| Line | Meaning |
|---|---|
| `OP lt thr PUT/DEL/GET key ... path` | client operation; `path` lists the last up to 9 nodes visited, in order |
| `OP AUDIT node lo hi lh rh bf lsz rsz sz` | audit of one node after the op; `lo`/`hi` are the bounds implied by its ancestors |
| `OP ROT ...` | rotation in progress, transient imbalance is legal here |
| `OP REBUILD begin/end id` | AUDIT lines between begin and end are exempt from all checks |
| `OP SNAP id pin dg` / `OP RECHK id pin dg` | digest of an immutable snapshot when taken / re-verified |
| `OP lt CELL/XOUT/XIN cell val ck ...` | cell written locally / migrated out / migrated in |
| `OP SPIN ...` | latch spinning, see below |
| `ERROR`, `WARN` | recovered operational noise, never a violation |

## Invariants (exactly these seven kinds)

* `ORDER`: AUDIT outside a rebuild has lo < node < hi (strict)
* `BALANCE`: AUDIT outside a rebuild has -1 <= bf <= 1
* `SIZE`: AUDIT outside a rebuild has sz = lsz + rsz + 1
* `STALE`: a GET with repl=0 returns the ver of the latest earlier PUT of that
  key in this trace, or 0 if there is none or the key was DELeted after it
  (repl=1 reads may lag)
* `PERSIST`: RECHK dg equals the dg of the SNAP with the same id
* `VERSION`: the ver of a PUT is the previous PUT's ver plus 1 (the first is 1)
* `PATH`: for every node x in a path, all nodes after it are on the same side of
  x as the operation's key (both smaller or both larger), and the key itself can
  only be the last node

The offending key is `node` for AUDIT kinds, `pin` for PERSIST, `key` otherwise.
A shard violates zero, one or two kinds, usually many times each. For every
violated kind report the FIRST occurrence.

## Cells

ck is 4 hex digits and equals (cell*40503 + val*7) mod 65536. Lines with retry=1
are link-level retransmissions and never count. Exactly six cells get corrupted:
a CELL line with retry=0 carries a wrong ck (the origin), and the corrupted cell
then migrates along 4 to 7 different shards (XOUT/XIN with retry=0 and wrong ck).

## Livelocks

While a shard spins it prints one SPIN line per second and nothing else. A
harmless spin ends by itself within 15 s (`SPIN resolved`). A spin that is still
going after 15 s is a livelock: the shard never recovers and must be terminated
no later than 90 s after its first SPIN line. Other shards must not be
terminated. Two to five shards per group spin at some time.
"""

TASK = """The workspace contains a sharded, persistent AVL map with seeded defects. Read docs/LOGFORMAT.md first: it defines the trace format, the invariants, the cell checksum and the livelock rule.

Run `bin/shard_stress --shard K` for K = 1..48 and
1. while the shards run, terminate every livelocked shard within 90 s of its first SPIN line, and no other shard;
2. find, per shard, the first violation of every violated invariant kind;
3. find the six corrupted cells: origin (shard, op) and ordered path of shards, starting with the origin. Paths cross worker boundaries.

Ownership: w1 = shards 1-12; w2 = 13-24; w3 = 25-36; w4 = 37-48. All twelve of your shards must run at the same time (barrier). Each trace is about 2 MB, so never print a whole trace.
Each worker writes answers/shard_K.json for each of its shards: {"KIND": {"op": int, "key": int}, ...}, with {} for a shard without violations, and shares its corrupted-cell evidence in board/.
w1 also combines everything, waiting for the others, into answers/final.json: {"shards": {"1": {...}, ..., "48": {...}}, "cells": [{"cell": int, "shard": int, "op": int, "path": [int, ...]}, ... 6 entries]}"""
