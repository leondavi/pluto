# The Task Claim Race — Demo Summary

## Overview

Three **real Copilot CLI agents** (**Analyst**, **Classifier**, **Publisher**)
all compete to claim and process the same 20 jobs simultaneously. Each agent
is a separate `copilot` process that autonomously executes a Python worker
script. The demo runs in two modes to illustrate the difference between
coordinated and uncoordinated concurrency.

| Property | Pluto mode | Chaos mode (`--no-pluto`) |
|---|---|---|
| Agent runtime | Real Copilot CLI (`copilot -p ...`) | Real Copilot CLI (`copilot -p ...`) |
| Coordination | PlutoServer distributed locks | None — shared-file ledger, no locking |
| Duplicate jobs | **0** | **18** (TOCTOU race across processes) |
| Fencing tokens | Unique, globally ordered | Not available |
| Exit code | 0 | 1 (duplicates detected) |

---

## Architecture

```
┌──────────────┐         ┌──────────────────────────┐
│  Launcher    │───┬────►│  copilot -p "run worker"  │  ← Analyst
│ pluto_demo.py│   │     │     agent_worker.py       │
│              │   ├────►│  copilot -p "run worker"  │  ← Classifier
│              │   │     │     agent_worker.py       │
│              │   └────►│  copilot -p "run worker"  │  ← Publisher
│              │         │     agent_worker.py       │
└──────┬───────┘         └────────────┬─────────────┘
       │ collects JSON results        │ raw TCP socket
       ▼                              ▼
  per-agent .json           ┌────────────────────┐
  files in /tmp/            │   PlutoServer      │
  pluto_20jobs/             │   (Erlang, :9000)  │
                            └────────────────────┘
```

Each Copilot agent receives a prompt telling it to execute `agent_worker.py`
with the appropriate `--agent-id` and `--mode`. The worker connects directly
to the PlutoServer over a raw TCP socket using the newline-delimited JSON
wire protocol. File-based barriers synchronise startup and completion so all
three agents race simultaneously. `agent_worker.py` is the per-agent worker,
not the primary demo entrypoint; the full demo is driven by `pluto_demo.py`,
which launches all three agents and clears `/tmp/pluto_20jobs` first.

---

## Agents

| Agent | Role |
|---|---|
| Analyst | Data analysis tasks (summarise, score, predict) |
| Classifier | Classification and tagging tasks |
| Publisher | Publishing, reporting, and formatting tasks |

All three agents attempt all 20 jobs. Only job assignment differs between modes.

---

## How It Works

### Pluto Mode (coordinated)

```
Worker ──► PlutoServer: register
Worker ──► file barrier: wait for all 3 agents ("ready")
Worker ──► PlutoServer: try_acquire(job:NNN)
                       ├── status: ok         →  claim job, hold lock
                       └── status: unavailable  →  skip (another agent holds it)
Worker ──► file barrier: wait for all 3 agents ("done")
Worker ──► PlutoServer: release(lock_ref)  ×N  (batch release)
Worker ──► disconnect
```

Each agent shuffles the job list to reduce head-of-line contention.
If a `try_acquire` returns `unavailable`, the agent skips that job — another
agent already holds the lock. Agents hold all acquired locks until every agent
has finished claiming, then batch-release. This prevents a fast agent's
released locks from being re-acquired by a slower agent. All 20 jobs are
distributed across the three agents with **zero duplicates** and every claim
receives a globally unique fencing token.

### Chaos Mode (no Pluto)

```
Worker ──► file barrier: wait for all 3 agents ("ready")
Worker ──► for each job:
             read shared ledger (JSON file)
             ╌╌╌ TOCTOU window (0 – 30 ms) ╌╌╌
             write claim to shared ledger
```

All three agents iterate through jobs in the same order to maximise contention.
A random `sleep(0 – 30 ms)` between the check and the act creates a realistic
time-of-check-time-of-use (TOCTOU) race window. Multiple agents observe the
same job as "unclaimed", then all process it independently.

---

## Comparison

### Correctness

| Metric | Pluto | Chaos |
|---|---|---|
| Jobs processed exactly once | ✅ 20/20 | ❌ many jobs processed 2–3× |
| Fencing token per claim | ✅ unique, ordered | ❌ none |
| Duplicate detection built-in | ✅ server-enforced | ❌ only post-hoc |

### Real-World Consequences of Duplicates

When a job is processed more than once without coordination, the following
can occur in production systems:

- **Duplicate API calls** — wasted compute and rate-limit headroom.
- **Double-charged billing** — customers invoiced twice for the same operation.
- **Conflicting outputs** — two agents write different results to the same file.
- **Downstream data pollution** — pipelines receive and propagate duplicate rows.
- **Inconsistent reports** — aggregation over double-counted records inflates totals.
- **Phantom audit entries** — compliance logs record work that should not exist.

### Performance

Each Copilot agent process takes ~15–20 s total (including AI model spin-up).
The actual job-claiming phase completes in under 1 second per agent. The Pluto
mode adds one TCP round-trip per lock operation (~0.5 ms on localhost), which
is negligible compared to the Copilot startup overhead.

---

## How to Run

### Prerequisites

- Python 3.8+
- PlutoServer running on `127.0.0.1:9000`
- Copilot CLI available on `PATH` (installed via VS Code Copilot extension)

### Start the server

```bash
./PlutoServer.sh --daemon
```

### Run coordinated mode (Pluto)

```bash
python tests/demo_20jobs/pluto_demo.py
```

### Run chaos mode (no Pluto)

```bash
python tests/demo_20jobs/pluto_demo.py --no-pluto
```

### About running `agent_worker.py` directly

```bash
python tests/demo_20jobs/agent_worker.py --agent-id Analyst --mode chaos \
  --host 127.0.0.1 --port 9000 --output-dir /tmp/pluto_20jobs --num-agents 3
```

That command runs only one worker process. By itself it does **not** reproduce
the documented race demo unless the other two peer workers are started at the
same time against a **clean** output directory. The launcher script handles the
required setup:

- removes stale barrier files such as `*.ready` / `*.done`
- removes any prior `chaos_ledger.json`
- starts **Analyst**, **Classifier**, and **Publisher** together

If you run a single worker manually against a reused `/tmp/pluto_20jobs`, it
may immediately see stale barrier or ledger state and write `0` results even
though the full three-agent demo still behaves as documented.

### Custom host / port

```bash
python tests/demo_20jobs/pluto_demo.py --host 10.0.0.5 --port 4000
```

---

## Output

### Pluto mode — actual result from `pluto_demo.py`

```
  ╔════════════════════════════════════════════════════╗
  ║  THE TASK CLAIM RACE — 20 Jobs × 3 Copilot Agents  ║
  ║  Mode: PLUTO (coordinated)                        ║
  ╚════════════════════════════════════════════════════╝

  JOB LEDGER
  Job ID     Agent          Fencing Token    Job Name
  ────────── ────────────── ──────────────── ──────────────────────────────
  job:001  Classifier     236              Summarise Q1 report
  job:002  Analyst        241              Classify support tickets
  job:003  Analyst        237              Generate weekly digest
  job:004  Publisher      240              Score lead candidates
  ...

  SUMMARY (Pluto mode)
  Duplicates detected  : 0
  Fencing tokens unique : YES  (sorted: 226..245)
  Unique agents worked : 3

  Per-agent job counts:
    Analyst        4 jobs
    Classifier     9 jobs
    Publisher      7 jobs
```

### Chaos mode — actual result from `pluto_demo.py`

```
  ╔════════════════════════════════════════════════════╗
  ║  THE TASK CLAIM RACE — 20 Jobs × 3 Copilot Agents  ║
  ║  Mode: CHAOS (no Pluto)                           ║
  ╚════════════════════════════════════════════════════╝

  JOB LEDGER
  Job ID     Agent          Fencing Token    Job Name
  ────────── ────────────── ──────────────── ──────────────────────────────
  job:001  Classifier     —                Summarise Q1 report
  job:002  Classifier     —                Classify support tickets
  job:003  Analyst        —                Generate weekly digest  (+1 dup)
  job:004  Analyst        —                Score lead candidates  (+2 dup)
  ...

  SUMMARY (Chaos mode — no Pluto)
  Total duplicates     : 18

  Duplicate details:
    ✗ job:004 processed by Analyst AND Classifier AND Publisher
    ✗ job:005 processed by Analyst AND Classifier AND Publisher
    ...

  Per-agent job counts (including duplicates):
    Analyst        18 jobs
    Classifier     19 jobs
    Publisher      16 jobs
```

---

## Key Takeaways

1. **Distributed locks eliminate duplicate work.** The PlutoServer `try_acquire`
   operation is atomic — exactly one agent receives `status: ok` for a given
   resource at any point in time.

2. **Fencing tokens prevent stale writes.** Even if a lock lease expires and a
   second agent acquires the same resource, downstream systems can reject the
   stale holder's writes by comparing fencing tokens.

3. **TOCTOU races are silent and pervasive.** Without a coordination server,
   the check-then-act pattern appears correct on the surface but fails under
   real concurrency. The 0–30 ms sleep in chaos mode models network jitter and
   scheduling delays that every distributed system experiences.
