# Experiment: PlutoSmartStreams vs native Claude Code

Status: protocol, pre-registered before any LLM run.

Four Claude Sonnet agents solve one task in a shared directory. Each agent is a
headless `claude -p` process.

| | A `smartstreams` | B `native` |
|---|---|---|
| Tools | B's tools plus the 15 `job_*` MCP tools | Bash (with `run_in_background`), BashOutput, Monitor, Read, Grep, Glob, Write, Edit, ... |
| Task, model, budget, timeout, workspace, agent-to-agent channel (files) | same | same |
| Prompt | same core, one tools paragraph | same core, one tools paragraph |
| MCP | `--strict-mcp-config` with smartstreams | `--strict-mcp-config` with no server |

B is told the strong native practice: log to a file, run detached, save the exit
code, filter with grep/awk/python. A may use that too, so A is "native plus
SmartStreams" and `smartstreams_calls` reports how far it was adopted.

Permissions: `--permission-mode dontAsk` and an explicit `--allowedTools` list.
Subagents, web, skills and messaging are denied in both conditions. All session
variables of an enclosing Claude Code process are stripped from agents, server
and jobs; only authentication variables remain.

## Experiments and hypotheses

Decision rule, fixed in advance: n >= 5 pairs per experiment. Effects are paired
differences (A minus B, same seed). A score difference counts as material if its
mean is at least 0.05 in absolute value and the sign holds in at least 4 of 5
pairs. Cost, bytes and wall time are material at a 15% difference of means with
the same sign condition. Anything else is reported as a tie. Scores in
[0,1] are the primary outcome, the rest is secondary. Report all runs, then
repeat with `--exclude-tainted`.

**E1 shard_debug (debugging).** Eight shards of a persistent AVL map, ~9 MB
trace each in ~90 s, two per agent. Find the first invariant violation (op,
kind, key) per shard, one shard being healthy, amid legal transients, replica
reads, noise lines and secondary damage. Find the origin of a corrupted cell and
its path across five shards. Kinds: ORDER, BALANCE, SIZE, STALE, PERSIST.
* H1 (expected tie): scores differ by less than 0.05 and A is not cheaper. The
  checks need arithmetic and joins, for which log files plus Python suffice.
* Falsified if: the score difference is material, or A costs at least 15% less.

**E2 algo_engineering.** Eight opaque shortest-path variants, five sizes, 4 to
240 s per run and up to ~20 MB of trace. Find the cheapest correct variant per size, two crossover
sizes, and the defective variant (size, vertex, correct distance). Dominated
runs should be stopped; benchmark seconds are part of the score. Costs are
deterministic counters under a simulated machine, so load does not change them.
* H2: A uses fewer benchmark seconds or fewer tool-result bytes (15%), with
  equal correctness, because waiting on patterns and cancelling is one call.
* Falsified if: neither falls by 15%, or A's winners/crossovers/bug parts are
  materially lower.

**E3 relay_handoff (2+2 agents).** Two shift-1 agents launch six 5-minute soak
jobs, hand over and end (hard stop at 150 s). Two shift-2 agents finish. One job
crashes, one stalls, some exit nonzero, some change the probe limit mid-run. A
relaunch is a different run, so a job that died with its session is measurable
(continuity, relaunches). Ownership is crossed: p1 starts 1,2,3 but q1 reports
1,3,5.
* H3: A scores higher, through continuity and exit codes.
* Falsified if: the score difference is not material or is negative. That is a
  realistic outcome, because B is told how to detach and save exit codes.

## Scoring (max 1.0)

* E1: 8 shards 0.085 each (op 0.035, kind 0.025, key 0.025; healthy shard: kind
  only), root cause 0.20, path 0.12.
* E2: winners 0.35, crossovers 0.20, defect 0.30, efficiency 0.15 (times winner
  accuracy). Efficiency is 1 up to 1.3 times the perfect-information early-stop
  total and 0 at full enumeration.
* E3: mean over jobs of run_id 0.10 and 0.15 each for outcome, exit code (stall:
  terminated), epoch, bucket, final digest or last epoch, continuity.

If `final.json` lacks a shard or job, its per-shard/job file is used.

## Secrecy and isolation

* Each (experiment, rep) has one seed, shared by A and B. Condition order is
  shuffled per pair and experiments are shuffled per rep.
* No truth file exists. The grader recomputes truth from the seed, which stays in
  the runner's memory until the pair is over (give `SS_EXP_SEED` or `--seed` only
  for reruns: they are visible to `ps`).
* Workspaces hold compiled programs and opaque images, no sources. They live in
  `OUT/runs/<random id>/ws`; a finished run's directory is mode 000 while its
  pair partner runs. After a hard kill of the runner: `chmod 700 OUT/runs/*`.
* Every tool input is checked for reads of harness, other runs, program
  internals, decompilers and transcripts; `bin/` is hashed before and after.
  Hits mark the run `tainted`. This detects, it does not prevent.
* In A each run has its own Pluto server and home under its run directory
  (`tests/smart_streams_server.py`), with limits of 32 running jobs and 16 per
  agent, so that the scheduler is never the bottleneck.
* At the end only processes of the run are killed: tracked descendants (pid and
  start time) and tty-less processes started after the run began whose command
  line or cwd is in the workspace.

## Metrics per run

Score and parts, wall time, cost (missing if an agent was killed:
`cost_complete`), input / cache-read / cache-write / output tokens, turns, calls
per tool, bytes of tool-result text returned to the model (total, per tool,
max), tool errors, permission denials, program seconds and launches, protocol
failures (timeout, budget, error result, denial, MCP not connected, SmartStreams
unused, missing final answer). A shift-1 `cutoff` is reported but is not counted
as a failure.

## Threats to validity

* Small n, one model, one machine; synthetic traces rather than field logs.
* The E1/E2 defects are planted in trace emission; the AVL map and the
  shortest-path algorithms underneath are real and correct. The E3 map is
  simulated. E2 costs come from a fitted simulated-machine model.
* A's tool descriptions add context tokens, and ToolSearch may defer MCP tools.
* A gets one sentence on ownership; B gets one on detaching. The paragraphs are
  similar in length but cannot be proven equivalent.
* Native `run_in_background`/Monitor behaviour depends on the CLI version (2.1.319 here).
* `job_start` is executed by the server, outside Claude Code's Bash handling.
* E3 rewards durability, the product's stated strength. E1 rewards free-form
  scripting, B's strength. Report the three experiments separately; do not pool.
* Secrecy relies on prompt rules plus detection; agents share the OS user.
* Killed agents have unknown cost and lower-bound tokens. E3 shift-1 agents that
  hit the cutoff therefore make the run's cost incomplete.

## Commands

    PY=<python with the mcp package>; H=docs/research/experiments/smart_streams
    (cd src_erl && rebar3 compile)
    $PY -m pytest tests/test_experiment_smart_streams.py
    $PY $H/verify_solvers.py /some/scratch --exp 1,2,3      # full size, no LLM
    $PY $H/run.py --out ~/ss-exp --reps 5 --dry-run
    $PY $H/run.py --out ~/ss-exp --reps 5 --budget-usd 3
    $PY $H/summarize.py ~/ss-exp
    $PY $H/summarize.py ~/ss-exp --exclude-tainted

`--out` must be outside git. `--http-port N` (with `--pluto-host` and
`--smartstreams-home`) uses a running server instead of private ones.
Worst case: 3 experiments x 2 conditions x 4 agents x budget per rep ($72 at $3).
