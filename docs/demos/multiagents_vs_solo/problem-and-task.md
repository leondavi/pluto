# 1. Problem & Task — Logistics Toolkit Benchmark

This demo asks one question:

> *Can a multi-agent team — with two agents that pursue **conflicting**
> objectives and a third that **reconciles** them — produce a better
> plan than a single solo LLM call on a task where the right answer
> requires explicit trade-off reasoning?*

## The benchmark task

Build a small **logistics & network-planning toolkit** in pure-stdlib
Python under `src/logistics/`. Five required modules, NP-hard cores in
three of them:

| Module | Problem | Complexity class |
|---|---|---|
| `routing.py` | Capacitated VRP with Time Windows (CVRPTW) | NP-hard |
| `scheduling.py` | Job-Shop Scheduling (JSSP) | NP-hard |
| `graph_paths.py` | Resource-Constrained Shortest Path | NP-hard |
| `graph_paths.py` | Unconstrained shortest path | polynomial (Dijkstra) |
| `integration.py` | End-to-end planner combining the three | composite |
| `api.py` | `run_demo_scenario()` flat-dict facade | trivial |

NP-hardness must be acknowledged in module docstrings — solvers must be
honest heuristics, not claim polynomial-time global optimality.

## What makes this benchmark hard for solos

A single LLM call given a multi-objective spec almost always collapses
the problem to **one scalar objective** and "forgets" the trade-off
nuances. To test that explicitly, the spec requires the planner to
balance **conflicting** objectives:

* **Routing trade-off:** `distance` vs `co2` vs `lateness_penalty`
  (sum of `max(0, arrival_time - time_window_close)` over all visited
  customers).
* **Scheduling trade-off:** `makespan` vs `energy` (machine-busy-time)
  vs `overtime` (sum of `max(0, end - shift_end)` over operations).

These six numbers MUST be returned by two named functions
(`routing_objectives()`, `scheduling_objectives()`) — they are *scalar
components*, not a single weighted sum, so callers can combine them
with their own weights.

### Why the collapse happens

When a solo LLM receives the full spec in a single prompt it must
produce all five modules in one pass. Under that pressure it
mentally picks one "obvious" winner — almost always **total distance**
for routing and **makespan** for scheduling — and builds everything
around that scalar. The other objectives become:

* **Proportional aliases** — e.g. `co2 = distance * 0.21`, so they
  always move in lockstep and can never diverge.
* **Structural zeros** — `lateness_penalty` and `overtime` are
  simply returned as `0.0` because the built-in scenario has generous
  time windows and shift boundaries. The code is not wrong; it just
  never has a reason to produce a non-zero value.
* **Identical candidates** — `plan_cost_optimized()` and
  `plan_service_optimized()` run the same heuristic on the same
  scenario and return the same plan under different names.

The result passes 13 of 17 pytest cases (everything that checks
feasibility and cost signs) but fails **tests 14–17** — the trade-off
discriminator — because the two candidate objective vectors are
identical within floating-point noise.

### Why the team avoids it

The multi-agent setup makes collapse structurally impossible by
assigning **constitutionally opposed mandates** to different agents:

| Agent | Mandate | Allowed cost |
|---|---|---|
| **CostOptimizer** | Minimise distance, energy, machine time | May incur lateness or overtime |
| **ServiceReliability** | Maximise on-time delivery, minimise overtime | May spend more distance or energy |
| **MetaPlanner** | Reconcile both candidates | Never writes a heuristic — only reasons |

No agent ever sees the full spec. CostOptimizer has no instruction
about on-time delivery, so it genuinely ignores it. ServiceReliability
has no instruction about distance, so it genuinely ignores that. The
divergence between their objective vectors is not hoped-for emergent
behaviour — it is **enforced by what each agent was never told**.

## The integrated planner: two candidates + reconciliation

The end-to-end planner does NOT pick one global objective. It exposes:

```python
plan_cost_optimized()    -> dict   # leans toward distance / energy / machine time
plan_service_optimized() -> dict   # leans toward on-time delivery, slack, low overtime
plan_end_to_end(weights) -> dict   # reconciles BOTH candidates into one integrated plan
```

The reconciled plan's `metrics` MUST surface:

* `tradeoff_components` — the full 6-key objective vector of the chosen plan.
* `alternatives` — a list with **both** candidate proposals' full
  objective vectors and one-sentence rationales.
* `chosen` — the name of the selected option.
* `rationale` — one sentence describing why this trade-off was preferred.

`api.run_demo_scenario(weights)` mirrors the same trade-off summary in a
flat dict at `summary["tradeoff_summary"]`.

## How the team mirrors the problem structure

The team setup is designed to mirror the trade-off:

| Agent | Role | Bias |
|---|---|---|
| **Planner** | Writes the complexity contract | Honest about NP-hardness |
| **Specialist** ×2 | Implement the three NP-hard engines (routing / scheduling / graph_paths) | None — implement the spec |
| **CostOptimizer** | Writes `plan_cost_optimized()` | Minimal operational cost (distance, energy, machine time); may incur lateness or overtime |
| **ServiceReliability** | Writes `plan_service_optimized()` | On-time delivery, slack, low overtime; may cost more distance or energy |
| **MetaPlanner** | Writes `plan_end_to_end(weights)` reconciliation | Reasons explicitly about the trade-off; chooses one or weighted reconciliation; emits rationale |
| **Reviewer** | One LLM pass over all 5 modules | Grades trade-off honesty, NP-hardness disclosure, and obvious bugs |

Solo runs receive the **same spec** in a single Copilot prompt and must
produce all of the above by themselves.

## Grading: the canonical pytest suite (17 cases)

Every setup is graded against a **byte-identical** copy of
[`canonical_test_logistics_toolkit.py`](../../../tests/demo_logistics_multiagent_vs_solo/canonical_test_logistics_toolkit.py)
the harness drops into each workspace immediately before pytest. The
suite is **never visible to the agents in advance**.

Coverage:

| # | Category | Cases | What it grades |
|---|---|---:|---|
| 1 | Routing engine | 4 | distance matrix, capacity feasibility, cost ≥ 0, improve never worsens |
| 2 | Scheduling engine | 3 | precedence + no machine overlap, makespan > 0, improve preserves feasibility |
| 3 | Graph paths | 3 | Dijkstra picks low-cost, infeasible returns None, feasible respects risk cap |
| 4 | Integration & API | 3 | combined plan keys, internal feasibility, API summary shape |
| 5 | **Multi-objective trade-off** | **4** | `routing_objectives` + `scheduling_objectives` 6-key breakdowns; `alternatives` list with cost ≠ service vectors; `tradeoff_summary` mirror in API |

The 4 trade-off cases (test 14–17) are the **discriminator** — they are
new in this iteration of the demo and exist specifically to detect the
"collapsed-to-one-scalar" failure mode.

Test #17 in particular asserts that `cost_optimized` and
`service_optimized` candidates differ by more than `1e-6` on at least
one of the six objective components (epsilon comparison, not strict
inequality — so floating-point noise alone never trips it). The spec
requires the built-in scenario to be calibrated tightly enough
(at least one binding time-window or shift-end constraint) for that
divergence to actually occur.

## What "winning" means here

There is no single winner — the demo deliberately measures multiple
dimensions:

| Dimension | What we measure |
|---|---|
| Correctness | pytest pass/fail, count of cases passed (out of 17) |
| Speed | total LLM wall-time across all calls |
| Coordination evidence | number of real Pluto `/locks/{acquire,release}` events |
| Reviewer value-add | substantive findings produced by the team Reviewer that pytest cannot see |
| **Self-diagnosis** | `tradeoff_bug_flagged` — whether the team Reviewer explicitly identified the trade-off-collapse failure mode (independent of pytest) |
| Trade-off honesty | whether candidates' objective vectors actually differ on the built-in scenario, or collapse to identical numbers |

Insights and the actual run table live in
[`logitstics_multiagent_vs_solo_demo.md`](logitstics_multiagent_vs_solo_demo.md).
The Pluto coordination layer the teams are running on is described in
[`pluto-setup-and-usage.md`](pluto-setup-and-usage.md).
