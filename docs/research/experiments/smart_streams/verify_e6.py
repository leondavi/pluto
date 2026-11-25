#!/usr/bin/env python3
"""Scripted solvers of experiment 6 on plain processes; see verify_solvers.py.

    python3 verify_e6.py DIR [--policy wise|lazy|errors] [--seed N]

wise follows the documented rules with its own reader and must score 1.0; lazy promotes
at every gate and never watches for outages; errors looks at raw status codes only."""

import argparse
import math
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exp6_train as e6  # noqa: E402
import ss_build  # noqa: E402
import ss_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402


class Lane:
    """Incremental reader of one lane: call feed(line); it returns a decision or None."""

    def __init__(self, policy="wise"):
        self.policy, self.limits, self.build, self.report, self.parts = policy, {}, None, {}, {}

    def feed(self, line):
        t = line.split()
        if len(t) < 3 or not t[0].isdigit():
            return None
        f = dict(x.split("=", 1) for x in t[2:] if "=" in x)
        if t[1:3] == ["BAKE", "begin"]:
            self.build, self.warm, self.row = f["build"], False, 0
            self.ms, self.err, self.rss, self.ck = {"canary": [], "stable": []}, {}, [], 0
            self.raw = {"canary": [0, 0], "stable": [0, 0]}
        elif t[1] == "MANIFEST":
            self.parts[self.build] = set(t[3:])
        elif t[1:3] == ["BAKE", "warm"]:
            self.warm = True
        elif t[1] == "POLICY":
            self.limits = {k: float(v) for k, v in f.items()}
        elif t[1] == "MEM" and self.warm and f["group"] == "canary":
            self.rss.append(float(f["rss_mb"]))
        elif t[1] == "REQ":
            g, bad = f["group"], 500 <= int(f["status"]) < 600
            self.raw[g][0] += 1
            self.raw[g][1] += bad
            if not self.warm or f["kind"] != "user" or f["retry"] != "0":
                return None
            self.ms[g].append(int(f["ms"]))
            self.err[g] = self.err.get(g, 0) + bad
            if g == "canary":
                self.row = self.row + 1 if bad else 0
                self.ck += (int(f["id"]) * 31 + int(f["ms"]) * 17) % 4093 != int(f["ck"])
                if self.row == e6.OUTAGE_RUN and self.policy == "wise":
                    self.report[self.build] = {"reason": "OUTAGE", "p95": None, "errors": None}
                    return self.build, "rollback"
        elif t[1:3] == ["GATE", "open"]:
            r = self.report[self.build] = self.verdict()
            return self.build, "promote" if r["reason"] == "OK" else "rollback"
        return None

    def verdict(self):
        q = {g: sorted(x)[math.ceil(.95 * len(x)) - 1] for g, x in self.ms.items()}
        pct = {g: 100 * self.err.get(g, 0) / len(x) for g, x in self.ms.items()}
        lim, why = self.limits, "OK"
        if self.policy == "errors":
            a, b = (100 * e / n for n, e in (self.raw["canary"], self.raw["stable"]))
            why = "ERRORS" if a > b + lim["err_margin"] else "OK"
        elif self.policy == "wise":
            if pct["canary"] > pct["stable"] + lim["err_margin"]:
                why = "ERRORS"
            elif q["canary"] > lim["lat_ratio"] * q["stable"]:
                why = "LATENCY"
            elif self.rss[-1] - self.rss[0] > lim["mem_mb"]:
                why = "MEMORY"
            elif self.ck:
                why = "CHECKSUM"
        good = self.policy == "wise"
        return {"reason": why, "p95": q["canary"] if good else None,
                "errors": self.err.get("canary", 0) if good else self.raw["canary"][1]}


def culprits(lanes):
    parts = {b: x for ln in lanes for b, x in ln.parts.items()}
    why = {b: r["reason"] for ln in lanes for b, r in ln.report.items()}
    out = {}
    for k in e6.BAD:
        inn = [x for b, x in parts.items() if why.get(b) == k]
        rest = set().union(*[x for b, x in parts.items() if why.get(b) != k])
        left = set.intersection(*inn) - rest if inn else set()
        out[k] = min(left) if left else None
    return out


def solve(ws, policy="wise", n=e6.LANES):
    be = vs.Plain(ws)
    hs = {k: be.start(e6.owner({"lane": k}), "train", "--lane", str(k)) for k in range(1, n + 1)}
    lanes, tails = {k: Lane(policy) for k in hs}, {k: vs.Tail(h) for k, h in hs.items()}
    live = set(hs)
    while live:
        for k in sorted(live):
            over = be.poll(hs[k]) is not None
            for line in tails[k].lines():
                d = lanes[k].feed(line)
                if d:
                    with open(os.path.join(ws, f"control/lane_{k}.txt"), "a") as f:
                        f.write(f"{d[0]} {d[1]}\n")
            if over:
                live.discard(k)
        time.sleep(1.0)
    vs._answer(ws, {"builds": {b: r for ln in lanes.values() for b, r in ln.report.items()},
                    "culprits": culprits(list(lanes.values()))})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--policy", default="wise", choices=("wise", "lazy", "errors"))
    ap.add_argument("--seed", type=int, default=12345)
    a = ap.parse_args()
    ws = os.path.join(os.path.realpath(a.dir), f"v6{a.policy}{a.seed}")
    shutil.rmtree(ws, ignore_errors=True)
    e6.build(ws, a.seed)
    t, tracker = time.time(), ss_procs.Tracker()
    try:
        solve(ws, a.policy)
    finally:
        wall = time.time() - t
        ss_procs.cleanup(tracker, ws, t)
    g, recs = e6.grade(ws, e6.truth(a.seed)), ss_build.launches(ws)
    size = sum(os.path.getsize(os.path.join(ws, f)) for f in os.listdir(ws) if f.endswith(".log"))
    span = sum(r["t_last"] - r["t0"] for r in recs)
    cpu = sum(r["cpu"] for r in recs)
    print(f"exp6 {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s MB={size / 1e6:.1f} "
          f"peaks={ss_build.peak_streams(recs, [], {}, e6.owner)} cpu={cpu:.0f}s "
          f"core_share_of_12={12 * cpu / span:.3f} max_lag={max(r['lag'] for r in recs):.2f}s")
    print("  ", g)


if __name__ == "__main__":
    main()
