"""Parse `claude -p --output-format stream-json --verbose` transcripts.

Facts about the format (verified against Claude Code 2.1.x):
  * An assistant message can be emitted as several events sharing one
    ``message.id`` with identical ``usage``, so usage is de-duplicated by id.
  * Subagent turns carry ``parent_tool_use_id``; orchestrator turns have null.
  * Background subagents cause several ``result`` events per process; the
    last one holds the cumulative ``total_cost_usd`` and ``modelUsage``.
  * Per-message ``usage.output_tokens`` is a stream-start snapshot and badly
    undercounts output, so the orchestrator/subagent split is only reliable
    for input-side (cache read / creation) tokens. Totals always come from
    ``modelUsage``.
The harness adds ``_recv_ts`` (wall clock at line arrival) to every event.
"""

import json

USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")

COORD_TOOLS = ("Agent", "Task", "SendMessage")


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _zero():
    return {k: 0 for k in USAGE_KEYS}


def _add(acc, usage):
    for k in USAGE_KEYS:
        acc[k] += int(usage.get(k) or 0)


def summarize_transcript(events):
    """Return token/tool/cost stats for one claude process."""
    seen_msgs, seen_tools = set(), set()
    by_role = {"orchestrator": _zero(), "subagents": _zero()}
    tools = {}
    last_result = None
    first_ts = last_ts = None
    for ev in events:
        ts = ev.get("_recv_ts")
        if ts is not None:
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts
        if ev.get("type") == "result":
            last_result = ev
            continue
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        role = "subagents" if ev.get("parent_tool_use_id") else "orchestrator"
        mid = msg.get("id")
        if mid and mid not in seen_msgs:
            seen_msgs.add(mid)
            _add(by_role[role], msg.get("usage") or {})
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid in seen_tools:
                    continue
                seen_tools.add(tid)
                key = f"{role}:{block.get('name')}"
                tools[key] = tools.get(key, 0) + 1
    total = _zero()
    model_usage = (last_result or {}).get("modelUsage") or {}
    if model_usage:
        for mu in model_usage.values():
            total["input_tokens"] += int(mu.get("inputTokens") or 0)
            total["output_tokens"] += int(mu.get("outputTokens") or 0)
            total["cache_read_input_tokens"] += int(mu.get("cacheReadInputTokens") or 0)
            total["cache_creation_input_tokens"] += int(mu.get("cacheCreationInputTokens") or 0)
    else:
        for part in by_role.values():
            _add(total, part)
    return {
        "tokens": total,
        "tokens_by_role": by_role,
        "cost_usd": float((last_result or {}).get("total_cost_usd") or 0.0),
        "tools": tools,
        "is_error": bool((last_result or {}).get("is_error")) if last_result else True,
        "terminal_reason": (last_result or {}).get("terminal_reason"),
        "subagent_stats": (last_result or {}).get("subagent_stats"),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def total_tokens(tok):
    return sum(tok.get(k, 0) for k in USAGE_KEYS)


def count_coordination(tools):
    """Tool calls that move information between agents."""
    n = 0
    for key, c in tools.items():
        name = key.split(":", 1)[1]
        if name in COORD_TOOLS or name.startswith("mcp__pluto__"):
            n += c
    return n


def merge_process_stats(per_proc):
    """Combine stats of several processes (Pluto condition) into one row.

    ``per_proc`` maps an agent name to its summary; ``orch`` counts as the
    orchestrator and every other process as a worker.
    """
    tokens, orch_tok, work_tok = _zero(), _zero(), _zero()
    tools, cost, errors = {}, 0.0, []
    for name, s in per_proc.items():
        _add(tokens, s["tokens"])
        _add(orch_tok if name == "orch" else work_tok, s["tokens"])
        cost += s["cost_usd"]
        if s["is_error"]:
            errors.append(name)
        for k, v in s["tools"].items():
            role = "orchestrator" if name == "orch" else "workers"
            key = f"{role}:{k.split(':', 1)[1]}"
            tools[key] = tools.get(key, 0) + v
    return {"tokens": tokens, "tokens_orchestrator": orch_tok, "tokens_workers": work_tok,
            "cost_usd": round(cost, 6), "tools": tools, "errored_processes": errors}
