# Experiment: PlutoSmartStreams vs native Claude Code (protocol v2)

Status: pre-registered before any v2 LLM run.

**History.** v1 (git a9eab16) is the previous protocol. v2 was designed after
seeing the v1 pilot and partial v1 main results, so it is not blind to them:
scores were at the ceiling in both conditions (0.97 to 1.0 in the pilot, 0.92 to
1.0 in main), A's score was about B's, and A cost about 1.7 to 3 times as much as
B and returned more tool-result text. v1 gave an agent only 2 to 4 streams. v1
rows have no `protocol_version`; v2 rows have 2, and the two are never mixed.

Four Claude Sonnet agents solve one task in a shared directory. Each agent is a
headless `claude -p` process.

| | A `smartstreams` | B `native` |
|---|---|---|
| Tools | B's tools plus the 15 `job_*` MCP tools | Bash (with `run_in_background`), BashOutput, Monitor, Read, Grep, Glob, Write, Edit, ... |
| Task, model, budget, timeout, workspace, agent-to-agent channel (files) | same | same |
| Prompt | same core, one tools paragraph | same core, one tools paragraph |

A's paragraph mentions that `job_wait` and `job_grep` take a `job_ids` list. B's
mentions detaching, saving exit codes, and shell loops/xargs over many logs. A
may use everything B uses, so A is "native plus SmartStreams".

Permissions: `--permission-mode dontAsk`, explicit `--allowedTools`,
`--strict-mcp-config`; subagents, web, skills and messaging are denied in both.
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

## Scoring (max 1.0)

* E1: shards 0.50 (per violated kind: kind 0.3, op 0.4, key 0.3, divided by the
  larger of true and claimed kinds; a clean shard needs an empty report), cells
  0.25, livelock reactions 0.25 (stops in time minus false kills, over 8).
* E2: podium 0.50, defects 0.30, efficiency 0.20 (times podium accuracy; 1 up to
  1.25 times the reference unimodal search, 0 at exhaustive enumeration).
* E3: 0.9 x mean over jobs (run id 0.05, outcome 0.15, exit code or timely
  termination 0.10, violation 0.20, leak 0.10, digest or last epoch 0.10,
  continuity 0.10, checkpoints in time 0.20) + 0.1 x node-group pairwise F1.

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

## Secrecy and isolation

* One seed per (experiment, rep), shared by A and B; condition order shuffled per
  pair, experiments shuffled per rep.
* No truth file exists; the grader recomputes truth from a seed that stays in the
  runner's memory until the pair ends (`SS_EXP_SEED`/`--seed` are visible to `ps`).
* Workspaces hold compiled programs and opaque images only, in
  `OUT/runs/<random id>/ws`. A finished run's directory is mode 000 while its
  partner runs. After a hard kill of the runner: `chmod 700 OUT/runs/*`.
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
are equal in both conditions: 1200 s (E1, E2, E3 shift 2), 240 s (E3 shift 1).

Measured with the scripted solvers (10 cores, 24 GB, no LLM), score 1.0 in every
cell. E2 is seed 4242, E1 and E3 seed 777; values are plain / jobs style.

| | Wall s | Output MB | Launches | Peak per agent | Peak total | Program CPU s | Machine CPU mean; max | Max lag s |
|---|---|---|---|---|---|---|---|---|
| E1 | 221 / 222 | 93 / 93 | 48 | 12 | 48 | 28 / 43 | 0.14; 0.38 / 0.09; 0.29 | 0.03 / 0.03 |
| E2 | 409 / 408 | 113 / 113 | 226 | 15 / 16 | 60 / 62 | 48 / 53 | 0.07; 0.54 / 0.09; 0.38 | 0.25 / 0.13 |
| E3 | 510 / 510 | 57 / 57 | 24 | 12 | 24 | 16 / 33 | 0.13; 0.49 / 0.07; 0.31 | 0.02 / 0.02 |

Maxima are single samples, and include everything else on the machine.

## Threats to validity

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
    $PY -m pytest tests/test_experiment_smart_streams.py
    $PY $H/verify_solvers.py SCRATCH --exp 1,2,3 --style plain     # full size, no LLM
    $PY $H/verify_solvers.py SCRATCH --exp 1,2,3 --style jobs      # real server and client
    $PY $H/run.py --out ~/Workspace/ss-experiments/v2 --reps 5 --dry-run
    $PY $H/run.py --out ~/Workspace/ss-experiments/v2 --reps 5 --budget-usd 5
    $PY $H/summarize.py ~/Workspace/ss-experiments/v2 [--exclude-tainted]

`--out` must be outside git. `--http-port N` (with `--pluto-host` and
`--smartstreams-home`) uses a running server instead of private ones.
