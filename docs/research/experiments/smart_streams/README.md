# Experiment: PlutoSmartStreams vs native Claude Code (protocol v3)

Status: E1 to E3 were pre-registered as protocol v2. v3 adds experiments E4 and E5, the
arm `native_sub` and call classes; E4, E5 and H4 to H8 were written before any agent
run of E4, E5 or `native_sub`. E1 to E3 tasks, graders and the prompts of their two
arms are unchanged. Rows carry `protocol_version` 3 and are not pooled with v2.

**History.** v1 (git a9eab16) is the previous protocol. v2 was designed after
seeing the v1 pilot and partial v1 main results, so it is not blind to them:
scores were at the ceiling in both conditions (0.97 to 1.0 in the pilot, 0.92 to
1.0 in main), A's score was about B's, and A cost about 1.7 to 3 times as much as
B and returned more tool-result text. v1 gave an agent only 2 to 4 streams. v1
rows have no `protocol_version`; v2 rows have 2, and the two are never mixed.

**Independence of E4 and E5.** The product was changed after pilot 1 (see Threats).
Features for log analysis and watching are being added by other people while E4 and E5
are written; their design was unknown to the author of E4 and E5, who had read the product
guide, its tool list and the v2 pilot. E4 and E5 are held-out tests for those features, once only:
changing the product after seeing their results makes later results tuned.

Four Claude Sonnet agents solve one task in a shared directory. Each agent is a
headless `claude -p` process.

| | A `smartstreams` | B `native` (C `native_sub`: below) |
|---|---|---|
| Tools | B's tools plus the 15 `job_*` MCP tools | Bash (with `run_in_background`), BashOutput, Monitor, Read, Grep, Glob, Write, Edit, ... |
| Task, model, budget, timeout, workspace, agent-to-agent channel (files) | same | same |
| Prompt | same core, one tools paragraph | same core, one tools paragraph |

A's paragraph mentions that `job_wait` and `job_grep` take a `job_ids` list. B's
mentions detaching, saving exit codes, and shell loops/xargs over many logs. A
may use everything B uses, so A is "native plus SmartStreams".

Permissions: `--permission-mode dontAsk`, explicit `--allowedTools`,
`--strict-mcp-config`; web, skills and messaging are denied in all arms, sub-agents in A and B.
Session variables of an enclosing Claude Code process are stripped from agents,
server and jobs; authentication variables stay.

## Tasks

Every agent owns and needs at least 8 streams at once. The row records each
agent's peak from the launch ledger (`peak_streams`) and sets
`low_parallelism` if any agent stays below 8.

**E1 shard_fleet** (w1..w4, 48 shards, 12 per worker). Real persistent AVL maps,
~2 MB each over ~200 s. Find the first occurrence of every violated invariant
(seven kinds, 0 to 2 per shard), trace six corrupted cells across workers, and
terminate livelocked shards within 90 s while the fleet runs.

**E2 tuning_tournament** (12 variants x 4 sizes x 8 tunings = 384 combinations,
96 per worker, at most 16 running per worker, launches allowed for 600 s). Cost
is unimodal in the tuning, so results decide what to launch next. Dominated and
hanging runs should be stopped. Two variants have different defects.

**E3 fleet_relay** (24 eight-minute jobs). p1, p2 launch 12 each and end by 240 s;
q1, q2 take over 12 each, crossed. Crashes, stalls, nonzero exits, probe-limit
changes, leaks, node groups found by correlating spikes over all jobs, and live
checkpoint requests with a 75 s deadline.

| Feature | How native does it | How SmartStreams does it |
|---|---|---|
| 12 simultaneous launches (barrier in E1) | one Bash loop with `nohup ... &` | one job_start call per stream |
| Livelock / stall / hang detection | loop over logs: last line, mtime or size | job_wait until=pattern/idle, job_status, or the same loop |
| Terminate one stream | `kill` with the saved pid | job_control |
| Checkpoint acknowledgement | write a file | write a file (same Bash call) |
| Result-driven queue, early stop | driver script or Bash calls | driver script or job tools |
| Arithmetic and stateful checks, joins | Python over logs | Python in a job pipeline or over the output |
| Surviving the shift change | nohup/detached | jobs are server-owned |
| Exit code after the shift change | wrapper writes `$?` to a file | job_status |

No feature needs an ability that only one condition has.

**E4 crash_recovery** (w1..w4, 40 jobs, 10 per agent, ~4 min each). Fenwick-tree sweeps
with line checksums. Per agent: three clean jobs, and seven that crash, hang or go corrupt
(two of them twice); seven jobs write checkpoints, three do not; a checkpoint may be torn or
already corrupt. The prompt warns all arms that programs, sessions and services may be killed.
The harness kills, at seeded times identical in all arms:

1. three agent sessions, at 95-115, 165-190 and 235-260 s, with `kill -9` of the process
   tree (`ss_procs.crash_tree`): the CLI, its process group and its live descendants.
   A process whose launching shell has exited has left the tree (nohup in a foreground
   Bash call); a `run_in_background` task has not. 5 s later a new session starts with
   the same prompt, the same agent id, the time that is left, and "you replace an agent that
   crashed", without a transcript.
2. the Pluto server (A only), `kill -9` at 130-150 s, restarted after 5 s with the same
   home and ports: a fault that B and C do not have.

A full reboot cannot be simulated here: detached processes, page cache, /tmp and clocks survive.
The native arms are told their tools for this: nohup, a wrapper that saves pid and exit code,
pid files, `ps -o pid,lstart,command`, `kill`. A is told that jobs, output and exit codes
outlive sessions and server, how to list them, and that kill in Bash always works.

*Known before the runs:* a replacement adapter that registers with the id of a session killed
less than 10 minutes ago is renamed by the server, sees no jobs of its own and does not own the
old ones (`job_list scope=all` shows them, output is readable, `job_control` says `not_owner` before and
after a server restart, Bash `kill` works; measured with the real adapter). This is how the
product behaves; it is not worked around, and it counts against A.

**E5 unpredictable_output** (w1..w4, 48 streams, 12 per agent, 300 s, 0.8 to 1.9 MB
each). Two interleaved writers per stream, each operating one of eight real data structures, in eight
output styles (kv, JSON, CSV, tables, multi-line blocks, three prose forms) with random
field names, which all change once midway; ANSI codes, time stamps, progress bars with carriage returns, harmless
ERROR lines and tracebacks. The docs explain the structures' rules and no format. About 160
anomalies of eleven kinds (rule violations, counter gaps and silent restarts, one-off messages
about lost guarantees among look-alikes), six root causes, 28 questions to answer within 90 s and
decoys. All needed facts are in the output.

Scripted results for E5, seed 1 (privileged = knows the styles, reads only the output):
privileged 1.000; grep for error/fatal/fail/warn .002; rare line shapes (<=3) .034, unique shapes .068;
counter jumps .019; a claim every 100 lines .025; upper bounds of perfect one-trick detectors: counters only .26,
messages only .28. Privileged is 1.0 on 12 seeds (2 at full size).

**E6 release_trains** (w1..w4, 40 lanes, 10 per agent, 5 to 9 minutes, about 1.3 MB each). Added after
E1 to E5 as a further held-out task. Its author is an LLM agent that read this directory (including this README
and the tools paragraphs in `ss_prompts.py`) and the tests, but no product code, product guide, article
notes or result files. A lane bakes four candidate builds in turn, each followed by a gate that waits 70 s for promote or
rollback. Outages must be rolled back within 60 s. The verdict follows documented rules (error rate, p95, memory, checksum,
with exclusions and limits that change during a bake). Each agent has 18 bad and 22 good builds. Five guilty
component versions are found by comparing manifests over all lanes. Decisions do not change bake lines, so the truth depends
on the seed only; wall time depends on how fast decisions come. The format is fully documented and regular: one streaming
script can decide everything, so E6 mainly tests whether it is ready in time (first gate after about 70 s; preparation
before launching is allowed). A ceiling is possible. No LLM has run it yet.

Scripted results for E6, seed 12345, `verify_e6.py`: rules followed with an independent reader 1.000 (301 s);
promote at every gate, ignore outages .144; error rates over all lines, ignore outages .056 (both 376 s). Program CPU is
0.02 of a core per 12 streams.

**E8 phase_transitions** (w1..w4, 48 nodes, 12 per agent, launches for 420 s, runs of 90 s, about 0.27 MB
each). Added as a further held-out task by an LLM agent that read E1 to E5, the runner, the prompts (including the tools
paragraphs in `ss_prompts.py`), parts of this README and the tests, but no product code, product guide, article notes, results, E6
or E7 sources. No LLM has run it yet.

*The physics.* The Ising model is a lattice of magnets that point up or down and prefer to agree with
their neighbours; in the q-state Potts model each site has one of q colours. Below a critical temperature T_c
the lattice orders. Ising and q = 3 change continuously: fluctuations and correlation times diverge, and their growth with
the lattice size is described by exponents (1/nu = 1 and 6/5). For q > 4 the transition is of first order: two phases coexist,
the energy jumps by the latent heat, and runs stay in the wrong phase (hysteresis). An external field removes the
transition. Finite lattices have no sharp transition, so T_c is found by finite-size scaling, for example where
the Binder cumulants of two sizes cross. Exact results serve as truth: Onsager's sinh(2 Jx/T) sinh(2 Jy/T) = 1 (2/ln(1 + sqrt 2) for
equal couplings), T_c = J/ln(1 + sqrt q), and Baxter's latent heat. Couplings, q and field are hidden and drawn from the seed.

*The task.* Each agent has two samples and twelve nodes and chooses sizes and temperatures. Runs are real Metropolis
simulations (eight replicas in one Python integer per spin bit, own PRNG per run, output a function of arguments, launch number and
the agent's answers). 16 of the 48 nodes are faulty in one of four ways, visible only in comparison with other nodes, in
fluctuations or in correlations. While they run, programs ask for replica-exchange decisions (the run waits up to
60 s) and single replicas get stuck and have to be reset within 60 s. The wrong reactions cost points. The formats are
documented and regular; preparation before the first launch is allowed.

Tolerances follow repeated reference simulation at run length: Binder crossings of L = 8/16 to 16/32 were within 0.1 to 0.9 % of
T_c (one of six 16/32 pairs 1.2 %), specific-heat peaks of L = 8, 16 extrapolate within 0.6 %, phase energies at L = 16, 24 give the latent heat within 5 %; the
local 1/nu from cumulant slopes scatters by about 0.1 for q = 3. Hence T_c full within 1 %, zero at 4 %.
Scripted results (`verify_e8.py`, reads only program output): RESULTS_HERE

**Arm C `native_sub`.** B's tools, prompt and limits plus Claude Code's Agent/Task tool. Its
paragraph is B's plus a neutral offer (foreground or background, at most four at a time). Rules: the same
Sonnet (`CLAUDE_CODE_SUBAGENT_MODEL` and `_FORCE`), depth 1 and 4 in parallel
(`..._MAX_SUBAGENT_SPAWN_DEPTH`, `..._MAX_CONCURRENT_SUBAGENTS`), the same workspace, wall time and
`--max-budget-usd`, no worktree or remote isolation, all other denials as in B. Sub-agent
messages carry `parent_tool_use_id`; their turns, calls, result bytes and tool inputs (leak
checks, one working directory per sub-agent) are counted and kept apart in `sub`. A sub-agent's prompt
is not matched against leak patterns (it may repeat the rules).

## Scoring (max 1.0)

* E1: shards 0.50 (per violated kind: kind 0.3, op 0.4, key 0.3, divided by the
  larger of true and claimed kinds; a clean shard needs an empty report), cells
  0.25, livelock reactions 0.25 (stops in time minus false kills, over 8).
* E2: podium 0.50, defects 0.30, efficiency 0.20 (times podium accuracy; 1 up to
  1.25 times the reference unimodal search, 0 at exhaustive enumeration).
* E3: 0.9 x mean over jobs (run id 0.05, outcome 0.15, exit code or timely
  termination 0.10, violation 0.20, leak 0.10, digest or last epoch 0.10,
  continuity 0.10, checkpoints in time 0.20) + 0.1 x node-group pairwise F1.

* E4: results 0.30 (digest .6, peak .4 per job), failure history 0.15, economy 0.20 (units executed
  beyond those that the best checkpoints require, relative to the job, 6% free plus 45 units per
  corruption), no unneeded relaunch 0.10 (a launch while a healthy one runs or after a valid
  result), recovery within 120 s 0.20 (36 faults), no process left at the end 0.05. Economy, relaunch and
  orphans count only for finished jobs. Raw figures: `grade.process`.
* E5: 0.75 x micro F1 over all claims (location .5, or .25 at 9 to 60 lines; kind .5; one claim per
  anomaly), 0.15 x (questions answered in time minus wrong codes), 0.10 x pairwise F1 of the cause groups.
* E6: decisions in time 0.50 (right +1, wrong -1, none 0, good and bad builds weighted equally, floor 0; first
  launch of a lane only), build reports 0.40 (reason .4, p95 .3, errors .3; outages reason only), culprits 0.10.
* E8: samples 0.50 (order .30, T_c .35, honest error bar .10, 1/nu or latent heat .25 each; without a transition order .6, null
  T_c .4), faulty nodes 0.25 (F1, kind counts half), exchange answers 0.10 (right minus wrong, of 96), resets 0.15 (right
  minus wrong, of 48).

Largest single item: E4 .0075, E5 about .005 (v2: up to .058).
Missing shards or jobs in `final.json` fall back to the per-shard/job file.

## Hypotheses (fresh for v2) and decision rule

At least 5 pairs per experiment; differences are paired (A minus B, same seed). A
score difference is material if its mean is at least 0.05 in absolute value with
the same sign in at least 4 of 5 pairs. Cost, tool-result bytes and wall time are
material at 15% of B's mean with the same sign condition. Otherwise: tie. Report
all runs, then without tainted ones. Runs with `low_parallelism` are kept and
reported; a sensitivity analysis drops them. Do not pool experiments.

* **H1 (E1): tie on score, B cheaper.** The analysis is script work and the
  reactions are possible with one watcher loop.
  Falsified if the score difference is material, or A is not at least 15% dearer.
* **H2 (E2): tie on score, and A does not use fewer benchmark seconds.** A driver
  script serves both conditions.
  Falsified if the score difference is material, or program seconds differ by 15%
  with a consistent sign.
* **H3 (E3): A scores higher,** through exit codes, continuity and stall/checkpoint
  reactions right after the shift change.
  Falsified if the score difference is not material or is negative. This is
  realistic: v1 gave A no clear score advantage.

The same rule applies to each pair of arms. Metrics whose B mean is 0 are material if every
condition on the sign holds. `summarize.py` prints the verdict ("a>b", "a<b", "tie").

* **H4 (E4): A loses less to crashes.** A has fewer recomputed program seconds
  (`recomputed_program_s`) AND fewer unneeded relaunches AND fewer orphans than B, each
  material, and no materially lower score. Supported if all three and the score clause hold; partly
  supported if at least one of the three holds and none is materially worse; refuted if
  any of the three is materially larger for A, or the A score is materially lower; otherwise null.
  *Honest expectation:* null or partly supported. All arms are warned, nohup survives these crashes,
  and the native wrapper recipe is in the prompt; A pays for the server fault and for lost ownership.
* **H5 (E5): A scores no lower and reads fewer tool-result bytes.** Supported if the score difference
  A - B is not materially negative and A's bytes are materially smaller. Refuted if the score is
  materially lower or the bytes are materially larger. Otherwise null on bytes.
  *Honest expectation:* score tie, bytes refuted (A larger). Finding anomalies is script work over
  files, A offers native tools plus more round trips, and in the v2 pilot A returned 2.1 to 3.0 times B's
  bytes. Product changes since then may alter this.
* **H6 (E4, E5): cost.** A's `cost_est_usd` is materially higher than B's. Falsified by a tie or a lower cost.
* **H7 (C against B, all experiments): no score difference, higher cost, no shorter wall time.** Per
  experiment: falsified if the score difference C - B is material, or the cost (`cost_est_usd`) is not
  materially higher, or wall time is materially shorter. *Expectation:* few sub-agents are started;
  with fewer than one per run on average H7 says nothing about sub-agents, and is reported as
  "not used".
* **H8 (A against C): descriptive**, no prediction.

**Power, n = 5.** The rule needs the same sign in 4 of 5 pairs. With no true effect the sign part alone holds
by chance in 12 of 32 cases (38%); the size part is what limits false alarms. By simulation (normal
differences), a true score difference of 0 / .05 / .10 is called material in 2 / 48 / 99% of cases with a
paired sd of .05, and in 20 / 41 / 78% with sd .10 (the v2 pilot suggests .05 to .15): a false alarm is likely if scores vary
much, so the observed sd is reported with every verdict. Cost sd between runs of one arm is 10 to 25%, so 15% differences are at the edge. Dozens of metrics times
three pairs times five experiments produce chance verdicts: only H1 to H7 are confirmatory. A null
result is "tie" on the primary metrics of a hypothesis. It means "no difference of this size seen",
not equivalence. E4 raw counts near zero in every arm (a ceiling) are also null. Errors cluster by agent (four
per run), so many small items give finer resolution, not more independent data.

## Secrecy and isolation

* One seed per (experiment, rep), shared by all arms; arm order shuffled per
  block, experiments shuffled per rep.
* No truth file exists; the grader recomputes truth from a seed that stays in the
  runner's memory until the pair ends (`SS_EXP_SEED`/`--seed` are visible to `ps`).
* Workspaces hold compiled programs and opaque images only, in
  `OUT/runs/<random id>/ws`. A finished run's directory is mode 000 while the
  others of its block run. After a hard kill of the runner: `chmod 700 OUT/runs/*`.
* Tool inputs are scanned for reads of harness, other runs, program internals,
  decompilers and transcripts; `bin/` is hashed. Hits mark the run `tainted`.
  This detects, it does not prevent.
* In A each run has its own Pluto server (`tests/smart_streams_server.py`) with
  `max_running=96, max_per_agent=32`. The server's defaults are 16 running and 8 per
  agent: with them these tasks would queue, so results hold for a raised limit.
* Cleanup kills only tracked descendants (pid + start time) and tty-less processes
  started after the run began whose command line or cwd is in the workspace.

## Metrics per run

v1's metrics plus `protocol_version`, `peak_streams`, `peak_streams_total`,
`low_parallelism`, per-agent `timed_out`, `cpu_share_mean/max` (whole machine, from
`ps`), `program_cpu_s` and `max_pacing_lag_s` (worst delay of any program behind its
own schedule; small values mean that load did not distort timing). Agent limits
are equal in all arms: 1200 s (E1, E2, E3 shift 2, E5), 240 s (E3 shift 1), 1100 s (E4).

Measured with the scripted solvers (10 cores, 24 GB, no LLM), score 1.0 in every
cell. E2 is seed 4242, E1 and E3 seed 777; values are plain / jobs style.

| | Wall s | Output MB | Launches | Peak per agent | Peak total | Program CPU s | Machine CPU mean; max | Max lag s |
|---|---|---|---|---|---|---|---|---|
| E1 | 221 / 222 | 93 / 93 | 48 | 12 | 48 | 28 / 43 | 0.14; 0.38 / 0.09; 0.29 | 0.03 / 0.03 |
| E2 | 409 / 408 | 113 / 113 | 226 | 15 / 16 | 60 / 62 | 48 / 53 | 0.07; 0.54 / 0.09; 0.38 | 0.25 / 0.13 |
| E3 | 510 / 510 | 57 / 57 | 24 | 12 | 24 | 16 / 33 | 0.13; 0.49 / 0.07; 0.31 | 0.02 / 0.02 |

Maxima are single samples, and include everything else on the machine.

E4 and E5 with the scripted solvers (real crash schedule and runner code; another agent's tests were running
on the machine). E4 wall times bound a fast team; agents need longer, limits 1100 and 1200 s.

| | Score | Wall s | Launches | Peak per agent | Max lag s |
|---|---|---|---|---|---|
| E4 plain, seed 777 (3 agent crashes) | 1.0 | 397 | 76 | 10 | 0.02 |
| E4 jobs, seed 778 (3 agent crashes, 1 server crash) | 1.0 | 404 | 76 | 10 to 11 | 0.02 |
| E5 plain, seed 777 | 1.0 | 311 | 48 | 12 | 0.12 |
| E5 jobs, seed 4242 | 1.0 | 314 | 48 | 12 | 0.02 |
| E4 plain, seed 777, policy naive | 0.8465 (6212 s recomputed) | 715 | 103 | 10 | 0.02 |

The first E4 plain run scored 0.9968: the solver relaunched a program in the moment between its exit and the
wrapper writing the exit code (a race that native agents can meet too). Only the solver was
changed. E5 found and fixed two generator defects before any agent run (a LEVEL plant without violation, a line number
off by one after a table header). The jobs solver calls the HTTP API under the crashed agent's id, so it does
not meet the lost-ownership problem described above.

**Cost.** `cost_usd` is the reported total. A killed session (E3 shift 1, E4 crashes) has no result event; `cost_est_usd`
then uses list prices, which reproduce the reported cost of all 24 v2-pilot sessions, with input and
cache tokens from the messages (equal to the final totals in all 24) and output tokens estimated
(leave-one-out error of the cost: median 1.4%, worst 7.7%, on whole sessions; an interrupted message is
undercounted). E4 uses `cost_est_usd` as its cost metric.

**Sub-agent accounting: what is and is not verified.** From the 78 transcripts at hand: every message has
`parent_tool_use_id` (always null there, as no sub-agent ran), result events have `subagent_stats`, and
message usage adds up to `modelUsage` (24 checked). From the installed CLI's text (2.1.319, not run): sub-agent messages
carry the id of the Agent call; the environment variables above exist; `--forward-subagent-text`
also streams their text. NOT verified, needs one run: that `modelUsage`/`total_cost_usd` and
`--max-budget-usd` include sub-agents, that the variables act in `-p` mode, that denials reach sub-agents.
Guards: if messages show more cache-read tokens than `modelUsage` the run is flagged `subagent_usage_missing` and the
estimate adds them; `subagent_model`, `subagent_nested`, `subagent_call` (in A or B), leaks `forbidden_tool` and
`isolation`. Check these flags on the first C run before spending more. Sub-agents that run in the background
may end silently with their parent.

**What calls are for** (`categories`, `ss_classify.py`, all arms and sub-agents): argument bytes, turns
and estimated output tokens per class: process handling, output analysis, coordination, answer writing, other. The
rules are in its docstring. A manual check of 40 random v2-pilot calls (one rater, rule output
hidden, held out) found 4 errors: 10% (95% CI 4 to 23%), on mixed Bash calls and scripts whose
file name does not tell what they do. Differences of a few percent between arms mean nothing. Exploratory.

## Threats to validity

* E4 warns agents of crashes, so it measures recovery by prepared teams. Unprepared native agents would
  lose more. Crash times follow the wall clock and hit agents at different stages of their work; an agent that is
  faster has finished more. A crash that finds its agent finished is recorded as missed (`chaos`).
  A server crash needs the agent to survive it; there is one per run only.
* E4 and E5 have the same author as the harness, who knows the product guide. E4's themes
  (durable output, exit codes) are the product's selling points, E5's theme is not.
* E5's structure mix, number formats and English messages are synthetic. Its root cause is one lane number.
* C differs from B by more than the offer if the Agent tool's description changes behaviour by itself.

* The SmartStreams implementation changed between the v1 runs and v2: compact
  results (after the v1 pilot), fixes from two code reviews, and list arguments
  (`commands`, `job_ids`, once-only `job_wait` events) added after the v2
  stress test and before any v2 agent run. v1 and v2 numbers must not be pooled.
* After v2 pilot 1 (one rep; excluded from the main results and reported as
  "before") the implementation changed to reduce agent cost: short ids, group
  handles, `each` templates, `wait_s`, the output path, the last line on exit,
  fewer wakeups, smaller schemas and results, longer waits, and a connection
  retry fix. Prompts, tasks, graders and hypotheses did not change. These are
  general features, but they were chosen after seeing agents on these tasks, so
  the cost result after them is partly tuned and needs other tasks to confirm.
  A new pilot precedes the main run.
* The leak detector then changed too: all four pilot-1 flags were false alarms, all
  in native runs (`pgrep` on a program name; `../..` inside the workspace). It now
  resolves `..` and matches within a line. It does not catch `pkill -f` patterns
  that reach a teammate's program, which happened once (native, E2, no score
  effect). `duration_ms` and MCP `tool_errors` were undercounted and are fixed.
* Then came `job_analyze` and `job_watch` (built by an agent that did not see the
  experiments), a smaller default tool set, the `pss` shell command, per-job grep
  limits, and crash-safe agent identity: building E4 showed that a replacement agent
  was renamed and did not own its predecessor's jobs. That is fixed in the product
  before any E4 agent run, so E4 was not blind to this defect. The SmartStreams prompt paragraph
  now names means instead of one tool per step.
* In pilot 2, three SmartStreams variants (MCP `ss_mcp`, shell `ss_cli`, both `smartstreams`) were
  compared on E1-E3 in pilot 2, one rep each, and the cheapest whose mean score is
  within 0.05 of the best is frozen. E1-E3 cost results are therefore tuned;
  E4 and E5 are run by no agent before the main run.
* Pilot 2 go/no-go against pilot 1 (fixed in advance): total SS cost on E1-E3
  at least 25 % lower, turns at least 25 % lower, mean score at most .05 lower,
  no protocol failure, every peak at least 8, the new means used.
* **v4.** Pilot 2 was stopped by its owner after ten runs (E1, E3). It showed agents writing
  watchers, id tables, wait loops and per-job collectors by hand in every arm, and never using
  `analyze` or `watch` from the shell. The product then got rules that act, names, `each`,
  `report`, `brief` and terse listings. They were designed from E1 and E3 transcripts, so
  E1-E3 are *tuned for A*, E3 most. E6 was written by an author who saw neither the plan nor
  the product code, and was not opened by the implementer before the product was frozen.
  The arm `smartstreams` is now `pss` without MCP tools, as shipped; `ss_mcp` and `ss_both`
  remain as variants.
* Pilot 3 pass mark, fixed before the code: E3 + E6 together cost less than same-day native with
  a mean score at most .05 lower, and E1 + E2 cost at most 1.25 times native with no lower score.
* The pilot 2 detector flagged agents that read raw output of their own run's jobs, which the
  product points them to, and `cd` inside here-documents. Both are fixed; the transcripts of a
  run's agents are now matched explicitly.
* Pilot 1's E3 SS run overlapped other work on the machine (pacing lag up to .62 s
  against .02 s for native).
* Scores are coarse: one E2 podium entry is .058 and one E1 livelock .031, against
  the .05 materiality threshold, and errors cluster by agent. In pilot 1 each score
  gap came from one mistake by one native agent.
* v2 was shaped after v1 results (see History), in order to lift tasks off the
  ceiling, not toward a condition; the ceiling may return, as Sonnet scripts well.
* Small n, one model, one machine. Defects are planted in trace emission; AVL and
  shortest paths are real, the E3 map and E2 costs are simulated.
* Reaction windows (75 to 150 s) interact with model latency. API slowness hits
  both conditions, at different moments.
* E3 shift-1 agents that hit the cutoff have unknown cost (`cost_complete` false).
  The deadline of a request issued shortly before the shift change may pass
  between shifts in both conditions. Early requests come at 55 to 120 s of job time.
* A's tool descriptions add context; ToolSearch may defer MCP tools. Prompt
  paragraphs cannot be proven equivalent.
* `job_start` runs outside Claude Code's Bash handling; Bash background and Monitor
  behaviour depend on the CLI version.
* Secrecy relies on prompt rules plus detection; agents share the OS user.

## Commands

    PY=<python with the mcp package>; H=docs/research/experiments/smart_streams
    (cd src_erl && rebar3 compile)
    $PY -m pytest tests/test_experiment_smart_streams.py tests/test_experiment_smart_streams_e45.py \
        tests/test_experiment_smart_streams_e6.py tests/test_experiment_smart_streams_e8.py
    $PY $H/verify_solvers.py SCRATCH --exp 1,2,3 --style plain     # full size, no LLM
    $PY $H/verify_solvers.py SCRATCH --exp 1,2,3 --style jobs      # real server and client
    $PY $H/verify_e45.py SCRATCH --exp 4,5 --style plain|jobs [--policy naive]
    $PY $H/verify_e6.py SCRATCH [--policy wise|lazy|errors]
    $PY $H/verify_e8.py SCRATCH [--policy wise|lazy]
    $PY $H/run.py --out ~/Workspace/ss-experiments/v3 --reps 5 --dry-run
    $PY $H/run.py --out ~/Workspace/ss-experiments/v3 --reps 5 --budget-usd 5 \
        [--exp 4,5] [--conditions smartstreams,native,native_sub]
    $PY $H/summarize.py ~/Workspace/ss-experiments/v3 [--exclude-tainted]
    $PY $H/ss_classify.py ~/Workspace/ss-experiments/v3 [--sample 40 --seed N --hide]

`--out` must be outside git. `--http-port N` (with `--pluto-host` and
`--smartstreams-home`) uses a running server instead of private ones.
