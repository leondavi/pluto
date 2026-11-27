"""Protocol 7 analysis: the primary decision, its sensitivity analyses, and per-task results.

usage: analyze_protocol7.py MAIN_DIR RERUN_DIR [--json OUT]

Primary decision (fixed in the protocol, harness README): pooled over all pairs,
PST is better if the mean paired relative cost difference has a one-sided 95%
bootstrap bound below 0 and the mean score difference a one-sided 95% bound above
-0.05; worse if the cost bound is above 0 or the score bound below -0.05;
otherwise inconclusive. Repeated (a) with pairs that had a permission denial
replaced by RERUN_DIR and (b) without pairs whose pacing lag exceeds 0.25 s in
either run (load). Secondary: per task, as run and with reruns. Score gap: PST minus native, paired
t-test and Wilcoxon signed-rank test, bootstrap 95 % interval. Cost and steps:
ratio PST / native as the geometric mean of paired ratios, one-sample t-test on
the log ratio, bootstrap 95 % interval. Needs numpy and scipy.
"""
import argparse
import collections
import json
import os

import numpy as np
from scipy import stats

TASKS = (("7", "E7 Riemann zeta zeros"), ("8", "E8 Ising/Potts transitions"),
         ("9", "E9 Sequencing cohort QC"), ("10", "E10 Graph colouring"),
         ("11", "E11 Storm-surge ensemble"))
ARMS = ("native", "smarttasks")


def load(path):
    pairs = collections.defaultdict(dict)
    with open(os.path.join(path, "results.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                pairs[(r["exp"], r["rep"])][r["condition"]] = r
    return {k: v for k, v in pairs.items() if all(a in v for a in ARMS)}


def denied(r):
    return any("permission_denied" in v for v in r["protocol_failures"].values())


def boot(x, rng, n=10000):
    x = np.asarray(x)
    means = x[rng.integers(0, len(x), (n, len(x)))].mean(axis=1)
    return [float(v) for v in np.percentile(means, [2.5, 97.5])]


def task_stats(pairs, rng):
    nat = [p["native"] for p in pairs]
    pst = [p["smarttasks"] for p in pairs]
    s_n = np.array([r["score"] for r in nat])
    s_p = np.array([r["score"] for r in pst])
    gap = s_p - s_n
    out = {"n": len(pairs), "score_native": float(s_n.mean()), "score_pst": float(s_p.mean()),
           "gap": float(gap.mean()), "gap_ci": boot(gap, rng),
           "p_t": float(stats.ttest_rel(s_p, s_n).pvalue),
           "p_wilcoxon": float(stats.wilcoxon(s_p, s_n).pvalue) if np.any(gap) else 1.0,
           "pst_better": int((gap > 0).sum())}
    for key, f in (("cost", lambda r: r["team"]["cost_usd"]), ("steps", lambda r: r["team"]["turns"])):
        lr = np.log(np.array([f(r) for r in pst]) / np.array([f(r) for r in nat]))
        out[key] = {"ratio": float(np.exp(lr.mean())), "ci": [float(np.exp(v)) for v in boot(lr, rng)],
                    "p": float(stats.ttest_1samp(lr, 0).pvalue), "pst_lower": int((lr < 0).sum()),
                    "native_mean": float(np.mean([f(r) for r in nat])),
                    "pst_mean": float(np.mean([f(r) for r in pst]))}
    return out


def analyse(by_task):
    rng = np.random.default_rng(1)
    return {e: task_stats(by_task[e], rng) for e, _ in TASKS if by_task.get(e)}


def table(title, res):
    lines = [f"\n{title}\n",
             "| Task | n | Native | PST | Gap [95% CI] | p (t) | Cost PST/native [95% CI] | p | Steps PST/native | p |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for e, name in TASKS:
        if e not in res:
            continue
        r = res[e]
        c, s = r["cost"], r["steps"]
        lines.append(f"| {name} | {r['n']} | {r['score_native']:.3f} | {r['score_pst']:.3f} | "
                     f"{r['gap']:+.3f} [{r['gap_ci'][0]:+.3f}, {r['gap_ci'][1]:+.3f}] | {r['p_t']:.3f} | "
                     f"{c['ratio']:.2f} [{c['ci'][0]:.2f}, {c['ci'][1]:.2f}] | {c['p']:.4f} | "
                     f"{s['ratio']:.2f} | {s['p']:.4f} |")
    return "\n".join(lines)


def decision(by_task, rng, n=10000):
    """The protocol's primary decision, pooled over all pairs: mean paired relative cost
    difference (PST - native) / native and mean paired score difference, with one-sided
    95% bootstrap bounds, resampling pairs within tasks."""
    groups = [[((p["smarttasks"]["team"]["cost_usd"] - p["native"]["team"]["cost_usd"])
                / p["native"]["team"]["cost_usd"], p["smarttasks"]["score"] - p["native"]["score"])
               for p in ps] for ps in by_task.values() if ps]
    allp = np.array([x for g in groups for x in g])
    boots = []
    for _ in range(n):
        smp = np.concatenate([np.asarray(g)[rng.integers(0, len(g), len(g))] for g in groups])
        boots.append(smp.mean(axis=0))
    boots = np.array(boots)
    cost_hi = float(np.percentile(boots[:, 0], 95))   # one-sided upper bound on cost difference
    cost_lo = float(np.percentile(boots[:, 0], 5))
    score_lo = float(np.percentile(boots[:, 1], 5))   # one-sided lower bound on score difference
    if cost_hi < 0 and score_lo > -0.05:
        verdict = "PST better"
    elif cost_lo > 0 or score_lo < -0.05:
        verdict = "PST worse"
    else:
        verdict = "inconclusive"
    return {"pairs": len(allp), "cost_diff": float(allp[:, 0].mean()), "cost_bounds_90": [cost_lo, cost_hi],
            "score_diff": float(allp[:, 1].mean()), "score_lower_95": score_lo, "verdict": verdict}


def lag_ok(p, limit=0.25):
    return all((p[a].get("max_pacing_lag_s") or 0) <= limit for a in ARMS)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("main_dir")
    ap.add_argument("rerun_dir")
    ap.add_argument("--json")
    a = ap.parse_args()
    main_pairs, reruns = load(a.main_dir), load(a.rerun_dir)
    primary = {e: [v for k, v in sorted(main_pairs.items(), key=lambda kv: kv[0][1]) if k[0] == e]
               for e, _ in TASKS}
    sensitivity = {e: [v for k, v in main_pairs.items() if k[0] == e
                       and not any(denied(v[a]) for a in ARMS)]
                   + [v for k, v in reruns.items() if k[0] == e] for e, _ in TASKS}
    low_load = {e: [p for p in ps if lag_ok(p)] for e, ps in primary.items()}
    rng = np.random.default_rng(7)
    res = {"decision": {"as_run": decision(primary, rng), "reruns": decision(sensitivity, rng),
                        "low_load": decision(low_load, rng)},
           "low_load_pairs": {e: len(ps) for e, ps in low_load.items()},
           "primary": analyse(primary), "sensitivity": analyse(sensitivity)}
    print("Primary decision (protocol, pooled over all pairs)")
    for k, d in res["decision"].items():
        print(f"  {k:8} pairs={d['pairs']:2}  cost diff {100 * d['cost_diff']:+.1f}% "
              f"[90%: {100 * d['cost_bounds_90'][0]:+.1f}, {100 * d['cost_bounds_90'][1]:+.1f}]  "
              f"score diff {d['score_diff']:+.3f} (lower 95% {d['score_lower_95']:+.3f})  -> {d['verdict']}")
    print("  pairs kept with pacing lag <= 0.25 s in both runs:", res["low_load_pairs"])
    print("\nSecondary, per task")
    print(table("Per task: all pairs as run", res["primary"]))
    print(table("Per task: pairs with a permission denial replaced by reruns", res["sensitivity"]))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
