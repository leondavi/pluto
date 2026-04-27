# 2. Pluto Setup & Usage in this Demo

This page documents how the demo harness uses the **real** Pluto/Erlang
server to coordinate the team setups: which endpoints are called, by
whom, in what order, and what concrete benefit Pluto provides for this
particular benchmark.

## Server lifecycle

The harness wraps `tests/pluto_test_server.py:PlutoTestServer`, which:

1. Pings `http://127.0.0.1:9201/health`. If the server is already up,
   it is reused (no port conflict, no double-launch).
2. Otherwise runs `./PlutoServer.sh --daemon` (compiles + boots a real
   Erlang OTP release of Pluto v0.2.5 listening on TCP 9200 / HTTP 9201)
   and waits for `/health` to return `{"status":"ok"}`.

```bash
./PlutoServer.sh --kill      # ensure clean state
./PlutoServer.sh --daemon    # start fresh
./PlutoServer.sh --status    # see uptime, agents connected, locks held
```

## Per-run-unique agent ids

Every agent id in this demo is composed by:

```python
RUN_TAG = f"{os.getpid()}-{int(time.time())}"
def _aid(prefix, role): return f"{prefix}-r{RUN_TAG}-{role}"
```

This is not cosmetic. With fixed ids (`logh-specialist-1`), an aborted
prior run would leave **stale registrations** behind on a long-running
Pluto server. New `register` calls would succeed, but the
orchestrator's `send` would route to the orphan session token nobody
owned — and the new specialists would long-poll forever.

Per-run-unique ids guarantee no collision with prior runs. (This was
the v1 → v2 fix; it remains a hard requirement.)

## Agents registered per team setup

Each of the two teams registers **6 real agents** through the HTTP API:

| Agent id (template) | Role | Pluto inbox |
|---|---|---|
| `<prefix>-r<TAG>-orchestrator-1` | Planner / orchestrator (broadcasts task list, dispatches, collects results) | yes |
| `<prefix>-r<TAG>-specialist-1` | Generic Specialist (handles `t-001`/`t-002`/`t-003`) | yes |
| `<prefix>-r<TAG>-specialist-2` | Generic Specialist (round-robin partner) | yes |
| `<prefix>-r<TAG>-cost-optimizer-1` | CostOptimizer (handles `t-004a`) | yes |
| `<prefix>-r<TAG>-service-reliability-1` | ServiceReliability (handles `t-004b`) | yes |
| `<prefix>-r<TAG>-meta-planner-1` | MetaPlanner (handles `t-004` reconciliation) | yes |

Plus one **virtual** id for QA tracing (`<prefix>-r<TAG>-qa-1`) and one
for the Reviewer LLM call (`<prefix>-r<TAG>-reviewer-1`).

The `(prefix)` is `logh` for the Haiku team and `logs` for the Sonnet
team, so both teams can run sequentially against the same server with
zero id collisions.

## Endpoints actually exercised (per team)

All calls go through `PlutoHttpClient` in
[`src_py/pluto_client.py`](../../../src_py/pluto_client.py), which uses
`urllib.request.urlopen` against the Erlang server.

| Endpoint | Caller | When | Per-team count (this demo) |
|---|---|---|---|
| `POST /agents/register` | every role agent | startup | 5 |
| `POST /agents/poll` (long-poll) | every role agent | continuously while alive | dozens (timeout=2s) |
| `POST /agents/send` | orchestrator | dispatching `task_assigned` | 5 (one per task) |
| `POST /agents/send` | specialists / role agents | replying with `task_result` | 5 |
| `POST /agents/broadcast` | orchestrator | publishing `task_list` for observability | 1 |
| `POST /locks/acquire` | each role agent before writing its file | per task | 5 |
| `POST /locks/release` | each role agent after the file write | per task | 5 |
| `POST /agents/unregister` | every role agent | teardown | 5 |

The lock count (acquire+release = **10 events per team**) is recorded
in `trace.json` and is the single most direct evidence that Pluto is
genuinely orchestrating writes — not just being pinged for show.

## Task DAG actually dispatched

```
                          PlannerLLM
                              │ (broadcast task_list)
                              ▼
       ┌──────────────────────┴──────────────────────┐
       │              parallel-eligible              │
       ▼                ▼                ▼
   t-001 routing   t-002 scheduling   t-003 graph_paths
    (Specialist)    (Specialist)       (Specialist)   ← round-robin
       │                │                │
       └────────────────┴────────────────┘
                        │ (deps satisfied)
                        ▼
               t-004a plan_cost_optimized()
                  (CostOptimizer agent)        ← writes integration.py
                        │
                        ▼
               t-004b plan_service_optimized()
                (ServiceReliability agent)     ← appends to integration.py
                        │
                        ▼
               t-004 plan_end_to_end(weights)
                  (MetaPlanner agent)          ← reconciles, emits rationale
                        │
                        ▼
               t-005 api.run_demo_scenario(weights)
                    (Specialist)
                        │
                        ▼
                 ReviewerLLM pass
                  (one shot over all 5 modules)
                        │
                        ▼
                  pytest (17 cases)
```

The dispatcher in `drive_team()` honors `task["dependencies"]` strictly
— `t-004a` cannot start until all of `t-001` / `t-002` / `t-003` report
`status="done"`, and `t-004b` cannot start until `t-004a` does (they
share `integration.py`). This is the part that cannot be done by a
single LLM call: ordered hand-offs across **6 distinct registered
identities** with **real write locks** on shared files.

## Locks: what they protect

The harness has each role agent acquire `file:<absolute path>` with
`mode="write"` and `ttl_ms=600_000` BEFORE invoking `copilot -p`, then
releases the lock after the LLM returns. For example, both
CostOptimizer and ServiceReliability target the same
`integration.py` — but the dependency edge `t-004a → t-004b` means they
serialise on the lock. The MetaPlanner then takes the same lock for
the reconciliation pass.

Total lock events per team in the v3 run: **14** (7 acquire + 7
release, one pair per specialist task).

## Reviewer fail-fast guard

The Reviewer LLM call is gated by a **deterministic file-presence
check**:

```python
missing = [p for p in required_modules if not os.path.isfile(p)]
if missing:
    return {"verdict": "needs_changes", ...}   # NO LLM call
```

This guard exists because Copilot is invoked with
`--allow-all-tools --allow-all-paths`. When the team produces no code,
the Reviewer Copilot would happily implement the missing modules
itself during what was supposed to be a code review pass and then
report `approved`. (Exactly that bug occurred in the demo's v1 run and
masked a coordination failure.) The fail-fast guard removes the
ambiguity.

## Reproducing

```bash
# Pluto on the demo ports (config/pluto_config.json)
./PlutoServer.sh --kill
./PlutoServer.sh --daemon

# Demo run
PLUTO_HAIKU_MODEL=claude-haiku-4.5 \
PLUTO_SONNET_MODEL=claude-sonnet-4.6 \
PLUTO_OPUS_MODEL=claude-opus-4.7 \
PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_logistics_multiagent_vs_solo -s
```

Outputs land under `/tmp/pluto/demo/logistics_multiagent_vs_solo/`:

* one workspace per setup (`haiku_team/`, `sonnet_team/`, `haiku_solo/`,
  `sonnet_solo/`, `opus_solo/`),
* `trace.json` — every Pluto event, copilot call, lock acquire/release,
  and pytest invocation,
* an auto-generated `logistics_multiagent_vs_solo_demo.md` side-by-side
  report.

Expected wall-time on a current Mac mini: **roughly 35–55 minutes**
(this v3 run took ~46 minutes).

## Pluto's value proposition for this benchmark

| Need | How Pluto satisfies it |
|---|---|
| Two LLM-driven agents must NOT race-edit the same file | Real `/locks/acquire` (write mode) on the file URI; the dependency edge `t-004a → t-004b` plus the lock guarantee serialised writes |
| MetaPlanner must wait until BOTH candidates exist before reconciling | Dependency `t-004 = {t-004a, t-004b}` enforced by the dispatcher; gated on `task_result status=done` |
| Reviewer must run exactly once after all 5 modules exist | Deterministic file-presence check, then one Reviewer LLM pass |
| All agent traffic must be auditable | Every send / broadcast / lock op is recorded by the harness Trace and emitted in `trace.json` |
| Multiple sequential team runs (Haiku then Sonnet) on the same server must not collide | `RUN_TAG` per-process suffix on every agent id |

For the result tables and the actual quality findings each setup
produced, see
[`logitstics_multiagent_vs_solo_demo.md`](logitstics_multiagent_vs_solo_demo.md).
For the problem definition and grading rubric, see
[`problem-and-task.md`](problem-and-task.md).
