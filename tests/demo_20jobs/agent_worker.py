#!/usr/bin/env python3
"""
agent_worker.py — Standalone worker for the Task Claim Race demo.

Each Copilot agent runs this script as a separate process. The worker
connects to the real PlutoServer (pluto mode) or uses an uncoordinated
shared file (chaos mode) to claim and process jobs.

Usage (invoked by copilot, not manually):
    python agent_worker.py --agent-id Analyst --mode pluto \
        --host 127.0.0.1 --port 9000 --output-dir /tmp/pluto_20jobs

Results are written to {output_dir}/{agent_id}.json.
"""

import argparse
import json
import os
import random
import socket
import sys
import time
from datetime import datetime

# ── Jobs ──────────────────────────────────────────────────────────────────────

JOBS = [
    {"id": "job:001", "name": "Summarise Q1 report",            "cost_ms": 400},
    {"id": "job:002", "name": "Classify support tickets",        "cost_ms": 300},
    {"id": "job:003", "name": "Generate weekly digest",          "cost_ms": 500},
    {"id": "job:004", "name": "Score lead candidates",           "cost_ms": 350},
    {"id": "job:005", "name": "Translate product docs",          "cost_ms": 450},
    {"id": "job:006", "name": "Extract invoice line items",      "cost_ms": 280},
    {"id": "job:007", "name": "Detect anomalies in logs",        "cost_ms": 520},
    {"id": "job:008", "name": "Tag customer feedback",           "cost_ms": 310},
    {"id": "job:009", "name": "Rank open bug reports",           "cost_ms": 370},
    {"id": "job:010", "name": "Draft release notes",             "cost_ms": 430},
    {"id": "job:011", "name": "Cluster user sessions",           "cost_ms": 490},
    {"id": "job:012", "name": "Flag policy violations",          "cost_ms": 260},
    {"id": "job:013", "name": "Enrich CRM contact records",      "cost_ms": 340},
    {"id": "job:014", "name": "Predict churn probability",       "cost_ms": 560},
    {"id": "job:015", "name": "Validate data pipeline output",   "cost_ms": 300},
    {"id": "job:016", "name": "Reformat onboarding emails",      "cost_ms": 390},
    {"id": "job:017", "name": "Deduplicate product catalogue",   "cost_ms": 410},
    {"id": "job:018", "name": "Summarise legal contract",        "cost_ms": 480},
    {"id": "job:019", "name": "Build performance dashboard",     "cost_ms": 550},
    {"id": "job:020", "name": "Generate A/B test report",        "cost_ms": 320},
]

SOCKET_TIMEOUT = 15

# ── Helpers ───────────────────────────────────────────────────────────────────

def ts():
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def log(agent_id, msg):
    print(f"  {ts()}  [{agent_id:<12}]  {msg}", flush=True)


# ── Wire protocol (newline-delimited JSON) ────────────────────────────────────

def send_msg(sock, payload):
    line = (json.dumps(payload) + "\n").encode("utf-8")
    sock.sendall(line)


_buffers = {}


def recv_msg(sock):
    sid = id(sock)
    buf = _buffers.get(sid, b"")
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
    line, remaining = buf.split(b"\n", 1)
    _buffers[sid] = remaining
    return json.loads(line.decode("utf-8"))


def recv_response(sock):
    """Read messages, skip async events, return first response."""
    while True:
        msg = recv_msg(sock)
        if msg is None:
            return None
        if "event" in msg:
            continue
        return msg


# ── File-based barrier ────────────────────────────────────────────────────────

def barrier_wait(agent_id, output_dir, expected_count, suffix="ready", timeout=60):
    """
    Simple file-based barrier: each agent creates a .{suffix} file,
    then polls until all expected agents have done the same.
    """
    ready_path = os.path.join(output_dir, f"{agent_id}.{suffix}")
    with open(ready_path, "w") as f:
        f.write(agent_id)
    log(agent_id, f"Barrier({suffix}): waiting ...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        ready_files = [
            f for f in os.listdir(output_dir) if f.endswith(f".{suffix}")
        ]
        if len(ready_files) >= expected_count:
            log(agent_id, f"Barrier({suffix}): all {expected_count} agents — GO!")
            return
        time.sleep(0.2)

    ready_files = [f for f in os.listdir(output_dir) if f.endswith(f".{suffix}")]
    log(agent_id, f"Barrier({suffix}): timeout ({len(ready_files)}/{expected_count})")


# ── Pluto mode ────────────────────────────────────────────────────────────────

def run_pluto(agent_id, host, port, output_dir, num_agents=3):
    """Claim jobs via PlutoServer try_acquire (distributed locks).

    Locks are held until all claiming is done so that each job can only
    be processed by one agent — exactly the pattern a real system would use.
    """
    results = []
    held_locks = []  # lock_refs to release at the end

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    sock.connect((host, port))

    # Register
    send_msg(sock, {"op": "register", "agent_id": agent_id})
    resp = recv_response(sock)
    if not resp or resp.get("status") != "ok":
        log(agent_id, f"Registration failed: {resp}")
        sock.close()
        return
    session_id = resp.get("session_id", "")
    log(agent_id, f"Registered  session={session_id}")

    # Wait for all agents to be ready before starting the race
    barrier_wait(agent_id, output_dir, num_agents)

    my_jobs = list(JOBS)
    random.shuffle(my_jobs)

    MAX_PASSES = 5
    for _pass in range(MAX_PASSES):
        still_unclaimed = []
        for job in my_jobs:
            jid = job["id"]

            if any(r["id"] == jid for r in results):
                continue

            send_msg(sock, {
                "op": "try_acquire",
                "resource": jid,
                "mode": "write",
                "agent": agent_id,
                "ttl_ms": 30000,
            })
            resp = recv_response(sock)
            if not resp:
                log(agent_id, "Connection lost")
                sock.close()
                break

            if resp.get("status") == "ok":
                lock_ref = resp["lock_ref"]
                fencing_token = resp.get("fencing_token", 0)
                held_locks.append(lock_ref)

                # Simulate work (scaled down)
                time.sleep(job["cost_ms"] / 1000.0 * 0.05)

                results.append({
                    "id": jid,
                    "name": job["name"],
                    "agent": agent_id,
                    "fencing_token": fencing_token,
                    "lock_ref": lock_ref,
                    "time": ts(),
                })
                log(agent_id,
                    f"Claimed {jid}  token={fencing_token}  {job['name']}")
            else:
                still_unclaimed.append(job)

        if len(results) >= len(JOBS):
            break
        my_jobs = still_unclaimed
        if not my_jobs:
            break
        time.sleep(0.01)

    # Wait for all agents to finish claiming before releasing locks.
    # This prevents a fast agent from releasing → slow agent re-acquiring.
    barrier_wait(agent_id, output_dir, num_agents, suffix="done")

    # Release all held locks
    for lock_ref in held_locks:
        send_msg(sock, {"op": "release", "lock_ref": lock_ref})
        recv_response(sock)

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
    log(agent_id, "Unregistered")

    out_path = os.path.join(output_dir, f"{agent_id}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    log(agent_id, f"Wrote {len(results)} results to {out_path}")


# ── Chaos mode ────────────────────────────────────────────────────────────────

def _read_chaos_ledger(path):
    """Read the shared chaos ledger (JSON dict: job_id → agent_id)."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_chaos_claim(path, job_id, agent_id):
    """Append a claim to the chaos ledger. No locking — intentional."""
    ledger = _read_chaos_ledger(path)
    ledger.setdefault(job_id, [])
    ledger[job_id].append(agent_id)
    with open(path, "w") as f:
        json.dump(ledger, f)


def run_chaos(agent_id, output_dir, num_agents=3):
    """Process jobs with TOCTOU race — no PlutoServer coordination."""
    results = []
    ledger_path = os.path.join(output_dir, "chaos_ledger.json")

    log(agent_id, "Started (no Pluto coordination)")

    # Wait for all agents to be ready before starting the race
    barrier_wait(agent_id, output_dir, num_agents)

    for job in JOBS:
        jid = job["id"]

        # ── CHECK: read shared ledger ────────────────────────────────
        ledger = _read_chaos_ledger(ledger_path)
        already_claimed = jid in ledger and len(ledger[jid]) > 0

        # ── TOCTOU gap — realistic race window ──────────────────────
        time.sleep(random.uniform(0, 0.03))

        # ── ACT: process if we think it's unclaimed ──────────────────
        if not already_claimed:
            time.sleep(job["cost_ms"] / 1000.0 * 0.05)

            _write_chaos_claim(ledger_path, jid, agent_id)

            results.append({
                "id": jid,
                "name": job["name"],
                "agent": agent_id,
                "time": ts(),
            })
            log(agent_id, f"Processed {jid}  {job['name']}")

    log(agent_id, "Finished")

    out_path = os.path.join(output_dir, f"{agent_id}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    log(agent_id, f"Wrote {len(results)} results to {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Agent worker for Task Claim Race")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--mode", choices=["pluto", "chaos"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--num-agents", type=int, default=3)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    if args.mode == "pluto":
        run_pluto(args.agent_id, args.host, args.port, args.output_dir, args.num_agents)
    else:
        run_chaos(args.agent_id, args.output_dir, args.num_agents)


if __name__ == "__main__":
    main()
