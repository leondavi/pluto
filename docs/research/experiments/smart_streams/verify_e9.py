#!/usr/bin/env python3
"""Scripted solvers of experiment 9 (no LLM), plain processes and log files only.

    python3 verify_e9.py DIR [--seed N] [--policy reference|lazy] [--reads R --duration S]

``reference`` watches every run while it goes (contamination and flowcell checks,
stops and re-sequencing as the SOP says), then calls genotypes with the analysis
standard and answers the cohort questions; it must score >= 0.99. ``lazy`` lets
every run finish, counts every read and base, trusts the manifest and skips the
cohort questions: it must score clearly lower.
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import e9_analysis as A  # noqa: E402
import exp9_cohort as e9  # noqa: E402
import ss_build  # noqa: E402
import ss_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402

C_FLAG, C_CHECK, C_RECHECK, D_FLAG = 0.03, 0.30, 0.42, 0.015


def _write(ws, rel, obj):
    path = os.path.join(ws, rel)
    with open(path + ".t", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".t", path)


class Fleet:
    def __init__(self, ws, be, strict):
        self.ws, self.be, self.p, self.strict, self.runs = ws, be, A.Panel(ws), strict, {}

    def start(self, s, lib=1, fc=1, frm=0):
        args = ["--sample", str(s)] + (["--library", "2"] if lib == 2 else []) + (
            ["--flowcell", "2", "--from", str(frm)] if fc == 2 else [])
        h = self.be.start(e9.owner({"sample": s}), "seqrun", *args)
        self.runs[s, lib, fc] = {"h": h, "tail": vs.Tail(h), "d": A.RunData(self.p, self.strict),
                                 "rc": None, "stopped": False}

    def pump(self, key):
        r = self.runs[key]
        for x in r["tail"].lines():
            r["d"].feed(x)
        rc = self.be.poll(r["h"])
        if rc is not None:
            for x in r["tail"].lines():
                r["d"].feed(x)
            r["rc"] = rc
            assert rc == 0 or r["stopped"], (key, rc)
        return rc

    def stop(self, key):
        self.runs[key]["stopped"] = True
        self.be.stop(self.runs[key]["h"])

    def busy(self):
        return [k for k, r in self.runs.items() if r["rc"] is None]


def watch(fleet, total, react):
    """Run everything; with ``react`` apply the SOP while runs go.
    Returns ({sample: first degraded read}, set of contaminated samples)."""
    for s in range(1, e9.SAMPLES + 1):
        fleet.start(s)
    onset, cont, checked = {}, set(), {}
    while fleet.busy():
        time.sleep(0.5)
        for key in fleet.busy():
            if fleet.pump(key) is not None or not react or key[1:] != (1, 1):
                continue
            s, d = key[0], fleet.runs[key]["d"]
            if s in onset or s in cont:
                continue
            if d.tail_rate() > D_FLAG:
                on = A.onset(d)
                if on and on[2] > 10 * max(on[1], 1e-4):
                    fleet.stop(key)
                    onset[s] = on[0]
                    fleet.start(s, 1, 2, on[0])
                    continue
            for i, frac in enumerate((C_CHECK, C_RECHECK)):
                if d.last >= frac * total and checked.get(s, -1) < i:
                    checked[s] = i
                    fm = A.freemix(d.counts(), fleet.p)
                    if fm >= C_FLAG:
                        fleet.stop(key)
                        cont.add(s)
                        fleet.start(s, 2)
                    elif fm < C_FLAG / 2:
                        checked[s] = 9
    return onset, cont


def reference(ws, be, total, report):
    fleet = Fleet(ws, be, True)
    onset, cont = watch(fleet, total, True)
    P, runs, gts, ans = fleet.p, fleet.runs, {}, {}
    for s in range(1, e9.SAMPLES + 1):
        if s in cont:
            c = runs[s, 2, 1]["d"].counts()
        elif s in onset:
            c = runs[s, 1, 1]["d"].counts(0, onset[s])
            for k, (r, a) in enumerate(runs[s, 1, 2]["d"].counts(onset[s])):
                c[k][0] += r
                c[k][1] += a
        else:
            c = runs[s, 1, 1]["d"].counts()
        gts[s] = A.call(c, P)
    for s in range(1, e9.SAMPLES + 1):
        a = {"individual": A.identity(gts[s], P), "contamination": 0.0, "contaminant": None,
             "degraded_from": onset.get(s),
             "calls": {k: A.GTS[gts[s][P.ids.index(k)]] for k in report}}
        if s in cont:
            lib1 = runs[s, 1, 1]["d"].counts()
            fits = {t: A.mix_fit(lib1, gts[s], gts[t]) for t in gts if t != s}
            t = max(fits, key=lambda x: fits[x][0])
            a.update(contamination=round(fits[t][1], 4), contaminant=t)
        ans[s] = a
        _write(ws, f"answers/sample_{s}.json", a)
    rel = A.relatives(gts)
    return {"samples": {str(s): a for s, a in ans.items()}, "relatives": rel,
            "allele_frequencies": A.frequencies(gts, rel, P, report)}


def lazy(ws, be, total, report):
    fleet = Fleet(ws, be, False)
    watch(fleet, total, False)
    P, gts = fleet.p, {}
    for s in range(1, e9.SAMPLES + 1):
        gts[s] = A.naive_call(fleet.runs[s, 1, 1]["d"].counts())
    samples = {str(s): {"individual": P.manifest[str(s)], "contamination": 0.0, "contaminant": None,
                        "degraded_from": None,
                        "calls": {k: A.GTS[gts[s][P.ids.index(k)] or 0] for k in report}}
               for s in gts}
    return {"samples": samples, "relatives": [],
            "allele_frequencies": A.frequencies(gts, [], P, report)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--policy", default="reference", choices=("reference", "lazy"))
    ap.add_argument("--reads", type=int, default=e9.READS)
    ap.add_argument("--duration", type=float, default=e9.DURATION)
    a = ap.parse_args()
    kw = {"reads": a.reads, "duration": a.duration, "duration2": a.duration * 2 / 3}
    ws = os.path.join(os.path.realpath(a.dir), f"v9{a.policy}{a.seed}")
    shutil.rmtree(ws, ignore_errors=True)
    e9.build(ws, a.seed, **kw)
    report = [e9.site_id(k) for k in e9.plan(a.seed, **kw)["report"]]
    be, tracker, t = vs.Plain(ws), ss_procs.Tracker(), time.time()
    sampler = ss_procs.Tracker()
    sampler.start()
    try:
        ans = (reference if a.policy == "reference" else lazy)(ws, be, a.reads, report)
        _write(ws, e9.ANSWER, ans)
    finally:
        sampler.stop()
        shares = sampler.cpu or [0.0]
        be.close()
        ss_procs.cleanup(tracker, ws, t)
    wall = time.time() - t
    g = e9.grade(ws, e9.truth(a.seed, **kw), {"run_end": time.time()})
    recs = ss_build.launches(ws)
    out_mb = sum(os.path.getsize(os.path.join(ws, f)) for f in os.listdir(ws)
                 if f.startswith("plain")) / 1e6
    cpu = sum(r["cpu"] for r in recs)
    print(f"exp9 {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s launches={len(recs)} "
          f"peaks={ss_build.peak_streams(recs, [], {}, e9.owner)} "
          f"peak_total={ss_build.peak([(r['t0'], r['t_last']) for r in recs])} "
          f"program_cpu={cpu:.0f}s ({100 * cpu / (wall * os.cpu_count()):.1f}% of machine) "
          f"cpu_share_mean={sum(shares) / len(shares):.3f} max_lag={max(r['lag'] for r in recs):.2f}s "
          f"output={out_mb:.1f}MB", flush=True)
    print("  " + json.dumps(g))


if __name__ == "__main__":
    main()
