"""Experiment 2: plan, workspace builder, ground truth and grader."""

import math
import os
import random

import ss_build
import wl_sssp

NAME = "e2_algo_engineering"
SIZES = [250, 500, 1000, 2000, 4000]
TIMEOUT = 1200
BEST_LARGE_S, MIN_S, MAX_S, LPS = 45.0, 4.0, 240.0, 1300
ANSWER = "answers/final.json"
MARGIN = 1.05


def plan(seed, sizes=None, best_large=BEST_LARGE_S, lps=LPS):
    """Random cost model over real operation counts, rejection-sampled until
    the instance is informative (see ``_informative``)."""
    sizes = sizes or SIZES
    rng = random.Random(f"plan2:{seed}")
    gseed = rng.randrange(2**40)
    algos = wl_sssp.ALGOS[:]
    rng.shuffle(algos)
    names = [f"V{i + 1}" for i in range(len(algos))]
    graphs = [wl_sssp.graph(gseed, n) for n in sizes]
    work = {v: [wl_sssp.solve(a, g)[2] for g in graphs] for v, a in zip(names, algos)}
    last = len(sizes) - 1
    for _ in range(200000):
        cost = {}
        for v in names:
            # reference cost at the largest size, then a startup share that
            # flattens the curve and moves the crossovers
            top = 1e6 * math.exp(rng.uniform(0, math.log(5)))
            share = rng.choice([0.0, 0.05, 0.15, 0.3, 0.45]) * rng.uniform(0.7, 1.3)
            # exponent != 1 models memory-hierarchy penalties of the simulated machine
            expo = rng.uniform(0.75, 1.35)
            unit = top * (1 - share) / work[v][last] ** expo
            cost[v] = [int(top * share + unit * w ** expo) for w in work[v]]
        bug = rng.choice(names)
        got = _informative(names, cost, bug, len(sizes))
        if got:
            pairs = rng.sample(got, 2)
            break
    else:
        raise RuntimeError("no informative instance")
    best = min(cost[v][last] for v in names if v != bug)
    rate = best / best_large
    lim = best_large / BEST_LARGE_S
    variants = {v: {"algo": a, "cost": cost[v],
                    "dur": [min(MAX_S * lim, max(MIN_S * lim, c / rate)) for c in cost[v]]}
                for v, a in zip(names, algos)}
    first = rng.choice([1, 2])
    sets = {}
    for si in range(first, len(sizes)):
        verts = rng.sample(range(sizes[si] // 8, sizes[si]), rng.randint(2, 4))
        sets[str(si)] = [(x, rng.randint(1, 5)) for x in sorted(verts)]
    return {"seed": gseed, "sizes": sizes, "lps": lps, "variants": variants, "pairs": pairs,
            "defect": {"variant": bug, "first": first, "sets": sets}}


def _informative(names, cost, bug, k):
    """Crossover pairs if: 3+ distinct winners; the buggy variant looks best at
    two or more larger sizes; every decision margin exceeds MARGIN; some
    variant is at least 4x worse than the winner at the largest size."""
    ok = [v for v in names if v != bug]
    win = []
    for i in range(k):
        s = sorted(ok, key=lambda v: cost[v][i])
        if cost[s[1]][i] < MARGIN * cost[s[0]][i]:
            return None
        win.append(s[0])
    if len(set(win)) < 3:
        return None
    if sum(cost[bug][i] * MARGIN < cost[win[i]][i] for i in range(2, k)) < 2:
        return None
    if max(cost[v][-1] for v in ok) < 4 * cost[win[-1]][-1]:
        return None
    pairs = []
    for x in ok:
        for y in ok:
            r = [cost[y][i] / cost[x][i] for i in range(k)]
            below = [q > 1 for q in r]
            at = below.index(True) if True in below else -1
            if x != y and at in (1, 2, 3) and all(below[at:]) and \
                    all(max(q, 1 / q) > MARGIN for q in r):
                pairs.append([x, y, at])
    return pairs if len(pairs) >= 2 else None


def truth(seed, **kw):
    p = plan(seed, **kw)
    bug = p["defect"]["variant"]
    ok = [v for v in p["variants"] if v != bug]
    k = len(p["sizes"])
    winners = [min(ok, key=lambda v: p["variants"][v]["cost"][i]) for i in range(k)]
    si = p["defect"]["first"]
    vert = min(x for x, _ in p["defect"]["sets"][str(si)])
    dist = wl_sssp.solve("bin_heap", wl_sssp.graph(p["seed"], p["sizes"][si]))[0]
    dur = {v: p["variants"][v]["dur"] for v in p["variants"]}
    full = sum(sum(d) for d in dur.values())
    ref = sum(min(d[i], dur[winners[i]][i]) for d in dur.values() for i in range(k))
    return {"winners": winners, "crossovers": [x[2] for x in p["pairs"]],
            "bug": {"variant": bug, "size": si, "vertex": vert, "distance": dist[vert]},
            "full_s": full, "ref_s": ref}


def build(ws, seed, **kw):
    p = plan(seed, **kw)
    ss_build.install(ws, "spbench", "wl_sssp", {"bench.img": p})
    (a, b, _), (c, d, _) = p["pairs"]
    ss_build.write(ws, "docs/BENCH.md", BENCH.format(a=a, b=b, c=c, d=d, sizes=SIZES))


def used_seconds(ws):
    return sum(max(0.0, r["t_last"] - r["t0"]) for r in ss_build.launches(ws))


def grade(ws, tr, meta=None):
    """Winners 0.35, crossovers 0.20, defect 0.30, compute efficiency 0.15
    (scaled by winner accuracy so that running nothing earns nothing)."""
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    a = ans if isinstance(ans, dict) else {}

    def seq(name):
        return a[name] if isinstance(a.get(name), list) else []

    def up(x):
        return str(x).strip().upper()

    wins = sum(up(g) == w for g, w in zip(seq("winners"), tr["winners"])) / len(tr["winners"])
    cross = sum(up(g) == str(w) for g, w in zip(seq("crossovers"), tr["crossovers"])) / 2
    g = a.get("bug") if isinstance(a.get("bug"), dict) else {}
    bug = sum(w * (up(g.get(k)) == up(tr["bug"][k])) for k, w in
              (("variant", .4), ("size", .2), ("vertex", .2), ("distance", .2)))
    used = used_seconds(ws)
    allow = 1.3 * tr["ref_s"]
    span = tr["full_s"] - allow
    eff = 0.0 if span <= 0 else max(0.0, min(1.0, (tr["full_s"] - used) / span))
    eff *= wins
    return {"score": round(.35 * wins + .20 * cross + .30 * bug + .15 * eff, 4),
            "parts": {"winners": wins, "crossovers": cross, "bug": round(bug, 4),
                      "efficiency": round(eff, 4)},
            "compute_s": round(used, 1), "full_s": round(tr["full_s"], 1),
            "ref_s": round(tr["ref_s"], 1), "final_valid": isinstance(ans, dict)}


BENCH = """# Shortest-path benchmark

`bin/spbench --variant V --size I` with V = V1..V8 and I = 0..4 (n = {sizes})
benchmarks one shortest-path variant on one graph and prints a verbose trace
(up to about 20 MB per run; runs take 4 to 240 s). All variants solve the same graph
at the same size index. Repeated runs give the same trace. The cost unit is
simulated and deterministic, so concurrent runs do not disturb it.

| Line | Meaning |
|---|---|
| `TICK cum queue settled` | cumulative cost so far. It never decreases |
| `SETTLE v d rank` | vertex v has final distance d. Some variants only settle at the end |
| `RELAX ...` | trace noise |
| `BLOCK b first dg` | at the end: digest of the reported distances of vertices first..first+63 |
| `RESULT ... cost` | at the end: the final cost of the run |

Exactly one variant is defective: from some size on it reports wrong distances
for a few vertices. All other variants agree with one another at every size.

Crossover pair 1: X={a}, Y={b}. Crossover pair 2: X={c}, Y={d}.
Both pairs are guaranteed to consist of non-defective variants.
The crossover of a pair is the smallest size index at which X is cheaper than Y.

Compute budget: every second a benchmark process is alive counts. A run whose
cum already exceeds the final cost of a better non-defective variant at the same
size cannot win and should be stopped. Do not run anything twice without need.
"""

TASK = """Perform empirical algorithm engineering on eight opaque shortest-path variants. Read docs/BENCH.md first.

Determine:
1. winners: for each size index 0..4, the cheapest NON-DEFECTIVE variant (the defective variant is excluded at every size, including sizes where it is correct).
2. crossovers: the crossover size index of pair 1 and of pair 2 (docs/BENCH.md).
3. bug: the defective variant, the smallest size index at which it is wrong, the smallest wrong vertex id at that size, and that vertex's correct distance.

Part of the score is the total benchmark time your team uses, so stop dominated runs early and tell your teammates (board/) the final costs you have. Running all 40 combinations to completion earns no efficiency credit.
Ownership: w1 = V1,V2; w2 = V3,V4; w3 = V5,V6; w4 = V7,V8. At most 4 benchmark processes per worker at a time. Publish your costs (final, or a lower bound for a stopped run), correctness evidence and stops in board/.
w1 also combines everything, waiting for the others, into answers/final.json: {"winners": ["Vx", ... 5 entries], "crossovers": [int, int], "bug": {"variant": "Vx", "size": int, "vertex": int, "distance": int}}"""
