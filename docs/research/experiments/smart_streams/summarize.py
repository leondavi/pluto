#!/usr/bin/env python3
"""Summarise results.jsonl: mean, sd, paired differences, per-run table.

    python3 summarize.py OUT_DIR [--exclude-tainted]
"""

import argparse
import json
import os
import statistics as st

METRICS = [("score", lambda r: r["score"]),
           ("wall_s", lambda r: r["wall_s"]),
           ("cost_usd", lambda r: r["team"]["cost_usd"] if r["team"]["cost_complete"] else None),
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
           ("protocol_failures", lambda r: r["n_protocol_failures"])]


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
        e = {"name": rs[0]["name"], "conditions": {}, "paired_diff_smartstreams_minus_native": {}}
        for c in sorted({r["condition"] for r in rs}):
            e["conditions"][c] = {m: stats([f(r) for r in rs if r["condition"] == c])
                                  for m, f in METRICS}
        by = {(r["condition"], r["seed"]): r for r in rs}
        seeds = sorted({s for _, s in by})
        for m, f in METRICS:
            d = []
            for s in seeds:
                a, b = by.get(("smartstreams", s)), by.get(("native", s))
                if a and b and f(a) is not None and f(b) is not None:
                    d.append(f(a) - f(b))
            e["paired_diff_smartstreams_minus_native"][m] = stats(d)
        out["experiments"][exp] = e
    for r in sorted(rows, key=lambda r: (r["exp"], r["rep"], r["condition"])):
        codes = sorted({c for fs in r["protocol_failures"].values() for c in fs})
        ss = sum(n for k, n in r["team"]["tool_calls"].items() if k.startswith("mcp__smartstreams__"))
        out["runs"].append({"exp": r["exp"], "rep": r["rep"], "condition": r["condition"],
                            "run_id": r["run_id"], "tainted": r["tainted"],
                            "timed_out": [n for n, t in r["timed_out"].items() if t],
                            "peak_streams": " ".join(f"{n}:{p}" for n, p in
                                                     r["peak_streams"].items()),
                            "low_parallelism": r["low_parallelism"],
                            "smartstreams_calls": ss, "failure_codes": codes,
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
    out = [f"# SmartStreams vs native Claude Code\n\nRuns: {s['n_runs']}, used: {s['n_used']}. "
           "Values are mean ± sample sd. Differences are SmartStreams minus native, paired by seed.\n"]
    for exp, e in s["experiments"].items():
        cs = list(e["conditions"])
        out.append(f"## Experiment {exp}: {e['name']}\n")
        out.append("| Metric | " + " | ".join(cs) + " | paired difference |")
        out.append("|---" * (len(cs) + 2) + "|")
        for m, _ in METRICS:
            out.append(f"| {m} | " + " | ".join(pm(e["conditions"][c][m]) for c in cs)
                       + f" | {pm(e['paired_diff_smartstreams_minus_native'][m])} |")
        out.append("")
    cols = ["exp", "rep", "condition", "score", "wall_s", "cost_usd", "output_tokens", "turns",
            "tool_result_bytes", "smartstreams_calls", "program_seconds", "protocol_failures",
            "timed_out", "peak_streams", "low_parallelism", "cpu_share_max", "tainted", "failure_codes"]
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
        rows = [json.loads(x) for x in f if x.strip()]
    old = [r for r in rows if r.get("protocol_version", 1) != 2]
    if old:
        raise SystemExit(f"{len(old)} rows are not protocol v2; use the v1 code (git a9eab16)")
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
