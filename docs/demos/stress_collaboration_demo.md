# Stress Collaboration Demo — Multi-Agent Coordination at Scale

## Overview

This demo validates Pluto's ability to coordinate multiple AI agents performing complex, overlapping work with **200+ directed messages**, **100+ lock operations**, and **shared resource contention** — all within seconds.

Three agents (a **coordinator** and two **workers**) execute **10 sprint rounds** of collaborative work involving nested locking, cross-reviews, peer scoring, shared scoreboard contention, and broadcast summaries.

## Agents

| Agent | Role | Key Behaviors |
|-------|------|---------------|
| `coordinator` | Sprint orchestrator | Assigns tasks, collects results/reviews, approves work, publishes leaderboards, updates dashboard/metrics, broadcasts round summaries |
| `worker-alpha` | Worker A | Acquires nested locks (design-doc → code-repo), writes results, cross-reviews beta's work, exchanges peer scores, updates shared scoreboard |
| `worker-beta` | Worker B | Acquires nested locks (code-repo → design-doc), writes results, cross-reviews alpha's work, exchanges peer scores, updates shared scoreboard |

## Per-Round Message Flow

Each of the 10 rounds involves:

1. **Task assignment** — coordinator → both workers (2 msgs)
2. **Task acknowledgements** — workers → coordinator (2 msgs)
3. **Nested lock acquire/release** — each worker acquires 2 locks in opposite order (4 acquire + 4 release per round)
4. **Result submission** — workers → coordinator (2 msgs)
5. **Cross-review dispatch** — coordinator → both workers (2 msgs)
6. **Review submission** — workers → coordinator (2 msgs)
7. **Approval notifications** — coordinator → both workers (2 msgs)
8. **Peer score exchange** — workers ↔ workers (2 msgs)
9. **Score update confirmations** — workers → coordinator (2 msgs)
10. **Leaderboard distribution** — coordinator → both workers (2 msgs)
11. **Dashboard + metrics lock** — coordinator acquires/releases 2 locks per round
12. **Shared scoreboard contention** — each worker acquires/releases scoreboard lock
13. **Broadcast round summary** — coordinator → all (1 broadcast)

## Resource Locking Pattern

```
Workers (nested, opposite order to test contention):
  worker-alpha: design-doc → code-repo  (acquire both, release both)
  worker-beta:  code-repo → design-doc  (acquire both, release both)

Coordinator (sequential):
  dashboard-lock  (update sprint dashboard)
  metrics-lock    (update quality metrics)

Shared contention:
  scoreboard-lock (both workers compete to update shared scoreboard)
```

## Test Results

```
============================================================
  STRESS COLLABORATION TEST RESULTS
============================================================

Messages:      226 directed  +  11 broadcasts
Locks:         112 acquired / 112 released
Total ops:     468 requests
Duration:      1.91 seconds
Verifications: 14/14 passed

Per-agent breakdown:
  coordinator   — 65 msgs sent, 62 msgs received, 40 locks
  worker-alpha  — 52 msgs sent, 41 msgs received, 40 locks
  worker-beta   — 52 msgs sent, 41 msgs received, 40 locks
```

### Verification Checks (14/14 Passed)

1. Coordinator sent ≥ 60 directed messages
2. Each worker sent ≥ 40 directed messages
3. ≥ 10 broadcast messages sent
4. ≥ 100 total lock acquires
5. All locks released (acquired == released)
6. ≥ 200 total directed messages across all agents
7. Coordinator received results from both workers every round
8. Cross-reviews exchanged every round
9. Peer scores exchanged between workers
10. Leaderboards distributed every round
11. Dashboard updated every round
12. Scoreboard contention resolved (both workers updated)
13. All 10 rounds completed
14. Total request count ≥ 400

## How to Run

### Prerequisites
- Pluto server running (Erlang/OTP)
- Python 3.10+

### Start the server
```bash
bash PlutoServer.sh --daemon
```

### Run the stress test
```bash
cd /workspaces/pluto
python tests/demo_stress_collab/run_stress_test.py
```

The test spawns three independent Python processes, each connecting to the Pluto server via TCP on port 9000. After all rounds complete, it queries the server's `/stats` endpoint and runs all 14 verification checks.

## Architecture Notes

- Each agent is a **separate OS process** with its own TCP connection — no shared memory
- All coordination happens exclusively through Pluto's messaging and locking primitives
- The opposite lock-ordering pattern (alpha: design→code, beta: code→design) deliberately creates contention to validate Pluto's lock management under pressure
- Broadcast messages are used for round summaries, simulating a shared event log
