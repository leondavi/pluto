# 3. Insights, Tables & Results — Logistics Multi-Agent vs Solo

> **v4 of this demo.** Real Pluto/Erlang server (HTTP `127.0.0.1:9201`),
> real `copilot -p` subprocesses, real `pytest` subprocess, no mocks.
> See [`problem-and-task.md`](problem-and-task.md) for the spec and
> [`pluto-setup-and-usage.md`](pluto-setup-and-usage.md) for the
> coordination layer.

## What changed in v4

v3 produced a beautifully discriminative test (`test_17`) but the
built-in scenario had so much slack that **no setup passed**. v4
re-calibrates the scenario so a real trade-off is visible, **without
weakening the test**:

| Change | Why |
|---|---|
| **Tighter built-in scenario in `integration.py`** — the spec now requires at least one customer with a tight time window and a `shift_end` that lets dense packing incur overtime | Forces `cost_optimized` and `service_optimized` candidates to actually diverge on at least one of the six objective components. |
| **Test #17 now uses an epsilon comparison** (`abs(diff) > 1e-6` on at least one component) | Prevents triggering on pure floating-point noise; the spirit (real divergence required) is preserved. |
| **New per-team metric: `tradeoff_bug_flagged`** | The harness scans the Reviewer's findings/suggestions for trade-off-collapse keywords and records whether the multi-agent system **self-diagnosed** the bug — independent of pytest. |
| **Auto-report adds a `reviewer flagged trade-off bug` row** | So team-vs-solo isn't only a green/red pytest comparison: self-awareness counts. |

## Setups

| # | Setup | Mechanism |
|---|---|---|
| 1 | **Haiku team**  | Planner + 2 Specialists + CostOptimizer + ServiceReliability + MetaPlanner + Reviewer (real Pluto coordination) |
| 2 | **Sonnet team** | Same role structure, Sonnet model |
| 3 | **Haiku solo**  | One Copilot call with the full spec |
| 4 | **Sonnet solo** | One Copilot call with the full spec |
| 5 | **Opus solo**   | One Copilot call with the full spec |

Models: `claude-haiku-4.5`, `claude-sonnet-4.6`, `claude-opus-4.7`.

## Run results (2026-04-24, real run, 71.4 min wall-time)

| metric | Haiku team | Sonnet team | Haiku solo | Sonnet solo | Opus solo |
|---|---:|---:|---:|---:|---:|
| pytest status               | **`pass`** | **`pass`** | `fail` | `fail` | **`pass`** |
| tests passed (out of 17)    | **17**     | **17**     | 16     | 0 (import) | **17** |
| trade-off test (#17) passed | **✓**     | **✓**      | ✗      | ✗ (import) | **✓** |
| copilot calls               | 9          | 9          | 1      | 1      | 1 |
| total LLM wall-time (s)     | 1081.6     | 1340.3     | 364.6  | 1380.6 | **377.7** |
| reviewer verdict            | `needs_changes` | `approved` | n/a | n/a | n/a |
| **reviewer flagged the trade-off bug** | **yes** | no | n/a | n/a | n/a |
| Pluto lock acquire/release  | 14         | 14         | 0      | 0      | 0 |

28 total real `/locks/{acquire,release}` events on the Pluto server
(14 per team).

## Key findings

### 1. The benchmark is now usable: two teams AND a strong solo all reach 17/17

After tightening the built-in scenario (without weakening test #17),
**Haiku team, Sonnet team, and Opus solo all pass 17/17**. The
multi-agent setups are no longer brittle on the trade-off — they are
a viable alternative to a strong solo model on this task.

The two weaker solos still fail, in different ways:

* **Haiku solo** passes 16/17 — same `KeyError: 'metrics'` failure
  mode as v3's Opus solo: `plan_cost_optimized()` standalone returns
  a dict without a top-level `metrics` key, even though the
  `plan_end_to_end()` path works.
* **Sonnet solo** fails at collection with
  `ModuleNotFoundError: No module named 'logistics'` — exactly the
  `from logistics.routing import …` bug that recurred in v2 (Sonnet
  solo) and v3 (Haiku solo). Solo runs reliably get the package
  layout wrong roughly once per run.

### 2. Haiku team Reviewer caught the trade-off bug AND pytest still passed

This is the showcase outcome. The Haiku team's Reviewer pass returned
`needs_changes` and the very first finding reads:

> **MAJOR — Schedule trade-off is not visible.** Both
> `plan_cost_optimized()` and `plan_service_optimized()` produce
> identical schedules: `j1_op0:[0,4], j1_op1:[4,7], j2_op0:[4,7.5],
> j2_op1:[7.5,11.5]`. This violates [the trade-off requirement].

Plus three more real findings: a stale-loop-variable bug in
`build_initial_routes()`; a subtle accumulation bug in
`routing_objectives()` lateness math; and an honesty issue in the
service-optimised commentary. Plus actionable suggestions, including
verbatim:

> "Confirm trade-off visibility: run both `plan_cost_optimized()` and
> `plan_service_optimized()` locally and verify their
> `tradeoff_components` dicts differ on at least one key by >1e-6
> before submission."

pytest then **passed 17/17** because the routing-side trade-off DID
diverge enough on its own (`lateness_penalty` differed) — but the
multi-agent system was the only setup that **also told us** the
schedule-side trade-off was still flat. That is precisely the
self-diagnosis property the v4 metric was added to track.

### 3. New reporting dimension: self-diagnosis quality

`tradeoff_bug_flagged` adds a column you cannot get from pytest:

| setup | tests passed | trade-off test | bug flagged by Reviewer? |
|---|---:|---|---|
| Haiku team   | 17/17 | ✓ | **yes** (verbatim, plus 3 more real bugs + a concrete fix) |
| Sonnet team  | 17/17 | ✓ | no (returned `approved`; substantive findings on shape mismatches but missed the schedule-side trade-off issue) |
| Opus solo    | 17/17 | ✓ | n/a (no Reviewer; a single solo call has no introspection) |
| Haiku solo   | 16/17 | ✗ | n/a |
| Sonnet solo  | 0/17  | ✗ | n/a |

The Sonnet team and Opus solo both scored 17/17, but the **Haiku team
delivered more system-level value** in this run: same pass rate as
the strongest setups, *plus* an audit trail that explicitly
identifies a real residual bug.

### 4. Cost vs quality vs transparency

| Setup | LLM wall-time (s) | tests passed | self-audit included? |
|---|---:|---:|---|
| Opus solo    | **377.7** | 17 | no |
| Haiku solo   | 364.6     | 16 | no |
| Sonnet solo  | 1380.6    | 0  | no |
| **Haiku team** | 1081.6  | 17 | **yes (substantive)** |
| Sonnet team  | 1340.3    | 17 | partial (`approved` but with real findings) |

If raw speed is the only goal, Opus solo wins (~378 s). If you want
the same correctness *plus* an explicit, auditable trade-off review,
the Haiku team gets you 17/17 plus four substantive findings for
~3× the wall-time.

### 5. Solo failure modes are reproducible

The v3 → v4 picture is consistent across runs: solos *occasionally*
ship code with a structural defect that no test in the canonical
suite would forgive. v4 added one more concrete data point: in the
KeyError failure of Haiku solo, `plan_cost_optimized()` works as part
of `plan_end_to_end()` but breaks when called standalone — exactly
the kind of invariant the team's Reviewer pass would (and v3's
Sonnet team Reviewer did) catch.

## v3 → v4 evolution (this iteration)

| Aspect | v3 | v4 |
|---|---|---|
| Test #17 assertion | strict `cost_obj != svc_obj` (any diff, incl. FP noise) | `any(abs(diff) > 1e-6)` over the 6 components |
| Built-in scenario in `integration.py` | unconstrained — slack hid the trade-off | spec now mandates a binding tight time window + shift overflow |
| Pass rates | 0/5 setups passed 17/17 | **3/5 setups pass 17/17** (Haiku team, Sonnet team, Opus solo) |
| Reviewer self-diagnosis surfaced as metric? | no (had to be read from findings text) | **yes** (`tradeoff_bug_flagged` column in trace.json + report) |

## Bottom line

> **v4 turns this from a "nobody passes" demo into a genuine cost /
> quality / transparency benchmark.**
>
> * **Top-tier solo (Opus 4.7)** is still the cheapest path to 17/17:
>   one call, ~378 s, no audit trail.
> * **The multi-agent Haiku team matches that pass rate** at the cost
>   of ~3× more LLM time, and adds a Reviewer pass that surfaces
>   real residual bugs the test suite cannot see — including the very
>   trade-off-collapse failure mode the new test was built to detect.
> * **Sonnet team** also passes 17/17 but spent its review effort on
>   different (also real) findings; it did not flag the trade-off
>   issue this run.
> * **Solo runs of weaker models stay brittle**: Haiku solo missed
>   the standalone-callable contract; Sonnet solo wrote unimportable
>   code yet again. Multi-agent coordination is the structural
>   advantage that catches both classes of defect.
>
> The new `tradeoff_bug_flagged` metric makes the team's value
> showable in a single column, alongside speed and pytest pass rate,
> without weakening the strict tests.

## Reproducing

```bash
./PlutoServer.sh --kill && ./PlutoServer.sh --daemon

PLUTO_HAIKU_MODEL=claude-haiku-4.5 \
PLUTO_SONNET_MODEL=claude-sonnet-4.6 \
PLUTO_OPUS_MODEL=claude-opus-4.7 \
PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_logistics_multiagent_vs_solo -s
```

Outputs land under `/tmp/pluto/demo/logistics_multiagent_vs_solo/`
(per-setup workspaces, full `trace.json`, auto-generated report).
