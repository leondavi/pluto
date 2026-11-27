# PlutoSmartTasks.sh — Durable Jobs Guide

PlutoSmartTasks treats every agent-launched process as a portable job
package with a durable output stream, lifecycle state and chunk manifest.
Packages can be opened, reviewed, transferred to another agent or host, and
run concurrently without the LLM managing the underlying process.

## Quick start

```bash
./PlutoServer.sh --daemon
./PlutoSmartTasks.sh --agent-id coder-1                        # Claude Code
./PlutoSmartTasks.sh --agent-id coder-2 --framework copilot
./PlutoSmartTasks.sh --agent-id coder-3 --with-pluto           # plus pluto_* tools
./PlutoSmartTasks.sh --manage                                  # you, not the agent
```

## The `pst` command

`PlutoSmartTasks.sh` puts `pst` on the agent's `PATH`; with it an agent needs
no MCP tools, so by default the adapter shows none and only wakes the agent
(`PST_TOOLS=core` or `all` shows them). One declaration replaces the launcher,
watcher and collector scripts an agent would otherwise write:

```
pst run -n soak -e k=1-12 'bin/soak --job {k}' \
    --on 'CKPT request token={t}' --append 'control/{k}.ack' '{t}' \
    --idle 60 --stop
pst wait soak -t 540                             # until all ended, or a rule tells
pst each soak 'python3 parse.py {log} {exit}' -o 'out/{k}.json'
pst give other-agent soak; pst brief soak        # handover
```

- **Names.** `soak`, `soak/3`, `soak/1-6,9`: the key is the parameter the job was
  started with (`soak/1:a` with two). A name means **your own** jobs of that name,
  and of each key the newest job: running a key again replaces it, and a key
  started later joins the name (`pst run -n zr -e k=8 ...` adds `zr/8`); a round
  with other parameters (`-e seed=...` after `-e n=...`) replaces the name. Agents
  sharing a directory often pick the same name: if you have none of that name, a
  command that only reads uses the others' jobs of this directory and says whose
  they are on stderr, while `follow` and `--after` refuse (name others' jobs by id).
  Lists with commas (`r1,r2,soak/3`, `l2/7,8`), ids and group handles (`g3f09ac`,
  `g3f09ac/3`) always work.
- **Rules** are a condition (`--on`, `--where`, `--idle`, `--absent`) and what to do
  (`--append FILE TEXT`, `--input TEXT`, `--stop`, `--pause`, `--tell`); several
  actions after one condition are several rules on it. They come
  with the start request and are stored before the job can run, so the shortest job
  cannot outrun them; they stay with the job when the session ends or it is given
  away, and `rerun_of` repeats them. `pst watch JOBS RULES` adds them later.
- **Placeholders** are what the line captured, then the job's parameters, then
  `_job _name _line`. An unknown one is refused when the rule is added.
- **`each`** runs your own program once per job when it ends. `{log} {key} {id}
  {state} {exit}` and the parameters are filled in, shell-quoted (and in the
  environment as `PST_LOG` ...); `-o` keeps its output, completely or not at all,
  and checks that a `.json` file is JSON.
- **`follow`** runs your own program on live output, one per job, as jobs of their own:
  `pst follow soak 'python3 -u judge.py {k}'` reads everything from the beginning on
  stdin, which its runner feeds from the followed job's log and closes when that job
  has ended (no process in between). Following again replaces it, again from the
  beginning, and the followed jobs never notice. Followers are counted apart from other
  jobs, under limits of their own of the same size, so jobs never crowd out the
  programs that watch them. Prefer it to
  `prog | filter`, where a bug in the filter cannot be fixed without starting the
  program again.
- **`report`** makes a record per job without a program:
  `-x 'E{e:int} probe={p:int}' -x 'E{e:int} CAL limit={limit:int}'
  -f 'bad=first:e where p > last.limit' -f 'code=_exit' -o 'out/{k}.json'`.
- `ps` and `wait` put jobs in the same state on one line; `-v` lists them. `wait -t S`
  prints where every job is when the time is up, so it replaces `sleep; ps`.
- `head`, `tail`, `grep` take the usual options (`-n`, `-A/-B/-C`, `-c`, `-v`, `-i`).
- **News.** After any command, one line on stderr says which of your jobs ended or
  were handed to you since the last one (`pst: meanwhile ended: z/3 failed, ...`).
  Each is said once; nothing is said inside a job.

### When a queued job starts

`pst run` takes three conditions besides the server's limits, also from `job_start`:

| Option | Is | Meaning |
|---|---|---|
| `--slot NAME:N` | a counting semaphore | at most N running jobs of this slot, all agents together |
| `--after JOBS` | a barrier | start once these have ended; with `--after-ok` only if all succeeded, else it is cancelled |
| `--lock RES[:read]` | Pluto's lock | hold the write (or shared read) lock on RES while running |

The lock is Pluto's own, held by `job:<id>`: agents taking the same resource through
`/locks/acquire` and jobs exclude each other, and `GET /locks` shows who holds it. It is
only tried, never queued, so a waiting job is not in the deadlock graph; it is renewed
while the job runs (`$PLUTO_LOCK_TOKEN` is the fencing token) and released when the job
ends. If it is lost, for instance across a server restart while someone else took it,
the job shows `lost-lock=RES`. Order is kept per lock resource and per slot: once an
older job that wants one is held back by it, younger ones wait behind it, so readers
cannot starve a writer; a slot's limit is the strictest `slot_max` of its jobs. A job
cancelled by `--after-ok` is reported in the news line. `ps` says what a queued job waits for:
`queued (after 2 jobs ok, slot gpu/2, lock db)`.

Example: build once, then test on at most 4 workers, each holding the shared database,
and report only if every test passed.

```
pst run -n build 'make -j8'
pst run -n t -e k=1-12 --after build --after-ok --slot workers:4 --lock db:read \
    'pytest tests/shard_{k}.py'
pst run -n report --after t --after-ok 'python report.py'
pst run -n an -e k=1-12 --after 't/{k}' 'python analyse.py {k}'   # each after its own
pst ps        # t waits for build, then runs 4 at a time
pst wait report -t 600
```

In `job_start` the same fields are `slot` and `slot_max`, `after_jobs` (a list of job ids
or names), `after_ok`, and `lock` and `lock_mode` (`write`, the default, or `read`).
Wrong combinations are refused when the job is started: `slot_max_without_slot`,
`lock_mode_without_lock`, `after_jobs_names_an_unknown_job`, more than 200 `after_jobs`.
Claude Code itself has none of these for processes; see
[the comparison](../research/claude-code-vs-pluto-coordination.md).

Without `PST_AGENT` it acts as you (`user:<name>`), who may change every job; a name
then changes jobs of the current directory only.

### Delivery of actions

Actions of one job happen in order. They wait in `watch.json`, fully expanded, until
carried out, so `kill -9` of the server loses none; one is repeated if the server dies
between doing it and recording that. Success is silent, failure is a notice. Output is
untrusted: a captured value in a file name must be `[A-Za-z0-9._-]`, 100 characters at
most, not starting with a dot or dash; the file must be a regular one below the job's
directory, reached without links; control characters become spaces; what is too long
is refused, not cut. There is no rule that runs a command built from output.

## Tools

| Tool | Purpose |
|---|---|
| `job_start` | Launch a command; `parent` nests it, `rerun_of` repeats it |
| `job_status`, `job_list` | State, active/idle, elapsed, owner, unread bytes; earlier sessions included |
| `job_wait` | Block for exit, output, idle, a regex or a watch hit, on one or many jobs |
| `job_next_chunk` | Next unread output; the cursor is durable and per agent |
| `job_window` | Bounded read at an offset, line, tail or cursor |
| `job_analyze` | Outline any output; extract, filter, aggregate, compare across jobs |
| `job_watch` | Standing rules on live output: notify, cancel or pause |
| `job_grep` | Regex on the server, with context, lines and offsets |
| `job_control` | `cancel`/`halt`, `pause`, `resume`, `signal` (INT TERM HUP USR1 USR2 QUIT KILL STOP CONT); `tree` includes children |
| `job_input` | Write to stdin |
| `job_stream` | Streams within the stream: `split`, `push`, `pause`, `resume`, `halt`, `followup` |
| `job_manifest` | 64 KiB chunks with chained SHA-256; `verify` re-hashes |
| `job_transfer` | Move a job to another agent |
| `job_review` | Verdict and note pinned to a hashed byte range |
| `job_open`, `job_export` | Open or import a package; write a `.pjob` |

These are shown with `PST_TOOLS=core` (ten, `job_more` reaches the rest) or `all`.
`job_start(commands=[...])` starts many jobs in one call; `job_status`,
`job_grep`, `job_wait`, `job_control` and `job_transfer` take `job_ids`.
`job_wait` reports each event once per session, so an agent can follow a
dozen jobs by repeating the same call.

## Understanding output

`job_analyze` with only job ids (or a group) outlines the output: lines grouped
into templates with counts, line numbers, an example and number ranges, then
likely errors, rare lines and, for several jobs, which jobs deviate. Nothing
needs to be known about the format. Anything else is one query:

| Question | Arguments |
|---|---|
| What fields are there? | `extract="auto"` |
| Error rate and latency per route | `by="path"`, `agg=["count","p95:dur"]`, `where="status >= 500"` |
| Slowest requests | `rows=["path","dur"]`, `sort="-dur"`, `limit=10` |
| Best epoch of each run | `extract="epoch {e:int} val={v:float}"`, `agg=["min:v","argmin:v"]` |
| Where did the loss jump? | `where="loss > prev*1.5"`, `rows=true` |
| Lost or repeated records | `by="worker"`, `where="seq != prev+1"`, `rows=true` |
| Gaps of a minute | `where="_time > prev+60"`, `rows=true` |
| Which replica disagrees? | `by="step"`, `compare="checksum"` |

- `extract`: `auto` (the default) reads `key=value`, `key: 1.5`, JSON (nested
  keys dotted, with or without a prefix), CSV/TSV with a header and access
  logs; or `kv`, `json`, `csv`, `tsv`, `csv:a,b,c` (no header); or a template,
  whose untyped fields are one word (`:int :float :word :hex :text`; `{rest}`
  is text; `{{` is a brace); or `re:` and a regex with named groups.
- `where`: `== != < <= > >=`, `contains`, `startswith`, `endswith`, `~` and
  `!~` (regex), `in (a, b)`, `has f`, `missing f`, `and or not ( )`, `+ - * /`.
  Right of `==` a bare word is text, elsewhere a field; `$f` is always a field
  and `` `odd name` `` quotes one. Text with spaces needs quotes unless it ends
  the condition. Units compare (`dur > 500ms`); sums and means are then in
  seconds or bytes. NaN equals NaN and is left out of numbers.
- `last.f` is the latest earlier value of `f` in the job, on whatever kind of line:
  `probe > last.limit`. `abs(v - prev.v) > 3` works. Several templates (a list, or
  `-x` repeated) read several kinds of lines as one sequence of records.
- `prev` is the previous record of the same job and `by` group that satisfied
  the conditions without `prev`; `prev.f` is another field of it.
- Fields of every line: `_job _name _n _offset _line _level _time`.
- Several jobs without `by` give a row per job and a total. `limit` applies
  per job for rows and to groups as a whole; totals are always stated.
  Percentiles interpolate linearly and are exact up to 4 million values (`~`
  marks a sample). Records compare with `compare` n-th to n-th within a key.
- A call scans for 30 s at most, about 20 to 80 MB/s. `INCOMPLETE` results
  cover the part they name; pass their `resume` to look at the rest.

Over HTTP: `POST /jobs/analyze` with `job_ids`, lists for `where by agg
compare`, and `from_offsets` (`{job: offset}`) for `resume`.

## Watching output

```
job_watch(job_id=g, match="Traceback")
job_watch(job_id=g, where="loss > prev*3", name="spike", then="pause", once=true)
job_watch(job_id=g, stalled="step", stalled_n=20)
job_watch(job_id=g, idle_s=120)                    # no output
job_watch(job_id=g, match="heartbeat", absent_s=60)
job_watch(job_id=g, match="retrying", count=5, window_s=30)
job_watch(action="list", job_id=g); job_watch(action="remove", job_id=g, rule="spike")
```

Rules belong to live jobs, at most 16 each; only the owner adds or removes
them, and they act with the authority of whoever owns the job at that moment.
Hits arrive as durable notices with line and text, wake the agent, and end
`job_wait(until="watch")`, which also ends when the job does. A rule reads the
output from its start (`from_now` skips it) to its end, including an
unfinished last line. Matches within `cooldown_s` (30) are counted and told
together when it is over; after `max_hits` (20) the rule is done. Times are
the job's own: pauses do not count. A transfer hands rules and unseen hits to
the new owner. Exports carry the rules as inert data.

Delivery: rules, positions, states and hits are saved with fsync and rename
before anything is announced, so after a `kill -9` of the server every hit
still becomes exactly one notice. At least once remains: a notice is shown
again if the result that carried it got lost, and a `pause` or `cancel` is
requested again if the server died between requesting and recording it. Lines
written while the server was down are taken to have been written evenly, which
`count` within `window_s` can notice.

One helper process evaluates all rules, only while there are any. A rule that
burns a second of CPU on one block of output is disabled, and says so.

A job ends when its whole process group has ended, so background children
(`server &`) keep it running, under limits and cancellable. The exit code is
the main process's; until the group ends it shows as `leader_exit_code`.

Pausing a *job* stops the process. Pausing a *nested stream* only stops
delivery and pushes; the job keeps running.

## States

`queued → starting → running ⇄ paused → succeeded | failed | cancelled |
signaled | timed_out | output_limit`. Also `orphaned` (its runner died, the
command lives), `lost` (both gone, exit code unknown) and `snapshot` (an
import of a job that was still running).

## Moving jobs

To another agent: `job_transfer`, or `m` in `--manage`. To another host:

```bash
./PlutoSmartTasks.sh --export J-20260101-0123456789abcdef | \
    ssh host pluto/PlutoSmartTasks.sh --import -
```

A live process cannot move between machines. A running job is exported as a
read-only snapshot; importing a later export extends it. Importing verifies
every hash and never runs anything.

## Security

- `/jobs/*` runs commands, so it needs the key in `~/.pluto/smarttasks/key`
  (mode 0600), accepts loopback only, and sends no CORS headers.
- The key is the boundary. Ownership coordinates agents; it does not isolate
  them, since all agents of one user hold the same key.
- Jobs run as the server's OS user, outside the agent CLI's Bash permission
  rules. The launcher never pre-approves `job_start`.
- Commands, arguments, directories and output are stored unencrypted. `env`
  values are not stored; PATH-like variables (PATH, HOME, VIRTUAL_ENV, ...) are
  kept in the package on this host and stripped from exports.
- Imported archives are untrusted: every field is checked and rewritten, every
  hash verified, and only jobs started on this host can ever run on it.
- Manifest hashes detect corruption. They do not prove who wrote a package.

## Configuration

Optional `smarttasks` section in `config/pluto_config.json`: `home`,
`python`, `max_running` (128), `max_per_agent` (32; followers are counted apart, with the same limits), `idle_after_ms` (30000),
`min_free_bytes` (256 MiB; below it jobs are stopped), `allow_remote` (false).
`PLUTO_SMARTTASKS_HOME` overrides the storage directory and must match for
server and clients; a directory is used by one server at a time. The server
needs `python3` (3.9+). A job may print 1 GiB unless `max_output_bytes` says
otherwise.

## HTTP

`POST /jobs/<op>` with `Authorization: Bearer <key>` and a JSON body that
includes `agent_id`: `start status list open window next grep analyze watch wait control
input transfer stream review manifest export import notices history stats
prune`. Errors are `{"status":"error","reason":"..."}`. Send a `request_id`
with changing calls to make retries safe. `start` takes `watch` (rules), `params` and, for
many jobs, `batch`: a list of `{name, command, params}`. `wait` takes `until` = `exit`,
`output`, `idle` (`idle_ms`), `pattern`, `watch` or `follow` (hits meant to be told, or
exit), for `job_id` or `job_ids`, with
`all` and `timeout_ms` (max 55000).

## Limits

A command that truncates its own log (on Linux also `> /dev/stderr`; use
`>> /dev/stderr` or `>&2`) loses that output; the job then reports
`output_truncated`. Processes that call `setsid` escape group signals. Grep matches bytes, so
`ignore_case` is ASCII only. Queued jobs that were given `env` values fail if
the server restarts before they start. If a runner is killed, its job is
`orphaned`: output limits and timeouts stop applying until it ends or is
cancelled, and line numbers far past the last manifest chunk are null.

## Tests

```bash
(cd src_erl && rebar3 compile && rebar3 eunit)
python -m pytest tests/
PLUTO_LIVE_TESTS=1 python -m pytest tests/test_smart_tasks_live.py \
    tests/test_smart_tasks_output_live.py tests/test_smart_tasks_cli.py \
    tests/test_smart_tasks_actions_live.py
```

The live tests start their own server on free ports.

## Agent identity

An agent restarted under the same id on the same machine, also after `kill -9`,
owns its predecessor's jobs. The launcher may still refuse an id whose crashed
session has not expired; pass `--allow-rename`, and the adapter takes the id back.
