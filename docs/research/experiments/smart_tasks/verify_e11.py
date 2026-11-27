#!/usr/bin/env python3
"""Scripted solvers of experiment 11 (no LLM), plain processes and log files.

    python3 verify_e11.py DIR [--seed N ...] [--policy ref|lazy] [--scale S]

They use only what an agent sees: docs/FORECAST.md and the programs' output.
``ref`` watches every run, stops a run at its first unstable step, reruns it
with --substeps 2 at once, checks mass and computes the products: it must score
at least 0.99. ``lazy`` waits for all runs, reruns members whose output went
non-finite, checks nothing else and must score clearly lower.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exp11_score as sc  # noqa: E402
import exp11_surge as e11  # noqa: E402
import pst_build  # noqa: E402
import pst_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402


def questions(ws):
    """The seeded questions, read from the docs as an agent would."""
    with open(os.path.join(ws, "docs/FORECAST.md")) as f:
        t = " ".join(f.read().split())
    q1 = re.search(r"station cell (\d+) exceeds ([-\d.]+) m at some obs time with lead time "
                   r"from ([\d.]+) to ([\d.]+) h", t)
    q2 = re.search(r"q2: .*? at station cell (\d+) is highest", t)
    q4 = re.search(r"exceeds ([-\d.]+) m in the field at lead time ([\d.]+) h", t)
    return {"q1": {"station": int(q1[1]), "threshold": float(q1[2]), "from_h": float(q1[3]),
                   "to_h": float(q1[4])},
            "q2": {"station": int(q2[1])},
            "q4": {"threshold": float(q4[1]), "lead_h": float(q4[2])}}


def read(path, lead):
    with open(path) as f:
        lines = f.read().splitlines()
    out = sc.parse(lines, lead)
    head = next(x for x in lines if x.startswith("# swm"))
    out["stations"] = [int(x) for x in re.search(r"stations=([\d,]+)", head)[1].split(",")]
    out["t_end"] = float(re.search(r"t_end=(\d+)", head)[1])
    return out


def unstable_line(x):
    """First unstable step as the docs define it."""
    if not x.startswith("step "):
        return None
    m = re.search(r" t=(\S+) cfl=(\S+)", x)
    return float(m[1]) if not float(m[2]) <= 1.0 else None


def solve(ws, be, policy):
    qs = questions(ws)
    lead = qs["q4"]["lead_h"] * 3600
    runs = {m: be.start(e11.owner({"member": m}), "swm", "--member", str(m))
            for m in range(1, 49)}
    runs[0] = be.start("w1", "swm", "--nature")
    tails = {m: vs.Tail(h) for m, h in runs.items()}
    onset, reruns, left = {}, {}, set(runs)
    while left:
        time.sleep(0.5)
        for m in sorted(left):
            done = be.poll(runs[m]) is not None
            if policy == "ref" and m and m not in onset:
                for x in tails[m].lines():
                    t = unstable_line(x)
                    if t is not None:
                        onset[m] = t / 3600
                        be.stop(runs[m])
                        reruns[m] = be.start(e11.owner({"member": m}), "swm", "--member",
                                             str(m), "--substeps", "2")
                        break
            if done or m in reruns:
                left.discard(m)
    if policy == "lazy":                                 # look at the logs afterwards
        for m in range(1, 49):
            with open(runs[m]["log"]) as f:
                bad = next((x for x in f if "nan" in x and x.startswith("step ")), None)
            if bad:
                onset[m] = float(re.search(r" t=(\S+)", bad)[1]) / 3600
                reruns[m] = be.start(e11.owner({"member": m}), "swm", "--member", str(m),
                                     "--substeps", "2")
    while any(be.poll(h) is None for h in reruns.values()):
        time.sleep(0.5)
    nature = read(runs[0]["log"], lead)
    members, valid = {}, {}
    for m in range(1, 49):
        if m in onset:
            members[str(m)] = {"status": "unstable", "onset_h": onset[m], "drift": None}
            valid[m] = read(reruns[m]["log"], lead)
            continue
        r = read(runs[m]["log"], lead)
        if policy == "ref" and r["drift_max"] > 1e-9:
            members[str(m)] = {"status": "nonconservative", "onset_h": None,
                               "drift": r["drift_last"]}
        else:
            members[str(m)] = {"status": "ok", "onset_h": None, "drift": None}
            valid[m] = r
    qs["stations"] = nature["stations"]
    pr = sc.products(valid, nature, qs, nature["t_end"])
    pr.pop("member_rmse")
    os.makedirs(os.path.join(ws, "answers"), exist_ok=True)
    with open(os.path.join(ws, e11.ANSWER), "w") as f:
        json.dump(dict(pr, members=members), f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seed", type=int, nargs="+", default=[1])
    ap.add_argument("--policy", default="ref", choices=("ref", "lazy"))
    ap.add_argument("--scale", type=float, default=1.0, help="shortens the runs")
    a = ap.parse_args()
    for seed in a.seed:
        ws = os.path.join(os.path.realpath(a.dir), f"v11{a.policy}{seed}")
        shutil.rmtree(ws, ignore_errors=True)
        kw = {"duration": e11.exp11_plan.DURATION * a.scale} if a.scale != 1 else {}
        e11.build(ws, seed, **kw)
        tr = e11.truth(seed, **kw)                      # before the clock starts
        be, tracker = vs.Plain(ws), pst_procs.Tracker()
        tracker.start()
        t = time.time()
        try:
            solve(ws, be, a.policy)
        finally:
            wall = time.time() - t
            tracker.stop()
            killed = pst_procs.cleanup(tracker, ws, t)
        g = sc.grade(ws, tr, {"phase1_start": t})
        recs = pst_build.launches(ws)
        peaks = pst_build.peak_streams(recs, [], {}, e11.owner)
        logs = [os.path.join(ws, f) for f in os.listdir(ws) if f.startswith("plain")]
        cpu = tracker.cpu or [0.0]
        print(f"e11 {a.policy} seed={seed}: score={g['score']} wall={wall:.0f}s "
              f"MB={sum(map(os.path.getsize, logs)) / 1e6:.1f} launches={len(recs)} "
              f"peaks={dict(sorted(peaks.items()))} "
              f"total_peak={pst_build.peak([(r['t0'], r['t_last']) for r in recs])} "
              f"program_cpu={sum(r['cpu'] for r in recs):.0f}s "
              f"machine_cpu mean={sum(cpu) / len(cpu):.3f} max={max(cpu):.3f} "
              f"max_lag={max(r['lag'] for r in recs):.2f}s leftovers={len(killed)}", flush=True)
        g.pop("members")
        print("  " + json.dumps(g), flush=True)


if __name__ == "__main__":
    main()
