# Corruption Test Results

**Date:** 2026-03-31
**Server:** Pluto 0.1.0 (Erlang/OTP) on `127.0.0.1:9000`
**Test file:** `tests/demo_corruption/test_corruption.py`

---

## Part 1 — WITHOUT Lock Coordination

4 agents (`writer-1` through `writer-4`) each perform 25 read-modify-write
cycles on the same file **without acquiring locks**. Each cycle reads the
entire file, appends one line in memory, and writes the whole file back.

| Metric             | Value   |
|--------------------|---------|
| Writers            | 4       |
| Lines per writer   | 25      |
| Expected total     | 100     |
| Actual lines       | 2       |
| Unique lines       | 2       |
| **Lines LOST**     | **98**  |

Per-writer breakdown:

| Writer   | Surviving Lines | Expected |
|----------|-----------------|----------|
| writer-1 | 1               | 25       |
| writer-2 | 1               | 25       |
| writer-3 | 0               | 25       |
| writer-4 | 0               | 25       |

**Result: PASS** — test correctly demonstrates that uncoordinated concurrent
read-modify-write causes massive data loss (98 of 100 lines lost).

---

## Part 2 — WITH Pluto Lock Coordination

4 agents (`writer-1` through `writer-4`) each append 5 lines to a shared file,
acquiring an exclusive Pluto lock (`acquire` / `release`) around every write.

| Metric             | Value   |
|--------------------|---------|
| Writers            | 4       |
| Lines per writer   | 5       |
| Expected total     | 20      |
| Actual lines       | 20      |
| Unique lines       | 20      |
| **Lines LOST**     | **0**   |

Per-writer breakdown:

| Writer   | Surviving Lines | Expected |
|----------|-----------------|----------|
| writer-1 | 5               | 5        |
| writer-2 | 5               | 5        |
| writer-3 | 5               | 5        |
| writer-4 | 5               | 5        |

**Result: PASS** — Pluto lock coordination preserved all 20 lines with zero
data loss.

---

## Summary

| Scenario            | Expected Lines | Actual Lines | Lines Lost | Test Result |
|---------------------|----------------|--------------|------------|-------------|
| Without Pluto locks | 100            | 2            | 98         | PASS        |
| With Pluto locks    | 20             | 20           | 0          | PASS        |

Both tests ran in **0.018 s** total. All 2 tests passed.

```
----------------------------------------------------------------------
Ran 2 tests in 0.018s

OK
```
