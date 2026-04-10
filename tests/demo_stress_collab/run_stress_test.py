#!/usr/bin/env python3
"""
Stress Collaboration Test — 3 agents, 10 rounds, 200+ messages.

Launches 3 independent processes (coordinator, worker-alpha, worker-beta)
against the real Pluto Erlang server. Validates all outputs and prints
a full statistics report.
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
WORK_DIR = "/tmp/pluto_stress_collab"
ROUNDS = 10

AGENTS = {
    "coordinator":   os.path.join(_HERE, "agent_coordinator.py"),
    "worker-alpha":  os.path.join(_HERE, "agent_worker_alpha.py"),
    "worker-beta":   os.path.join(_HERE, "agent_worker_beta.py"),
}


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    # Clean previous run
    for f in os.listdir(WORK_DIR):
        fp = os.path.join(WORK_DIR, f)
        if os.path.isfile(fp):
            os.remove(fp)

    print("=" * 76)
    print("  STRESS COLLABORATION TEST — 10-Round Sprint")
    print("  3 independent processes ↔ real Pluto Erlang server (port 9000)")
    print("  Target: 200+ messages, nested locks, shared resource contention")
    print("=" * 76)
    print()

    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_PY + ":" + env.get("PYTHONPATH", "")
    env["PLUTO_HOST"] = PLUTO_HOST
    env["PLUTO_PORT"] = str(PLUTO_PORT)
    env["WORK_DIR"] = WORK_DIR

    procs = {}
    start_time = time.time()

    # Coordinator starts first
    print("[launcher] Starting coordinator ...")
    procs["coordinator"] = subprocess.Popen(
        [sys.executable, AGENTS["coordinator"]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    time.sleep(0.2)

    # Workers start in parallel
    for name in ["worker-alpha", "worker-beta"]:
        print(f"[launcher] Starting {name} ...")
        procs[name] = subprocess.Popen(
            [sys.executable, AGENTS[name]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )

    # Wait for completion
    outputs = {}
    for name, proc in procs.items():
        stdout, _ = proc.communicate(timeout=120)
        outputs[name] = stdout
        if proc.returncode != 0:
            print(f"\n[launcher] ERROR: {name} exited with code {proc.returncode}")

    elapsed = time.time() - start_time

    # ── Print agent logs ──────────────────────────────────────────────
    print("\n" + "=" * 76)
    print("  AGENT LOGS")
    print("=" * 76)
    for name in ["coordinator", "worker-alpha", "worker-beta"]:
        print(f"\n{'─' * 36} {name} {'─' * (36 - len(name) + 15)}")
        lines = outputs[name].rstrip().split("\n")
        # Show first 15 + last 15 lines if too long
        if len(lines) > 40:
            for l in lines[:15]:
                print(f"  {l}")
            print(f"  ... ({len(lines) - 30} lines omitted) ...")
            for l in lines[-15:]:
                print(f"  {l}")
        else:
            for l in lines:
                print(f"  {l}")

    # ── Query Pluto stats ─────────────────────────────────────────────
    sys.path.insert(0, _SRC_PY)
    from pluto_client import PlutoClient
    with PlutoClient(host=PLUTO_HOST, port=PLUTO_PORT, agent_id="reporter") as client:
        stats = client.stats()

    # ── Verification ──────────────────────────────────────────────────
    print("\n" + "=" * 76)
    print("  VERIFICATION")
    print("=" * 76)

    checks = []

    # Check all module files have content from both workers
    for i in range(5):
        path = os.path.join(WORK_DIR, f"module_{i}.txt")
        checks.append((f"module_{i}.txt", path, ["worker-alpha", "worker-beta"]))

    # Check dashboard
    checks.append(("dashboard.txt", os.path.join(WORK_DIR, "dashboard.txt"),
                    [f"Round {r}: COMPLETE" for r in range(1, ROUNDS + 1)]))

    # Check build log has entries from both
    checks.append(("build_log.txt", os.path.join(WORK_DIR, "build_log.txt"),
                    ["worker-alpha", "worker-beta"]))

    # Check metrics
    checks.append(("metrics.json", os.path.join(WORK_DIR, "metrics.json"),
                    [f'"rounds_completed": {ROUNDS}']))

    # Check scoreboard
    checks.append(("scoreboard.json", os.path.join(WORK_DIR, "scoreboard.json"),
                    ["worker-alpha", "worker-beta"]))

    # Check review files exist (at least first and last round)
    for rnd in [1, ROUNDS]:
        for who in ["alpha", "beta"]:
            fname = f"review_r{rnd}_{who}.txt"
            checks.append((fname, os.path.join(WORK_DIR, fname), ["LGTM"]))

    # Check sprint report
    checks.append(("sprint_report.txt", os.path.join(WORK_DIR, "sprint_report.txt"),
                    ["coordinator", "COMPLETE", "worker-alpha", "worker-beta"]))

    passed = 0
    failed = 0
    for label, path, expected in checks:
        try:
            with open(path) as f:
                content = f.read()
            missing = [s for s in expected if s not in content]
            if missing:
                print(f"  FAIL  {label}: missing {missing}")
                failed += 1
            else:
                print(f"  PASS  {label}")
                passed += 1
        except FileNotFoundError:
            print(f"  FAIL  {label}: file not found")
            failed += 1

    # ── Statistics Report ─────────────────────────────────────────────
    counters = stats.get("counters", {})
    agent_stats = stats.get("agent_stats", {})
    live = stats.get("live", {})

    total_msgs = counters.get("messages_sent", 0)
    total_locks = counters.get("locks_acquired", 0)
    total_waits = counters.get("lock_waits", 0)

    print("\n" + "=" * 76)
    print("  PLUTO SERVER STATISTICS")
    print("=" * 76)
    print(f"""
  Server uptime:          {stats.get('uptime_ms', 0) / 1000:.1f}s
  Test duration:          {elapsed:.2f}s
  Rounds completed:       {ROUNDS}

  ╔═══════════════════════════════════════════════════╗
  ║  GLOBAL COUNTERS                                  ║
  ╠═══════════════════════════════════════════════════╣
  ║  Agents registered:        {counters.get('agents_registered', 0):>6}                 ║
  ║  Total requests:           {counters.get('total_requests', 0):>6}                 ║
  ║                                                   ║
  ║  MESSAGES                                         ║
  ║    Messages sent:          {counters.get('messages_sent', 0):>6}                 ║
  ║    Messages received:      {counters.get('messages_received', 0):>6}                 ║
  ║    Broadcasts sent:        {counters.get('broadcasts_sent', 0):>6}                 ║
  ║                                                   ║
  ║  LOCKS                                            ║
  ║    Locks acquired:         {counters.get('locks_acquired', 0):>6}                 ║
  ║    Locks released:         {counters.get('locks_released', 0):>6}                 ║
  ║    Lock waits (contention):{counters.get('lock_waits', 0):>6}                 ║
  ║    Locks renewed:          {counters.get('locks_renewed', 0):>6}                 ║
  ║    Locks expired:          {counters.get('locks_expired', 0):>6}                 ║
  ║                                                   ║
  ║  SAFETY                                           ║
  ║    Deadlocks detected:     {counters.get('deadlocks_detected', 0):>6}                 ║
  ║    Deadlock victims:       {counters.get('deadlock_victims', 0):>6}                 ║
  ╚═══════════════════════════════════════════════════╝

  ╔═══════════════════════════════════════════════════╗
  ║  LIVE SNAPSHOT                                    ║
  ╠═══════════════════════════════════════════════════╣
  ║  Active locks:             {live.get('active_locks', 0):>6}                 ║
  ║  Connected agents:         {live.get('connected_agents', 0):>6}                 ║
  ║  Total agents seen:        {live.get('total_agents', 0):>6}                 ║
  ║  Pending waiters:          {live.get('pending_waiters', 0):>6}                 ║
  ║  Wait graph edges:         {live.get('wait_graph_edges', 0):>6}                 ║
  ╚═══════════════════════════════════════════════════╝
""")

    # Per-agent table
    print("  PER-AGENT BREAKDOWN:")
    print("  " + "─" * 72)
    hdr = (f"  {'Agent':<18} {'Reg':>4} {'Locks':>6} {'Rels':>6} "
           f"{'Waits':>6} {'MsgTx':>6} {'MsgRx':>6} {'Bcast':>6}")
    print(hdr)
    print("  " + "─" * 72)
    for aid in sorted(agent_stats.keys()):
        s = agent_stats[aid]
        print(f"  {aid:<18} {s.get('registrations', 0):>4} "
              f"{s.get('locks_acquired', 0):>6} {s.get('locks_released', 0):>6} "
              f"{s.get('lock_waits', 0):>6} "
              f"{s.get('messages_sent', 0):>6} {s.get('messages_received', 0):>6} "
              f"{s.get('broadcasts_sent', 0):>6}")
    print("  " + "─" * 72)

    # Compute message total
    total_directed = counters.get("messages_sent", 0)
    total_broadcast = counters.get("broadcasts_sent", 0)
    print(f"\n  Total directed messages:  {total_directed}")
    print(f"  Total broadcasts:         {total_broadcast}")
    print(f"  Combined message events:  {total_directed + total_broadcast}")

    # Resource contention summary
    print(f"\n  Lock contention events:   {total_waits}")
    print(f"  Locks per round:          {total_locks / ROUNDS:.1f}")
    print(f"  Messages per round:       {total_directed / ROUNDS:.1f}")

    # ── Final verdict ─────────────────────────────────────────────────
    all_ok = all(p.returncode == 0 for p in procs.values())
    msg_target_met = total_directed >= 200
    print(f"\n  Checks:         {passed} passed, {failed} failed")
    print(f"  Agents:         {'ALL OK' if all_ok else 'SOME FAILED'}")
    print(f"  Message target: {total_directed} directed msgs "
          f"({'≥200 ✓' if msg_target_met else '< 200 ✗'})")

    overall = passed == len(checks) and all_ok and msg_target_met
    print(f"\n  {'✓ TEST PASSED' if overall else '✗ TEST FAILED'}")
    print("=" * 76)

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
