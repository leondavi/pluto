#!/usr/bin/env python3
"""Scripted solvers of experiment 8 (no LLM, plain processes).

    python3 verify_e8.py DIR [--seed N] [--policy wise|lazy]

One supervisor process per agent. ``wise`` first runs one configuration on all twelve
nodes to find the faulty ones, scans coarsely, then finely, then at several sizes, and
reacts while runs are active. ``lazy`` scans coarsely twice, answers every exchange with
yes, resets nothing and takes every transition for a continuous one.
"""

import argparse
import json
import math
import os
import re
import shutil
import statistics as st
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import e8_analysis as an  # noqa: E402
import exp8_spins as e8  # noqa: E402
import pst_build  # noqa: E402
import pst_procs  # noqa: E402

ASK = re.compile(r"EXCHANGE\? row=\d+ low=r(\d) T=([\d.]+) E=(-?[\d.]+) "
                 r"high=r(\d) T=([\d.]+) E=(-?[\d.]+) u=([\d.]+)")
COARSE = [1.42 * (3.0 / 1.42) ** (i / 5) for i in range(6)]


class Tail(an.Run):
    def __init__(self, ws, me, node, launch, sample, L, t, start, react):
        super().__init__(node, launch, sample, L, t)
        self.start, self.react, self.pos, self.reset, self.ws = start, react, 0, set(), ws
        self.path = os.path.join(ws, "logs", f"{node}_{launch}.log")
        self.ctl = os.path.join(ws, f"control/node_{node}_{launch}.cmd")
        with open(self.path, "wb") as f:
            self.proc = subprocess.Popen(
                ["bin/mc", "--node", str(node), "--sample", sample, "--L", str(L), "--T",
                 f"{t:.5f}", "--start", start], cwd=ws, stdout=f, stderr=f)

    def write(self, text):
        with open(self.ctl, "a") as f:
            f.write(text + "\n")

    def poll(self):
        over = self.proc.poll() is not None
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        data = data[:data.rfind(b"\n") + 1]
        self.pos += len(data)
        for line in data.decode().splitlines():
            m = None if self.feed(line) else ASK.fullmatch(line)
            if m:
                d = (1 / float(m[2]) - 1 / float(m[5])) * (float(m[3]) - float(m[6]))
                yes = math.log(float(m[7])) < d or self.react == "yes"
                self.write(f"swap {'yes' if yes else 'no'}")
        if self.react == "full" and data and self.launch <= 2:
            for r in set(range(an.REPS)) - self.reset:
                if self.stuck(r):
                    self.reset.add(r)
                    self.write(f"reset {r}")
        return over


def wave(ws, me, jobs, count, react):
    """Run [(node, sample, L, T, start)] at the same time."""
    runs = []
    for node, sample, L, t, start in jobs:
        count[node] = count.get(node, 0) + 1
        t = min(3.55, max(1.0, t))
        runs.append(Tail(ws, me, node, count[node], sample, L, round(t, 5), start, react))
    left = list(runs)
    while left:
        time.sleep(1.0)
        left = [r for r in left if not r.poll()]
    for r in runs:
        r.poll()
        if r.proc.returncode:
            print(me, "run failed", r.node, r.launch, r.proc.returncode, flush=True)
    return runs


def size(runs, sample, L):
    return an.Ensemble([r for r in runs if r.sample == sample and r.L == L])


def drop_t(runs, sample):
    """Temperature where the order parameter of the coarse scan falls fastest."""
    pts = []
    for r in runs:
        if r.sample == sample:
            for g in (0, 1):
                pts.append((r.temp(g), an.mean([x[1] for d in r.group(g, .4) for x in d])))
    pts.sort()
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        if a > .5 >= b:
            return t0 + (t1 - t0) * (a - .5) / (a - b)
    return an.mean([t for t, _ in pts])


def rough(runs, sample, t0):
    """(order, T_c) from sizes 8 and 16."""
    a, b = size(runs, sample, 8), size(runs, sample, 16)
    lo, hi = t0 * .9, t0 * 1.12
    grow = math.log(b.peak("chi", lo, hi)[1] / a.peak("chi", lo, hi)[1]) / math.log(2)
    if grow < 1.2:
        return "none", b.peak("chi", lo, hi)[0]
    if grow > 2.05:
        return "first", (4 * b.peak("c", lo, hi)[0] - a.peak("c", lo, hi)[0]) / 3
    hits = an.crossing(a, b, lo, hi, 60)
    t1 = b.peak("chi", lo, hi)[0]
    return "second", min(hits, key=lambda t: abs(t - t1 * .96)) if hits else t1 * .96


def finish(runs, sample, t0):
    order, tc = rough(runs, sample, t0)
    if order == "none":
        return {"order": order, "Tc": None}
    if order == "first":
        up, down = [], []
        for r in runs:
            if r.sample == sample and r.L >= 16:
                for g in (0, 1):
                    rows = [x for d in r.group(g, .3) for x in d]
                    if abs(r.temp(g) / tc - 1) < .012:
                        up += [x[0] / r.L ** 2 for x in rows if x[1] < .25]
                        down += [x[0] / r.L ** 2 for x in rows if x[1] > .6]
        heat = an.mean(up) - an.mean(down) if up and down else None
        return {"order": order, "Tc": tc, "Tc_err": .006 * tc, "latent_heat": heat}
    ens = {L: size(runs, sample, L) for L in (8, 16, 24, 32)}
    ens = {L: e for L, e in ens.items() if e.sets}
    hits = []
    for a in ens:
        for b in ens:
            if b >= 2 * a:
                for t in an.crossing(ens[a], ens[b], tc * .97, tc * 1.03, 60):
                    hits += [t] * (1 if b == 16 else 2)
    best = st.median(hits) if hits else tc

    # alpha/nu: 0 (a logarithm) for Ising, 0.4 for three states; small sizes are most precise
    peaks = [ens[L].peak("c", best * .97, best * 1.1)[1] for L in (8, 16)]
    nu = 1.2 if math.log(peaks[1] / peaks[0]) / math.log(2) > .47 else 1.0
    return {"order": order, "Tc": best, "Tc_err": max(.004 * tc, abs(best - tc) / 2),
            "inv_nu": nu}


def wise(ws, me):
    g = int(me[1]) - 1
    nodes = list(range(g * 12 + 1, g * 12 + 13))
    a, b = e8.SAMPLES[2 * g:2 * g + 2]
    count, t0 = {}, time.time()
    first = wave(ws, me, [(n, a, 16, 3.0, "hot") for n in nodes], count, "full")
    bad = an.faults(first)
    ok = [n for n in nodes if bad.get(n) not in ("THERMAL", "BALANCE")]
    ok.sort(key=lambda n: n in bad)
    # faulty nodes take the hottest and coldest points of the coarse scan
    order = [2, 3, 1, 4, 0, 5]
    jobs = [(n, (a, b)[i % 2], 16, COARSE[order[i // 2]], "hot") for i, n in enumerate(
        ok + [n for n in nodes if n not in ok])]
    runs = wave(ws, me, jobs, count, "full")
    good = [r for r in runs if r.node in ok]
    mid = {s: drop_t(runs, s) for s in (a, b)}
    good = [r for r in good if bad.get(r.node) != "FILTER"]
    ok = [n for n in ok if bad.get(n) != "FILTER"]
    steps = [(8, .99), (16, .99), (16, 1.05), (16, .93)]
    jobs = []
    for i, n in enumerate(ok[:8]):
        L, f = steps[i // 2]
        jobs.append((n, (a, b)[i % 2], L, mid[(a, b)[i % 2]] * f, "hot"))
    good += wave(ws, me, jobs, count, "full")
    est = {s: rough(good, s, mid[s]) for s in (a, b)}
    jobs = []
    for i, n in enumerate(ok[:8]):
        s = (a, b)[i % 2]
        o, tc = est[s]
        opt = ([(24, .995, "hot"), (24, .995, "cold"), (16, .99, "hot"), (8, 1.01, "hot")]
               if o == "first" else
               [(32, .99, "hot"), (16, .985, "hot"), (8, .99, "hot"), (24, .99, "hot")])
        L, f, start = opt[i // 2]
        jobs.append((n, s, L, tc * f, start))
    if time.time() - t0 < 415:
        good += wave(ws, me, jobs, count, "full")
    ans = {"samples": {s: finish(good, s, mid[s]) for s in (a, b)},
           "nodes": {str(n): k for n, k in bad.items()}}
    ans["debug"] = {"mid": mid, "est": est}
    return ans


def lazy(ws, me):
    g = int(me[1]) - 1
    nodes, pair, count = range(g * 12 + 1, g * 12 + 13), e8.SAMPLES[2 * g:2 * g + 2], {}
    runs = wave(ws, me, [(n, pair[i % 2], 16, COARSE[i // 2], "hot")
                         for i, n in enumerate(nodes)], count, "yes")
    mid = {s: drop_t(runs, s) for s in pair}
    runs += wave(ws, me, [(n, pair[i % 2], 16, mid[pair[i % 2]] * (.9 + .04 * (i // 2)), "hot")
                          for i, n in enumerate(nodes)], count, "yes")
    out = {}
    for s in pair:
        t = size(runs, s, 16).peak("chi", mid[s] * .9, mid[s] * 1.12)[0]
        out[s] = {"order": "second", "Tc": t, "Tc_err": .02 * t, "inv_nu": 1.0}
    return {"samples": out, "nodes": {}}


def run8(ws, policy):
    os.makedirs(os.path.join(ws, "logs"), exist_ok=True)
    tracker, t0 = pst_procs.Tracker(), time.time()
    tracker.start()
    try:
        kids = [subprocess.Popen([sys.executable, os.path.abspath(__file__), ws, "--policy",
                                  policy, "--as", f"w{i}"]) for i in range(1, 5)]
        for k in kids:
            k.wait()
    finally:
        tracker.stop()
        pst_procs.cleanup(tracker, ws, t0)
    final = {"samples": {}, "nodes": {}}
    for i in range(1, 5):
        a = pst_build.read_json(os.path.join(ws, f"answers/agent_w{i}.json")) or {}
        for k in final:
            final[k].update(a.get(k, {}))
    pst_build.write(ws, e8.ANSWER, json.dumps(final))
    return tracker, t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--policy", default="wise", choices=("wise", "lazy"))
    ap.add_argument("--as", dest="me", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.me:
        ans = {"wise": wise, "lazy": lazy}[a.policy](a.dir, a.me)
        return pst_build.write(a.dir, f"answers/agent_{a.me}.json", json.dumps(ans, indent=1))
    ws = os.path.join(os.path.realpath(a.dir), f"v8{a.policy}{a.seed}")
    shutil.rmtree(ws, ignore_errors=True)
    e8.build(ws, a.seed)
    tracker, t0 = run8(ws, a.policy)
    wall = time.time() - t0
    tr = e8.truth(a.seed)
    g = e8.grade(ws, tr)
    recs = pst_build.launches(ws)
    logs = [os.path.join(ws, "logs", f) for f in os.listdir(os.path.join(ws, "logs"))]
    cpu = tracker.cpu or [0.0]
    span = max(r["t_last"] for r in recs) - min(r["t0"] for r in recs)
    print(f"exp8 {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s "
          f"launches={len(recs)} peaks={pst_build.peak_streams(recs, [], {}, e8.owner)} "
          f"total_peak={pst_build.peak([(r['t0'], r['t_last']) for r in recs])} "
          f"MB={sum(map(os.path.getsize, logs)) / 1e6:.0f} "
          f"cpu={sum(r['cpu'] for r in recs):.0f}s ({sum(r['cpu'] for r in recs) / span:.2f} cores)"
          f" share={sum(cpu) / len(cpu):.3f} max={max(cpu):.3f} "
          f"max_lag={max(r['lag'] for r in recs):.2f}s", flush=True)
    print("  " + json.dumps(g))
    for s, t in tr["samples"].items():
        print("  ", s, t, pst_build.read_json(os.path.join(ws, e8.ANSWER))["samples"].get(s))


if __name__ == "__main__":
    main()
