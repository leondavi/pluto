# Coordination primitives: Claude Code vs Pluto and PlutoSmartStreams

What an agent can use to keep concurrent work in order (exclusion, limits, ordering)
in native Claude Code, in the Pluto server, and in PlutoSmartStreams v0.6.0 after the
admission work (`--slot`, `--after`, `--lock`). Checked 26 Nov 2026 against the Claude
Code documentation and the code of this branch; the demonstrations at the end were run
on that day against a live server.

## Capabilities

| Primitive | Claude Code (documented) | Pluto server | PlutoSmartStreams jobs |
|---|---|---|---|
| Mutex (exclusive lock) | none for agents or processes | yes: write lock with FIFO waiters, TTL lease, fencing token | `--lock RES`: Pluto's write lock, held by `job:<id>` while the job runs |
| Shared (read) lock | none | yes | `--lock RES:read` |
| Counting semaphore | none; `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (default 20) refuses new subagents with an error instead of queueing them | no | `--slot NAME:N`: at most N running, across all agents of the server; the rest queue |
| Barrier / dependency | agent teams: a task can depend on others and stays blocked until they are completed (tasks only, not processes) | no | `--after JOBS`: start when they have ended; `--after-ok`: only if all succeeded, otherwise cancelled with a reason |
| Deadlock detection | none | yes (`pluto_deadlock`) | job locks are only tried, never queued, so queued jobs never form a cycle |
| Fencing token | none | yes | `$PLUTO_LOCK_TOKEN` in the job's environment |
| Process limit | none for background Bash processes | n/a | server and per-agent limits; excess jobs queue (followers counted apart) |
| Task claiming | agent teams: file locking when claiming a task | task assignment over messages | job transfer between agents |
| Barrier between agents | none | none | none (open: "all 4 agents reach step 3") |

Sources: https://code.claude.com/docs/en/agent-teams, https://code.claude.com/docs/en/sub-agents,
https://code.claude.com/docs/en/env-vars. "None" means not documented. The docs don't say
whether something internal exists.

Since job locks are Pluto's own, a job and an agent that takes the same resource through
`/locks/acquire` exclude each other, and `GET /locks` shows the job as the holder.

## Why it matters, from the study

Results from the protocol 5 run (stopped by request after 35 pairs, before these features):

* E10 caps each agent at 16 running solvers. SmartStreams agents hit that cap about twice as
  often as native ones (57 vs 30 mentions in the logs): the cap is what `--slot solver:16`
  enforces without the agent having to count.
* E7's followers were queued behind the 32-jobs-per-agent limit in 4 of 7 runs, so the
  reactions they were meant to run never ran. Followers are now counted apart and fed by their runner.
* Agents ordered stages with `sleep` and `pss ps` loops. That's a large part of why they took
  18.9 turns per run against 9.4 for native agents. `--after` hands that ordering to the server.

## Demonstrations (26 Nov 2026)

On a Pluto daemon started with `PlutoServer.sh --daemon` from this branch (tracked config,
HTTP 9202; `/health` reports `"capabilities":["smartstreams"]`, version 0.6.0), with `pss`:

```
$ pss run -q -n pre -e k=1-2 'echo pre {k}; sleep 1'
$ pss run -n post --after pre --after-ok --slot cpu:1 --lock smoke:res \
      'echo post token=$PLUTO_LOCK_TOKEN'
$ pss ps
post c69440 queued (after 2 jobs ok, slot cpu/1, lock smoke:res) 0.0s 0B
pre/1,2 2x running 0s 6B
$ pss wait pre,post -t 30
pre/1,2 2x succeeded 1s 6B: e.g. pre 1
post c69440 succeeded 0.5s 13B: post token=1

$ pss run -q -n bad 'exit 3'
$ pss run -q -n dep --after bad --after-ok 'echo never'
$ pss wait dep -t 20
dep b8c455 cancelled (an_earlier_job_did_not_succeed) 0.0s 0B

$ pss run -q -n src 'for i in 1 2 3 4; do echo v=$i; sleep 0.3; done'
$ pss follow src 'grep -c v= > counted'      # counted: 4
$ pss head -n 1 src                           # v=1
$ pss grep -c v= src                          # src:4
$ pss run -q -n late 'sleep 1; exit 2'; sleep 3; pss head -n1 src
v=1
pss: meanwhile ended: late failed             # news line, on stderr
```

Automated checks (`tests/test_smart_streams_admit_live.py`, a live server on spare ports):

| Check | Result |
|---|---|
| 6 jobs from 3 agents in `slot gpu:2`, each 1 s | never more than 2 at once |
| `after_jobs` then `after_ok` with a failing dependency | order kept; the dependent job cancelled with the reason |
| Job lock while an agent holds `db` through `/locks/acquire` | job stays queued; it starts on release, gets the token, releases at its end |
| Two jobs with one write lock | never overlap |
| Follower while its agent's 2 job slots are full | starts at once, reads all 2,001 lines, ends when its job ends |

CPU (40 quiet jobs, same machine): server 5.0% → 1.7% of a core; one quiet runner
0.33% → 0.10%; one busy runner 0.6% → 0.4%. For busy streams with followers, the total
saving is only 4–6%.

Test totals at the commit: 166 Erlang, 614 offline, 85 live, all passing.

## Open

* A barrier between agents, in Pluto (not tied to jobs).
* Whether agents use these features without being told: the study prompt doesn't mention them,
  so the protocol 6 run measures discovery through `pss help` too.
