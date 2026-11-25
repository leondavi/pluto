# PlutoSmartStreams.sh — Durable Jobs Guide

PlutoSmartStreams treats every agent-launched process as a portable job
package with a durable output stream, lifecycle state and chunk manifest.
Packages can be opened, reviewed, transferred to another agent or host, and
run concurrently without the LLM managing the underlying process.

## Quick start

```bash
./PlutoServer.sh --daemon
./PlutoSmartStreams.sh --agent-id coder-1                        # Claude Code
./PlutoSmartStreams.sh --agent-id coder-2 --framework copilot
./PlutoSmartStreams.sh --agent-id coder-3 --with-pluto           # plus pluto_* tools
./PlutoSmartStreams.sh --manage                                  # you, not the agent
```

## Tools

| Tool | Purpose |
|---|---|
| `job_start` | Launch a command; `parent` nests it, `rerun_of` repeats it |
| `job_status`, `job_list` | State, active/idle, elapsed, owner, unread bytes; earlier sessions included |
| `job_wait` | Block up to 55 s for exit, output, idle or a regex, on one or many jobs |
| `job_next_chunk` | Next unread output; the cursor is durable and per agent |
| `job_window` | Bounded read at an offset, line, tail or cursor |
| `job_grep` | Regex on the server, with context, lines and offsets |
| `job_control` | `cancel`/`halt`, `pause`, `resume`, `signal` (INT TERM HUP USR1 USR2 QUIT KILL STOP CONT); `tree` includes children |
| `job_input` | Write to stdin |
| `job_stream` | Streams within the stream: `split`, `push`, `pause`, `resume`, `halt`, `followup` |
| `job_manifest` | 64 KiB chunks with chained SHA-256; `verify` re-hashes |
| `job_transfer` | Move a job to another agent |
| `job_review` | Verdict and note pinned to a hashed byte range |
| `job_open`, `job_export` | Open or import a package; write a `.pjob` |

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
./PlutoSmartStreams.sh --export J-20260101-0123456789abcdef | \
    ssh host pluto/PlutoSmartStreams.sh --import -
```

A live process cannot move between machines. A running job is exported as a
read-only snapshot; importing a later export extends it. Importing verifies
every hash and never runs anything.

## Security

- `/jobs/*` runs commands, so it needs the key in `~/.pluto/smartstreams/key`
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

Optional `smartstreams` section in `config/pluto_config.json`: `home`,
`python`, `max_running` (16), `max_per_agent` (8), `idle_after_ms` (30000),
`min_free_bytes` (256 MiB; below it jobs are stopped), `allow_remote` (false).
`PLUTO_SMARTSTREAMS_HOME` overrides the storage directory and must match for
server and clients; a directory is used by one server at a time. The server
needs `python3` (3.9+). A job may print 1 GiB unless `max_output_bytes` says
otherwise.

## HTTP

`POST /jobs/<op>` with `Authorization: Bearer <key>` and a JSON body that
includes `agent_id`: `start status list open window next grep wait control
input transfer stream review manifest export import notices history stats
prune`. Errors are `{"status":"error","reason":"..."}`. Send a `request_id`
with changing calls to make retries safe. `wait` takes `until` = `exit`,
`output`, `idle` (`idle_ms`) or `pattern`, for `job_id` or `job_ids`, with
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
PLUTO_LIVE_TESTS=1 python -m pytest tests/test_smart_streams_live.py
```

The live tests start their own server on free ports.
