"""Experiment 1: plan, workspace builder, ground truth and grader."""

import json
import os
import random

import ss_build
import wl_shards
from wl_shards import cell_ck

NAME = "e1_shard_debug"
KINDS = ["BALANCE", "SIZE", "ORDER", "STALE", "PERSIST"]
N_SHARDS = 8
OPS, DURATION, TIMEOUT = 38000, 90.0, 900
ANSWER = "answers/final.json"


def plan(seed, ops=OPS, duration=DURATION):
    """Return ({shard: params}, cross-shard truth)."""
    rng = random.Random(f"plan1:{seed}")
    kinds = KINDS + rng.sample(KINDS, N_SHARDS - 1 - len(KINDS)) + ["NONE"]
    rng.shuffle(kinds)
    shards = {}
    for s in range(1, N_SHARDS + 1):
        shards[s] = {"seed": rng.randrange(2**40), "shard": s, "ops": ops,
                     "duration": duration * rng.uniform(0.85, 1.1), "kind": kinds[s - 1],
                     "fault_op": int(ops * rng.uniform(0.50, 0.88)), "cells": []}

    def cell(s, op, text):
        shards[s]["cells"].append((min(max(op, 1), ops), text))

    def txt(verb, key, val, bad=False, retry=0, peer=""):
        ck = cell_ck(key, val) ^ (0x40 << rng.randrange(8) if bad else 0)
        return f"{verb} cell={key:06d} val={val:06d} ck={ck:04x}{peer} retry={retry}"

    pool = rng.sample(range(500000, 600000), 700)
    poison = pool[0]
    scale = ops / OPS
    for _ in range(int(9000 * scale)):                  # healthy cell traffic
        k, v = rng.choice(pool[1:]), rng.randrange(10**6)
        s, o = rng.randrange(1, N_SHARDS + 1), rng.randrange(1, ops)
        cell(s, o, txt("CELL", k, v, bad=(b := rng.random() < 0.012), retry=int(b)))
    for _ in range(int(5000 * scale)):                  # healthy migrations
        _xfer(rng, cell, txt, ops, rng.choice(pool[1:]), rng.randrange(10**6),
              *rng.sample(range(1, N_SHARDS + 1), 2), rng.randrange(1, int(ops * .97)), False)
    path = rng.sample(range(1, N_SHARDS + 1), 5)
    o0 = int(ops * rng.uniform(0.28, 0.34))
    val = rng.randrange(10**6)
    for frac in (0.06, 0.13, 0.20):                     # the poison cell while still healthy
        _xfer(rng, cell, txt, ops, poison, rng.randrange(10**6),
              *rng.sample(range(1, N_SHARDS + 1), 2), int(ops * frac), False)
    cell(path[0], o0, txt("CELL", poison, val, bad=True))
    o = o0
    for a, b in zip(path, path[1:]):
        o = _xfer(rng, cell, txt, ops, poison, val, a, b,
                  o + int(ops * rng.uniform(0.06, 0.11)), True)
    for p in shards.values():
        p["cells"].sort(key=lambda c: c[0])
    return shards, {"shard": path[0], "op": o0, "cell": poison, "path": path}


def _xfer(rng, cell, txt, ops, k, v, a, b, o, bad):
    o2 = o + rng.randrange(20, 120)
    if rng.random() < 0.02 and not bad:
        cell(a, o, txt("XOUT", k, v, bad=True, retry=1, peer=f" to={b}"))
    cell(a, o, txt("XOUT", k, v, bad=bad, peer=f" to={b}"))
    cell(b, o2, txt("XIN", k, v, bad=bad, peer=f" from={a}"))
    return o2


def check_lines(lines):
    """Independent checker over log text: first tree violation + bad cells."""
    snaps, first, bad = {}, None, []
    for line in lines:
        if line.startswith("#"):
            continue
        t = line.split()
        op = int(t[0])
        f = dict(x.split("=", 1) for x in t[1:] if "=" in x)
        hit = None
        if t[1] == "AUDIT":
            k, lo, hi = int(f["node"]), int(f["lo"]), int(f["hi"])
            if not lo < k < hi:
                hit = ("ORDER", k)
            elif abs(int(f["bf"])) > 1:
                hit = ("BALANCE", k)
            elif int(f["sz"]) != int(f["lsz"]) + int(f["rsz"]) + 1:
                hit = ("SIZE", k)
        elif t[1] == "SNAP":
            snaps[f["id"]] = f["dg"]
        elif t[1] == "RECHK" and snaps[f["id"]] != f["dg"]:
            hit = ("PERSIST", int(f["pin"]))
        elif len(t) > 3 and t[3] == "GET" and f["repl"] == "0" and f["ver"] != f["want"]:
            hit = ("STALE", int(f["key"]))
        elif t[2:3] and t[2] in ("CELL", "XOUT", "XIN") and f["retry"] == "0":
            k, v = int(f["cell"]), int(f["val"])
            if int(f["ck"], 16) != cell_ck(k, v):
                bad.append({"op": op, "lt": int(f["lt"]), "verb": t[2], "cell": k,
                            "peer": f.get("to") or f.get("from")})
        if hit and first is None:
            first = {"op": op, "kind": hit[0], "key": hit[1]}
    return first or {"op": None, "kind": "NONE", "key": None}, bad


def truth(seed, ops=OPS, duration=DURATION):
    shards, cross = plan(seed, ops, duration)
    return {"shards": {str(s): check_lines(l for _, l in wl_shards.trace(p))[0]
                       for s, p in shards.items()}, "root_cause": cross}


def build(ws, seed, ops=OPS, duration=DURATION):
    shards, _ = plan(seed, ops, duration)
    ss_build.install(ws, "shard_stress", "wl_shards",
                     {f"shard{s}.img": p for s, p in shards.items()})
    ss_build.write(ws, "docs/LOGFORMAT.md", LOGFORMAT)


def _eq(a, b):
    if a is None or b is None:
        return a is b
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a).strip().upper() == str(b).strip().upper()


def _d(x):
    return x if isinstance(x, dict) else {}


def grade(ws, tr, meta=None):
    """Score in [0,1]. Shards 0.68, root cause 0.20, propagation path 0.12."""
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    parts, pts = {}, 0.0
    got = _d(_d(ans).get("shards"))
    for s, want in tr["shards"].items():
        g = got.get(s)
        if not isinstance(g, dict):
            g = _d(ss_build.read_json(os.path.join(ws, f"answers/shard_{s}.json")))
        kind = _eq(g.get("kind"), want["kind"]) if "kind" in g else False
        x = 0.025 * kind
        if want["kind"] == "NONE":
            x = 0.085 * kind
        else:
            x += 0.035 * _eq(g.get("op"), want["op"]) + 0.025 * _eq(g.get("key"), want["key"])
        parts[f"shard{s}"] = round(x / 0.085, 4)
        pts += x
    rc, g = tr["root_cause"], _d(_d(ans).get("root_cause"))
    x = (0.06 * _eq(g.get("shard"), rc["shard"]) + 0.08 * _eq(g.get("op"), rc["op"])
         + 0.06 * _eq(g.get("cell"), rc["cell"]))
    gp = _d(ans).get("path")
    gp = gp if isinstance(gp, list) else []
    hits = sum(_eq(a, b) for a, b in zip(gp, rc["path"]))
    y = 0.12 * hits / max(len(rc["path"]), len(gp))
    parts["root_cause"], parts["path"] = round(x / 0.20, 4), round(y / 0.12, 4)
    return {"score": round(min(1.0, pts + x + y), 4), "parts": parts,
            "final_valid": isinstance(ans, dict)}


LOGFORMAT = """# Shard stress log format

`bin/shard_stress --shard K` (K = 1..8) runs a stress test of one shard of a
persistent AVL map, prints a trace of roughly 10 MB over about 90 s and exits.
A rerun of the same shard prints the same trace. Every data line starts with a
6-digit op index. `lt` is a Lamport time that is comparable across shards.

| Line | Meaning |
|---|---|
| `OP lt thr PUT/DEL/GET ...` | client operation. For GET, `ver` is what was read and `want` is the newest committed version |
| `OP AUDIT node lo hi lh rh bf lsz rsz sz` | audit of one node after the op; `lo`/`hi` are the bounds implied by its ancestors |
| `OP ROT ...` | a rotation in progress; transient imbalance here is legal |
| `OP SNAP id pin dg` / `OP RECHK id pin dg` | digest of an immutable snapshot when taken / when re-verified |
| `OP lt CELL/XOUT/XIN cell val ck ...` | a cell written locally / migrated out / migrated in |
| `ERROR`, `WARN` | operational noise, recovered by the engine, not a violation |

## Tree invariants (exactly these five kinds)

* `ORDER`: AUDIT must have lo < node < hi (strict)
* `BALANCE`: AUDIT must have -1 <= bf <= 1
* `SIZE`: AUDIT must have sz = lsz + rsz + 1
* `STALE`: a GET with repl=0 must have ver = want (repl=1 reads may lag)
* `PERSIST`: RECHK dg must equal the dg of the SNAP with the same id

The offending key is `node` for AUDIT, `key` for GET, `pin` for RECHK. A shard
may violate one kind (then usually many times; the FIRST occurrence matters) or
none at all, in which case its kind is `NONE` and op and key are null.

## Cell checksums

ck is 4 hex digits and must equal (cell*40503 + val*7) mod 65536. Lines with
retry=1 are link-level retransmissions and never count. Exactly one cell was
corrupted: a CELL line with retry=0 carries a wrong ck (the root cause) and the
corrupted cell then migrated from shard to shard (XOUT/XIN with retry=0 and a
wrong ck).
"""

TASK = """The workspace contains a sharded, persistent AVL map with seeded defects. Read docs/LOGFORMAT.md first: it defines the trace format, the five tree invariants and the cell checksum.

Run `bin/shard_stress --shard K` for K = 1..8 and determine:
1. For every shard: the FIRST tree-invariant violation (op index, kind, offending key), or kind NONE.
2. The root cause of the cell corruption (shard, op index, cell) and the ordered list of shards the corrupted cell passed through, starting with the root-cause shard.

Ownership: w1 = shards 1,2; w2 = shards 3,4; w3 = shards 5,6; w4 = shards 7,8. Run both of your shards at the same time. Each trace is about 10 MB, so never print a whole trace.
Each worker writes answers/shard_K.json for its shards: {"op": int|null, "kind": "ORDER|BALANCE|SIZE|STALE|PERSIST|NONE", "key": int|null}, and shares its corrupted-cell evidence in board/.
w1 also combines everything, waiting for the others, into answers/final.json: {"shards": {"1": {"op":..,"kind":..,"key":..}, ..., "8": {...}}, "root_cause": {"shard": int, "op": int, "cell": int}, "path": [int, ...]}"""
