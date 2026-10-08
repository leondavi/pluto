#!/usr/bin/env python3
"""Print a timeline of tool calls, results and text from one transcript.

    python3 experiments/subagents_vs_pluto/trace.py RUN_DIR/w1.jsonl [--width 200]
"""

import argparse
import json


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("transcript")
    p.add_argument("--width", type=int, default=200)
    args = p.parse_args()
    w = args.width
    t0 = None
    with open(args.transcript, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            ts = d.get("_recv_ts")
            if ts is None:
                continue
            t0 = t0 or ts
            if d.get("type") not in ("assistant", "user"):
                continue
            content = (d.get("message") or {}).get("content")
            who = "sub " if d.get("parent_tool_use_id") else ""
            for c in content if isinstance(content, list) else []:
                kind = c.get("type")
                if kind == "tool_use":
                    print(f"{ts - t0:7.1f} {who}CALL {c['name']} {json.dumps(c['input'])[:w]}")
                elif kind == "tool_result":
                    print(f"{ts - t0:7.1f} {who}  -> {json.dumps(c.get('content'))[:w]}")
                elif kind == "text" and d["type"] == "assistant":
                    print(f"{ts - t0:7.1f} {who}SAY {c['text'][:w]!r}")


if __name__ == "__main__":
    main()
