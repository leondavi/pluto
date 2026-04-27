# Multi-Agent Teams vs Solo Models

Can a weaker model split across a coordinated team — via Pluto roles, locks,
and a reviewer gate — match or beat a stronger model on multi-file tasks?
This page documents two runs of that experiment, each with a different task.

---

## Experiment 1: Cache Toolkit (v0.2.6)

### Introduction

The first experiment used a small thread-safe in-memory cache toolkit in
pure-stdlib Python, made of three interconnected modules. The task was
chosen specifically to give a multi-Specialist team something to coordinate
on: the two cache classes are independent (no module imports the other), so
they can be implemented in parallel by two Specialists holding disjoint write
locks. The third module (`persist.py`) depends on both, so it can only start
after both cache classes exist — exactly what Pluto's dependency-aware
dispatch enforces.

**Modules**

1. **`src/cache/lru_ttl.py`** — `LruTtlCache(maxsize, ttl_seconds)`: full
   LRU+TTL semantics, capacity-bounded eviction, per-entry TTL expiry on
   `get`, `threading.Lock` thread-safety, `stats()`, and
   `to_snapshot()` / `from_snapshot()` for persistence.
2. **`src/cache/sieve.py`** — `SieveCache(maxsize)`: modern SIEVE eviction
   algorithm. Each entry carries a `visited` bit; on overflow a circular
   *hand* clears visited bits until it finds an unvisited entry to evict.
3. **`src/cache/persist.py`** — `dump(cache, path)` / `load(path) -> cache`.
   JSON snapshot format with a `"kind"` discriminator; `load` dispatches and
   rebuilds the right class via `from_snapshot`.

The canonical pytest suite has 14 cases: 8 for `LruTtlCache`, 3 for
`SieveCache`, and 3 for `persist`.

### Setups

| Setup | Agents | Pluto coordination | Model |
|---|---|---|---|
| **Haiku team** | Orchestrator (Python harness) + 2× Specialist + Reviewer | yes | `claude-haiku-4.5` |
| **Haiku solo** | One Copilot call | no | `claude-haiku-4.5` |
| **Sonnet solo** | One Copilot call | no | `claude-sonnet-4.5` |
| **Opus solo** | One Copilot call | no | `claude-opus-4.7` |

All setups graded against the **same 14-test canonical suite** copied into
each workspace before pytest.

#### Team task DAG

```
t-001 lru_ttl.py  ─┐
                    ├──►  t-003 persist.py
t-002 sieve.py    ─┘
```

`t-001` and `t-002` are dispatched in parallel to two Specialists.
`t-003` is gated on both completing.

### Results (run on 2026-04-24)

| Metric | Haiku team | Haiku solo | Sonnet solo | Opus solo |
|--------|----------:|----------:|------------:|----------:|
| pytest status | ✅ pass | ✅ pass | ✅ pass | ✅ pass |
| tests passed (out of 14) | **14 / 0** | **14 / 0** | **14 / 0** | **14 / 0** |
| copilot calls | 4 | 1 | 1 | 1 |
| total LLM wall-time (s) | 226.4 | 164.8 | 137.2 | **48.8** |
| toolkit bytes / lines (3 mods) | 12 949 / 407 | 12 594 / 405 | 12 448 / 408 | **8 725 / 285** |
| reviewer verdict | `approved` | n/a | n/a | n/a |

Haiku team call breakdown:

| Step | Actor | Task | Wall-time |
|------|-------|------|----------:|
| 1a | `haiku-specialist-1` | `t-001` (`lru_ttl.py`) | 52.0 s |
| 1b | `haiku-specialist-2` | `t-002` (`sieve.py`) | 48.6 s ← parallel with 1a |
| 2 | `haiku-specialist-1` | `t-003` (`persist.py`) | 43.7 s ← waited for 1a + 1b |
| 3 | `haiku-reviewer-1` | review all 3 files | 82.0 s |

Critical-path wall-time: `max(52.0, 48.6) + 43.7 + 82.0 = 177.7 s` — the
parallel dispatch saves ~48 s vs. running the two cache classes sequentially.

### Insights

- **Correctness is not the differentiator at this task size.** Every setup
  passes all 14 cases when the spec is precise enough. The interesting
  questions are *speed*, *conciseness*, and *what the coordination overhead
  buys*.
- **Opus 4.7 dominates raw speed and brevity** (48.8 s, 285 lines). If the
  only metric is "pass the tests as fast as possible with the fewest tokens",
  a single strong-model call wins.
- **Same-model comparison (Haiku solo vs Haiku team)** isolates the
  collaboration tax: ~13 s of extra critical-path time and one extra
  reviewer call, in exchange for an audit trail, real Pluto write locks, and
  a structured reviewer verdict.
- **Parallel multi-Specialist dispatch works end-to-end.** Two Specialists
  held disjoint write locks concurrently (`lru_ttl.py` and `sieve.py` at
  `t≈0.013s` and `t≈0.016s`) and finished within seconds of each other.
  The dependency wait then correctly blocked `persist.py` until both locks
  were released.
- **Inlining `protocol.md`** into the role injection (the v0.2.6 fix) is
  what let the Reviewer Haiku produce a structured JSON verdict from any
  CWD — the Reviewer never needs to locate `protocol.md` on disk.

---

## Experiment 2: Logistics Toolkit (v0.2.6+)

### Introduction

The cache toolkit was small enough that any competent model passed it in a
single solo call, so the team's overhead looked expensive without clear
payback. The second experiment used a genuinely harder task — a logistics
planning toolkit built around three NP-hard optimisation cores — to probe
where coordination *actually* matters.

| Module | Problem | Complexity |
|---|---|---|
| `routing.py` | Capacitated VRP with Time Windows (CVRPTW) | NP-hard |
| `scheduling.py` | Job-Shop Scheduling (JSSP) | NP-hard |
| `graph_paths.py` | Resource-Constrained Shortest Path + Dijkstra | NP-hard + polynomial |
| `integration.py` | End-to-end planner combining the three | composite |
| `api.py` | `run_demo_scenario()` flat-dict facade | trivial |

Explicit acknowledgement of NP-hardness in docstrings is a spec requirement
graded by the canonical suite.

### Setups

| # | Setup | Agents | Pluto coordination | Model |
|---|---|---|---|---|
| 1 | **Haiku team** | Planner + 2× Specialist + Reviewer | yes | `claude-haiku-4.5` |
| 2 | **Sonnet team** | Planner + 2× Specialist + Reviewer | yes | `claude-sonnet-4.6` |
| 3 | **Haiku solo** | One Copilot call | no | `claude-haiku-4.5` |
| 4 | **Sonnet solo** | One Copilot call | no | `claude-sonnet-4.6` |
| 5 | **Opus solo** | One Copilot call | no | `claude-opus-4.7` |

All 5 setups graded against a **byte-identical** canonical pytest suite
(13 cases) copied into every workspace before running pytest.

#### Team task DAG

```
t-001 routing.py     ─┐
t-002 scheduling.py  ─┼──►  t-004 integration.py  ──►  t-005 api.py
t-003 graph_paths.py ─┘
```

`t-001`, `t-002`, `t-003` are dispatched concurrently to the two
Specialists. `t-004` and `t-005` are gated on completion of their parents.

### Results (run on 2026-04-24, ~47 min wall-time)

| Metric | Haiku team | Sonnet team | Haiku solo | Sonnet solo | Opus solo |
|---|---:|---:|---:|---:|---:|
| pytest status | `pass` | `pass` | `pass` | `pass` | `pass` |
| tests passed (out of 13) | 13 | 13 | 13 | 13 | 13 |
| copilot calls | 2 | 7 | 1 | 1 | 1 |
| total LLM wall-time (s) | 90.6 | 261.2 | 67.5 | 28.0 | 40.7 |
| toolkit bytes / lines (5 mods) | 30 885 / 915 | 32 259 / 973 | 32 483 / 918 | 27 775 / 850 | 21 305 / 650 |
| reviewer verdict | `approved` | `approved` | n/a | n/a | n/a |
| Pluto lock acquire/release | 0¹ | 10 | 0 | 0 | 0 |

¹ See *Anomaly* below.

### Anomaly: Haiku team Specialists never received their assignments

The trace shows the Haiku Planner finished in 16.3 s, the orchestrator sent
three `task_assigned` messages — and then **no `task_result` ever came back**
from the Specialists. After the 40-minute deadline, the Reviewer Copilot ran
for 74 s and (launched with `--allow-all-tools --allow-all-paths`) **wrote
all five modules itself during what was supposed to be a code review**. pytest
then passed 13/13.

The Haiku-team column passes, but it is not a real team result — it is
"Reviewer Copilot doing solo work in a workspace it was meant to inspect."
This is a concrete finding: **giving a Reviewer agent unrestricted
filesystem tools can silently mask team-wide coordination failures.** A
future fix should either run the Reviewer with read-only tools or treat any
`<missing>` files in the review prompt as a hard failure before the LLM call.

The **Sonnet team** had no such issue: 5 Specialist tasks dispatched and
completed in dependency order, all 10 lock acquire/release pairs landed on
the real Pluto server, and the Reviewer pass produced substantive suggestions
(4 nits, including a real bug in `check_schedule_feasibility` where partial
schedules were silently accepted).

### Insights

- **For a spec at this size, all solo runs pass.** Single-shot calls from
  Haiku, Sonnet, and Opus all pass 13/13. The team setup's payoff shows up
  in *artefact quality* (reviewer-found bugs, explicit complexity contracts)
  and *evidence of real Pluto coordination* (lock events, planner→specialist
  handoffs) — not in a green/red pass rate.
- **Sonnet solo (28 s) beats Haiku solo (67.5 s) and the Haiku team
  (90.6 s).** On raw speed for well-specified tasks, throwing a stronger
  model at the problem is the cheapest path to a passing implementation.
- **The Sonnet team's value proposition** is the reviewer verdict with 4
  concrete code-quality findings and the complete audit trail, at the cost
  of ~261 s and 7 LLM calls vs 28 s and 1 call for Sonnet solo.
- **The anomaly is as instructive as the success.** The Haiku team silently
  degraded into a solo run because the Reviewer had write access. The Sonnet
  team exposed this contrast cleanly. A reviewer-role constraint (read-only
  tools, hard-fail on missing files) is now a tracked improvement.

---

## Cross-experiment summary

| Dimension | Cache toolkit (Exp 1) | Logistics toolkit (Exp 2) |
|---|---|---|
| Task size | 3 modules, ~300–410 lines | 5 modules, ~650–973 lines |
| All solos pass? | Yes | Yes |
| Team adds unique value | Audit trail, locks, reviewer verdict | Same + reviewer found a real bug |
| Strongest solo | Opus (48.8 s, 285 lines) | Sonnet (28 s, 850 lines) |
| "Collaboration tax" | ~13 s critical-path overhead vs Haiku solo | Haiku team degraded; Sonnet team 261 s vs 28 s |
| Key finding | Parallel lock dispatch works end-to-end | Reviewer write-access can mask coordination failure |

**Bottom line:** A single top-tier model is still the fastest path to a
passing implementation when correctness on a small, well-specified task is
the only goal. The Pluto stack earns its keep when you need multi-file
coordination, real write locks, dependency gating, and a reviewer gate that
produces an auditable verdict — not when you're racing one model against the
clock on a few small modules.

---

## How to re-run

```bash
# Restart Pluto on the demo ports (config in config/pluto_config.json)
./PlutoServer.sh --kill
./PlutoServer.sh --daemon

# Cache toolkit (Experiment 1)
PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_multiagent_vs_solo -s

# Logistics toolkit (Experiment 2)
PLUTO_HAIKU_MODEL=claude-haiku-4.5 \
PLUTO_SONNET_MODEL=claude-sonnet-4.6 \
PLUTO_OPUS_MODEL=claude-opus-4.7 \
PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_logistics_multiagent_vs_solo -s
```

Outputs land under `/tmp/pluto/demo/multiagent_vs_solo/` and
`/tmp/pluto/demo/logistics_multiagent_vs_solo/` respectively
(workspace subdirectories, `trace.json`, auto-generated report).

Sources:
- [tests/demo_multiagent_vs_solo](../../tests/demo_multiagent_vs_solo)
- [tests/demo_logistics_multiagent_vs_solo](../../tests/demo_logistics_multiagent_vs_solo)
