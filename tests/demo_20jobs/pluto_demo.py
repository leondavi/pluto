#!/usr/bin/env python3
"""
The Task Claim Race — demo_20jobs

Three Copilot agents (Analyst, Classifier, Publisher) all compete to claim
and process the same 20 jobs simultaneously. Each agent is a real
GitHub Copilot CLI process.

  Pluto mode  (default):      agents coordinate through PlutoServer —
                               each job is processed exactly once,
                               fencing tokens are printed.

  Chaos mode  (--no-pluto):   agents race with no coordination —
                               duplicate jobs are detected and reported.

Usage:
    python pluto_demo.py                 # Pluto-coordinated mode
    python pluto_demo.py --no-pluto      # chaos / race mode
    python pluto_demo.py --host 10.0.0.5 --port 9000
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────────────

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
WORKER_SCRIPT = os.path.join(_HERE, "agent_worker.py")
OUTPUT_DIR = "/tmp/pluto_20jobs"

AGENTS = ["Analyst", "Classifier", "Publisher"]

# ── ANSI colours ──────────────────────────────────────────────────────────────

GREEN   = "\033[32m"
CYAN    = "\033[36m"
MAGENTA = "\033[35m"
RED     = "\033[31m"
YELLOW  = "\033[33m"
DIM     = "\033[2m"
BOLD    = "\033[1m"
RESET   = "\033[0m"

AGENT_COLOURS = {
    "Analyst":    GREEN,
    "Classifier": CYAN,
    "Publisher":  MAGENTA,
}

# ── Jobs (for reporting) ─────────────────────────────────────────────────────

JOBS = [
    {"id": "job:001", "name": "Summarise Q1 report"},
    {"id": "job:002", "name": "Classify support tickets"},
    {"id": "job:003", "name": "Generate weekly digest"},
    {"id": "job:004", "name": "Score lead candidates"},
    {"id": "job:005", "name": "Translate product docs"},
    {"id": "job:006", "name": "Extract invoice line items"},
    {"id": "job:007", "name": "Detect anomalies in logs"},
    {"id": "job:008", "name": "Tag customer feedback"},
    {"id": "job:009", "name": "Rank open bug reports"},
    {"id": "job:010", "name": "Draft release notes"},
    {"id": "job:011", "name": "Cluster user sessions"},
    {"id": "job:012", "name": "Flag policy violations"},
    {"id": "job:013", "name": "Enrich CRM contact records"},
    {"id": "job:014", "name": "Predict churn probability"},
    {"id": "job:015", "name": "Validate data pipeline output"},
    {"id": "job:016", "name": "Reformat onboarding emails"},
    {"id": "job:017", "name": "Deduplicate product catalogue"},
    {"id": "job:018", "name": "Summarise legal contract"},
    {"id": "job:019", "name": "Build performance dashboard"},
    {"id": "job:020", "name": "Generate A/B test report"},
]

CONSEQUENCES = [
    "duplicate API calls wasted compute",
    "double-charged billing operation",
    "conflicting outputs overwrote each other",
    "downstream pipeline received duplicate data",
    "redundant notifications sent to users",
    "stale cache served to end-users",
    "duplicate database writes caused constraint violation",
    "inconsistent report totals from double-counted rows",
    "parallel mutations corrupted shared state",
    "audit trail recorded phantom transaction",
]


# ── Copilot agent launcher ───────────────────────────────────────────────────

def launch_copilot_agent(agent_id, mode, host, port, output_dir):
    """
    Launch a Copilot CLI process that runs the agent_worker.py script.
    Returns subprocess.Popen handle.
    """
    worker_cmd = (
        f"python {WORKER_SCRIPT} "
        f"--agent-id {agent_id} "
        f"--mode {mode} "
        f"--host {host} "
        f"--port {port} "
        f"--output-dir {output_dir} "
        f"--num-agents {len(AGENTS)}"
    )

    prompt = (
        f"Run this exact shell command and wait for it to complete. "
        f"Do not modify the command. Do not add any other commands. "
        f"Just run it:\n\n"
        f"{worker_cmd}"
    )

    copilot_bin = shutil.which("copilot")
    if not copilot_bin:
        print(f"  {RED}ERROR: 'copilot' CLI not found on PATH{RESET}")
        sys.exit(1)

    proc = subprocess.Popen(
        [
            copilot_bin,
            "-p", prompt,
            "--allow-all",
            "--no-ask-user",
            "--no-auto-update",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=_PROJECT,
    )
    return proc


# ── Result collection ─────────────────────────────────────────────────────────

def collect_results(output_dir):
    """Read per-agent JSON result files and merge into a unified ledger."""
    all_claims = {}  # job_id → [{"agent", "fencing_token", "name", ...}, ...]

    for agent_id in AGENTS:
        path = os.path.join(output_dir, f"{agent_id}.json")
        if not os.path.exists(path):
            print(f"  {RED}WARNING: No result file for {agent_id}{RESET}")
            continue
        with open(path) as f:
            results = json.load(f)
        for entry in results:
            jid = entry["id"]
            all_claims.setdefault(jid, []).append(entry)

    return all_claims


# ── Report printing ──────────────────────────────────────────────────────────

def print_ledger(all_claims):
    print()
    print(f"  {BOLD}{'═' * 74}{RESET}")
    print(f"  {BOLD}  JOB LEDGER{RESET}")
    print(f"  {BOLD}{'═' * 74}{RESET}")
    print(f"  {'Job ID':<10} {'Agent':<14} {'Fencing Token':<16} {'Job Name'}")
    print(f"  {'─' * 10} {'─' * 14} {'─' * 16} {'─' * 30}")

    for job in JOBS:
        jid = job["id"]
        claims = all_claims.get(jid, [])
        if claims:
            entry = claims[0]
            colour = AGENT_COLOURS.get(entry["agent"], "")
            tok = str(entry.get("fencing_token", "")) if entry.get("fencing_token") is not None else "—"
            dup_mark = f"  {RED}(+{len(claims)-1} dup){RESET}" if len(claims) > 1 else ""
            print(f"  {YELLOW}{jid}{RESET}  "
                  f"{colour}{entry['agent']:<14}{RESET} "
                  f"{tok:<16} "
                  f"{DIM}{entry['name']}{RESET}{dup_mark}")
        else:
            print(f"  {YELLOW}{jid}{RESET}  "
                  f"{RED}{'UNCLAIMED':<14}{RESET} "
                  f"{'—':<16} "
                  f"{DIM}{job['name']}{RESET}")
    print(f"  {BOLD}{'═' * 74}{RESET}")


def print_pluto_summary(all_claims):
    duplicates = [(jid, cl) for jid, cl in all_claims.items() if len(cl) > 1]

    tokens = sorted([
        entry.get("fencing_token", 0)
        for claims in all_claims.values()
        for entry in claims
        if entry.get("fencing_token") is not None
    ])
    all_unique = len(tokens) == len(set(tokens)) if tokens else True
    monotonic = (
        all_unique
        and (all(tokens[i] < tokens[i + 1] for i in range(len(tokens) - 1))
             if len(tokens) > 1 else True)
    )

    counts = {}
    for claims in all_claims.values():
        if claims:
            a = claims[0]["agent"]
            counts[a] = counts.get(a, 0) + 1

    print()
    print(f"  {BOLD}SUMMARY (Pluto mode){RESET}")
    dup_count = len(duplicates)
    dup_colour = GREEN if dup_count == 0 else RED
    print(f"  Duplicates detected  : {dup_colour}{dup_count}{RESET}")
    mono_colour = GREEN if monotonic else RED
    mono_label = "YES" if monotonic else "NO"
    if tokens:
        print(f"  Fencing tokens unique : {mono_colour}{mono_label}{RESET}  (sorted: {tokens[0]}..{tokens[-1]})")
    print(f"  Unique agents worked : {len(counts)}")
    print()
    print(f"  {BOLD}Per-agent job counts:{RESET}")
    for agent in AGENTS:
        c = counts.get(agent, 0)
        colour = AGENT_COLOURS.get(agent, "")
        print(f"    {colour}{agent:<14}{RESET} {c} jobs")
    print()


def print_chaos_summary(all_claims):
    duplicates = [(jid, cl) for jid, cl in all_claims.items() if len(cl) > 1]

    counts = {}
    for claims in all_claims.values():
        for entry in claims:
            a = entry["agent"]
            counts[a] = counts.get(a, 0) + 1

    print()
    print(f"  {BOLD}SUMMARY (Chaos mode — no Pluto){RESET}")
    print(f"  Total duplicates     : {RED}{len(duplicates)}{RESET}")
    print()

    if duplicates:
        print(f"  {BOLD}Duplicate details:{RESET}")
        for i, (jid, claims) in enumerate(duplicates):
            job_name = claims[0]["name"]
            note = CONSEQUENCES[i % len(CONSEQUENCES)]
            agents_in_dup = [c["agent"] for c in claims]
            agent_str = " AND ".join(
                AGENT_COLOURS.get(a, "") + a + RESET for a in agents_in_dup
            )
            print(f"    {RED}✗{RESET} {YELLOW}{jid}{RESET} processed by "
                  f"{agent_str}  — {DIM}{note}{RESET}")
        print()

    print(f"  {BOLD}Per-agent job counts (including duplicates):{RESET}")
    for agent in AGENTS:
        c = counts.get(agent, 0)
        colour = AGENT_COLOURS.get(agent, "")
        print(f"    {colour}{agent:<14}{RESET} {c} jobs")
    print()

    return len(duplicates)


def print_agent_logs(outputs):
    """Print captured stdout from each Copilot agent."""
    print()
    print(f"  {BOLD}{'═' * 74}{RESET}")
    print(f"  {BOLD}  COPILOT AGENT LOGS{RESET}")
    print(f"  {BOLD}{'═' * 74}{RESET}")
    for agent_id, output in outputs.items():
        colour = AGENT_COLOURS.get(agent_id, "")
        print(f"\n  {colour}{'─' * 30} {agent_id} {'─' * 30}{RESET}")
        lines = output.strip().splitlines()
        if len(lines) > 40:
            for line in lines[:15]:
                print(f"  {line}")
            print(f"  {DIM}  ... ({len(lines) - 30} lines omitted) ...{RESET}")
            for line in lines[-15:]:
                print(f"  {line}")
        else:
            for line in lines:
                print(f"  {line}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="The Task Claim Race — 20-job demo (Copilot agents)"
    )
    parser.add_argument(
        "--no-pluto", action="store_true",
        help="Run in chaos mode (no coordination)",
    )
    parser.add_argument(
        "--host", default=PLUTO_HOST,
        help=f"PlutoServer host (default: {PLUTO_HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=PLUTO_PORT,
        help=f"PlutoServer port (default: {PLUTO_PORT})",
    )
    args = parser.parse_args()

    mode = "chaos" if args.no_pluto else "pluto"
    mode_label = "CHAOS (no Pluto)" if mode == "chaos" else "PLUTO (coordinated)"

    print()
    print(f"  {BOLD}╔{'═' * 52}╗{RESET}")
    print(f"  {BOLD}║  THE TASK CLAIM RACE — 20 Jobs × 3 Copilot Agents  ║{RESET}")
    print(f"  {BOLD}║  Mode: {mode_label:<43}║{RESET}")
    print(f"  {BOLD}╚{'═' * 52}╝{RESET}")
    print()

    # Clean output directory
    if os.path.exists(OUTPUT_DIR):
        for f in os.listdir(OUTPUT_DIR):
            os.remove(os.path.join(OUTPUT_DIR, f))
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Launch all Copilot agents simultaneously
    procs = {}
    start_time = time.time()

    for agent_id in AGENTS:
        print(f"  {BOLD}[launcher]{RESET} Starting Copilot agent: "
              f"{AGENT_COLOURS.get(agent_id, '')}{agent_id}{RESET}")
        procs[agent_id] = launch_copilot_agent(
            agent_id, mode, args.host, args.port, OUTPUT_DIR,
        )

    # Wait for all agents to complete
    outputs = {}
    all_ok = True
    for agent_id, proc in procs.items():
        try:
            stdout, _ = proc.communicate(timeout=120)
            outputs[agent_id] = stdout or ""
            if proc.returncode != 0:
                all_ok = False
                print(f"  {RED}[launcher] {agent_id} exited with code {proc.returncode}{RESET}")
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            outputs[agent_id] = stdout or ""
            all_ok = False
            print(f"  {RED}[launcher] {agent_id} timed out{RESET}")

    elapsed = time.time() - start_time

    # Print agent logs
    print_agent_logs(outputs)

    # Collect results from agent output files
    print()
    print(f"  {BOLD}[launcher]{RESET} All agents finished in {elapsed:.1f}s")

    all_claims = collect_results(OUTPUT_DIR)

    # Final report
    print_ledger(all_claims)

    if mode == "pluto":
        print_pluto_summary(all_claims)
    else:
        dup_count = print_chaos_summary(all_claims)
        if dup_count > 0:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
