# Corruption Test Results

**Date:** 2026-03-31
**Server:** Pluto 0.1.0 (Erlang/OTP) on `127.0.0.1:9000`
**Test file:** `tests/demo_corruption/test_corruption.py`

**Experiment parameters:** 4 agents × 25 read-modify-write cycles × 10 appends per cycle = 1000 expected lines.

---

## Part 1 — WITHOUT Lock Coordination

4 agents (`writer-1` through `writer-4`) each perform 25 read-modify-write
cycles on the same file **without acquiring locks**. Each cycle reads the
entire file, appends 10 lines in memory, and writes the whole file back.

| Metric             | Value    |
|--------------------|----------|
| Writers            | 4        |
| Cycles per writer  | 25       |
| Appends per cycle  | 10       |
| Lines per writer   | 250      |
| Expected total     | 1000     |
| Actual lines       | 80       |
| Unique lines       | 80       |
| **Lines LOST**     | **920**  |

Per-writer breakdown:

| Writer   | Surviving Lines | Expected |
|----------|-----------------|----------|
| writer-1 | 30              | 250      |
| writer-2 | 0               | 250      |
| writer-3 | 0               | 250      |
| writer-4 | 50              | 250      |

**Result: PASS** — test correctly demonstrates that uncoordinated concurrent
read-modify-write causes massive data loss (920 of 1000 lines lost, 92%).

---

## Part 2 — WITH Pluto Lock Coordination

4 agents (`writer-1` through `writer-4`) each perform 25 lock-protected cycles
(10 appends per cycle) to a shared file, acquiring an exclusive Pluto lock
(`acquire` / `release`) around every cycle.

| Metric             | Value    |
|--------------------|----------|
| Writers            | 4        |
| Cycles per writer  | 25       |
| Appends per cycle  | 10       |
| Lines per writer   | 250      |
| Expected total     | 1000     |
| Actual lines       | 1000     |
| Unique lines       | 1000     |
| **Lines LOST**     | **0**    |

Per-writer breakdown:

| Writer   | Surviving Lines | Expected |
|----------|-----------------|----------|
| writer-1 | 250             | 250      |
| writer-2 | 250             | 250      |
| writer-3 | 250             | 250      |
| writer-4 | 250             | 250      |

**Result: PASS** — Pluto lock coordination preserved all 1000 lines with zero
data loss.

### Pluto Server Statistics

| Counter              | Value |
|----------------------|-------|
| Locks acquired       | 120   |
| Locks released       | 120   |
| Lock waits           | 118   |
| Locks expired        | 0     |
| Locks renewed        | 0     |
| Deadlocks detected   | 0     |
| Deadlock victims     | 0     |
| Messages sent        | 0     |
| Broadcasts sent      | 0     |
| Agents registered    | 17    |
| Agents disconnected  | 16    |
| Total requests       | 260   |

**Live state at test end:**

| Metric            | Value |
|-------------------|-------|
| Active locks      | 0     |
| Connected agents  | 1     |
| Pending waiters   | 0     |
| Wait graph edges  | 0     |

Per-agent lock stats (from server):

| Agent    | Locks Acquired | Locks Released | Registrations | Disconnections |
|----------|----------------|----------------|---------------|----------------|
| writer-1 | 30             | 30             | 4             | 4              |
| writer-2 | 30             | 30             | 4             | 4              |
| writer-3 | 30             | 30             | 4             | 4              |
| writer-4 | 30             | 30             | 4             | 4              |

> Note: Each agent shows 30 lock acquires (not 25) because the server counts
> include prior test runs within the same server session. Lock waits (118 of
> 120 acquires) confirm heavy contention across agents.

---

## Summary

| Scenario            | Expected Lines | Actual Lines | Lines Lost | Loss % | Test Result |
|---------------------|----------------|--------------|------------|--------|-------------|
| Without Pluto locks | 1000           | 80           | 920        | 92.0%  | PASS        |
| With Pluto locks    | 1000           | 1000         | 0          | 0.0%   | PASS        |

```
----------------------------------------------------------------------
Ran 2 tests in 0.146s

OK
```
