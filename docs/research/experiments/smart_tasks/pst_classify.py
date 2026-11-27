#!/usr/bin/env python3
"""What agents spend their calls on: deterministic classes of tool calls.

    python3 pst_classify.py OUT_DIR [--sample N --seed S] [--json FILE]

Every tool call gets exactly one class:

* ``process``      start, stop, wait for, list or check processes and jobs, exit
                   codes, pid files, acknowledgements in control/, sleeping
* ``analysis``     read or search program output, and scripts that do
* ``coordination`` read or write board/, wait for other agents' files, job_transfer
* ``answer``       write, merge or check files in answers/ (only listing it is coordination)
* ``other``        docs, directory listings, tool search, to-do lists, sub-agent calls,
                   anything else

Rules. Job tools, Read, Grep, Monitor and the rest are mapped by name and path
(TOOL_CLASS, by_path). A Bash command (or the command of job_start, or a Write/Edit
of a script) is cut into lines and, on one line outside heredocs, into segments at
``;``, ``&&``, ``||``. Each segment votes with its bytes for the first class whose pattern
matches, in the order answer, coordination, process, analysis, other. Segments
that match nothing, cd and mkdir vote for nothing, and the workspace path has no weight. A script that loops
(``while``) and kills is ``process``. Otherwise the heaviest class wins; ties go to the earlier one in
this order. Because scripts that analyse usually end by writing an answer, a script of
over 400 bytes is ``answer`` only if that class has more bytes than analysis.

Attribution. A call's argument bytes are the UTF-8 bytes of its JSON input. The
turn of an assistant message is shared equally by its calls (a message without calls
is ``text``). Output tokens of a session are shared among messages in proportion to
their text and tool input bytes plus 100 (thinking cannot be attributed to a message),
and within a message like its bytes. Token figures per class are therefore estimates.
"""

import argparse
import glob
import json
import os
import random
import re
import sys

import pst_metrics

CLASSES = ("process", "analysis", "coordination", "answer", "other", "text")
ORDER = ("answer", "coordination", "process", "analysis", "other")
TOOL_CLASS = {
    "job_analyze": "analysis", "job_watch": "process", "job_more": "other",
    "job_start": "process", "job_status": "process", "job_list": "process",
    "job_wait": "process", "job_control": "process", "job_input": "process",
    "job_stream": "process", "job_transfer": "coordination", "job_grep": "analysis",
    "job_window": "analysis", "job_next_chunk": "analysis", "job_manifest": "analysis",
    "job_export": "analysis", "job_open": "analysis", "job_review": "analysis",
    "BashOutput": "process", "KillShell": "process", "TaskOutput": "process",
    "TaskStop": "process", "Grep": "analysis", "Glob": "other", "TodoWrite": "other",
    "ToolSearch": "other", "Agent": "other", "Task": "other"}
PATTERNS = {
    "answer": re.compile(r"\banswers\\?/|\bcomb\w*\.py|\bans(?:wers?)?_dir\b|final\.json|\b(?:shard|job|stream)_\{?\w*\}?\.json"),
    "coordination": re.compile(
        r"\bpst\s+give\b|\bboard/|handoff|\bls\b.*\b(?:board|answers)\b|\bwhile\b.*\banswers/"),
    "process": re.compile(
        r"\bpst\s+(?:run|wait|ps|stop|pause|resume|watch|log)\b|\bbin/\w+|\bnohup\b|\bp?kill(?:all)?\b|\bpgrep\b|\bps\s+-|\bps\b.*\bpid\b|\bsleep\b|\bwait\b|\bdisown\b"
        r"|\.(?:pid|exit|rc)\b|\bcontrol/|subprocess|Popen|os\.kill|\bjobs\b|\blsof\b|\btrap\b"
        r"|\$[!?]|time\.sleep|proc\.(?:poll|wait)|\.ack\b|\bckpt/"
        r"|\b\w*(?:watch|kill|start|launch|run)\w*\.(?:sh|txt|out)\b"),
    "analysis": re.compile(
        r"\bpst\s+(?:analyze|grep|tail|cat)\b|\b(?:grep|egrep|rg|awk|sed|tail|head|wc|sort|uniq|cut|md5|diff|zgrep|less|column|jq|tr)\b"
        r"|\bopen\(|re\.(?:compile|search|match|findall|sub)|\.split\(|\.startswith\(|\bfor \w+ in\b"
        r"|readlines|\bpython3?\b|\.log\b|output\.log|\bdef \w+|\bif \w|\bprint\(|defaultdict|Counter"
        r"|\.append\(|\w+\s*[-+]?=\s*\w|\.get\(|\bint\(|\belse\b|\btar\b|\bcat\b.*(?:log|out|txt)"),
    "other": re.compile(r"\bdocs/|\bls\b|\bmkdir\b|\bpwd\b|\bcd\b|\becho\b|\bdate\b|\bcat\b")}
BIG_SCRIPT = 400
# a loop that also stops processes is a supervisor, however much text it parses
SUPERVISE = re.compile(r"\bwhile\b.*(?:\bp?kill\b|os\.kill|\.terminate\(|send_signal)", re.S)


def segments(cmd):
    """(text, class or None) pieces; the body of a heredoc written to board/, answers/
    or control/ takes the class of its target. cd and absolute paths carry no vote."""
    out, end, forced = [], None, None
    for line in re.sub(r"(?<![\w.])/(?:[\w.-]+/)+ws(?:/|\b)", "", cmd).split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            else:
                out.append((line, forced))
            continue
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if m:
            end = m[1]
            t = re.search(r">>?\s*(board|answers|control)/", line)
            forced = {"board": "coordination", "answers": "answer",
                      "control": "process"}[t[1]] if t else None
        out += [(x, forced if m else None) for x in re.split(r";|&&|\|\|", line)
                if x.strip() and not re.match(r"\s*(?:cd\b|mkdir\b|set\b|$)", x)]
    return out


def class_of_command(cmd):
    votes = dict.fromkeys(ORDER, 0)
    for text, forced in segments(cmd):
        s = text.strip()
        if not s or s.startswith("#"):
            continue
        if forced is None and re.match(r"ls\b[^|]*\b(?:board|answers)\b", s):
            forced = "coordination"                      # looking for others' files
        c = forced or next((c for c in ORDER if PATTERNS[c].search(s)), None)
        if c:
            votes[c] += len(s.encode())
    if not any(votes.values()):
        return "other"
    if SUPERVISE.search(cmd):
        return "process"
    if len(cmd) > BIG_SCRIPT and votes["answer"] and votes["analysis"] >= votes["answer"]:
        votes["answer"] = 0
    return max(ORDER, key=lambda c: (votes[c], -ORDER.index(c)))


def by_path(path, default):
    for c, rx in (("answer", r"(?:^|/)answers/"), ("coordination", r"(?:^|/)board/"),
                  ("process", r"(?:^|/)control/|\.(?:pid|exit|rc)$"), ("other", r"(?:^|/)docs/")):
        if re.search(rx, path):
            return c
    return default


def classify(name, arg):
    """Class of one tool call."""
    arg = arg if isinstance(arg, dict) else {}
    short = name.rsplit("__", 1)[-1]
    if name in ("Bash", "Monitor"):
        return class_of_command(str(arg.get("command") or "")) if arg.get("command") else "process"
    if name in ("Write", "Edit"):
        text = str(arg.get("content") or arg.get("new_string") or "")
        c = by_path(str(arg.get("file_path") or ""), None)
        return c or class_of_command(text)
    if name == "Read":
        return by_path(str(arg.get("file_path") or ""), "analysis")
    if name == "Grep":
        return by_path(str(arg.get("path") or ""), "analysis")
    if short == "job_start":
        cmds = [c for c in [arg.get("command")] + list(arg.get("commands") or [])
                if isinstance(c, str)]
        if cmds and not any(re.search(r"\bbin/\w+", c) for c in cmds):
            return "analysis" if class_of_command("\n".join(cmds)) == "analysis" else "process"
    return TOOL_CLASS.get(short, "other")


def calls(events):
    """[(message id, name, input)] in order, and {message id: text bytes}."""
    out, text, seen = [], {}, set()
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        mid = (ev.get("message") or {}).get("id")
        text.setdefault(mid, 0)
        for b in pst_metrics.blocks(ev):
            if b.get("type") == "text":
                text[mid] += len(str(b.get("text") or "").encode())
            elif b.get("type") == "tool_use" and b.get("id") not in seen:
                seen.add(b.get("id"))
                out.append((mid, b.get("name") or "?", b.get("input") or {}))
    return out, text


def empty():
    return {c: {"calls": 0, "arg_bytes": 0, "turns": 0.0, "output_tokens": 0.0} for c in CLASSES}


def by_category(events, parsed):
    """Per-class totals of one session (sub-agents included)."""
    tot, (cs, text) = empty(), calls(events)
    rows = [(mid, classify(n, a), len(json.dumps(a, ensure_ascii=False).encode()))
            for mid, n, a in cs]
    size = {m: t + 100 for m, t in text.items()}
    for mid, _, n in rows:
        size[mid] += n
    per = {}
    for mid, c, n in rows:
        per.setdefault(mid, []).append((c, n))
    rate = parsed["tokens"]["output_tokens"] / (sum(size.values()) or 1)
    for mid in text:
        mine, tokens = per.get(mid, []), size[mid] * rate
        if not mine:
            tot["text"]["turns"] += 1
            tot["text"]["output_tokens"] += tokens
            continue
        whole = sum(n for _, n in mine) or 1
        for c, n in mine:
            t = tot[c]
            t["calls"] += 1
            t["arg_bytes"] += n
            t["turns"] += 1 / len(mine)
            t["output_tokens"] += tokens * n / whole
    return {c: {k: round(v, 2) for k, v in t.items()} for c, t in tot.items()}


def merge(per_session):
    tot = empty()
    for cats in per_session.values():
        for c in CLASSES:
            for k in tot[c]:
                tot[c][k] = round(tot[c][k] + cats[c][k], 2)
    return tot


def main():
    ap = argparse.ArgumentParser(description="Classify tool calls of finished runs.")
    ap.add_argument("out_dir")
    ap.add_argument("--sample", type=int, default=0, help="print N random calls for a manual check")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--hide", action="store_true", help="sample without the assigned class")
    ap.add_argument("--json")
    a = ap.parse_args()
    with open(os.path.join(a.out_dir, "results.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    table, pool = [], []
    for r in rows:
        cats = {}
        for path in sorted(glob.glob(os.path.join(a.out_dir, "runs", r["run_id"], "logs", "*.jsonl"))):
            ev = pst_metrics.read_jsonl(path)
            who = os.path.basename(path)[:-6]
            cats[who] = by_category(ev, pst_metrics.parse_agent(ev))
            pool += [(r["exp"], r["condition"], r["run_id"], who, i, n, arg)
                     for i, (_, n, arg) in enumerate(calls(ev)[0])]
        table.append({"exp": r["exp"], "condition": r["condition"], "run_id": r["run_id"],
                      "categories": merge(cats)})
    if a.sample:
        for n, (e, c, run, who, i, name, arg) in enumerate(
                random.Random(a.seed).sample(sorted(pool, key=lambda x: x[:5]), a.sample), 1):
            cls = "" if a.hide else f" -> {classify(name, arg)}"
            print(f"## {n}. exp{e} {c} {run} {who} call {i} {name}{cls}\n"
                  f"{json.dumps(arg, ensure_ascii=False)}\n")
        return
    print("| exp | arm | class | calls | arg bytes | turns | output tokens (est.) | share of tokens |")
    print("|---" * 8 + "|")
    for t in sorted(table, key=lambda t: (t["exp"], t["condition"])):
        whole = sum(v["output_tokens"] for v in t["categories"].values()) or 1
        for c in CLASSES:
            v = t["categories"][c]
            print(f"| {t['exp']} | {t['condition']} | {c} | {v['calls']:.0f} | {v['arg_bytes']:,.0f} "
                  f"| {v['turns']:.1f} | {v['output_tokens']:,.0f} | {v['output_tokens'] / whole:.0%} |")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(table, f, indent=1)


if __name__ == "__main__":
    sys.exit(main())
