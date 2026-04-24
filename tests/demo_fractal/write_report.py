"""Render a markdown report from the demo trace JSON."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def render(trace_path: str, out_md: str) -> None:
    with open(trace_path) as f:
        data = json.load(f)
    trace = data["trace"]
    result = data["result"]
    tasks = data["tasks"]
    workspace = data["workspace"]

    lines: list[str] = []
    lines.append("# Demo: Fractal Collaboration (real Pluto + real Copilot)")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append("## Setup")
    lines.append("")
    pluto_http_port = data.get("pluto_http_port", 9201)
    lines.append(f"- Pluto server: real Erlang server on `127.0.0.1:{pluto_http_port}` (HTTP)")
    lines.append(f"- Workspace:    `{workspace}`")
    lines.append("- Roles loaded from `library/roles/`:")
    lines.append("  - **orchestrator-1** (Python harness driving the protocol)")
    lines.append("  - **specialist-1**   (real `copilot -p ... --model <default>`)")
    lines.append("  - **reviewer-1**     (deterministic static checks)")
    lines.append("  - **qa-1**           (real `pytest` runner)")
    lines.append("")
    lines.append("## Task Decomposition")
    lines.append("")
    for t in tasks:
        lines.append(f"### `{t['task_id']}` — {t['title']}")
        lines.append(f"- **owner:** `{t['owner']}`  ")
        lines.append(f"- **type:**  `{t['type']}`  ")
        lines.append(f"- **dependencies:** `{t.get('dependencies') or 'none'}`  ")
        lines.append(f"- **files:** `{t['files']}`  ")
        lines.append(f"- **definition_of_done:** {t['definition_of_done']}")
        lines.append(f"- **verification_hint:** `{t['verification_hint']}`")
        lines.append("")

    lines.append("## Final State")
    lines.append("")
    lines.append("| task_id | state |")
    lines.append("|---------|-------|")
    for tid, s in result["states"].items():
        lines.append(f"| `{tid}` | `{s}` |")
    lines.append("")

    if result.get("qa"):
        qa = result["qa"]
        lines.append("## QA Result")
        lines.append("")
        lines.append(f"- **status:** `{qa.get('status')}`")
        if qa.get("metrics"):
            lines.append(f"- **metrics:**")
            for k, v in qa["metrics"].items():
                lines.append(f"  - `{k}`: `{v}`")
        if qa.get("failed_checks"):
            lines.append("- **failed_checks:**")
            for fc in qa["failed_checks"]:
                lines.append(f"  - `{fc.get('name')}`")
        lines.append("")

    lines.append("## Message Trace (chronological, filtered)")
    lines.append("")
    lines.append("_Filtering: payload_type=None and duplicate consecutive recv events are suppressed; capped at 200 rows._")
    lines.append("")
    lines.append("| t (s) | actor | kind | summary |")
    lines.append("|------:|-------|------|---------|")
    rendered = 0
    last_key = None
    for e in trace:
        # Suppress noise: empty-payload recvs and consecutive duplicates.
        if e.get("kind") == "recv" and not e.get("payload_type"):
            continue
        key = (e.get("actor"), e.get("kind"), e.get("payload_type"),
               e.get("to"), e.get("frm"), e.get("op"), e.get("resource"))
        if key == last_key and e.get("kind") in ("recv", "send"):
            continue
        last_key = key
        summary = ""
        if e["kind"] == "send":
            summary = f"→ **{e.get('to')}** type=`{e.get('payload_type')}`"
        elif e["kind"] == "recv":
            summary = f"← **{e.get('frm')}** type=`{e.get('payload_type')}`"
        elif e["kind"] == "lock":
            summary = f"`{e.get('op')}` {e.get('resource', '')}"
        elif e["kind"] == "release":
            summary = f"lock_ref=`{e.get('lock_ref')}`"
        elif e["kind"] == "shell":
            summary = (f"`{e.get('cmd', e.get('op', ''))}`"
                       + (f" rc={e['rc']}" if "rc" in e else ""))
        elif e["kind"] == "note":
            summary = f"`{e.get('event', '')}` {e.get('err', '')}"
        lines.append(f"| {e['t']:>5.2f} | `{e['actor']}` | `{e['kind']}` | {summary} |")
        rendered += 1
        if rendered >= 200:
            lines.append(f"| ... | ... | ... | _trace truncated; full data in trace.json ({len(trace)} total events)_ |")
            break

    lines.append("## Notes")
    lines.append("")
    lines.append("- Every file mutation by the Specialist was preceded by a real")
    lines.append("  Pluto `write` lock acquisition over `/locks/acquire` and")
    lines.append("  followed by `/locks/release`.")
    lines.append("- The Orchestrator never wrote files itself — it only published")
    lines.append("  the task list, dispatched `task_assigned`, and consumed")
    lines.append("  `task_result` / `review` / `qa_result`.")
    lines.append("- Re-run with: `PLUTO_RUN_DEMOS=1 python -m pytest "
                 "tests/demo_fractal -s`")

    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
