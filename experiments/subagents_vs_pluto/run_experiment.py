#!/usr/bin/env python3
"""Run the subagents-vs-Pluto experiment.

    python3 experiments/subagents_vs_pluto/run_experiment.py \
        --scenario all --condition all --reps 5 --model claude-sonnet-5

Each run gets a fresh directory under --out/runs/<scenario>/<condition>/rep<k>/
containing ledger.py, the agents' stream-json transcripts (one per process),
events.jsonl written by ledger.py, and result.json. One summary row per run
is appended to --out/results.jsonl. Pluto runs use a private sandbox server
(see pluto_sandbox.py) restarted for every run.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import conditions  # noqa: E402
import metrics  # noqa: E402
import pluto_sandbox  # noqa: E402
from scenarios import SCENARIOS, validate  # noqa: E402

DEFAULT_OUT = os.path.join(HERE, "..", "..", "docs", "research", "data", "subagents-vs-pluto")


def _pump(stream, path):
    """Copy a process's stdout to JSONL, stamping each event's arrival time."""
    with open(path, "w", encoding="utf-8") as out:
        for raw in stream:
            ts = time.time()
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ev["_recv_ts"] = ts
                out.write(json.dumps(ev) + "\n")
            except ValueError:
                out.write(json.dumps({"_raw": line, "_recv_ts": ts}) + "\n")
            out.flush()


def run_processes(cmds, run_dir, timeout_s):
    """Start all commands concurrently; return {name: (rc, timed_out, secs)}."""
    procs, pumps, started = {}, [], {}
    env = dict(os.environ, LEDGER_DIR=run_dir)
    for name, cmd in cmds.items():
        err = open(os.path.join(run_dir, f"{name}.stderr"), "w")
        p = subprocess.Popen(cmd, cwd=run_dir, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=err, start_new_session=True)
        started[name] = time.time()
        t = threading.Thread(target=_pump, args=(p.stdout, os.path.join(run_dir, f"{name}.jsonl")),
                             daemon=True)
        t.start()
        procs[name], pumps = p, pumps + [t]
    deadline = time.time() + timeout_s
    status = {}
    try:
        _wait_all(procs, started, status, deadline)
    finally:
        _kill_all(procs)  # only non-empty if interrupted
    for t in pumps:
        t.join(timeout=5)
    return status


def _kill_all(procs):
    for p in procs.values():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_all(procs, started, status, deadline):
    while procs:
        for name, p in list(procs.items()):
            if p.poll() is not None:
                status[name] = (p.returncode, False, round(time.time() - started[name], 3))
                del procs[name]
        if procs and time.time() > deadline:
            for name, p in procs.items():
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
            time.sleep(3)
            for name, p in procs.items():
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                status[name] = (p.returncode, True, round(time.time() - started[name], 3))
            procs.clear()
        time.sleep(0.2)


def run_one(scenario, condition, rep, args):
    run_dir = os.path.abspath(os.path.join(args.out, "runs", scenario, condition, f"rep{rep}"))
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    shutil.copy(os.path.join(HERE, "ledger.py"), run_dir)

    sandbox = None
    if condition == "subagents":
        cmds = conditions.subagents_commands(scenario, run_dir, args.model, args.budget)
    else:
        sandbox = pluto_sandbox.PlutoSandbox(run_dir).start()
        cmds = conditions.pluto_commands(scenario, run_dir, args.model, args.budget,
                                         pluto_sandbox.HOST, pluto_sandbox.HTTP_PORT)
    t0 = time.time()
    try:
        status = run_processes(cmds, run_dir, args.timeout)
    finally:
        wall = round(time.time() - t0, 3)
        if sandbox:
            sandbox.stop()
    pluto_version = sandbox.health.get("version") if sandbox else None

    per_proc = {n: metrics.summarize_transcript(metrics.read_jsonl(os.path.join(run_dir, f"{n}.jsonl")))
                for n in cmds}
    if condition == "subagents":
        s = per_proc["orch"]
        agg = {"tokens": s["tokens"],
               "tokens_orchestrator": s["tokens_by_role"]["orchestrator"],
               "tokens_workers": s["tokens_by_role"]["subagents"],
               "cost_usd": round(s["cost_usd"], 6), "tools": s["tools"],
               "errored_processes": ["orch"] if s["is_error"] else [],
               "subagent_stats": s["subagent_stats"]}
    else:
        agg = metrics.merge_process_stats(per_proc)
    verdict = validate(scenario, run_dir)
    row = {
        "scenario": scenario, "condition": condition, "rep": rep, "model": args.model,
        "pluto_version": pluto_version,
        # killed processes emit no `result` event, so their cost is missing
        "cost_complete": not any(s["is_error"] and s["terminal_reason"] is None
                                 for s in per_proc.values()),
        "started_at": t0, "wall_s": wall,
        "timed_out": any(v[1] for v in status.values()),
        "exit_codes": {k: v[0] for k, v in status.items()},
        "process_secs": {k: v[2] for k, v in status.items()},
        "total_tokens": metrics.total_tokens(agg["tokens"]),
        "coordination_calls": metrics.count_coordination(agg["tools"]),
        **agg,
        "verdict": verdict,
    }
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(row, f, indent=2)
    with open(os.path.join(args.out, "results.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _sigterm(signum, frame):
    raise KeyboardInterrupt  # unwinds through run_processes' cleanup


def main():
    signal.signal(signal.SIGTERM, _sigterm)
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", default="all", help="all or comma list of " + ",".join(SCENARIOS))
    p.add_argument("--condition", default="all", choices=["all", "subagents", "pluto"])
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--rep-start", type=int, default=1)
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--budget", type=float, default=3.0, help="per-process USD cap")
    p.add_argument("--timeout", type=int, default=600, help="per-run wall-clock cap (s)")
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args()
    args.out = os.path.abspath(args.out)
    os.makedirs(args.out, exist_ok=True)

    scenarios = list(SCENARIOS) if args.scenario == "all" else args.scenario.split(",")
    conds = ["subagents", "pluto"] if args.condition == "all" else [args.condition]
    for rep in range(args.rep_start, args.rep_start + args.reps):
        for sc in scenarios:
            for cond in conds:  # interleave conditions to spread drift evenly
                row = run_one(sc, cond, rep, args)
                v = row["verdict"]
                print(f"[{sc} {cond} rep{rep}] ok={v['ok']} wall={row['wall_s']}s "
                      f"tokens={row['total_tokens']} cost=${row['cost_usd']:.3f} "
                      f"coord={row['coordination_calls']} timeout={row['timed_out']}",
                      flush=True)


if __name__ == "__main__":
    main()
