"""Render a markdown comparison report from the fractal_compare trace JSON."""

from __future__ import annotations

import json
import time


def render(trace_path: str, out_md: str) -> None:
    with open(trace_path) as f:
        data = json.load(f)

    haiku = data["haiku"]
    sonnet = data["sonnet"]
    models = data.get("models", {})
    port = data.get("pluto_http_port", 9201)

    def total_dur(team: dict) -> float:
        return round(
            sum(c.get("duration_s", 0.0) for c in team["metrics"].get("calls", [])),
            1,
        )

    def total_calls(team: dict) -> int:
        return len(team["metrics"].get("calls", []))

    def tasks_completed(team: dict) -> str:
        n = sum(1 for s in team["states"].values() if s == "completed")
        return f"{n} / {len(team['states'])}"

    def any_needs_changes(team: dict) -> bool:
        return any(
            r.get("status") == "needs_changes"
            for r in team["reviews"].values()
        )

    lines: list[str] = []
    lines.append("# Demo: Fractal Compare — Haiku Team vs Sonnet Team")
    lines.append("")
    lines.append(
        f"_Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_"
    )
    lines.append("")
    lines.append(
        f"- Pluto server: `127.0.0.1:{port}` (HTTP, real Erlang node)"
    )
    lines.append(f"- Haiku model:  `{models.get('haiku', 'claude-haiku-4.5')}`")
    lines.append(
        f"- Sonnet model: `{models.get('sonnet', 'claude-sonnet-4.5')}`"
    )
    lines.append(f"- Haiku workspace:  `{haiku['workspace']}`")
    lines.append(f"- Sonnet workspace: `{sonnet['workspace']}`")
    lines.append("")

    lines.append("## Task Decomposition")
    lines.append("")
    lines.append("| task | file | depends on |")
    lines.append("|------|------|-----------|")
    lines.append("| `t-001` | `src/fractals/mandelbrot.py` | — |")
    lines.append("| `t-002` | `src/fractals/julia.py` | — |")
    lines.append("| `t-003` | `src/fractals/stats.py` | `t-001`, `t-002` |")
    lines.append(
        "| `t-004` | `scripts/render_fractals.py` | `t-001`, `t-002`, `t-003` |"
    )
    lines.append(
        "| `t-005` | `tests/test_fractals.py` | `t-001`, `t-002`, `t-003` |"
    )
    lines.append("")
    lines.append(
        "`t-001` and `t-002` have no dependencies and are dispatched in the "
        "same Orchestrator tick (parallel-eligible)."
    )
    lines.append("")

    lines.append("## Side-by-side Results")
    lines.append("")
    lines.append("| metric | Haiku team | Sonnet team |")
    lines.append("|--------|----------:|------------:|")

    def row(label: str, h: str, s: str) -> None:
        lines.append(f"| {label} | {h} | {s} |")

    row(
        "**pytest status**",
        f"`{haiku['qa']['status']}`",
        f"`{sonnet['qa']['status']}`",
    )
    row(
        "canonical tests passed / failed (out of 10)",
        f"{haiku['qa']['passed']} / {haiku['qa']['failed']}",
        f"{sonnet['qa']['passed']} / {sonnet['qa']['failed']}",
    )
    row("tasks completed", tasks_completed(haiku), tasks_completed(sonnet))
    row(
        "any reviewer `needs_changes`",
        "yes" if any_needs_changes(haiku) else "no",
        "yes" if any_needs_changes(sonnet) else "no",
    )
    row("total Copilot calls", str(total_calls(haiku)), str(total_calls(sonnet)))
    row(
        "total LLM wall-time (s)", str(total_dur(haiku)), str(total_dur(sonnet))
    )
    row(
        "impl wall-time (s)",
        str(haiku.get("impl_duration_s", "n/a")),
        str(sonnet.get("impl_duration_s", "n/a")),
    )
    row(
        "render script rc",
        str(haiku["qa"].get("render_rc", "n/a")),
        str(sonnet["qa"].get("render_rc", "n/a")),
    )
    lines.append("")

    # Output stats from render script (if available).
    for label, team in [("Haiku", haiku), ("Sonnet", sonnet)]:
        ost = team["qa"].get("output_stats", {})
        if ost:
            lines.append(f"### {label} team — rendered fractal stats")
            lines.append("")
            lines.append("| stat | value |")
            lines.append("|------|------:|")
            for k, v in ost.items():
                fv = f"`{round(float(v), 4)}`" if isinstance(v, (int, float)) else f"`{v}`"
                lines.append(f"| `{k}` | {fv} |")
            lines.append("")

    # Per-team task state + reviewer verdict table.
    for label, team in [("Haiku", haiku), ("Sonnet", sonnet)]:
        lines.append(f"## {label} Team — Task States")
        lines.append("")
        lines.append("| task | state | reviewer verdict |")
        lines.append("|------|-------|-----------------|")
        for tid, state in sorted(team["states"].items()):
            rev = team["reviews"].get(tid, {})
            verdict = rev.get("status", "—")
            lines.append(f"| `{tid}` | `{state}` | `{verdict}` |")
        lines.append("")

    # Reviewer findings per team per task.
    for label, team in [("Haiku", haiku), ("Sonnet", sonnet)]:
        if not team["reviews"]:
            continue
        lines.append(f"## {label} Team — Reviewer Findings")
        lines.append("")
        for tid, rev in sorted(team["reviews"].items()):
            findings = rev.get("findings") or []
            if not findings:
                continue
            lines.append(f"**{tid} ({rev.get('status', '?')}):**")
            for finding in findings[:5]:
                if isinstance(finding, dict):
                    lines.append(f"- {finding.get('message', str(finding))}")
                else:
                    lines.append(f"- {finding}")
        lines.append("")

    # Copilot call tables.
    for label, team in [("Haiku", haiku), ("Sonnet", sonnet)]:
        lines.append(f"## {label} Team — Copilot Calls")
        lines.append("")
        lines.append("| actor | task | model | rc | duration_s |")
        lines.append("|-------|------|-------|---:|-----------:|")
        for c in team["metrics"].get("calls", []):
            lines.append(
                f"| `{c['actor']}` | `{c['task_id']}` "
                f"| `{c['model']}` | {c['rc']} | {c['duration_s']} |"
            )
        lines.append("")

    # QA output.
    for label, team in [("Haiku", haiku), ("Sonnet", sonnet)]:
        lines.append(f"## {label} Team — QA Output")
        lines.append("")
        lines.append("```")
        lines.append((team["qa"].get("stdout_tail") or "").rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## Re-run")
    lines.append("")
    lines.append(
        "```bash\n"
        "PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_fractal_compare -s\n"
        "```"
    )

    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
