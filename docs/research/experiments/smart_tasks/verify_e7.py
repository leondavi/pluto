#!/usr/bin/env python3
"""Scripted solver of experiment 7 (plain processes, no LLM).

    python3 verify_e7.py DIR [--policy wise|lazy] [--seed N] [--length L --duration S]

``wise`` uses only docs/ZETA.md (its formula, C0 only) and the program output, and
must score 1.0. ``lazy`` starts everything, reacts to nothing and adds up what was
printed: it must score clearly lower.
"""

import argparse
import json
import math
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exp7_zeta as e7  # noqa: E402
import pst_build  # noqa: E402
import pst_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402

TAU = 2 * math.pi


def my_z(t, drop=0, rem=True, shift=0.0):
    """The formula of the docs; ``drop``, ``rem`` and ``shift`` model the faults."""
    a = math.sqrt(t / TAU)
    n = int(a)
    p = a - n
    if abs(math.cos(TAU * p)) < 1e-7:
        p += 1e-6
    th = t / 2 * math.log(t / TAU) - t / 2 - math.pi / 8 + 1 / (48 * t) + shift
    s = 2 * sum([math.cos(th - t * math.log(k)) / math.sqrt(k) for k in range(1, n - drop + 1)])
    if not rem:
        return s
    c0 = math.cos(TAU * (p * p - p - 1 / 16)) / math.cos(TAU * p)
    return s + (c0 if n % 2 else -c0) / math.sqrt(a)


def classify(samples):
    """Kind of evaluator fault from (t, Z) pairs after its onset."""
    if max(abs(my_z(t, rem=False) - z) for t, z in samples) < 1e-4:
        return "REMAINDER"
    votes = 0
    for t, z in samples:
        n = int(math.sqrt(t / TAU))
        votes += any(abs(my_z(t, drop=d) - z) < 6e-4 for d in range(1, n // 8 + 2))
    if votes > .9 * len(samples):
        return "TRUNC"
    def misfit(sh):
        return max(abs(my_z(t, shift=sh) - z) for t, z in samples)
    sh, step = min((misfit(i / 200), i / 200) for i in range(-100, 101))[1], 1 / 200
    while step > 1e-6:                          # a pure phase shift fits to rounding error
        sh = min((sh - step, sh, sh + step), key=misfit)
        step /= 2
    if misfit(sh) < 2e-3:
        return "THETA"
    return "NOISE"


class Launch:
    """Incremental parser of one launch."""

    def __init__(self, h, start):
        self.h, self.tail, self.start = h, vs.Tail(h), start
        self.g, self.s, self.r, self.q, self.y = {}, [], [], [], []
        self.n, self.it, self.width, self.same, self.checked = None, 0, None, 0, 0
        self.window = self.grid = None
        self.asked, self.open, self.count = set(), None, 0
        self.bad = self.kind = None

    def read(self, check):
        for x in self.tail.lines():
            f = x.split()
            if not f:
                continue
            if f[0] in "GS" and len(f[0]) == 1:
                t, z = (float(v.split("=")[1]) for v in f[-2:])
                if f[0] == "G":
                    self.n = int(f[1][2:])
                    self.g[self.n] = (t, z)
                    if check and self.bad is None and abs(my_z(t) - z) > .002:
                        back = [a for a, b, n in self.s[-8:] if n == self.n - 1
                                and abs(my_z(a) - b) > .002]
                        self.bad = self.n - bool(back)
                self.s.append((t, z, self.n))
            elif f[0] == "R":
                self.r.append(float(f[1][2:]))
            elif f[0] == "Q":
                self.q.append(float(f[1][2:]))
            elif f[0] == "B":
                lo, hi = float(f[3][3:]), float(f[4][3:])
                w = hi - lo
                self.same = self.same + 1 if w == self.width else 0
                self.width = w
                if check and self.same > 15 and self.bad is None:
                    self.bad, self.kind = int(f[1][2:]), "STUCK"
            elif x.startswith("# window"):
                a, b = f[2][2:].split("..")
                self.window, self.grid = (int(a), int(b)), int(f[5][5:])

    def blocks(self):
        """Complete Gram blocks not seen before whose zeros can all have been printed."""
        ns = sorted(self.g)
        out, lo = [], self.open
        for n in ns[self.checked:]:
            self.checked += 1
            if (self.g[n][1] > 0) == (n % 2 == 0):
                if lo is not None:
                    out.append((lo, n))
                lo = n
        self.open = lo
        return out

    def short(self, a, b):
        ta, tb = self.g[a][0], self.g[b][0]
        return sum(ta <= r < tb for r in self.r[-4000:]) < b - a


def refine(ws, j, la, a, b, fac):
    with open(os.path.join(ws, f"control/job_{j}.cmd"), "a") as f:
        f.write(f"refine {a} {b} {fac}\n")


def supervise(ws, be, wise):
    runs = {j: [] for j in range(1, e7.JOBS + 1)}

    def start(j, *args, at=None):
        h = be.start(e7.owner({"job": j}), "zeta", "--job", str(j), *args)
        runs[j].append(Launch(h, at))
    for j in runs:
        start(j)
    left, pending, t0 = set(runs), {j: [] for j in runs}, time.time()
    while left:
        if not wise and time.time() - t0 > 480:          # gives up like a session that ends
            for j in left:
                be.stop(runs[j][-1].h)
            break
        time.sleep(0.5)
        for j in sorted(left):
            la = runs[j][-1]
            rc = be.poll(la.h)
            la.read(wise and len(runs[j]) == 1)
            if wise and la.bad is not None and len(runs[j]) == 1:
                be.stop(la.h)
                good = max(n for n in la.g if n <= la.bad
                           and (la.g[n][1] > 0) == (n % 2 == 0) and
                           (n < la.bad or la.kind == "STUCK"))
                # blocks not yet judged are repeated by the repair
                good = min([good] + [a for a, b in pending[j] + la.blocks() if b <= good])
                start(j, "--safe", "--from", str(good), at=good)
                pending[j] = []
                continue
            if wise:
                # the zero of the last cell is printed after the closing G line, so a block
                # is judged only once a later sample (or the end) has been read
                pending[j] += la.blocks()
                after = la.s[-1][0] if la.s else 0.0
                ready = [x for x in pending[j] if rc is not None or after > la.g[x[1]][0]]
                for a, b in ready:
                    if la.short(a, b):
                        refine(ws, j, la, a, b, 16)
                pending[j] = [x for x in pending[j] if x not in ready]
            if rc is not None:
                assert rc == 0 or not wise, (j, rc)
                if not pending[j]:
                    left.discard(j)
    return runs


def own_zeros(a, b, count):
    """Zeros between two t values by a fine grid of the docs' formula."""
    for m in (100, 800, 6400):
        ts = [a + (b - a) * i / m for i in range(m + 1)]
        zs = [my_z(t) for t in ts]
        out = []
        for i in range(m):
            if zs[i] * zs[i + 1] < 0:
                lo, hi, s = ts[i], ts[i + 1], zs[i] > 0
                for _ in range(40):
                    c = (lo + hi) / 2
                    lo, hi = (c, hi) if (my_z(c) > 0) == s else (lo, c)
                out.append((lo + hi) / 2)
        if len(out) == count:
            return out
    return out


def merge(launches, wise):
    """One job's Gram points, scan zeros, refined zeros."""
    first, last = launches[0], launches[-1]
    g, r = dict(first.g), first.r
    if len(launches) > 1:
        cut = last.g[last.start][0]
        g = {n: v for n, v in g.items() if n < last.start}
        g.update(last.g)
        r = [x for x in r if x < cut] + [x for x in last.r if x >= cut]
        return g, r, [x for x in first.q if x < cut] + last.q
    return g, r, first.q


def spacing(a, b):
    return (b - a) * math.log(a / TAU) / TAU


def cdf(s):
    return math.erf(2 * s / math.pi ** .5) - 4 * s / math.pi * math.exp(-4 * s * s / math.pi)


def answer(runs, wise):
    jobs, bands, alld, border = {}, {}, [], []
    for b in range(1, e7.BANDS + 1):
        mine = [j for j in runs if (j - 1) % e7.BANDS == b - 1]
        g, scan, extra = {}, [], []
        for j in mine:
            x, r, q = merge(runs[j], wise)
            g.update(x)
            scan += r
            extra += q
        zeros, ends = sorted(scan), [n for n in sorted(g) if (g[n][1] > 0) == (n % 2 == 0)]
        edges = {n for j in mine for n in runs[j][0].window}
        if wise:
            lost = []
            for lo, hi in zip(ends, ends[1:]):
                ta, tb = g[lo][0], g[hi][0]
                have = [z for z in zeros if ta <= z < tb]
                if len(have) == hi - lo:
                    continue
                new = sorted(z for z in extra if ta <= z < tb)
                if len(new) != hi - lo:
                    if not any(lo < n < hi for n in edges):
                        print(f"  band {b} block {lo}..{hi}: {len(have)} seen, {len(new)} refined")
                    new = own_zeros(ta, tb, hi - lo)
                lost.append((ta, tb, new))
            for ta, tb, new in lost:
                zeros = [z for z in zeros if not ta <= z < tb] + new
            for lo, hi in ((min(g), ends[0]), (ends[-1], max(g))):     # partial blocks
                if hi > lo:
                    ta, tb = g[lo][0], g[hi][0]
                    zeros = [z for z in zeros if not ta <= z < tb] + own_zeros(ta, tb, -1)
        zeros.sort()
        for j in mine:
            n0, n1 = runs[j][0].window
            ta, tb = g[n0][0], g.get(n1, (math.inf,))[0]
            n_z = sum(ta <= z < tb for z in zeros)
            k, seen = runs[j][0].grid, 0
            if wise:
                i, ns = 0, [z for z in zeros if ta <= z < tb]
                for n in range(n0, n1):
                    a, h = g[n][0], (g[n + 1][0] - g[n][0]) / k
                    cells = {}
                    while i < len(ns) and ns[i] < g[n + 1][0]:
                        c = int((ns[i] - a) / h)
                        cells[c] = cells.get(c, 0) + 1
                        i += 1
                    seen += sum(c % 2 for c in cells.values())
            la = runs[j][0]
            kind = la.kind
            if la.bad is not None and not kind:
                kind = classify([(t, z) for t, z, n in la.s if n > la.bad][:25])
            jobs[str(j)] = {"zeros": n_z, "missed": n_z - seen if wise else 0,
                            "gram_failures": sum((g[n][1] > 0) != (n % 2 == 0)
                                                 for n in range(n0, n1) if n in g),
                            "config": kind or "OK", "onset": la.bad}
            if n1 != max(g) and wise:
                below = [z for z in zeros if z < tb][-1]
                above = next(z for z in zeros if z >= tb)
                border.append([below, spacing(below, above)])
        ds = [[a, spacing(a, c)] for a, c in zip(zeros, zeros[1:])]
        alld += ds
        mean = sum(d for _, d in ds) / len(ds)
        longest = max((hi - lo, -lo) for lo, hi in zip(ends, ends[1:]))
        i, plus, minus = 0, 0, 0
        below = sum(z < g[ends[0]][0] for z in zeros)
        for n in sorted(g):
            while i < len(zeros) and zeros[i] < g[n][0]:
                i += 1
            s = i - below - (n - ends[0])
            plus, minus = plus + (s > 0), minus + (s < 0)
        bands[str(b)] = {"closest": sorted(ds, key=lambda x: x[1])[:3],
                         "variance": sum((d - mean) ** 2 for _, d in ds) / len(ds),
                         "longest_block": [-longest[1], longest[0]],
                         "S_plus": plus, "S_minus": minus}
    xs = sorted(d for _, d in alld)
    ks = max(max((i + 1) / len(xs) - cdf(x), cdf(x) - i / len(xs)) for i, x in enumerate(xs))
    return {"jobs": jobs, "bands": bands,
            "campaign": {"ks": ks, "closest": min(alld, key=lambda x: x[1]),
                         "border": min(border, key=lambda x: x[1]) if border else None}}


def solve(ws, policy):
    be = vs.Plain(ws)
    runs = {}
    try:
        runs = supervise(ws, be, policy == "wise")
    finally:
        be.close()
    meta = {"run_end": time.time()}
    with open(os.path.join(ws, e7.ANSWER), "w") as f:
        json.dump(answer(runs, policy == "wise"), f)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--policy", default="wise", choices=("wise", "lazy"))
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--length", type=int, default=e7.LENGTH)
    ap.add_argument("--duration", type=float, default=e7.DURATION)
    a = ap.parse_args()
    ws = os.path.join(os.path.realpath(a.dir), f"v7{a.policy}{a.seed}")
    shutil.rmtree(ws, ignore_errors=True)
    kw = {"length": a.length, "duration": a.duration}
    e7.build(ws, a.seed, **kw)
    tracker, t = pst_procs.Tracker(), time.time()
    tracker.start()
    try:
        meta = solve(ws, a.policy)
    finally:
        tracker.stop()
        pst_procs.cleanup(tracker, ws, t)
    wall = meta["run_end"] - t
    g = e7.grade(ws, e7.truth(a.seed, **kw), meta)
    recs = pst_build.launches(ws)
    cpu = tracker.cpu or [0.0]
    size = sum(os.path.getsize(os.path.join(ws, f)) for f in os.listdir(ws) if f.endswith(".log"))
    print(f"exp7 {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s "
          f"launches={len(recs)} peaks={pst_build.peak_streams(recs, [], meta, e7.owner)} "
          f"total={pst_build.peak([(r['t0'], r['t_last']) for r in recs])} "
          f"program_cpu={sum(r['cpu'] for r in recs):.0f}s machine_cpu_mean="
          f"{sum(cpu) / len(cpu):.2f} max={max(cpu):.2f} "
          f"max_lag={max(r['lag'] for r in recs):.2f}s output={size / 1e6:.1f}MB", flush=True)
    print("  " + json.dumps(g))


if __name__ == "__main__":
    main()
