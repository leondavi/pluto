#!/usr/bin/env python3
"""
Real multi-process agent collaboration test.

Launches 3 independent Python processes as real Copilot-style agents,
each connecting to the real Pluto Erlang server over TCP.

Task: "Build a Microservices Deployment Plan"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Phase 1 (SEQUENTIAL):
  lead-architect designs the system topology and writes the master plan.

Phase 2 (PARALLEL):
  infra-engineer, api-developer, data-engineer all work simultaneously
  on their sections — each acquires their own resource lock + contends
  for the shared status-board lock.

Phase 3 (PARALLEL cross-review):
  Each agent reviews another's work (circular):
    infra-engineer reviews api-developer's work
    api-developer reviews data-engineer's work
    data-engineer reviews infra-engineer's work
  All three contend for each other's file locks.

Phase 4 (SEQUENTIAL):
  lead-architect waits for all reviews, assembles the final plan,
  publishes completion on a topic channel.

Uses: locks, messages, broadcasts, pub/sub topics, task assignments, stats.
"""

import json
import os
import subprocess
import sys
import time
import textwrap

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000
WORK_DIR = "/tmp/pluto_real_collab_test"

AGENT_SCRIPTS = {
    "lead-architect": os.path.join(_HERE, "agent_lead_architect.py"),
    "infra-engineer": os.path.join(_HERE, "agent_infra_engineer.py"),
    "api-developer":  os.path.join(_HERE, "agent_api_developer.py"),
}


def main():
    os.makedirs(WORK_DIR, exist_ok=True)

    # Clean previous run
    for f in os.listdir(WORK_DIR):
        os.remove(os.path.join(WORK_DIR, f))

    print("=" * 72)
    print("  REAL MULTI-AGENT COLLABORATION TEST")
    print("  3 independent processes ↔ real Pluto Erlang server (port 9000)")
    print("=" * 72)
    print()

    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_PY + ":" + env.get("PYTHONPATH", "")
    env["PLUTO_HOST"] = PLUTO_HOST
    env["PLUTO_PORT"] = str(PLUTO_PORT)
    env["WORK_DIR"] = WORK_DIR

    # Launch all agents as independent subprocesses
    procs = {}
    start_time = time.time()

    # lead-architect starts first (Phase 1 is sequential)
    print("[launcher] Starting lead-architect ...")
    procs["lead-architect"] = subprocess.Popen(
        [sys.executable, AGENT_SCRIPTS["lead-architect"]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    # Small delay so architect registers first
    time.sleep(0.3)

    # Phase 2 agents start in parallel
    for name in ["infra-engineer", "api-developer"]:
        print(f"[launcher] Starting {name} ...")
        procs[name] = subprocess.Popen(
            [sys.executable, AGENT_SCRIPTS[name]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )

    # Wait for all agents to complete
    outputs = {}
    all_ok = True
    for name, proc in procs.items():
        stdout, _ = proc.communicate(timeout=60)
        outputs[name] = stdout
        if proc.returncode != 0:
            all_ok = False
            print(f"\n[launcher] ERROR: {name} exited with code {proc.returncode}")

    elapsed = time.time() - start_time

    # ── Print agent logs ──────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  AGENT LOGS")
    print("=" * 72)
    for name in ["lead-architect", "infra-engineer", "api-developer"]:
        print(f"\n{'─' * 36} {name} {'─' * 36}")
        print(outputs[name].rstrip())

    # ── Query Pluto stats ─────────────────────────────────────────────
    sys.path.insert(0, _SRC_PY)
    from pluto_client import PlutoClient
    with PlutoClient(host=PLUTO_HOST, port=PLUTO_PORT, agent_id="reporter") as client:
        stats = client.stats()

    # ── Verify outputs ────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  VERIFICATION")
    print("=" * 72)

    checks = [
        ("Master plan",     f"{WORK_DIR}/master_plan.txt",     ["lead-architect", "topology"]),
        ("Infra spec",      f"{WORK_DIR}/infra_spec.txt",      ["infra-engineer", "Kubernetes"]),
        ("API spec",        f"{WORK_DIR}/api_spec.txt",        ["api-developer", "REST"]),
        ("Infra review",    f"{WORK_DIR}/review_infra.txt",    ["reviewed"]),
        ("API review",      f"{WORK_DIR}/review_api.txt",      ["reviewed"]),
        ("Final plan",      f"{WORK_DIR}/final_deployment.txt", ["lead-architect", "FINAL", "infra", "api"]),
        ("Status board",    f"{WORK_DIR}/status.txt",          ["lead-architect", "infra-engineer", "api-developer"]),
    ]

    passed = 0
    failed = 0
    for label, path, expected_strings in checks:
        try:
            with open(path) as f:
                content = f.read()
            missing = [s for s in expected_strings if s not in content]
            if missing:
                print(f"  FAIL  {label}: missing {missing}")
                failed += 1
            else:
                print(f"  PASS  {label}")
                passed += 1
        except FileNotFoundError:
            print(f"  FAIL  {label}: file not found ({path})")
            failed += 1

    # ── Print report ──────────────────────────────────────────────────
    counters = stats.get("counters", {})
    agent_stats = stats.get("agent_stats", {})
    live = stats.get("live", {})

    print("\n" + "=" * 72)
    print("  PLUTO SERVER STATISTICS")
    print("=" * 72)
    print(f"""
  Server uptime:        {stats.get('uptime_ms', 0) / 1000:.1f}s
  Test duration:        {elapsed:.2f}s

  ┌─────────────────────────────────────────────┐
  │  GLOBAL COUNTERS                            │
  ├─────────────────────────────────────────────┤
  │  Agents registered:     {counters.get('agents_registered', 0):>6}              │
  │  Locks acquired:        {counters.get('locks_acquired', 0):>6}              │
  │  Locks released:        {counters.get('locks_released', 0):>6}              │
  │  Lock waits (contention):{counters.get('lock_waits', 0):>5}              │
  │  Messages sent:         {counters.get('messages_sent', 0):>6}              │
  │  Messages received:     {counters.get('messages_received', 0):>6}              │
  │  Broadcasts sent:       {counters.get('broadcasts_sent', 0):>6}              │
  │  Total requests:        {counters.get('total_requests', 0):>6}              │
  │  Deadlocks detected:    {counters.get('deadlocks_detected', 0):>6}              │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  LIVE SNAPSHOT                              │
  ├─────────────────────────────────────────────┤
  │  Active locks:          {live.get('active_locks', 0):>6}              │
  │  Connected agents:      {live.get('connected_agents', 0):>6}              │
  │  Total agents seen:     {live.get('total_agents', 0):>6}              │
  │  Pending waiters:       {live.get('pending_waiters', 0):>6}              │
  └─────────────────────────────────────────────┘
""")

    print("  PER-AGENT BREAKDOWN:")
    print("  " + "─" * 70)
    header = f"  {'Agent':<20} {'Locks Acq':>10} {'Locks Rel':>10} {'Msgs Sent':>10} {'Msgs Recv':>10}"
    print(header)
    print("  " + "─" * 70)
    for aid in sorted(agent_stats.keys()):
        s = agent_stats[aid]
        print(f"  {aid:<20} {s.get('locks_acquired', 0):>10} {s.get('locks_released', 0):>10} "
              f"{s.get('messages_sent', 0):>10} {s.get('messages_received', 0):>10}")
    print("  " + "─" * 70)

    # ── Final verdict ─────────────────────────────────────────────────
    print(f"\n  Checks: {passed} passed, {failed} failed")
    all_agents_ok = all(p.returncode == 0 for p in procs.values())
    print(f"  Agents: {'ALL OK' if all_agents_ok else 'SOME FAILED'}")
    overall = passed == len(checks) and all_agents_ok
    print(f"\n  {'✓ TEST PASSED' if overall else '✗ TEST FAILED'}")
    print("=" * 72)

    # Dump full stats JSON
    print("\n  Full stats JSON:")
    print(textwrap.indent(json.dumps(stats, indent=2), "    "))

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
