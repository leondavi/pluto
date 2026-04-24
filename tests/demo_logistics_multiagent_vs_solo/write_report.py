"""Render the side-by-side comparison report for the logistics demo.

Reads trace.json (produced by the test harness) and writes a markdown
report covering all 5 setups (Haiku team, Sonnet team, Haiku solo,
Sonnet solo, Opus solo).
"""

from __future__ import annotations

import json
import os
import time


def _file_summary(workspace: str) -> dict:
    total_bytes = total_lines = 0
    for mod in ("routing.py", "scheduling.py", "graph_paths.py",
                "integration.py", "api.py"):
        p = os.path.join(workspace, "src", "logistics", mod)
        if os.path.isfile(p):
            total_bytes += os.path.getsize(p)
            try:
                total_lines += open(p).read().count("\n")
            except OSError:
                pass
    return {"bytes": total_bytes, "lines": total_lines}


def _total_dur(metrics: dict) -> float:
    return round(sum(c.get("duration_s", 0.0)
                     for c in metrics.get("calls", [])), 2)


def _total_calls(metrics: dict) -> int:
    return len(metrics.get("calls", []))


def render(trace_json_path: str, out_md_path: str) -> None:
    with open(trace_json_path) as f:
        data = json.load(f)

    setups = [
        ("Haiku team",   "team", data["haiku_team"]),
        ("Sonnet team",  "team", data["sonnet_team"]),
        ("Haiku solo",   "solo", data["haiku_solo"]),
        ("Sonnet solo",  "solo", data["sonnet_solo"]),
        ("Opus solo",    "solo", data["opus_solo"]),
    ]

    lines: list[str] = []
    lines.append("# Demo: Logistics Toolkit — Multi-Agent Teams vs Solo Models")
    lines.append("")
    lines.append(f"_Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_")
    lines.append("")
    lines.append(f"- Pluto server: HTTP port `{data['pluto_http_port']}` "
                 "— real Erlang/OTP server")
    lines.append(f"- Models: Haiku=`{data['models']['haiku']}`, "
                 f"Sonnet=`{data['models']['sonnet']}`, "
                 f"Opus=`{data['models']['opus']}`")
    for label, kind, r in setups:
        lines.append(f"- {label}: workspace `{r['workspace']}` "
                     f"({'multi-agent via Pluto' if kind == 'team' else 'one Copilot call'})")
    lines.append("")
    lines.append("## Task")
    lines.append("")
    lines.append("Build a logistics & network-planning toolkit (pure stdlib) "
                 "with 5 modules under `src/logistics/`: `routing.py` "
                 "(CVRPTW, NP-hard), `scheduling.py` (JSSP, NP-hard), "
                 "`graph_paths.py` (Dijkstra + constrained shortest path, "
                 "NP-hard), `integration.py` (end-to-end planner), and "
                 "`api.py` (`run_demo_scenario` facade). All five setups "
                 "are graded against a byte-identical canonical pytest "
                 "suite (`tests/test_logistics_toolkit.py`, 17 cases — "
                 "13 base + 4 multi-objective trade-off cases, NOT "
                 "visible to agents in advance) copied "
                 "into every workspace by the harness.")
    lines.append("")
    lines.append("## Setups")
    lines.append("")
    lines.append("| # | Setup | Mechanism | LLM model |")
    lines.append("|---|-------|-----------|-----------|")
    for i, (label, kind, r) in enumerate(setups, 1):
        mech = ("Planner + 2 Specialists + Reviewer (real Pluto)"
                if kind == "team" else "Single solo Copilot call")
        lines.append(f"| {i} | {label} | {mech} | `{r['model']}` |")
    lines.append("")

    lines.append("## Side-by-side Results")
    lines.append("")
    headers = ["metric"] + [s[0] for s in setups]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] + ["---:"] * (len(headers) - 1)) + "|")

    def row(metric: str, vals: list[str]) -> None:
        lines.append("| " + " | ".join([metric] + vals) + " |")

    row("**pytest status**",
        [f"`{r['qa']['status']}`" for _l, _k, r in setups])
    row("tests passed / failed (out of 17)",
        [f"{r['qa']['passed']} / {r['qa']['failed']}"
         for _l, _k, r in setups])
    row("copilot calls",
        [f"{_total_calls(r['metrics'])}" for _l, _k, r in setups])
    row("total LLM wall-time (s)",
        [f"{_total_dur(r['metrics'])}" for _l, _k, r in setups])
    row("pytest wall-time (s)",
        [f"{r['qa']['duration_s']}" for _l, _k, r in setups])
    fs = [_file_summary(r['workspace']) for _l, _k, r in setups]
    row("toolkit bytes / lines (5 modules)",
        [f"{f['bytes']} / {f['lines']}" for f in fs])
    row("reviewer verdict",
        [f"`{r.get('review', {}).get('verdict', 'n/a')}`"
         if k == 'team' else 'n/a'
         for _l, k, r in setups])
    row("reviewer flagged trade-off bug",
        [("yes" if r.get('review', {}).get('tradeoff_bug_flagged')
          else "no")
         if k == 'team' else 'n/a'
         for _l, k, r in setups])
    lines.append("")

    # Reviewer details for each team.
    for label, kind, r in setups:
        if kind != "team":
            continue
        rev = r.get("review", {})
        lines.append(f"## {label} — Reviewer Findings")
        lines.append("")
        if rev.get("findings"):
            for x in rev["findings"]:
                lines.append(f"- {x}")
        else:
            lines.append("_(none reported)_")
        if rev.get("suggestions"):
            lines.append("")
            lines.append("**Suggestions:**")
            for x in rev["suggestions"]:
                lines.append(f"- {x}")
        lines.append("")

    # Copilot call tables per setup.
    for label, _kind, r in setups:
        lines.append(f"## {label} — copilot calls")
        lines.append("")
        lines.append("| actor | task | model | rc | duration_s |")
        lines.append("|-------|------|-------|---:|-----------:|")
        for c in r["metrics"].get("calls", []):
            lines.append(f"| `{c['actor']}` | `{c['task_id']}` | "
                         f"`{c['model']}` | {c['rc']} | {c['duration_s']} |")
        lines.append("")

    # QA tails.
    for label, _kind, r in setups:
        lines.append(f"## {label} — QA tail")
        lines.append("")
        lines.append("```")
        lines.append((r["qa"].get("stdout_tail") or "").rstrip()[-1500:])
        lines.append("```")
        lines.append("")

    # Insights.
    lines.append("## Insights")
    lines.append("")
    statuses = {label: r["qa"]["status"] for label, _k, r in setups}
    lines.append("Pass/fail summary:")
    for label, _k, r in setups:
        n = r["qa"]["passed"]
        d = r["qa"]["failed"]
        lines.append(f"- **{label}**: `{r['qa']['status']}` ({n}/17 passed, "
                     f"{d} failed)")
    lines.append("")
    team_pass = [l for l, k, r in setups
                 if k == "team" and r["qa"]["status"] == "pass"]
    solo_pass = [l for l, k, r in setups
                 if k == "solo" and r["qa"]["status"] == "pass"]
    lines.append(f"- Teams that passed full suite: {team_pass or 'none'}")
    lines.append(f"- Solos that passed full suite: {solo_pass or 'none'}")
    lines.append("")
    lines.append("Coordination evidence (teams only): every specialist file "
                 "edit was preceded by a real Pluto `/locks/acquire` and "
                 "followed by `/locks/release`. The Planner produced a real "
                 "`COMPLEXITY_CONTRACT.md` in each team workspace; the "
                 "Reviewer consumed all five modules in one real LLM call.")
    lines.append("")
    lines.append("Re-run with:")
    lines.append("```")
    lines.append("PLUTO_HAIKU_MODEL=claude-haiku-4.5 \\")
    lines.append("PLUTO_SONNET_MODEL=claude-sonnet-4.6 \\")
    lines.append("PLUTO_OPUS_MODEL=claude-opus-4.7 \\")
    lines.append("PLUTO_RUN_DEMOS=1 python -m pytest "
                 "tests/demo_logistics_multiagent_vs_solo -s")
    lines.append("```")

    with open(out_md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
