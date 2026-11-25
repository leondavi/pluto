"""Metrics and leak checks over ``claude -p --output-format stream-json``.

Facts about the format (Claude Code 2.1.x): one assistant message may be
emitted as several events with the same id and usage; totals come from
``modelUsage`` of the last ``result`` event; a killed process has no result
event, so its cost is unknown and its tokens are a lower bound.
"""

import json
import os
import re

KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")
MU = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
      "cache_read_input_tokens": "cacheReadInputTokens",
      "cache_creation_input_tokens": "cacheCreationInputTokens"}


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    except OSError:
        pass
    return out


def content_bytes(content):
    """UTF-8 bytes of the text of one tool_result's content."""
    if isinstance(content, str):
        return len(content.encode("utf-8", "replace"))
    if isinstance(content, dict):
        content = [content]
    n = 0
    for b in content or []:
        if isinstance(b, str):
            n += len(b.encode("utf-8", "replace"))
        elif isinstance(b, dict) and b.get("type") == "text":
            n += len(str(b.get("text") or "").encode("utf-8", "replace"))
        elif isinstance(b, dict) and b.get("type") == "image":
            pass                                    # not text
        elif isinstance(b, dict):
            n += len(json.dumps(b))
    return n


def blocks(ev):
    msg = ev.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def parse_agent(events):
    m = {"tokens": dict.fromkeys(KEYS, 0), "cost_usd": None, "turns": 0, "tool_calls": {},
         "tool_result_bytes": 0, "tool_result_bytes_by_tool": {}, "max_tool_result_bytes": 0,
         "tool_errors": 0, "permission_denials": 0, "mcp_status": {}, "model": None,
         "subtype": None, "has_result": False, "is_error": True, "duration_ms": None}
    seen_msg, uses, seen_res, denied = {}, {}, set(), 0
    result = None
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            m["model"] = ev.get("model")
            m["mcp_status"] = {s.get("name"): s.get("status") for s in ev.get("mcp_servers") or []
                               if isinstance(s, dict)}
        elif t == "result":
            result = ev
        elif t == "assistant":
            msg = ev.get("message") or {}
            if msg.get("id"):
                seen_msg[msg["id"]] = msg.get("usage") or {}
            for b in blocks(ev):
                if b.get("type") == "tool_use" and b.get("id") not in uses:
                    uses[b.get("id")] = b.get("name") or "?"
                    m["tool_calls"][uses[b["id"]]] = m["tool_calls"].get(uses[b["id"]], 0) + 1
        elif t == "user":
            for b in blocks(ev):
                if b.get("type") != "tool_result" or b.get("tool_use_id") in seen_res:
                    continue
                seen_res.add(b.get("tool_use_id"))
                n = content_bytes(b.get("content"))
                tool = uses.get(b.get("tool_use_id"), "?")
                m["tool_result_bytes"] += n
                by = m["tool_result_bytes_by_tool"]
                by[tool] = by.get(tool, 0) + n
                m["max_tool_result_bytes"] = max(m["max_tool_result_bytes"], n)
                if b.get("is_error"):
                    m["tool_errors"] += 1
                    text = json.dumps(b.get("content"))
                    denied += bool(re.search(r"requested permissions?|permission to use", text, re.I))
    if result:
        m.update(has_result=True, subtype=result.get("subtype"),
                 is_error=bool(result.get("is_error")), duration_ms=result.get("duration_ms"),
                 cost_usd=result.get("total_cost_usd"))
        pd = result.get("permission_denials")
        denied = max(denied, len(pd) if isinstance(pd, list) else 0)
    m["permission_denials"] = denied
    m["turns"] = len(seen_msg) or (result or {}).get("num_turns") or 0
    mu = (result or {}).get("modelUsage") or {}
    src = list(mu.values()) if mu else list(seen_msg.values())
    for u in src:
        for k in KEYS:
            m["tokens"][k] += int(u.get(MU[k] if mu else k) or 0)
    return m


def failures(m, status, condition):
    """Protocol failures of one agent as a list of short codes.

    ``status``: {"rc", "timed_out", "expected_cutoff"} from the runner.
    """
    out = []
    if status.get("timed_out"):
        out.append("cutoff" if status.get("expected_cutoff") else "timeout")
    if not m["has_result"] and not status.get("timed_out"):
        out.append("no_result_event")
    if m["subtype"] and m["subtype"] != "success":
        out.append(m["subtype"])
    elif m["has_result"] and m["is_error"]:
        out.append("result_error")
    if m["permission_denials"]:
        out.append("permission_denied")
    if condition == "smartstreams":
        st = m["mcp_status"].get("smartstreams")
        if st != "connected" and st != "pending":
            out.append("mcp_not_connected")
        if not any(k.startswith("mcp__smartstreams__") for k in m["tool_calls"]):
            out.append("smartstreams_unused")
    return out


def leak_patterns(ws, out_dir):
    """(code, regex) pairs matched against every tool input."""
    out = re.escape(out_dir.rstrip("/"))
    rel = ws.rstrip("/")[len(out_dir.rstrip("/")):]
    # a job's own output in this run's SmartStreams home is legitimate to read
    own = f"(?:{re.escape(rel)}|{re.escape(os.path.dirname(rel))}/pluto/home/jobs)"
    return [
        ("harness", re.compile(r"experiments/smart_streams|pluto-v0\.6\.0|exp[123]_\w+\.py|wl_\w+\.py\b")),
        ("other_run", re.compile(out + r"(?!" + own + r")(?:/|\b)")),
        ("parent_dir", re.compile(r"\.\./\.\.")),
        ("truth", re.compile(r"_private|truth\.json")),
        ("decompile", re.compile(r"\bmarshal\b|\bdis\.dis\b|-m dis\b|uncompyle|decompyle|pycdc|"
                                 r"\bunseal\b")),
        ("program_internals", re.compile(r"bin/img|\.img\b|\b(?:strings|xxd|hexdump|od|cp|cat)\b[^;|&]*\.pyc|"
                                         r"\.runs\b")),
        ("transcripts", re.compile(r"\.claude/projects")),
    ]


def find_leaks(events, ws, out_dir):
    """[(code, tool, excerpt)] for tool INPUTS that touch forbidden material."""
    pats, hits, seen = leak_patterns(ws, out_dir), [], set()
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in blocks(ev):
            if b.get("type") != "tool_use" or b.get("id") in seen:
                continue
            seen.add(b.get("id"))
            text = json.dumps(b.get("input"), ensure_ascii=False)
            for code, rx in pats:
                mt = rx.search(text)
                if mt:
                    hits.append((code, b.get("name"), text[max(0, mt.start() - 60):mt.end() + 80]))
    return hits


def merge(per_agent):
    """Team totals. cost_complete is False if any agent has no cost."""
    tot = {"tokens": dict.fromkeys(KEYS, 0), "cost_usd": 0.0, "turns": 0, "tool_calls": {},
           "tool_result_bytes": 0, "max_tool_result_bytes": 0, "tool_errors": 0,
           "permission_denials": 0, "cost_complete": True}
    for m in per_agent.values():
        for k in KEYS:
            tot["tokens"][k] += m["tokens"][k]
        for k in ("turns", "tool_result_bytes", "tool_errors", "permission_denials"):
            tot[k] += m[k]
        tot["max_tool_result_bytes"] = max(tot["max_tool_result_bytes"], m["max_tool_result_bytes"])
        for k, n in m["tool_calls"].items():
            tot["tool_calls"][k] = tot["tool_calls"].get(k, 0) + n
        if m["cost_usd"] is None:
            tot["cost_complete"] = False
        else:
            tot["cost_usd"] += float(m["cost_usd"])
    tot["cost_usd"] = round(tot["cost_usd"], 6)
    return tot
