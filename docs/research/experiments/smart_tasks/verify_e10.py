#!/usr/bin/env python3
"""Scripted solvers of experiment 10 (no LLM), plain processes and log files.

    python3 verify_e10.py DIR [--policy ref|lazy] [--seed N] [--speed X]

They use only what an agent sees: docs, graphs/ and program output. ``ref``
checks every certificate, shares colourings with exact runs at once, stops exact
runs whose closed share cannot reach 1 in time and reruns a run that misread its
graph with another seed; it must score >= 0.99. ``lazy`` runs everything once,
believes every line and waits for every run to end; it must score clearly lower.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exp10_coloring as e10  # noqa: E402
import pst_build  # noqa: E402
import pst_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402

DECIDE_S = 115                      # look at an exact run's progress this long after start


def read_graph(path):
    n, edges = 0, []
    with open(path) as f:
        for x in f:
            t = x.split()
            if t and t[0] == "p":
                n = int(t[2])
            elif t and t[0] == "e":
                edges.append((int(t[1]) - 1, int(t[2]) - 1))
    adj = [set() for _ in range(n)]
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    return n, edges, adj


def kv(line):
    return dict(t.split("=", 1) for t in line.split() if "=" in t)


class Campaign:
    def __init__(self, ws, policy, speed):
        self.ws, self.policy, self.speed = ws, policy, speed
        self.be = vs.Plain(ws)
        self.g = {}
        for f in sorted(os.listdir(os.path.join(ws, "graphs"))):
            self.g[f[:-4]] = read_graph(os.path.join(ws, "graphs", f))
        self.best = {g: (10**9, None) for g in self.g}          # ub, colouring
        self.clique = {g: [] for g in self.g}
        self.proof = {}
        self.rejected, self.runs, self.queue = set(), [], []
        for g in self.g:
            self.queue += [(g, "exact", 1), (g, "tabu", 1)]
        self.later = [(g, "anneal", 1) for g in self.g]

    def owner(self, g):
        return e10.owner({"graph": g})

    def running(self, who):
        return sum(1 for r in self.runs if r["alive"] and self.owner(r["g"]) == who)

    def launch(self):
        for job in list(self.queue):
            g, m, s = job
            if self.running(self.owner(g)) < e10.CAP:
                h = self.be.start(self.owner(g), "gcol", "--graph", g, "--method", m,
                                  "--seed", str(s))
                self.runs.append({"g": g, "m": m, "s": s, "h": h, "tail": vs.Tail(h),
                                  "alive": True, "t0": time.time(), "inc": 10**9,
                                  "closed": 0.0, "hint_t": None, "hint_c": 0.0,
                                  "bad": False, "opt": None})
                self.queue.remove(job)

    def proper(self, g, col):
        n, edges, _ = self.g[g]
        return len(col) == n and all(col[a] != col[b] for a, b in edges)

    def reject(self, r):
        self.rejected.add((r["g"], r["m"]))
        if r["m"] == "exact" and not r["bad"]:
            r["bad"] = True
            if self.policy == "ref":
                self.be.stop(r["h"])
                self.queue.append((r["g"], "exact", r["s"] + 1))

    def colouring(self, r, line):
        f = kv(line)
        col = [int(c) for c in line.split("c=", 1)[1].split()]
        k = int(f["k"])
        if self.policy == "lazy":
            if k < self.best[r["g"]][0]:
                self.best[r["g"]] = (k, col)
            return
        if not self.proper(r["g"], col) or len(set(col)) > k:
            return self.reject(r)
        if k < self.best[r["g"]][0]:
            self.best[r["g"]] = (k, col)
            self.share(r["g"])

    def share(self, g):
        """One file per graph, rewritten only for a better colouring; exact runs
        started later read it too."""
        k, col = self.best[g]
        if self.policy != "ref":
            return
        path = os.path.join(self.ws, "bounds", g + ".ub")
        with open(path + ".tmp", "w") as f:
            f.write(" ".join(map(str, col)) + "\n")
        os.replace(path + ".tmp", path)

    def line(self, r, x):
        g = r["g"]
        if x.startswith("COLORING"):
            self.colouring(r, x)
        elif x.startswith("CLIQUE"):
            vs_ = [int(v) - 1 for v in x.split("v=", 1)[1].split()]
            adj = self.g[g][2]
            ok = all(b in adj[a] for i, a in enumerate(vs_) for b in vs_[i + 1:])
            if not ok and self.policy == "ref":
                return self.reject(r)
            if len(vs_) > len(self.clique[g]):
                self.clique[g] = vs_
        elif x.startswith("INCUMBENT"):
            r["inc"] = int(kv(x)["k"])
        elif x.startswith("HINT accepted"):
            r["hint_t"], r["hint_c"] = time.time(), r["closed"]
        elif x.startswith("HINT rejected"):
            a, b = (int(v) - 1 for v in kv(x)["conflict"].split("-"))
            if b not in self.g[g][2][a]:
                self.reject(r)
        elif x.startswith("PROGRESS"):
            r["closed"] = float(kv(x)["closed"])
        elif x.startswith("OPTIMAL"):
            r["opt"] = int(kv(x)["chi"])

    def decide(self, r, now):
        """Stop an exact run whose closed share cannot reach 1 before its limit."""
        age = (now - r["t0"]) * self.speed
        if self.policy != "ref" or r["m"] != "exact" or r.get("decided") or age < DECIDE_S:
            return
        r["decided"] = True
        t1, c1 = (r["hint_t"], r["hint_c"]) if r["hint_t"] else (r["t0"], 0.0)
        rate = (r["closed"] - c1) / max(1e-9, (now - t1) * self.speed)
        if rate <= 0 or (1 - r["closed"]) / rate > 3 * (e10.LIMIT_S - age):
            self.be.stop(r["h"])

    def step(self):
        now = time.time()
        for r in self.runs:
            if not r["alive"]:
                continue
            rc = self.be.poll(r["h"])
            for x in r["tail"].lines():
                self.line(r, x)
            if rc is not None:
                for x in r["tail"].lines():
                    self.line(r, x)
                r["alive"] = False
                if r["m"] == "exact" and r["opt"] is not None and not r["bad"]:
                    if self.policy == "ref" and r["opt"] > self.best[r["g"]][0]:
                        self.reject(r)
                    else:
                        self.proof[r["g"]] = r["opt"]
                if r["m"] == "tabu":
                    self.queue += [j for j in self.later if j[0] == r["g"]]
                continue
            self.decide(r, now)
        self.launch()
        return any(r["alive"] for r in self.runs) or self.queue

    def answer(self):
        gs = {}
        for g, (n, _, _) in self.g.items():
            k, col = self.best[g]
            lb = max(self.proof.get(g, 0), len(self.clique[g]))
            gs[g] = {"lb": lb, "ub": k, "coloring": col,
                     "clique": [v + 1 for v in self.clique[g]]}
        return {"graphs": gs, "rejected": [{"graph": g, "method": m}
                                           for g, m in sorted(self.rejected)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--policy", default="ref", choices=("ref", "lazy"))
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    ws = os.path.join(os.path.realpath(a.dir), f"v10{a.policy}{a.seed}")
    shutil.rmtree(ws, ignore_errors=True)
    t = time.time()
    e10.build(ws, a.seed, a.speed)
    tr = e10.truth(a.seed, a.speed)
    print(f"build+truth {time.time() - t:.0f}s", flush=True)
    c, tracker = Campaign(ws, a.policy, a.speed), pst_procs.Tracker()
    tracker.start()
    t = time.time()
    try:
        c.launch()
        while c.step():
            time.sleep(0.5)
        with open(os.path.join(ws, e10.ANSWER), "w") as f:
            json.dump(c.answer(), f)
    finally:
        wall = time.time() - t
        tracker.stop()
        killed = pst_procs.cleanup(tracker, ws, t)
    g = e10.grade(ws, tr)
    recs = pst_build.launches(ws)
    logs = [os.path.join(ws, f) for f in os.listdir(ws) if f.startswith("plain")]
    cpu = tracker.cpu or [0.0]
    low = {k: round(v, 2) for k, v in g["detail"]["graphs"].items() if v < 1}
    print(f"exp10 {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s "
          f"MB={sum(map(os.path.getsize, logs)) / 1e6:.1f} launches={len(recs)} "
          f"peaks={pst_build.peak_streams(recs, [], {}, e10.owner)} "
          f"total_peak={pst_build.peak([(r['t0'], r['t_last']) for r in recs])} "
          f"program_cpu={sum(r['cpu'] for r in recs):.0f}s "
          f"machine_cpu mean={sum(cpu) / len(cpu):.2f} max={max(cpu):.2f} "
          f"max_lag={max(r['lag'] for r in recs):.2f}s leftovers={len(killed)}", flush=True)
    print("  " + json.dumps({k: v for k, v in g.items() if k != "detail"}))
    print("  low graphs:", low, " rejected:", g["detail"]["rejected_got"], "want", tr["rejected"])
    print("  sharing<1:", {k: round(v, 2) for k, v in g["detail"]["sharing"].items() if v < 1},
          " cutoff:", {k: round(v, 2) for k, v in g["detail"]["cutoff"].items()})


if __name__ == "__main__":
    main()
