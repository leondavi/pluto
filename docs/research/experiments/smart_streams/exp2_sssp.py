"""Experiment 2 (v2): plan, workspace builder, ground truth and grader."""

import math
import os
import random

import ss_build
import wl_sssp

NAME = "e2_tuning_tournament"
SIZES = [400, 800, 1500, 2500]
N_VARIANTS, PER_WORKER, TUNES, CAP, WINDOW_S = 12, 3, 8, 16, 600
TIMEOUT = 1200
BEST_S, MAX_S, LPS = 25.0, 150.0, 120
TOP, MARGIN, N_HANG = 3, 1.05, 10
EFF_SLACK = 1.25
ANSWER = "answers/final.json"


def owner(rec):
    """Agent that owns a ledger record."""
    return f"w{(int(rec['variant'][1:]) - 1) // PER_WORKER + 1}"


def _curve(rng, base, kstar):
    """Strictly unimodal cost over the tunings; neighbours differ by > 6%."""
    out = [0] * TUNES
    out[kstar] = int(base)
    for step in (-1, 1):
        k, c = kstar + step, base
        while 0 <= k < TUNES:
            c *= rng.uniform(1.08, 1.45)
            out[k] = int(c)
            k += step
    return out


def plan(seed, best_s=BEST_S, lps=LPS, window_s=WINDOW_S, sizes=None):
    sizes = sizes or SIZES
    rng = random.Random(f"plan2v2:{seed}")
    gseed = rng.randrange(2**40)
    names = [f"V{i + 1}" for i in range(N_VARIANTS)]
    algos = (wl_sssp.ALGOS + rng.sample(wl_sssp.ALGOS, N_VARIANTS - len(wl_sssp.ALGOS)))
    rng.shuffle(algos)
    graphs = [wl_sssp.graph(gseed, n) for n in sizes]
    base = {a: [wl_sssp.solve(a, g)[2] for g in graphs] for a in set(algos)}
    last = len(sizes) - 1
    for _ in range(200000):
        best = {}
        for v, a in zip(names, algos):
            # simulated machine: fixed share plus a power of the real work count
            top = 1e6 * math.exp(rng.uniform(0, math.log(3.0)))
            share, expo = rng.choice([0.0, 0.05, 0.15, 0.3]), rng.uniform(0.8, 1.3)
            unit = top * (1 - share) / base[a][last] ** expo
            best[v] = [top * share + unit * w ** expo for w in base[a]]
        dist, ctr = rng.sample(names, 2)
        if _informative(names, best, dist, ctr, len(sizes)):
            break
    else:
        raise RuntimeError("no informative instance")
    variants, cells = {}, []
    for v, a in zip(names, algos):
        grid = []
        for si in range(len(sizes)):
            good = min(best[x][si] for x in names if x not in (dist, ctr))
            kstar = rng.randrange(TUNES)
            costs = _curve(rng, best[v][si], kstar)
            grid.append([{"cost": c, "hang": 0.0, "dur":
                          min(MAX_S / BEST_S * best_s, max(0.5 * best_s, best_s * c / good))}
                         for c in costs])
            # hangs sit away from the optimum so that they never hide an answer
            cells += [(v, si, k) for k in range(TUNES) if abs(k - kstar) >= 2
                      and v not in (dist, ctr)]
        variants[v] = {"algo": a, "grid": grid}
    for v, si, k in rng.sample(cells, N_HANG):
        variants[v]["grid"][si][k]["hang"] = rng.uniform(0.15, 0.45)
    first, sets = rng.choice([1, 2]), {}
    for si in range(first, len(sizes)):
        verts = rng.sample(range(sizes[si] // 8, sizes[si]), rng.randint(2, 4))
        sets[str(si)] = [(x, rng.randint(1, 5)) for x in sorted(verts)]
    return {"seed": gseed, "sizes": sizes, "lps": lps, "tunes": TUNES, "cap": CAP,
            "per": PER_WORKER, "window_s": window_s, "variants": variants, "defects": {
                "DISTANCE": {"variant": dist, "first": first, "sets": sets},
                "COUNTER": {"variant": ctr, "first": rng.choice([0, 1, 2]),
                            "at": {"frac": rng.uniform(0.35, 0.6),
                                   "drop": rng.uniform(0.35, 0.5)}}}}


def _informative(names, best, dist, ctr, k):
    """Top-4 of the correct variants are separated by MARGIN at every size; at
    least 3 different winners; both defective variants look like a top-3
    contender at two or more sizes."""
    ok = [v for v in names if v not in (dist, ctr)]
    wins, tempt = set(), {dist: 0, ctr: 0}
    for i in range(k):
        s = sorted(ok, key=lambda v: best[v][i])
        c = [best[v][i] for v in s]
        if any(c[j + 1] < MARGIN * c[j] for j in range(TOP)):
            return False
        wins.add(s[0])
        for v in tempt:
            tempt[v] += best[v][i] < c[TOP - 1]
    return len(wins) >= 3 and min(tempt.values()) >= 2


def next_probes(state):
    """Reference search of one (variant, size) cell.

    ``state`` maps k to ("done", cost), ("gt", bound) or ("hang",). Returns the
    tunings to run next (raced against each other and the cell's best cost), or
    [] when the minimum is established."""
    done = {k: s[1] for k, s in state.items() if s[0] == "done"}
    if not done:
        order = [k for k in (2, 5, 3, 4, 1, 6, 0, 7) if k not in state]
        return order[:2]
    best = min(done, key=done.get)
    out = []
    for step in (-1, 1):
        k = best + step
        while 0 <= k < TUNES and state.get(k, ("",))[0] == "hang":
            k += step                       # a hang says nothing about the slope
        if 0 <= k < TUNES and k not in state:
            out.append(k)
    return out


def _search_cost(row):
    """Stream-seconds of the reference search on one cell."""
    state, secs = {}, 0.0
    while True:
        ks = next_probes(state)
        if not ks:
            return secs
        known = [s[1] for s in state.values() if s[0] == "done"]
        bound = min(known + [row[k]["cost"] for k in ks if not row[k]["hang"]] or [0])
        for k in ks:
            c = row[k]
            if c["hang"]:
                state[k] = ("hang",)
                secs += c["dur"] * c["hang"] + 12
            else:
                secs += c["dur"] * min(1.0, bound / c["cost"])
                state[k] = ("done", c["cost"]) if c["cost"] <= bound else ("gt", bound)


def truth(seed, **kw):
    p = plan(seed, **kw)
    D, vs, n = p["defects"], p["variants"], len(p["sizes"])
    ok = [v for v in vs if v not in (D["DISTANCE"]["variant"], D["COUNTER"]["variant"])]
    podium = []
    for i in range(n):
        b = sorted((min((c["cost"], k) for k, c in enumerate(vs[v]["grid"][i])) + (v,))
                   for v in ok)
        podium.append([[v, k] for _, k, v in b[:TOP]])
    si = D["DISTANCE"]["first"]
    vert = min(x for x, _ in D["DISTANCE"]["sets"][str(si)])
    dist = wl_sssp.solve("bin_heap", wl_sssp.graph(p["seed"], p["sizes"][si]))[0]
    rows = [vs[v]["grid"][i] for v in vs for i in range(n)]
    return {"podium": podium,
            "defects": {"DISTANCE": {"variant": D["DISTANCE"]["variant"], "size": si,
                                     "vertex": vert, "distance": dist[vert]},
                        "COUNTER": {"variant": D["COUNTER"]["variant"],
                                    "size": D["COUNTER"]["first"]}},
            "full_s": sum(c["dur"] for r in rows for c in r),
            "ref_s": sum(_search_cost(r) for r in rows)}


def build(ws, seed, **kw):
    ss_build.install(ws, "spbench", "wl_sssp", {"bench.img": plan(seed, **kw)})
    ss_build.write(ws, "docs/BENCH.md", BENCH.replace("@SIZES@", str(SIZES))
                   .replace("@WIN@", str(WINDOW_S)).replace("@CAP@", str(CAP)))


def used_seconds(ws):
    return sum(max(0.0, r["t_last"] - r["t0"]) for r in ss_build.launches(ws))


def grade(ws, tr, meta=None):
    """Podium 0.50 (per entry: variant 0.5, tuning 0.5), defects 0.30,
    efficiency 0.20 scaled by podium accuracy."""
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    a = ans if isinstance(ans, dict) else {}

    def up(x):
        return str(x).strip().upper()

    def lst(x):
        return x if isinstance(x, list) else []

    got, pts, n = lst(a.get("podium")), 0.0, 0
    for i, want in enumerate(tr["podium"]):
        g = lst(got[i]) if i < len(got) else []
        for j, (v, k) in enumerate(want):
            e = lst(g[j]) if j < len(g) else []
            n += 1
            if len(e) == 2 and up(e[0]) == v:
                pts += .5 + .5 * (up(e[1]) == str(k))
    pod = pts / n
    d = a.get("defects") if isinstance(a.get("defects"), dict) else {}
    g1, g2 = (d.get(k) if isinstance(d.get(k), dict) else {} for k in ("DISTANCE", "COUNTER"))
    w1, w2 = tr["defects"]["DISTANCE"], tr["defects"]["COUNTER"]
    bug = (sum(w * (up(g1.get(k)) == up(w1[k])) for k, w in
               (("variant", .25), ("size", .1), ("vertex", .15), ("distance", .1)))
           + .25 * (up(g2.get("variant")) == w2["variant"])
           + .15 * (up(g2.get("size")) == up(w2["size"])))
    used, allow = used_seconds(ws), EFF_SLACK * tr["ref_s"]
    eff = max(0.0, min(1.0, (tr["full_s"] - used) / (tr["full_s"] - allow))) * pod
    return {"score": round(.5 * pod + .3 * bug + .2 * eff, 4),
            "parts": {"podium": round(pod, 4), "defects": round(bug, 4),
                      "efficiency": round(eff, 4)},
            "compute_s": round(used, 1), "full_s": round(tr["full_s"], 1),
            "ref_s": round(tr["ref_s"], 1), "final_valid": isinstance(ans, dict)}


BENCH = """# Shortest-path tuning tournament

`bin/spbench --variant V --size I --tune K` with V = V1..V12, I = 0..3
(n = @SIZES@) and K = 0..7 benchmarks one variant with one tuning on one graph
and prints a trace. There are 384 combinations; a run takes 12 to 150 s. All
runs at one size index solve the same graph. Repeated runs give the same trace.
Costs are simulated and deterministic: concurrent runs do not disturb them, but
they are comparable only within one size index.

| Line | Meaning |
|---|---|
| `TICK cum queue settled` | cumulative cost so far; never decreases in a correct run |
| `SETTLE v d rank` | vertex v has final distance d; some variants settle only near the end |
| `RELAX ...` | trace noise |
| `BLOCK b first dg` | at the end: digest of the distances of vertices first..first+63 |
| `RESULT ... cost` | at the end: final cost of the run |

Facts you may rely on:
* For a fixed variant and size, the cost as a function of K has one minimum and
  increases strictly on both sides of it. The best K differs by variant and size.
* Some combinations hang: cum stays constant for 10 s or more and the run never
  ends. A hanging combination is infeasible and is never an optimum.
* Exactly two variants are defective, independent of K, from some size index on
  and at all larger ones:
  - `DISTANCE`: a few vertices get a wrong distance (all other variants agree);
  - `COUNTER`: cum drops once during the run, so its reported costs are wrong.
* The podium of a size lists the three best non-defective variants, each at its
  best K, cheapest first. Defective variants are excluded at every size.
* A non-defective run whose cum exceeds the final cost of three other
  non-defective variants at that size cannot matter for the podium.

Limits: at most @CAP@ running benchmarks per worker (more are refused, exit 76).
The machine is reserved for @WIN@ s from the team's first launch; later launches
are refused (exit 77). Every second a benchmark process is alive counts as
compute time, and total compute time is part of the score.
"""

TASK = """Run a tuning tournament over twelve opaque shortest-path variants. Read docs/BENCH.md first. There are 384 combinations, far more than may run at once or than are worth running: let finished or stopped runs decide what to launch next.

Determine
1. podium: per size index 0..3, the three cheapest non-defective variants, each with its best tuning, cheapest first;
2. the DISTANCE defect: variant, smallest affected size index, smallest wrong vertex id at that size, its correct distance;
3. the COUNTER defect: variant and smallest affected size index.

Part of the score is the team's total benchmark time: stop dominated and hanging runs early, skip combinations the docs let you rule out, and share final costs in board/ as soon as you have them. But the reservation window is short, so keep 8 to 16 benchmarks running while you have candidates.
Ownership: w1 = V1-V3; w2 = V4-V6; w3 = V7-V9; w4 = V10-V12. Only run your own variants. Publish your results (best tuning and cost per size, lower bounds for stopped runs, defect evidence) in board/.
w1 also combines everything, waiting for the others, into answers/final.json: {"podium": [[["Vx", tune], ["Vy", tune], ["Vz", tune]], ... 4 sizes], "defects": {"DISTANCE": {"variant": "Vx", "size": int, "vertex": int, "distance": int}, "COUNTER": {"variant": "Vx", "size": int}}}"""
