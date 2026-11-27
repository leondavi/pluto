# PlutoSmartTasks (PST): technical overview and experiments

This document explains what PlutoSmartTasks (PST) is, how it is built on top of
Pluto, what each feature does, and how it was evaluated against plain Claude Code.
It is the companion to the data in this directory. For day-to-day use, see the
[user guide](../../guide/pluto-smart-tasks.md); for the study protocol in full, see the
[experiment harness](../experiments/smart_tasks/README.md).

## 1. Why PST exists

LLM coding agents now *run* work as well as write code: test suites, parameter
sweeps, benchmark campaigns, scientific simulations, often a dozen at a time and
often as a team of agents on one machine. With only a shell, an agent does what a
person in a hurry would do: start each program in the background, send its output
to a file, write a watcher script, and then repeat "sleep, look" until done.

That has four costs, and all of them grow with the amount of work:

1. **Every look is a paid model step.** An agent works in steps, and each step
   rereads the whole conversation. Cost follows the number of steps.
2. **Output does not fit.** A run that prints tens of megabytes cannot be read; the
   agent sees a truncated tail or floods its context.
3. **Work dies with the session.** Background processes and their meaning are lost
   when the agent's session ends.
4. **Teams collide.** Agents sharing a machine share its processes, files and
   names. (In our pilots one agent's clean-up command killed a teammate's session.)

Process supervisors and batch schedulers solve the runtime side for people and
programs. PST adapts those ideas to a caller that reads by the token, acts by the
step and pays for both: the server owns the processes, keeps all output, and does
the routine watching deterministically, so the agent is woken only when needed.

## 2. Where it sits: Pluto

[Pluto](../../../README.md) is an Erlang/OTP coordination server for teams of AI
agents. Before PST it offered agent identities, direct and broadcast messages,
read/write locks with leases and fencing tokens, deadlock detection and a task
registry, over TCP, HTTP and MCP. Two earlier studies shaped PST:

- [Token efficiency](../token-efficiency.md) (Aug 2026): the unbounded cost of a
  connected agent is steps spent waiting, so waiting must move out of the model.
- [Sub-agents vs Pluto](../subagents-vs-pluto-communication.md) (Oct 2026, 40 runs):
  a dedicated mechanism paid off where coordination was hard (hand-offs, stale
  leases); on simple fan-out, the model's own improvised scheme was cheaper.

PST is a set of modules inside the same Erlang server (`src_erl/src/pluto_job_*.erl`),
reached over `/jobs/*` HTTP routes, plus a Python client: the `pst` command and an
MCP adapter (`src_py/agent_smart_tasks/`). It reuses Pluto's agent identities and
locks.

```
agent (Claude Code / Copilot CLI)
   │  pst …  (shell)   or   job_* MCP tools
   ▼
PST client (src_py/agent_smart_tasks)  ──HTTP /jobs/* + key──►  Pluto server (Erlang)
                                                                 ├─ pluto_job_mgr      registry, scheduler, recovery
                                                                 ├─ pluto_job_admit    limits, slots, after, locks
                                                                 ├─ pluto_job_watch    standing rules
                                                                 ├─ pluto_job_read/grep/analyze   bounded reads
                                                                 └─ pluto_job_store    packages on disk
                                                                        │ spawns
                                                                        ▼
                                                  pluto_job_runner.py (one detached runner per job) ─► the command
                                                                        │
                                                       ~/.pluto/smarttasks/jobs/<id>/   (the job package)
```

## 3. Concepts

**Job.** A command run on the agent's behalf. The server, not the agent's
session, owns it: a detached runner starts it in its own process group, the
command writes straight to `output.log`, and the job survives the agent's session,
a crash of the runner (state `orphaned`) and a restart of the server (it is
reattached).

**Job package.** A directory per job: `package.json` (spec, owner, provenance),
`state.json` (lifecycle, exit code, size), `output.log` (raw output),
`manifest.jsonl` (64 KiB chunks with chained SHA-256 hashes), `watch.json`
(rules and pending actions), `reviews.jsonl`. It can be exported as one `.pjob`
file and imported on another host, where every hash is verified and nothing runs.

**Lifecycle.** `queued → starting → running ⇄ paused → succeeded | failed |
cancelled | signaled | timed_out | output_limit`, plus `orphaned`, `lost` and
`snapshot`.

**Owner.** Every job has one owning agent. Any agent of the same user may read
it; only the owner (or the user) may control it. Ownership can be handed over.

## 4. Features

### 4.1 Launch many jobs with one command

```
pst run -n soak -e k=1-12 'bin/soak --job {k}'
```

starts twelve jobs named `soak/1` … `soak/12`. Names are how agents think about
their work, so every command accepts them: `soak`, `soak/3`, `soak/1-6,9`. A name
means **your own** jobs. Only when you have none of that name do reading commands
(`ps`, `wait`, `tail`, `grep`, `each`, …) use teammates' jobs of the same directory,
and say whose they are; `follow` and `--after` never do (name others' jobs by id or
group handle). Running a key again replaces it under the same name.

### 4.2 Read without flooding the context

- `pst head`, `pst tail`, `pst grep` (with `-A/-B/-C`, `-c`, `-v`, `-i`): bounded
  reads and server-side search (`pst cat` still prints a whole log when asked).
- Durable per-agent cursors (`job_next_chunk`) for "what is new since I last looked".
- `job_analyze`: outline output that has never been seen (line templates with
  counts, likely errors, rare lines, jobs that deviate) and extract fields across jobs.

### 4.3 Standing rules: the server reacts while the agent does something else

A rule is a condition plus actions, attached at launch so that even the shortest
job cannot outrun it:

| Condition | Action |
|---|---|
| `--on 'PATTERN {field}'`: a line matches (fields are captured) | `--append FILE TEXT`: write a reply to a control file |
| `--where 'c > 1'`, `'loss > prev * 1.5'`, `'abs(a - b) > 3'`: conditions on captured fields (comparisons, arithmetic between fields, previous values, `abs`; no `exp`/`log`) | `--input TEXT`: write to the job's stdin |
| `--idle S`: no output for S seconds | `--stop`, `--pause` |
| `--absent S`: a pattern has not appeared for S seconds | `--tell`: wake the agent with a notice |

Example: stop any member whose numerical stability number exceeds 1, and tell me:

```
pst run -n m -e k=1-12 'bin/swm --member {k}' --on 'step {n} cfl={c}' --where 'c > 1' --stop --tell
```

Rules and their pending actions are written to disk before anything is announced,
so after a `kill -9` of the server every hit still becomes exactly one notice.
Output is untrusted: captured values are checked before they are used in file
names, and **no rule ever runs a command built from output**.

### 4.4 The agent's own code, run by the server

Not everything fits a rule. These features run a small program the agent writes:

- `pst each JOBS 'python3 parse.py {log} {state}' -o 'out/{k}.json'` runs once per
  job as it ends, from the agent's shell (output kept completely or not at all;
  values filled into `-o` must be plain file-name parts). With `{state}` it can also
  restart the jobs a rule stopped, in one command.
- `pst follow JOBS 'python3 -u judge.py {k}'` runs on live output, from the
  beginning, as a server job of its own; it can be replaced without restarting the
  work. Followers have their own running limits, apart from other jobs.
- `pst report` builds a record per job from patterns, with no program at all.

### 4.5 Waiting that ends when something happens

`pst wait JOBS -t S` returns when jobs end, a rule tells, or the time is up, and
then prints where every job is, so it replaces `sleep; ps`. After every command one
line on stderr reports which of the agent's jobs ended or were handed over since
the last command (`pst: meanwhile ended: z/3 failed, …`).

### 4.6 Admission: limits, ordering and locks for processes

| Option | Is | Meaning |
|---|---|---|
| `--slot NAME:N` | a counting semaphore | at most N running jobs of this slot, all agents together |
| `--after JOBS` (`--after-ok`) | a barrier / dependency | start once these have ended (only if all succeeded) |
| `--lock RES[:read]` | Pluto's lock | hold the write or shared read lock on RES while running |

The server also caps running jobs per server (128) and per agent (32); excess jobs
queue rather than fail. Order is kept per lock and per slot: a job held back by
its slot or its lock keeps only that resource for itself, so readers cannot starve a
writer while its other resource stays free. Claude Code offers none of these for processes; see
[the comparison](../claude-code-vs-pluto-coordination.md).

### 4.7 Handing work over

`pst give AGENT JOBS` (or `job_transfer`) moves ownership, rules and unseen notices
to another agent; `pst brief JOBS` summarises the state for the new owner. Jobs
can move to another host with `--export | ssh host … --import -`; a running job
moves as a read-only snapshot.

### 4.8 Safety

- `/jobs/*` runs commands, so it needs the key in `~/.pluto/smarttasks/key`
  (mode 0600), accepts loopback only and sends no CORS headers.
- Jobs run as the server's OS user; the launcher never pre-approves job start in
  the agent's permission rules.
- Environment values are never stored; exports strip local paths.

### 4.9 Interfaces

- **`pst` (shell).** The default: costs no context until used and composes with
  pipes and loops. `PlutoSmartTasks.sh` puts it on the agent's `PATH`.
- **MCP tools** (`job_start`, `job_wait`, `job_grep`, `job_watch`, …), shown with
  `PST_TOOLS=core` or `all`.
- **Launcher** `PlutoSmartTasks.sh` for Claude Code and Copilot CLI; `--manage`
  (terminal UI for people), `--history`, `--stats`, `--export`, `--import`, `--prune`.

## 5. How PST was evaluated

### 5.1 Design

- **Arms.** *native*: Claude Code with its own tools (Bash, Read, Grep, …); its
  tools paragraph mentions `nohup` and background monitoring. *smarttasks*: the same
  plus `pst`; its tools paragraph describes `pst`, and PST also gave a short usage
  example through its tool instructions and wake-up notices when jobs ended. Same
  model (Claude Sonnet), prompt core, budget ($5 per agent) and limits.
- **Teams.** Four agents per run, coordinating through files; each agent owns twelve
  parallel jobs.
- **Pairs.** Both arms of a pair use the same random seed, so they face the same
  instance and the same seeded faults.
- **Blind tasks.** Each task was written by a separate LLM agent that saw the
  harness but not PST's code or design notes. The same five tasks were also run in
  the stopped protocols 5 and 6, with product changes in between.
- **Protocol fixed in advance (protocol 7).** Eight pairs per task; pairs in which
  the test harness denied a command in either arm (a Claude Code permission quirk,
  equally likely in both arms) are rerun as fresh pairs; a watcher stops the study
  at the first sign of agents' jobs leaking across agents.

### 5.2 Tasks

| Task | What the team does | What must happen while jobs run |
|---|---|---|
| **E7 Riemann zeta zero campaign** | Verify the Riemann hypothesis on 48 windows of the critical line (Riemann–Siegel formula, Gram blocks certified by Rosser's rule); report counts, faults, close pairs, spacing statistics | Refine blocks short of zeros within 75 s; stop and restart evaluators whose arithmetic goes wrong within 120 s |
| **E8 Ising/Potts phase transitions** | Find the phase transition of 8 unknown magnetic samples with Metropolis Monte Carlo on 48 nodes, some faulty; report order, T_c with error bar, 1/ν or latent heat, faulty nodes | Answer replica-exchange questions within 60 s using u < exp((1/T₁ − 1/T₂)(E₁ − E₂)); reset stuck replicas |
| **E9 Sequencing cohort QC** | 48-sample SNP-panel cohort (simulated library, sequencing, alignment); call genotypes, estimate contamination, check identity, find relatives, allele frequencies | Stop and re-sequence contaminated libraries and degrading flowcells within 60 s |
| **E10 Graph colouring campaign** | Chromatic number of 32 graphs: race exact branch and bound against tabu search and annealing | Hand better colourings to running exact searches within 45 s; stop hopeless searches by 150 s; reject wrong certificates |
| **E11 Storm-surge ensemble forecast** | 48 members of a shallow-water surge model; verify against a nature run (RMSE, spread, rank histogram) | Stop members that go unstable and rerun them with a shorter time step |

### 5.3 Measures

- **Score** in [0, 1], computed by each task's grader from the files the agents wrote
  in `answers/`, against a reference computed independently of the agents' programs.
- **Cost** in USD, as reported by Claude Code for each agent, summed over the team.
- **Steps** (model turns) and tool calls, summed over the team; turns are classified
  by purpose (starting/checking jobs, reading output, coordination, answers).
- **Validity checks**: leak detector (tool inputs outside the run's workspace),
  cross-agent job links, permission denials, timeouts, leftover processes.

## 6. Results (protocol 7, final)

80 runs plus 14 reruns, all recomputed from the raw transcripts and re-graded by an
independent audit (every value matched).

### 6.1 Pre-registered decision (pooled over 40 pairs)

PST is better if the mean paired relative cost difference has a one-sided 95%
bootstrap bound below 0 and the mean score difference a one-sided 95% bound above
−0.05; worse if the cost bound is above 0 or the score bound below −0.05; otherwise
inconclusive. Brackets: 90% interval (the one-sided 95% bounds).

| Analysis | Pairs | Cost difference | Score difference | Decision |
|---|---|---|---|---|
| As run | 40 | +3.2 % [−0.7, +7.0] | +0.003 [−0.018, +0.025] | **inconclusive** |
| Refused pairs replaced by reruns | 40 | +1.9 % [−1.7, +5.4] | −0.011 [−0.030, +0.008] | inconclusive |
| Without pairs with program lag > 0.25 s | 32 | +6.8 % [+2.8, +10.6] | +0.009 [−0.010, +0.028] | worse |

The load check disagrees with the primary analysis, so by the protocol the result is
**load-dependent**. The check removes 7 of 8 Riemann pairs, the task on which PST
saves most; the lag (up to 1 s) occurred mostly in PST runs of that task and worked
against PST, whose reactions there have deadlines. Its likely source is the PST
runner falling behind in reading heavy output, a product issue to investigate.

### 6.2 Per task (secondary)

All pairs as run; brackets: refused pairs replaced by reruns, where different.
Ratios PST ÷ native are geometric means of the 8 paired ratios with p-values from a
t-test on the log ratio; score p-values from a paired t-test.

| Task | Score gap | Cost ratio | Steps ratio |
|---|---|---|---|
| E7 Riemann zeta zeros | +0.01 (n.s.) | **0.77** (p = 0.007) | 0.78 |
| E8 Ising/Potts | −0.06 (n.s.) [**−0.11**, p = 0.02] | **0.77** (p = 0.002) | 0.80 |
| E9 Sequencing QC | 0.00 | 1.03 (n.s.) | 1.02 |
| E10 Graph colouring | **+0.03** (p = 0.02) | **1.24** (p = 0.001) [1.19] | 1.32 |
| E11 Storm surge | +0.04 (n.s.) [+0.01] | **1.29** (p = 0.006) [1.37] | 1.49 [1.64] |

The cost ratio rises with the total running time of the programs a team launched
(22,200, 18,300, 14,800, 9,500 and 8,800 program-seconds per run without PST). That
ordering, and calling E7/E8 heavy and E10/E11 light, is an exploratory reading made
after the results.

**Stray processes.** The harness had to kill processes left running after 18 of 40
native runs and 3 of 40 PST runs (Riemann 7 vs 0, Ising/Potts 5 vs 0).

**Why light workloads cost more.** On a light workload the native agent writes one
supervisor script (launch, watch, restart, record state) and then checks a status
file. PST agents did restarts, waits and teammate lookups as separate commands,
often where an existing feature (`pst each` with `{state}`, `pst run -e k=…`, rerun)
would have done it in one; Claude Code also blocked long `sleep` commands in 12 PST
runs against 1 native run. On Ising/Potts, PST teams lost on resetting stuck
replicas and identifying faulty nodes; exchange answers scored the same in both arms.

**What this points to** (for a future, pre-registered protocol on new tasks):
- help and tool instructions that lead with whole recipes, and a measure of feature
  adoption;
- bounded retries: a rule that restarts a stopped job with changed parameters, with
  attempt limits, backoff and a start-rate limit, and an `--after-fail` dependency;
- waking the agent through the host's notifications rather than timed waits;
- naming a teammate's job directly for reading commands;
- `exp`/`log` in rule conditions;
- a workload sweep within one task to locate the break-even point;
- a baseline in which agents run their own supervisor script under `pst run`.

## 7. Earlier protocols

Protocols 1–6 were pilots and stopped studies that shaped the design (tool set,
the move from MCP tools to the `pst` command, rules, names, `each`/`follow`,
containment). They are kept for transparency and are **not** pooled with protocol 7.
Protocol 6 was stopped when a name-resolution bug let a bare job name resolve to a
teammate's jobs in a shared directory; names were changed to mean the agent's own
jobs first before protocol 7, and protocol 7 saw no dependencies between agents'
jobs. After the study, the fallback for reading commands was further limited to
the same directory (it could previously reach a same-named job anywhere).

## 8. Data in this directory

| Path | Content |
|---|---|
| `v7main/`, `v7rerun/` | Protocol 7: `results.jsonl` (one row per run), driver log, commit, rerun plan |
| `protocol7-transcripts.tar.xz` | All agent transcripts (`logs/w*.jsonl`) and per-run `result.json` of protocol 7 |
| `v6main/`, `v5main/`, `v5pilot*/`, `v4pilot/`, `v3pilot/`, `v2pilot/`, `pilot/`, `main/` | Earlier protocols and pilots (results and logs only) |

Program outputs and agent workspaces (about 13 GB) are not included. The tool was
named differently while these runs were recorded; in this published copy the arm
label, command name and paths were normalised to `smarttasks`, `pst` and
`pst-experiments`. Byte counts in `results.jsonl` were measured on the originals.

Reproduce the summary from this data:

```bash
cd docs/research/experiments/smart_tasks          # needs numpy and scipy
python3 analyze_protocol7.py ../../pst-experiments/v7main ../../pst-experiments/v7rerun
```
