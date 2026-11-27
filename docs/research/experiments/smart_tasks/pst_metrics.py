"""Metrics and leak checks over ``claude -p --output-format stream-json``.

Facts about the format (Claude Code 2.1.x): one assistant message may be
emitted as several events with the same id and usage; totals come from
``modelUsage`` of the last ``result`` event; a killed process has no result
event, so its cost is estimated (``cost_est_usd``) and ``cost_usd`` is None.

Messages of a sub-agent carry ``parent_tool_use_id`` (the Agent tool use that
started it). Their turns, tool calls, result bytes and tool inputs are counted;
``sub`` holds their share. ``modelUsage`` is documented as the session total. In case
it did not include sub-agents, ``usage_missing`` (cache-read tokens seen in the
messages but absent from modelUsage) would be large: then the estimate adds
the sub-agents' messages.
"""

import json
import os
import re

KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")
MU = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
      "cache_read_input_tokens": "cacheReadInputTokens",
      "cache_creation_input_tokens": "cacheCreationInputTokens"}


# USD per million tokens, reproduces the reported cost of all 24 sessions of the v2
# pilot to four digits (claude-sonnet-5-5 at list price, 1-hour cache writes).
PRICE = {"input_tokens": 2.0, "output_tokens": 10.0, "cache_read_input_tokens": 0.1,
         "cache_creation_input_tokens": 4.0}
# Output tokens of a session from its transcript (usage of single messages is not
# final): thinking estimate, characters of text and tool input, messages. Fitted on the
# same sessions; leave-one-out error of the cost: median 1.4%, worst 7.7%.
OUT_FIT = (0.82, 0.27, 174.0)


#: Every agent and sub-agent must be this model family; anything else fails the run.
REQUIRED_MODEL = "sonnet"

def price(tokens):
    return sum(PRICE[k] * tokens[k] for k in KEYS) / 1e6


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


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict))


def blocks(ev):
    msg = ev.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def parse_agent(events):
    m = {"tokens": dict.fromkeys(KEYS, 0), "cost_usd": None, "turns": 0, "tool_calls": {},
         "tool_result_bytes": 0, "tool_result_bytes_by_tool": {}, "max_tool_result_bytes": 0,
         "tool_errors": 0, "pst_calls": 0, "permission_denials": 0, "mcp_status": {}, "model": None,
         "subtype": None, "has_result": False, "is_error": True, "duration_ms": None,
         "cost_est_usd": 0.0, "usage_missing": 0, "agent_calls": 0, "subagent_stats": {},
         "sub": {"turns": 0, "tool_calls": 0, "tool_result_bytes": 0, "models": [],
                 "agents": 0, "cache_read_input_tokens": 0}}
    seen_msg, uses, seen_res, denied = {}, {}, set(), 0
    sub_msg, parents, chars, sub_chars, think = {}, set(), 0, 0, 0
    result, duration = None, 0
    for ev in events:
        t, parent = ev.get("type"), ev.get("parent_tool_use_id")
        if t == "system" and ev.get("subtype") == "thinking_tokens":
            think += int(ev.get("estimated_tokens_delta") or 0)
        if t == "system" and ev.get("subtype") == "init" and m["model"] is None:
            m["model"] = ev.get("model")
            m["mcp_status"] = {s.get("name"): s.get("status") for s in ev.get("mcp_servers") or []
                               if isinstance(s, dict)}
        elif t == "result":
            result = ev            # totals are cumulative, durations are not
            duration += int(ev.get("duration_ms") or 0)
        elif t == "assistant":
            msg = ev.get("message") or {}
            if msg.get("id"):
                seen_msg[msg["id"]] = msg.get("usage") or {}
                if parent:
                    sub_msg[msg["id"]] = msg.get("usage") or {}
                    parents.add(parent)
                    if msg.get("model") and msg["model"] not in m["sub"]["models"]:
                        m["sub"]["models"].append(msg["model"])
            for b in blocks(ev):
                n = len(str(b.get("text") or "")) if b.get("type") == "text" else 0
                if b.get("type") == "tool_use" and b.get("id") not in uses:
                    n = len(json.dumps(b.get("input") or {}))
                    m["sub"]["tool_calls"] += bool(parent)
                    m["agent_calls"] += b.get("name") in ("Agent", "Task")
                chars, sub_chars = chars + n, sub_chars + (n if parent else 0)
                if b.get("type") == "tool_use" and b.get("id") not in uses:
                    uses[b.get("id")] = b.get("name") or "?"
                    m["tool_calls"][uses[b["id"]]] = m["tool_calls"].get(uses[b["id"]], 0) + 1
                    m["pst_calls"] += len(re.findall(r"\bpst\s+[a-z]", (str(
                        (b.get("input") or {}).get("command") or ""))))
        elif t == "user":
            for b in blocks(ev):
                if b.get("type") != "tool_result" or b.get("tool_use_id") in seen_res:
                    continue
                seen_res.add(b.get("tool_use_id"))
                n = content_bytes(b.get("content"))
                tool = uses.get(b.get("tool_use_id"), "?")
                m["tool_result_bytes"] += n
                m["sub"]["tool_result_bytes"] += n if parent else 0
                by = m["tool_result_bytes_by_tool"]
                by[tool] = by.get(tool, 0) + n
                m["max_tool_result_bytes"] = max(m["max_tool_result_bytes"], n)
                if tool.startswith("mcp__") and not b.get("is_error") and re.match(
                        r'\{"error":', _text(b.get("content"))):
                    m["tool_errors"] += 1
                if b.get("is_error"):
                    m["tool_errors"] += 1
                    text = json.dumps(b.get("content"))
                    denied += bool(re.search(r"requested permissions?|permission to use", text, re.I))
    if result:
        m.update(has_result=True, subtype=result.get("subtype"),
                 is_error=bool(result.get("is_error")), duration_ms=duration,
                 cost_usd=result.get("total_cost_usd"))
        pd = result.get("permission_denials")
        denied = max(denied, len(pd) if isinstance(pd, list) else 0)
    m["permission_denials"] = denied
    m["turns"] = len(seen_msg) or (result or {}).get("num_turns") or 0
    mu = (result or {}).get("modelUsage") or {}
    m["models"] = sorted({*mu, *m["sub"]["models"], *([m["model"]] if m["model"] else [])})
    src = list(mu.values()) if mu else list(seen_msg.values())
    for u in src:
        for k in KEYS:
            m["tokens"][k] += int(u.get(MU[k] if mu else k) or 0)
    m["subagent_stats"] = (result or {}).get("subagent_stats") or {}
    seen = {k: sum(int(u.get(k) or 0) for u in seen_msg.values()) for k in KEYS}
    part = {k: sum(int(u.get(k) or 0) for u in sub_msg.values()) for k in KEYS}
    m["sub"].update(turns=len(sub_msg), agents=len(parents),
                    cache_read_input_tokens=part["cache_read_input_tokens"])
    k = "cache_read_input_tokens"
    m["usage_missing"] = max(0, seen[k] - m["tokens"][k])
    seen["output_tokens"] = int(OUT_FIT[0] * think + OUT_FIT[1] * chars
                                + OUT_FIT[2] * len(seen_msg))
    if m["cost_usd"] is None:
        m["tokens"] = dict(m["tokens"], output_tokens=max(m["tokens"]["output_tokens"],
                                                          seen["output_tokens"]))
        m["cost_est_usd"] = price(seen)
    else:
        m["cost_est_usd"] = float(m["cost_usd"])
        if m["usage_missing"] > 0.5 * part[k] > 0:      # modelUsage left the sub-agents out
            part["output_tokens"] = int(OUT_FIT[1] * sub_chars + OUT_FIT[2] * len(sub_msg))
            m["cost_est_usd"] += price(part)
    m["cost_est_usd"] = round(m["cost_est_usd"], 6)
    return m


def failures(m, status, condition):
    """Protocol failures of one agent as a list of short codes.

    ``status``: {"rc", "timed_out", "expected_cutoff"} from the runner.
    """
    out = []
    if status.get("timed_out"):
        out.append("cutoff" if status.get("expected_cutoff") else "timeout")
    if status.get("crashed"):
        return ["crash_injected"]
    if not m["has_result"] and not status.get("timed_out"):
        out.append("no_result_event")
    if any(REQUIRED_MODEL not in x for x in m.get("models", ())):
        out.append("wrong_model")
    if m["subtype"] and m["subtype"] != "success":
        out.append(m["subtype"])
    elif m["has_result"] and m["is_error"]:
        out.append("result_error")
    if m["permission_denials"]:
        out.append("permission_denied")
    if set(m["sub"]["models"]) - {m["model"]}:
        out.append("subagent_model")
    if condition != "native_sub" and m["agent_calls"]:
        out.append("subagent_call")
    if (m["subagent_stats"].get("max_depth") or 0) > 1:
        out.append("subagent_nested")
    if m["usage_missing"] > 0.5 * m["sub"]["cache_read_input_tokens"] > 0:
        out.append("subagent_usage_missing")
    if condition == "smarttasks" or condition.startswith("pst_"):   # and its variants
        st = m["mcp_status"].get("smarttasks")
        if st != "connected" and st != "pending":
            out.append("mcp_not_connected")
        if not m["pst_calls"] and not any(
                k.startswith("mcp__smarttasks__") for k in m["tool_calls"]):
            out.append("smarttasks_unused")
    return out


def leak_patterns(ws, out_dir):
    """(code, regex) pairs matched against every tool input."""
    out = re.escape(out_dir.rstrip("/"))
    rel = ws.rstrip("/")[len(out_dir.rstrip("/")):]
    # a job's own output in this run's SmartTasks home is legitimate to read
    run = re.escape(os.path.dirname(rel))
    own = f"(?:{re.escape(rel)}|{run}/pluto/home/jobs|{run}/?(?![\\w/]))"
    return [
        ("harness", re.compile(r"experiments/smart_tasks|pluto-v0\.6\.0|exp[1-5]_\w+\.py|wl_\w+\.py\b")),
        ("other_run", re.compile(out + r"(?!" + own + r")(?:/|\b)")),
        ("truth", re.compile(r"_private|truth\.json")),
        ("decompile", re.compile(r"\bmarshal\b|\bdis\.dis\b|-m dis\b|uncompyle|decompyle|pycdc|"
                                 r"\bunseal\b")),
        ("program_internals", re.compile(r"bin/img|\.img\b|\b(?:strings|xxd|hexdump|od|cp|cat)\b[^;|&<>\n]*\.pyc|"
                                         r"\.runs\b")),
        ("transcripts", re.compile(r"\.claude/projects|\blogs/\w+\.jsonl|\bresult\.json\b")),
    ]


def outside_by_parent(command, cwd, ws):
    """(path reached through '..' that lies outside ws or None, new cwd).

    Follows cd in textual order; subshells and variables are not modelled, and
    here-documents are text. A job's own output in this run is fair to read."""
    hit = None
    command = re.sub(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2$", "", command, flags=re.S | re.M)
    jobs = os.path.join(os.path.dirname(ws), "pluto", "home", "jobs")
    for mt in re.finditer(r"(?:^|[;&|(\n])\s*cd\s+([^\s;&|)]+)|(?<![\w./*-])((?:~?/)?(?:[\w.*-]+/)*\.\.(?:/[\w.*-]*)*)(?=[\s;&|)'\"]|$)",
                          command):
        target = (mt.group(1) or mt.group(2)).strip("'\"")
        if "$" in target:
            continue
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))
        if ".." in target.split("/") and path != ws and \
                not path.startswith((ws + "/", jobs)):
            hit = hit or mt
        if mt.group(1):
            cwd = path
    return hit, cwd


def find_leaks(events, ws, out_dir, denied=()):
    """[(code, tool, excerpt)] for tool INPUTS that touch forbidden material.

    Sub-agent tool calls are checked like all others, with a working directory of
    their own. The prompt given to a sub-agent is not matched (it may repeat the
    rules, which name forbidden paths); what the sub-agent then does is."""
    pats, hits, seen = leak_patterns(ws, out_dir), [], set()
    refused = {b.get("tool_use_id") for ev in events if ev.get("type") == "user"
               for b in blocks(ev) if b.get("is_error")}
    ws = os.path.normpath(ws)
    cwds = {}
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in blocks(ev):
            if b.get("type") != "tool_use" or b.get("id") in seen:
                continue
            seen.add(b.get("id"))
            arg = b.get("input") or {}
            who, name = ev.get("parent_tool_use_id"), b.get("name")
            if name in ("Agent", "Task"):
                if arg.get("isolation"):
                    hits.append(("isolation", name, str(arg["isolation"])))
                arg = {}
            if name in denied and b.get("id") not in refused:
                hits.append(("forbidden_tool", name, json.dumps(arg)[:120]))
            # real line breaks, so that a pattern stays within one line
            text = json.dumps(arg, ensure_ascii=False).replace("\\n", "\n")
            cmds = [arg.get("command")] + list(arg.get("commands") or [])
            for cmd in (c for c in cmds if isinstance(c, str)):
                mt, after = outside_by_parent(cmd, cwds.get(who, ws), ws)
                if b.get("name") == "Bash":
                    cwds[who] = after
                if mt:
                    hits.append(("parent_dir", b.get("name"),
                                 cmd[max(0, mt.start() - 60):mt.end() + 80]))
            for code, rx in pats:
                mt = rx.search(text)
                if mt:
                    hits.append((code, b.get("name"), text[max(0, mt.start() - 60):mt.end() + 80]))
    return hits


def merge(per_agent):
    """Team totals. cost_complete is False if any agent has no cost."""
    tot = {"tokens": dict.fromkeys(KEYS, 0), "cost_usd": 0.0, "turns": 0, "tool_calls": {},
           "tool_result_bytes": 0, "max_tool_result_bytes": 0, "tool_errors": 0, "pst_calls": 0,
           "permission_denials": 0, "cost_complete": True, "cost_est_usd": 0.0,
           "sub": {"agents": 0, "turns": 0, "tool_calls": 0, "tool_result_bytes": 0}}
    for m in per_agent.values():
        for k in KEYS:
            tot["tokens"][k] += m["tokens"][k]
        for k in ("turns", "tool_result_bytes", "tool_errors", "pst_calls", "permission_denials"):
            tot[k] += m[k]
        tot["cost_est_usd"] = round(tot["cost_est_usd"] + m["cost_est_usd"], 6)
        for k in tot["sub"]:
            tot["sub"][k] += m["sub"][k]
        tot["max_tool_result_bytes"] = max(tot["max_tool_result_bytes"], m["max_tool_result_bytes"])
        for k, n in m["tool_calls"].items():
            tot["tool_calls"][k] = tot["tool_calls"].get(k, 0) + n
        if m["cost_usd"] is None:
            tot["cost_complete"] = False
        else:
            tot["cost_usd"] += float(m["cost_usd"])
    tot["cost_usd"] = round(tot["cost_usd"], 6)
    return tot
