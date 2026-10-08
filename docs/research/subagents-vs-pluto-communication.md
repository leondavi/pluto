# Claude Code Subagents vs Pluto: Communication, Cost and Concurrency in a 4-Worker Team

**Status:** research / experiment report. The experiment found three queued-lock delivery defects; their fixes ship in v0.5.1 (§6).
**Date:** 7 October 2026.
**Model:** `claude-sonnet-5` for every agent.
**Harness:** `experiments/subagents_vs_pluto/`.
**Raw data:** `docs/research/data/subagents-vs-pluto/`.

---

## 1. Question and hypotheses

There are two ways to coordinate a team of LLM workers in Claude Code today:

- **Native subagents.** One orchestrator spawns worker subagents with the Agent tool. Information flows only through the prompt the parent writes and the final reply the child returns. Workers cannot talk to each other, and Claude Code itself provides no lock, lease or fencing primitive.
- **Pluto.** Every worker is an independent Claude Code process with its own Pluto identity, connected via the MCP adapter (`src_py/agent_mcp_friend/`). Agents message each other directly and get server-side primitives:
  - read/write locks with FIFO wait queues and TTL leases (`src_erl/src/pluto_lock_mgr.erl`)
  - monotonic fencing tokens (`pluto_lock_mgr.erl:505`)
  - deadlock detection (`src_erl/src/pluto_deadlock.erl:29`)
  - a task registry

We asked what each approach costs and what each buys in four respects: communication efficiency, token use, delay, and correctness under concurrency (locks, fencing, contention).

Hypotheses going in:

- **H1 (communication).** Peer-to-peer hand-offs over Pluto are faster than routing every hand-off through an orchestrator.
- **H2 (tokens).** Pluto costs more tokens, for two reasons. Each agent is a full process that re-reads its own context on every turn. And every request re-sends the Pluto tool schemas, about 1.5 k tokens (`token-efficiency.md` §3.2).
- **H3 (concurrency).** Without primitives, subagent teams lose updates under contention and accept stale writes from a lease holder whose lease expired. Pluto prevents both.

H1 held. H2 held, though the cost gap was much smaller than the token gap. H3 was **refuted for correctness**: in every run the subagent teams improvised working locks and fencing on the filesystem. The real differences were delay and cost, plus the three Pluto defects the experiment uncovered.

## 2. Setup

| | Native subagents (A) | Pluto (B) |
|---|---|---|
| Processes | 1 `claude -p` (orchestrator) | 5 `claude -p` (`orch`, `w1`..`w4`) started concurrently |
| Workers | 4 subagents `w1`..`w4` defined via `--agents` | 4 independent sessions |
| Tools | orchestrator: Bash, Read, Write, Agent; workers: Bash, Read, Write | all agents: Bash, Read, Write and the Pluto MCP server (one identity each) |
| Settings | `--setting-sources ""`, `--strict-mcp-config`, `bypassPermissions` in a fresh run directory, $3 budget cap per process, 600 s per run | same |
| Pluto server | — | private sandbox server (`pluto_sandbox.py`), restarted for every run, so every run starts with empty locks, fencing counter and tasks; ports 9300/9302, so the configured server is never touched |

Software and hardware:

- Claude Code 2.1.283.
- Pluto v0.5.1: this branch, with the fixes in §6.
- Baseline Pluto v0.5.0 (`master` at `a8e1fb6`), for two "before" runs.
- Erlang/OTP 28 on an Apple M4 Mac mini, macOS 26.6.

**Fairness rules** (`scenarios.py`):

- Both orchestrators receive identical task text.
- Workers in both conditions receive only a generic preamble, and get concrete instructions from their orchestrator.
- The only text that differs between conditions is a one-paragraph "COORDINATION" hint. It names the primitives that exist: the Agent tool and a shared filesystem in A; Pluto messages, locks and tasks in B.
- Every agent touches shared state only through the same storage CLI, `ledger.py`. That CLI logs a timestamp for every operation to `events.jsonl`, and all latency and correctness numbers below come from that log, not from the agents' own reports.

**Metrics:**

- **Wall time:** harness start until every process has exited.
- **Task span:** from the first to the last `ledger.py` operation. This is the coordination-relevant interval; it excludes orientation and wrap-up.
- **Tokens and cost:** the cumulative `modelUsage` and `total_cost_usd` of the last `result` event of each process, summed over the 5 processes in B. Subagent turns are included in A's totals.
- **Coordination calls:** tool calls that move information between agents: `Agent`/`SendMessage` in A, `mcp__pluto__*` in B.
- **Reporting:** all tables show mean ± sd over n = 5 runs.
- **Run order:** conditions were interleaved within each repetition to spread drift (API latency, cache warmth) evenly.

## 3. Scenarios

| | Scenario | Property tested | Pass criterion |
|---|---|---|---|
| S1 | **Ring relay.** A token visits w1→w2→w3→w4→w1 for two laps (9 holds); each holder runs `ledger.py hop` | inter-agent message latency and hop cost | hop sequence is exactly w1,w2,w3,w4,w1,w2,w3,w4,w1 |
| S2 | **Ledger contention.** 4 workers × 5 appends to `ledger.json` through a deliberately non-atomic read-sleep-write | mutual exclusion | 20 entries out of 20 append calls |
| S3 | **Stale lease holder.** w1 takes a 4 s lease, then stalls for 60 s (`ledger.py stall`) and then writes; w2..w4 take the lease after it expires and write. `ledger.py put` rejects a `--token` below the highest already accepted | fencing | the stale write is attempted after a fresh write, and the fresh data survives |
| S4 | **Fan-out.** 12 jobs of uneven duration (1–12 s, 57 s in total; the ideal 4-worker makespan is 15 s) | dynamic load balancing / concurrency | each job runs exactly once |

## 4. Results

40 valid runs: 4 scenarios × 2 conditions × 5 repetitions.

### 4.1 Headline table

| Scenario | Condition | Pass | Wall (s) | Task span (s) | Tokens (k) | Cost (USD) | Coordination calls |
|---|---|---|---|---|---|---|---|
| S1 ring | subagents | 5/5 | 104 ± 27 | 80 ± 20 | 616 ± 166 | 0.30 ± 0.06 | 9.0 |
| S1 ring | **pluto** | 4/5 | **79 ± 3** | **46 ± 4** | 1 010 ± 38 | 0.37 ± 0.04 | 36.8 |
| S2 contention | **subagents** | 5/5 | **91 ± 23** | **36 ± 28** | 397 ± 113 | **0.28 ± 0.05** | 4.0 |
| S2 contention | pluto | 5/5 | 237 ± 93 | 130 ± 21 | 2 802 ± 348 | 0.82 ± 0.09 | 96.8 |
| S3 fencing | subagents | 5/5 | 236 ± 41 | 53 ± 10 | 572 ± 101 | 0.47 ± 0.10 | 4.0 |
| S3 fencing | **pluto** | 5/5 | **134 ± 35** | 51 ± 5 | 1 172 ± 173 | 0.43 ± 0.08 | 43.2 |
| S4 fan-out | subagents | 5/5 | 105 ± 31 | 51 ± 33 | 572 ± 523 | 0.32 ± 0.23 | 6.4 |
| S4 fan-out | pluto | 5/5 | 89 ± 41 | 48 ± 7 | 1 493 ± 254 | 0.52 ± 0.08 | 63.4 |

The full table with 95% confidence intervals is in `data/subagents-vs-pluto/summary.csv`.

Totals:
- 40 runs cost $17.51: $6.82 for subagents, $10.68 for Pluto.
- Including smoke tests, discarded runs (§7) and the v0.5.0 baseline, the whole study cost about $28.

### 4.2 Communication efficiency (S1)

| | Subagents | Pluto |
|---|---|---|
| Mean hand-off latency (ledger timestamps) | 10.0 ± 2.5 s (95% CI 7.0–13.1) | **6.0 ± 0.7 s** (95% CI 5.1–6.8) |
| Agent-to-agent channel | parent → `Agent` → child reply → parent → `Agent` … | `pluto_send` worker → worker |
| Hops through the orchestrator per hand-off | 1 round trip (9 `Agent` spawns per run) | 0 |
| Tokens per hand-off | 77 k | 126 k |
| Run-to-run variability of task span (sd) | 20 s | 4 s |

- **H1 holds.** Direct peer messaging hands off about 1.7× faster, and about 5× more predictably.
- **Why the subagent path is slow.** Each subagent hand-off costs:
  - one orchestrator turn to write the next prompt
  - a cold subagent start: a fresh context, about 5.6 k tokens of cache creation
  - the child's own turn
  - a background-task notification back to the parent
- **Why the Pluto path is fast.** A hand-off is one tool call by the sender and a wake-up of the receiver, which is already blocked in `pluto_wait_for_messages`.

The faster channel costs more tokens per hand-off, because all four Pluto workers stay resident. Each one re-reads its whole conversation (Pluto tool schemas included) on every turn, even when that turn only says "keep waiting".

The single Pluto failure in S1 was a protocol misunderstanding, not lost or late delivery. That run's orchestrator told w1 to report the end of lap 2 but not to record the final `hop`, so the log shows 8 of the 9 holds.

### 4.3 Token usage and cost

Pluto used **1.6× to 7× more tokens** than subagents (S1 1.6×, S2 7.1×, S3 2.0×, S4 2.6×), but only **0.9× to 3.0× more money** (S1 1.26×, S2 2.95×, S3 0.91×, S4 1.61×). The difference is prompt caching:

| Cache-read share of all tokens | S1 | S2 | S3 | S4 |
|---|---|---|---|---|
| Subagents | 92.8% | 89.2% | 87.2% | 90.9% |
| Pluto | 96.4% | 98.2% | 96.8% | 97.1% |

A resident Pluto agent's context is a stable prefix that grows by appending, so almost all of it is a cache read billed at about a tenth of the input price. Each subagent, by contrast, starts a fresh context and pays cache *creation* on its first turn.

Where the Pluto tokens go:
- Workers, not the orchestrator, dominate. Mean worker share of tokens: S1 86%, S2 89%, S3 70%, S4 77%.
- Much of it is waiting. Each `pluto_wait_for_messages` call is a model turn. When it returns empty after the 30 s window, the model re-reads its full context just to call wait again.
- Empty waits per run averaged 13 in S2, 7 in S3 and 2 in S4, with a peak of 32 in one S2 run.
- This is the per-model-turn cost class that `token-efficiency.md` §2 identifies as the only unbounded one. The push-delivery work (A2 there) is the structural fix.

S3 is the one scenario where Pluto was **cheaper**. Both teams needed fencing:
- The subagent orchestrators spent long thinking turns designing a lease and token protocol from scratch: about 20 k output tokens against 9 k for Pluto, and a 236 s wall time against 134 s.
- The Pluto orchestrators just named the primitive.

When the coordination problem is hard, a ready-made primitive saves more tokens than resident agents cost.

### 4.4 Locks and contention (S2)

Neither condition lost an update: both scored 20/20 in all 5 runs. So **H3 is refuted** for this model.

In every run the subagent orchestrator read `ledger.py`, saw the race, and wrapped each worker's appends in a filesystem mutex (`mkdir`-based lock directories; 5/5 runs).

The difference is where the lock is held:

| | Subagents | Pluto |
|---|---|---|
| Lock held around | one shell command (the `mkdir` lock is taken and released inside the same Bash call as the append) | three model turns: `pluto_lock_acquire`, then the Bash append, then `pluto_lock_release` |
| Critical-section length | about 0.3–0.5 s (the storage's own sleep) | about 6–7 s (three LLM turns) |
| Acquires that had to queue | — | 18.6 of 20 |
| Task span | **36 ± 28 s** | 130 ± 21 s |

Pluto's lock is correct, but in this workload it is used at the wrong granularity. Twenty critical sections, each lasting three LLM round trips, serialize to about 130 s.

This is an LLM-specific hazard: **model latency ends up inside the critical section.** Two fixes would close most of the gap:
- coarser locks: one acquire per batch of five appends
- taking the lock from code, so the lock never waits on the model: a Bash script using `pluto_sdk`, the "code-mode" proposal A1 in `token-efficiency.md`

Neither was prompted here, because the hint text was fixed for the whole study.

### 4.5 Fencing (S3)

Both conditions defended against the stale writer in **5/5** runs: w1's write after its 60 s stall was always attempted after fresh writes, and always rejected.

| | Subagents | Pluto |
|---|---|---|
| Fencing-token source | improvised: a shared epoch counter file (3/5) or **wall-clock milliseconds** (2/5) | server-issued `fencing_token`, strictly monotonic and persisted (`pluto_lock_mgr.erl:505`) |
| Lease mechanism | improvised `lease.json` and lock file, with a helper script written by the orchestrator in 2/5 runs | `pluto_lock_acquire(ttl_ms=4000, auto_renew=false)` |
| Stale write rejected | 5/5 | 5/5 |
| Wall time | 236 ± 41 s | **134 ± 35 s** |

The improvised schemes worked on one machine, but they are not equivalent to Pluto's. Wall-clock tokens are only safe while every writer shares one clock. And the counter files are only safe because every agent cooperates and runs on one filesystem.

The fencing also caught a hazard in the other direction. In Pluto run 3, w3 was legitimately granted token 3. But its 4 s lease expired during its own LLM turn, before it wrote. w4 was then granted token 4 and wrote first, so w3's write was (correctly) rejected. **A lease TTL must exceed the agent's turn latency (several seconds), not just the work's duration.** Pluto's default `auto_renew=true` exists for exactly this; the scenario disabled it on purpose.

### 4.6 Fan-out (S4)

Both conditions ran every job exactly once in 5/5 runs, with no duplicates and no misses. The makespans are close, 51 ± 33 s for subagents against 48 ± 7 s for Pluto, but **the subagent number is confounded.** The job-duration table is visible in `ledger.py` (`experiments/subagents_vs_pluto/ledger.py:38`):
- In 4 of 5 runs the subagent orchestrator read it and built a near-optimal static partition (14–15 s of work per worker), reaching a 23–32 s task span when the background subagents started promptly.
- Its two slow runs (82 s and 92 s) came from subagents starting tens of seconds apart.
- The Pluto orchestrators scheduled dynamically through `pluto_task_assign`/`task_update`, reaching 41–55 s.

Pluto's dynamic scheduling is limited by LLM turn latency: each job completion costs about two model turns before the next assignment arrives, so workers sat idle 65–74% of the time. Pluto's variance was 5× lower, but this scenario cannot establish which approach balances load better on unknown durations. See §7.

### 4.7 Capability matrix

| Capability | Native subagents | Pluto | Evidence |
|---|---|---|---|
| Worker ↔ worker messaging | ✗ (parent-mediated only) | ✓ direct `pluto_send`, no orchestrator turn per hand-off | S1 |
| Broadcast / topics | ✗ | ✓ | `pluto_msg_hub:broadcast/2` |
| Mutual exclusion | improvised (filesystem `mkdir`/`O_EXCL`; same host, cooperative) | ✓ read/write locks, FIFO queue | S2; `pluto_lock_mgr.erl:345` |
| Leases / TTL | improvised | ✓ TTL, auto-renew, expiry warnings | S3 |
| Fencing tokens | improvised (counter file or wall clock) | ✓ monotonic, persisted across restarts | S3; `pluto_lock_mgr.erl:505` |
| Deadlock detection | ✗ | ✓ wait-for graph, newest-requester victim | `pluto_deadlock.erl:29` (not exercised here) |
| Task registry / dynamic assignment | parent's own bookkeeping | ✓ `task_assign` / `task_update` events | S4 |
| Works across machines | ✗ (one process tree, one filesystem) | ✓ (TCP/HTTP) | design |
| Worker state survives the parent | ✗ (subagents live inside the parent process) | ✓ independent sessions, snapshots | design |
| Setup cost | none | a server, an MCP adapter per agent, about 1.5 k tokens of tool schemas per request | §4.3 |

## 5. Discussion

Choose **native subagents** for fan-out/fan-in work:
- The workers don't need to talk to each other.
- Contention is low enough that a filesystem mutex inside a single shell command suffices.
- In S2 and S4 subagents were 1.6–3× cheaper, and in S2 also 3.6× faster.
- A capable model improvises correct locks and even fencing on demand (S2, S3).

Choose **Pluto** when:
- **Workers must exchange information mid-task.** S1: 1.7× faster and 5× steadier hand-offs.
- **The coordination primitive is non-trivial**, as with leases and fencing in S3: 1.8× faster, and slightly cheaper, than inventing it.
- **The team has to outlive one process or one machine.** Here the improvised schemes stop being safe.

Pluto's main cost is **idle resident agents**: every blocked `pluto_wait_for_messages` that times out is a model turn that re-reads the whole context. That cost, together with the three-turn critical sections of §4.4, explains most of its token and latency overhead. The cheapest improvements, in order:
1. **Push delivery instead of wait-loop polling** (`token-efficiency.md` A2).
2. **Locks taken from code (`pluto_sdk`) or at batch granularity**, so model latency stays out of critical sections.
3. **Default TTLs well above agent turn latency**, keeping `auto_renew=true`.

## 6. Defects found and fixed in v0.5.1

The first smoke runs measured Pluto S2 at 307 s against 34 s for subagents. Transcripts showed workers blocked on 30 s `pluto_wait_for_messages` timeouts after a queued `pluto_lock_acquire` (`status: "wait"`) that was never granted. Three defects combined:

1. **Server: queued HTTP grants were dropped.**
   - `POST /locks/acquire` queues HTTP waiters with `session_pid => undefined` (`src_erl/src/pluto_http_listener.erl:368`).
   - On release, `notify_lock_granted/2` deleted the waiter, then fell into a catch-all clause, "No session PID — can't notify". That clause created no lock and sent no event (v0.5.0 `pluto_lock_mgr.erl:501`).
   - So every MCP or HTTP agent whose acquire queued silently lost its place.
   - Fix: the lock is always created, and the event goes through `pluto_msg_hub:push_event_to_agent/2` into the agent's inbox (`notify_waiter/3`, `pluto_lock_mgr.erl:509`). `wait_timeout` uses the same path.
2. **Server: queued grants ignored `ttl_ms`.**
   - Every queued grant got a hard-coded 30 s lease (v0.5.0 `pluto_lock_mgr.erl:482`), on TCP as well.
   - Fix: the waiter now records the requested `ttl_ms` (`pluto_records.hrl`, field `wait_entry.ttl_ms`).
3. **Adapter: grants were filtered as noise.**
   - The MCP adapter's inbox only surfaced `message`, `broadcast`, `task_assigned` and `topic_message`, so a delivered `lock_granted` would still have been discarded.
   - Fix: `lock_granted` and `wait_timeout` are now actionable, and their `wait_ref`/`lock_ref`/`fencing_token`/`resource` fields survive envelope trimming (`src_py/agent_mcp_friend/inbox.py:30`). The PTY adapter's filter got the same change.
   - A queued `auto_renew=true` acquire is now put under auto-renewal the moment its grant lands (`tools.py:371`).

Regression tests:
- `src_erl/test/pluto_v051_tests.erl`: 3 tests, which all fail on v0.5.0 and pass on v0.5.1.
- `tests/test_mcp_lock_loss.py::TestQueuedLockGrant`.

Before-and-after with identical prompts, 1 run each on v0.5.0:

| | v0.5.0 S2 | v0.5.1 S2 (n = 5) | v0.5.0 S3 | v0.5.1 S3 (n = 5) |
|---|---|---|---|---|
| Task span | 276 s | 130 ± 21 s | 155 s | 51 ± 5 s |
| Queued acquires / grants delivered | 5 / **0** | 18.6 / 18.6 | 1 / **0** | 2 / 2 |
| Empty 30 s waits | 27 | 13 | 18 | 7 |

The v0.5.0 runs still passed. The agents recovered by noticing the silence, polling `pluto_lock_info` and re-acquiring, which is why the bug went unnoticed until latency was measured.

## 7. Threats to validity

- **S4 duration leak.** As §4.6 explains, the job durations were readable, which favours a static planner. A follow-up should hide them, for example in a separate server-side process.
- **Prompt sensitivity.** The coordination hints are one fixed wording. Pluto's S2 result in particular depends on the hint asking for one lock per append (§4.4).
- **A single model.** A weaker model might not improvise the filesystem locks that rescued the subagent teams in S2 and S3.
- **Small n.** n = 5 per cell, so several intervals are wide (S2 and S4 task spans for subagents). Read the means as indicative.
- **Discarded runs.** The first full sweep was stopped during its sixth run, because Claude Code's Bash tool refuses a standalone `sleep 60`. That made w1's stall unreliable in S3; in the one completed S3 run, w1 never wrote. The stall moved into `ledger.py stall`. The four completed rep-1 runs of S1 and S2 were kept, since those scenarios never stall. Every S3 and S4 run, rep 1 included, used the fixed protocol. The one discarded S3 row remains in `results.jsonl`; `analyze.py` keeps the latest row per cell.
- **Smoke runs.** Smoke runs used stale v0.4.1 beams, on which broadcasts and therefore task events never reached HTTP agents (fixed upstream in v0.5.0). Smoke data is excluded from every table.
- **Single host.** The cross-machine advantages in the capability matrix are by design and were not measured.
- **Token split for subagents.** The orchestrator-versus-subagent token split comes from per-message usage. That field undercounts output tokens, so only the totals (from `modelUsage`) are used for comparisons.

## 8. Reproduction

```bash
./PlutoServer.sh --build              # or build pinned copies, as below
# pin the server and adapter to the build under test
export PLUTO_EBIN=<build>/src_erl/_build/default/lib/pluto/ebin
export PLUTO_FRIEND_SCRIPT=<build>/src_py/agent_mcp_friend/pluto_mcp_friend.py
python3 experiments/subagents_vs_pluto/run_experiment.py --scenario all --condition all --reps 5
python3 experiments/subagents_vs_pluto/analyze.py
python3 experiments/subagents_vs_pluto/trace.py docs/research/data/subagents-vs-pluto/runs/s2_contention/pluto/rep1/w1.jsonl
```

Each run directory holds:
- one stream-json transcript per process
- `events.jsonl` (the ground truth)
- `result.json`
- for Pluto runs, the sandbox server's log

The offline tests (`pytest tests/test_experiment_subagents_vs_pluto.py`) check the storage race, fencing rejection, the validators and transcript parsing, without a server or an LLM.
