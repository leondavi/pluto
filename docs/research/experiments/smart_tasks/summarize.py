#!/usr/bin/env python3
"""Summarise results.jsonl: mean, sd, paired differences of all arms, per-run table.

    python3 summarize.py OUT_DIR [--exclude-tainted]
"""

import argparse
import json
import os
import statistics as st

import pst_metrics


def process(key):
    """A process metric of experiments 4 and 5 (None elsewhere)."""
    return lambda r: (r["grade"].get("process") or {}).get(key)


def cat(cls, key):
    return lambda r: ((r.get("categories") or {}).get(cls) or {}).get(key)


def sub(key):
    return lambda r: (r["team"].get("sub") or {}).get(key)


def cost_est(r):
    """Reported cost, or with estimates for sessions that were killed."""
    return r["team"].get("cost_est_usd", r["team"]["cost_usd"]
                         if r["team"]["cost_complete"] else None)


METRICS = [("score", lambda r: r["score"]),
           ("wall_s", lambda r: r["wall_s"]),
           ("cost_usd", lambda r: r["team"]["cost_usd"] if r["team"]["cost_complete"] else None),
           ("cost_est_usd", cost_est),
           ("input_tokens", lambda r: r["team"]["tokens"]["input_tokens"]),
           ("cache_read_tokens", lambda r: r["team"]["tokens"]["cache_read_input_tokens"]),
           ("cache_write_tokens", lambda r: r["team"]["tokens"]["cache_creation_input_tokens"]),
           ("output_tokens", lambda r: r["team"]["tokens"]["output_tokens"]),
           ("turns", lambda r: r["team"]["turns"]),
           ("tool_calls", lambda r: sum(r["team"]["tool_calls"].values())),
           ("tool_result_bytes", lambda r: r["team"]["tool_result_bytes"]),
           ("max_tool_result_bytes", lambda r: r["team"]["max_tool_result_bytes"]),
           ("program_seconds", lambda r: r["program_seconds"]),
           ("min_peak_streams", lambda r: min(r["peak_streams"].values())),
           ("cpu_share_max", lambda r: r["cpu_share_max"]),
           ("max_pacing_lag_s", lambda r: r["max_pacing_lag_s"]),
           ("agents_timed_out", lambda r: sum(r["timed_out"].values())),
           ("protocol_failures", lambda r: r["n_protocol_failures"]),
           ("subagents", sub("agents")), ("subagent_turns", sub("turns")),
           ("subagent_tool_calls", sub("tool_calls")),
           ("subagent_result_bytes", sub("tool_result_bytes"))]
METRICS += [(f"{c}_{k}", cat(c, k)) for c in ("process", "analysis", "coordination", "answer")
            for k in ("arg_bytes", "turns", "output_tokens")]
METRICS += [(k, process(k)) for k in (
    "recomputed_program_s", "unneeded_relaunches", "orphans", "duplicate_s", "jobs_done",
    "healthy_kills", "died_in_agent_crash", "faults_recovered_in_time",
    "precision", "recall", "answers_in_time", "wrong_codes")]
ARMS = ("smarttasks", "native", "native_sub")
PAIRS = (("smarttasks", "native"), ("native_sub", "native"), ("smarttasks", "native_sub"))
SCORE_MIN, RELATIVE_MIN, SAME_SIGN = 0.05, 0.15, 0.8


def verdict(metric, diffs, base):
    """The README's decision rule: 'a>b', 'a<b' or 'tie'; None below 5 pairs.

    score: |mean| >= 0.05; other metrics: |mean| >= 15% of the second arm's mean
    (any difference if that mean is 0). Both need the same sign in 4 of 5 pairs."""
    if len(diffs) < 5:
        return None
    mean = st.fmean(diffs)
    least = SCORE_MIN if metric == "score" else RELATIVE_MIN * abs(base or 0)
    same = sum((d > 0) == (mean > 0) and d != 0 for d in diffs)
    if mean == 0 or abs(mean) < least - 1e-12 or same < SAME_SIGN * len(diffs):
        return "tie"
    return "a>b" if mean > 0 else "a<b"


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "sd": None}
    return {"n": len(xs), "mean": st.fmean(xs), "sd": st.stdev(xs) if len(xs) > 1 else None}


def summarize(rows, exclude_tainted=False):
    use = [r for r in rows if not (exclude_tainted and r["tainted"])]
    out = {"n_runs": len(rows), "n_used": len(use), "experiments": {}, "runs": []}
    for exp in sorted({r["exp"] for r in use}):
        rs = [r for r in use if r["exp"] == exp]
        e = {"name": rs[0]["name"], "conditions": {}, "verdicts": {}}
        for c in sorted({r["condition"] for r in rs}, key=lambda c: (c not in ARMS, ARMS.index(c)
                                                                     if c in ARMS else c)):
            e["conditions"][c] = {m: stats([f(r) for r in rs if r["condition"] == c])
                                  for m, f in METRICS}
        by = {(r["condition"], r["seed"]): r for r in rs}
        seeds = sorted({s for _, s in by})
        for ca, cb in PAIRS:
            if not (ca in e["conditions"] and cb in e["conditions"]):
                continue
            key, e["verdicts"][f"{ca}_vs_{cb}"] = f"paired_diff_{ca}_minus_{cb}", {}
            e[key] = {}
            for m, f in METRICS:
                d, base = [], []
                for s in seeds:
                    a, b = by.get((ca, s)), by.get((cb, s))
                    if a and b and f(a) is not None and f(b) is not None:
                        d.append(f(a) - f(b))
                        base.append(f(b))
                e[key][m] = stats(d)
                e["verdicts"][f"{ca}_vs_{cb}"][m] = verdict(m, d, st.fmean(base) if base else 0)
        out["experiments"][exp] = e
    for r in sorted(rows, key=lambda r: (r["exp"], r["rep"], r["condition"])):
        codes = sorted({c for fs in r["protocol_failures"].values() for c in fs})
        pst = sum(n for k, n in r["team"]["tool_calls"].items() if k.startswith("mcp__smarttasks__")) \
            + r["team"].get("pst_calls", 0)
        out["runs"].append({"exp": r["exp"], "rep": r["rep"], "condition": r["condition"],
                            "run_id": r["run_id"], "tainted": r["tainted"],
                            "timed_out": [n for n, t in r["timed_out"].items() if t],
                            "peak_streams": " ".join(f"{n}:{p}" for n, p in
                                                     r["peak_streams"].items()),
                            "low_parallelism": r["low_parallelism"],
                            "smarttasks_calls": pst,
                            "chaos": [f"{x['kind']}:{x.get('agent') or ''}"
                                      + ("(missed)" if x.get("missed") else "(error)"
                                         if x.get("error") else "")
                                      for x in r.get("chaos", [])], "failure_codes": codes,
                            "parts": r["grade"]["parts"], **{m: f(r) for m, f in METRICS}})
    return out


def fmt(x):
    if x is None:
        return "-"
    if isinstance(x, float) and abs(x) < 100:
        return f"{x:.3f}"
    return f"{x:,.0f}"


def pm(s):
    if not s or not s["n"]:
        return "-"
    return fmt(s["mean"]) + (f" ± {fmt(s['sd'])}" if s["sd"] is not None else "") + f" (n={s['n']})"


def markdown(s):
    out = [f"# SmartTasks vs native Claude Code\n\nRuns: {s['n_runs']}, used: {s['n_used']}. "
           "Values are mean ± sample sd. Differences are paired by seed; the verdict "
           "in brackets applies the README's rule and needs 5 pairs.\n"]
    for exp, e in s["experiments"].items():
        cs = list(e["conditions"])
        ds = [k for k in e if k.startswith("paired_diff_")]
        out.append(f"## Experiment {exp}: {e['name']}\n")
        out.append("| Metric | " + " | ".join(cs + [d[12:].replace("_minus_", " - ") for d in ds])
                   + " |")
        out.append("|---" * (1 + len(cs) + len(ds)) + "|")
        for m, _ in METRICS:
            if not any(e["conditions"][c][m]["n"] for c in cs):
                continue
            cells = [pm(e["conditions"][c][m]) for c in cs]
            for d in ds:
                v = e["verdicts"][d[12:].replace("_minus_", "_vs_")][m]
                cells.append(pm(e[d][m]) + (f" [{v}]" if v else ""))
            out.append(f"| {m} | " + " | ".join(cells) + " |")
        out.append("")
    cols = ["exp", "rep", "condition", "score", "wall_s", "cost_est_usd", "output_tokens", "turns",
            "tool_result_bytes", "smarttasks_calls", "program_seconds", "subagents", "protocol_failures",
            "chaos", "timed_out", "peak_streams", "low_parallelism", "cpu_share_max", "tainted", "failure_codes"]
    out += ["## Runs\n", "| " + " | ".join(cols) + " |", "|---" * len(cols) + "|"]
    for r in s["runs"]:
        cells = [fmt(v) if isinstance(v, (int, float)) and not isinstance(v, bool) or v is None
                 else ", ".join(v) if isinstance(v, list) else str(v) for v in (r[c] for c in cols)]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out_dir")
    p.add_argument("--exclude-tainted", action="store_true")
    a = p.parse_args()
    with open(os.path.join(a.out_dir, "results.jsonl"), encoding="utf-8") as f:
        rows = [pst_metrics.renamed(json.loads(x)) for x in f if x.strip()]
    vs = {r.get("protocol_version", 1) for r in rows}
    if not vs <= {2, 3}:
        raise SystemExit("rows of protocol v1: use the v1 code (git a9eab16)")
    if len(vs) > 1:
        raise SystemExit("rows of protocols 2 and 3 must not be pooled")
    s = summarize(rows, a.exclude_tainted)
    md = markdown(s)
    tag = "_clean" if a.exclude_tainted else ""
    with open(os.path.join(a.out_dir, f"summary{tag}.json"), "w") as f:
        json.dump(s, f, indent=1)
    with open(os.path.join(a.out_dir, f"summary{tag}.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
