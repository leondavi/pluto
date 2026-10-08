#!/usr/bin/env python3
"""Aggregate results.jsonl into summary tables.

    python3 experiments/subagents_vs_pluto/analyze.py [--data DIR]

Writes DIR/summary.csv and DIR/summary.md (markdown tables with mean ± sd
and a 95% t-interval per scenario × condition) and prints the markdown.
"""

import argparse
import csv
import json
import math
import os
import statistics

from run_experiment import DEFAULT_OUT

# two-sided 95% Student-t critical values by degrees of freedom
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228}

SCENARIO_ORDER = ["s1_ring", "s2_contention", "s3_fencing", "s4_fanout"]


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    # keep the latest row per (scenario, condition, rep) — reruns overwrite
    latest = {}
    for r in rows:
        latest[(r["scenario"], r["condition"], r["rep"])] = r
    return list(latest.values())


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    half = T95.get(len(xs) - 1, 1.96) * sd / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
    return {"n": len(xs), "mean": m, "sd": sd, "ci_lo": m - half, "ci_hi": m + half}


def fmt(s, digits=1, scale=1.0):
    if not s:
        return "–"
    return f"{s['mean'] / scale:.{digits}f} ± {s['sd'] / scale:.{digits}f}"


def metric_values(rows):
    """Per-run scalar metrics used in the tables."""
    out = []
    for r in rows:
        v = r["verdict"]
        out.append({
            "ok": 1.0 if v.get("ok") else 0.0,
            "wall_s": r["wall_s"],
            "task_span_s": v.get("task_span_s"),
            "total_tokens": r["total_tokens"],
            "output_tokens": r["tokens"]["output_tokens"],
            "cost_usd": r["cost_usd"],
            "coordination_calls": r["coordination_calls"],
            "mean_hop_s": (statistics.fmean(v["hop_latency_s"]) if v.get("hop_latency_s") else None),
            "lost_updates": v.get("lost_updates"),
            "corrupted": (1.0 if v.get("corrupted") else 0.0) if "corrupted" in v else None,
            "exercised": (1.0 if v.get("exercised") else 0.0) if "exercised" in v else None,
            "stale_rejected": (1.0 if v.get("stale_rejected") else 0.0) if "stale_rejected" in v else None,
            "duplicates": v.get("duplicates"),
            "makespan_s": v.get("makespan_s"),
            "timed_out": 1.0 if r.get("timed_out") else 0.0,
        })
    return out


def summarize(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["condition"]), []).append(r)
    summary = {}
    for key, rs in groups.items():
        vals = metric_values(rs)
        summary[key] = {m: stats([v[m] for v in vals]) for m in vals[0]}
    return summary


def markdown(summary):
    lines = ["| Scenario | Condition | n | Success | Wall (s) | Task span (s) | Tokens (k) "
             "| Output tok | Cost (USD) | Coord. calls |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for sc in SCENARIO_ORDER:
        for cond in ("subagents", "pluto"):
            s = summary.get((sc, cond))
            if not s:
                continue
            ok = s["ok"]
            lines.append(
                f"| {sc} | {cond} | {ok['n']} | {ok['mean'] * ok['n']:.0f}/{ok['n']} "
                f"| {fmt(s['wall_s'])} | {fmt(s['task_span_s'])} | {fmt(s['total_tokens'], 0, 1000)} "
                f"| {fmt(s['output_tokens'], 0)} | {fmt(s['cost_usd'], 3)} | {fmt(s['coordination_calls'])} |")
    lines += ["", "| Scenario | Condition | Mean hop (s) | Lost updates | Stale write exercised "
              "| Stale write rejected | Corrupted | Duplicates | Makespan (s) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for sc in SCENARIO_ORDER:
        for cond in ("subagents", "pluto"):
            s = summary.get((sc, cond))
            if not s:
                continue

            def rate(m):
                x = s[m]
                return f"{x['mean'] * x['n']:.0f}/{x['n']}" if x else "–"
            lines.append(f"| {sc} | {cond} | {fmt(s['mean_hop_s'])} | {fmt(s['lost_updates'])} "
                         f"| {rate('exercised')} | {rate('stale_rejected')} | {rate('corrupted')} "
                         f"| {fmt(s['duplicates'])} "
                         f"| {fmt(s['makespan_s'])} |")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=DEFAULT_OUT)
    args = p.parse_args()
    rows = load(os.path.join(args.data, "results.jsonl"))
    summary = summarize(rows)
    with open(os.path.join(args.data, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "condition", "metric", "n", "mean", "sd", "ci95_lo", "ci95_hi"])
        for (sc, cond), ms in sorted(summary.items()):
            for m, s in ms.items():
                if s:
                    w.writerow([sc, cond, m, s["n"], round(s["mean"], 4), round(s["sd"], 4),
                                round(s["ci_lo"], 4), round(s["ci_hi"], 4)])
    md = markdown(summary)
    with open(os.path.join(args.data, "summary.md"), "w") as f:
        f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
