# PlutoAgentFriend — Skill Guide for AI Agents

> Read this guide to understand how to use PlutoAgentFriend and the Pluto
> coordination system when working inside a wrapped terminal session.

---

## What Is PlutoAgentFriend?

PlutoAgentFriend is a transparent PTY (pseudo-terminal) wrapper that sits
between you (the AI agent) and your terminal.  It does two things:

1. **Proxies all I/O** — everything you read and write passes through
   unchanged.  The user sees exactly what you output and you receive exactly
   what the user types.
2. **Injects Pluto coordination messages** — when you are idle (not
   producing output and not asking the user a question), the wrapper may
   type messages into your stdin on behalf of other agents or the Pluto
   server.

You do **not** need to change how you work.  The messages arrive as
natural-language text in your input, prefixed with `[Pluto …]`.  Process
them like any other instruction from the user.

```
┌───────────┐       ┌──────────────────┐       ┌──────────┐
│   User    │ ←───→ │ PlutoAgentFriend  │ ←───→ │   You    │
│ terminal  │       │  (PTY proxy)     │       │ (agent)  │
└───────────┘       └────────┬─────────┘       └──────────┘
                             │
                    ┌────────┴─────────┐
                    │   Pluto Server   │
                    │  (coordination)  │
                    └──────────────────┘
```

---

## How Messages Appear

When Pluto has messages for you, they are injected into your stdin as
natural-language blocks.  They always start with a header line:

```
You have received the following Pluto coordination messages. Process them and take appropriate action.

[Pluto Message from coder-2]
{
  "type": "review_request",
  "file": "src/main.py"
}

[Pluto Task Assignment - TASK-7]
From: orchestrator
Description: Refactor the auth module
Payload: { ... }

Work on this task. When done, update it with pluto_task_update("TASK-7", "completed", {"result": ...}).
```

### Message Types You May Receive

| Header | What It Means |
|--------|---------------|
| `[Pluto Message from <agent>]` | A direct message from another agent |
| `[Pluto Broadcast from <agent>]` | A message sent to all agents |
| `[Pluto Topic '<name>' from <agent>]` | A pub/sub message for a topic you subscribed to |
| `[Pluto Task Assignment - <id>]` | A task assigned to you — work on it and report back |
| `[Pluto Event: <type>]` | Any other server event |

---

## How to Respond to Pluto Messages

You are an AI agent running inside a terminal.  You **cannot** call the
Pluto API directly (there is no SDK in your session).  Instead:

> **Important:** PlutoAgentFriend **already registered you** with the Pluto
> server when it launched.  **Do NOT register again** — that would create a
> duplicate session.  Your agent ID and session token are provided in the
> startup prompt.  Use the token in `curl` calls that require authentication
> (e.g. `/agents/send`, `/agents/broadcast`).  Incoming messages are
> delivered to you automatically by the wrapper — you do not need to poll.

### Option A — Use shell commands (recommended)

The Pluto server exposes an HTTP API on `localhost:9001`.  Use `curl` to
interact with it.  Use your session token (provided in the startup prompt)
for authenticated calls like sending messages:

```bash
# Send a message to another agent (use YOUR token from the startup prompt)
curl -s -X POST http://localhost:9001/agents/send \
  -H "Content-Type: application/json" \
  -d '{"token":"YOUR-TOKEN-HERE","to":"other-agent","payload":{"type":"done","file":"main.py"}}'

# Broadcast to all agents
curl -s -X POST http://localhost:9001/agents/broadcast \
  -H "Content-Type: application/json" \
  -d '{"token":"YOUR-TOKEN-HERE","payload":{"text":"build complete"}}'

# Acquire an exclusive lock on a resource
curl -s -X POST http://localhost:9001/locks/acquire \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"your-agent-id","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}'

# Release a lock
curl -s -X POST http://localhost:9001/locks/release \
  -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","agent_id":"your-agent-id"}'

# List connected agents
curl -s http://localhost:9001/agents

# Find agents by role
curl -s -X POST http://localhost:9001/agents/find \
  -H "Content-Type: application/json" \
  -d '{"filter":{"role":"reviewer"}}'

# Update a task
curl -s -X POST http://localhost:9001/task/update \
  -H "Content-Type: application/json" \
  -d '{"task_id":"TASK-7","status":"completed","result":{"summary":"done"}}'

# Check server health
curl -s http://localhost:9001/health
```

### Option B — Use the PlutoClient.sh CLI (if available)

```bash
# Send a message
./PlutoClient.sh send --to reviewer-1 --payload '{"type":"ready"}'

# List agents
./PlutoClient.sh list

# Acquire a lock
./PlutoClient.sh lock --resource "file:/src/main.py" --mode write --ttl 30000
```

---

## Injection Modes

PlutoAgentFriend operates in one of three modes (set by the user at launch):

| Mode | Behavior |
|------|----------|
| **auto** | Messages are injected into your stdin automatically when you are idle. This is the default. |
| **confirm** | A notification appears to the user; if they don't intervene within 10 seconds, the message is injected. |
| **manual** | The user is notified of pending messages but must manually paste or type them to you. |

You do not need to know which mode is active — just process messages as they
arrive.

---

## Safety Rules

These are enforced by PlutoAgentFriend (not by you), but understanding them
helps you know when to expect messages:

1. **User input has priority** — if the user is actively typing, injection
   is delayed until they stop for 5 seconds.
2. **Never during questions** — if you are asking the user a question
   (detected by patterns like `?`, `[y/n]`, `Confirm?`), injection is
   paused until you resume normal output.
3. **Transparency** — every injected message is shown to the user on stderr
   so they can see what you received.

---

## Resource Locking — When and How

If you are about to modify a file that other agents might also touch, **lock
it first**.  This prevents data corruption from concurrent writes.

### Lock lifecycle

```
1. Acquire   →  curl POST /lock  { resource, mode, ttl_ms }
2. Work      →  edit the file
3. Release   →  curl POST /release  { lock_ref }
```

### Lock modes

| Mode | Use When |
|------|----------|
| `write` | You need exclusive access (editing a file) |
| `read` | You only need to read (multiple readers allowed simultaneously) |

### Important rules

- **Always release locks** when done.  If you forget, the lock expires after
  the TTL (typically 30 seconds), but this blocks other agents in the
  meantime.
- **Renew long operations** — if your work will take longer than the TTL,
  renew the lock before it expires:
  ```bash
  curl -s -X POST http://localhost:9001/renew \
    -d '{"lock_ref":"LOCK-42","ttl_ms":30000}'
  ```
- **Handle `unavailable`** — if `try_acquire` returns `unavailable`, the
  resource is held by another agent.  Wait and retry, or use `acquire` to
  queue for it (you'll receive the lock via a Pluto message when it's
  granted).

### Inspecting who holds or is waiting for a resource

Before you decide to wait on a busy resource (or ping whoever is blocking
you), ask the server who currently holds it, who held it last, and how long
the waiting queue is.  Three read-only HTTP endpoints are available:

```bash
# Full picture: current holders + last holder + queue
curl -s "http://localhost:9001/locks/resource?resource=file:/src/main.py"

# Just the most recent holder (even after they released / lock expired)
curl -s "http://localhost:9001/locks/last_holder?resource=file:/src/main.py"

# Just how many agents are waiting in the FIFO queue
curl -s "http://localhost:9001/locks/queue?resource=file:/src/main.py"
```

Example response from `/locks/resource`:

```json
{
  "status": "ok",
  "resource": "file:/src/main.py",
  "now_ms": 1734123456789,
  "current_holders": [
    {"agent_id": "alice", "mode": "write", "lock_ref": "LOCK-42",
     "expires_at": 1734123486789, "fencing_token": 17}
  ],
  "last_holder": {
    "agent_id": "bob", "lock_ref": "LOCK-41",
    "released_at": 1734123450000, "reason": "released"
  },
  "queue_length": 2,
  "queue": [
    {"agent_id": "carol", "mode": "write", "requested_at": 1734123455000,
     "max_wait_until": 1734123515000, "wait_ref": "WAIT-8"},
    {"agent_id": "dave",  "mode": "write", "requested_at": 1734123456100,
     "max_wait_until": null,             "wait_ref": "WAIT-9"}
  ]
}
```

When to use these:
- **Before acquiring** — if `queue_length` is high, consider working on a
  different resource first.
- **While blocked** — ask `last_holder` for the agent that just released
  (useful for coordination: "hey bob, I saw you released main.py, I'll take
  it now").
- **Debugging stuck work** — `reason` is `"released"` after a clean
  release and `"expired"` when a TTL timed out (agent probably died).
- `last_holder` is `null` if the server has never seen this resource.

---

## Message Delivery (peek / ack)

PlutoAgentFriend uses **at-least-once delivery**.  Messages stay in the
server inbox until you ack them, so if the wrapper crashes mid-inject
the next attempt will see them again.

The wrapper handles this automatically:

1. `/agents/peek` — non-destructive read (still uses your HTTP token).
2. Inject into the editor and wait for the echo to confirm the paste
   actually landed.
3. `/agents/ack` — drain the server inbox up to the last confirmed
   `seq_token`.

Each peeked message carries a `seq_token` (monotonically increasing
integer).  Ack is idempotent: re-acking a `seq_token` you already
drained returns `{"drained": 0}`.

You can use these endpoints directly, for example to recover a lost
message or to inspect the inbox without disturbing it:

```bash
# Peek (does not drain)
curl -s "http://localhost:9001/agents/peek?token=<TOKEN>&since_token=0"

# Ack everything up to seq 42
curl -s -X POST http://localhost:9001/agents/ack \
  -H 'Content-Type: application/json' \
  -d '{"token":"<TOKEN>","up_to_seq":42}'
```

> ⚠ `/agents/poll` is still available (destructive long-poll) for
> custom clients, but if you are running under PlutoAgentFriend the
> wrapper owns polling — don't call `/poll` yourself or you'll race the
> wrapper.  Use `/peek` for observation and let the wrapper ack.

---

## Task Workflow

1. **Read the description and payload** — they tell you what to do.
2. **Update status to `in_progress`**:
   ```bash
   curl -s -X POST http://localhost:9001/task/update \
     -d '{"task_id":"TASK-7","status":"in_progress"}'
   ```
3. **Do the work** — follow the task instructions.
4. **Update status to `completed`** (or `failed`) with a result:
   ```bash
   curl -s -X POST http://localhost:9001/task/update \
     -d '{"task_id":"TASK-7","status":"completed","result":{"summary":"Refactored auth module","files_changed":["src/auth.py"]}}'
   ```

---

## Multi-Agent Coordination Patterns

### Pattern 1 — Lock-Edit-Unlock

```
Agent A                    Pluto                    Agent B
  │                          │                         │
  ├─ acquire write lock ────→│                         │
  │←── lock granted ─────────│                         │
  │                          │                         │
  │  ... edit file ...       │                         │
  │                          │     acquire write lock ─┤
  │                          │←────────────────────────┤
  │                          │     (queued — A holds)  │
  ├─ release lock ──────────→│                         │
  │                          ├── lock granted ────────→│
  │                          │     ... edit file ...   │
```

### Pattern 2 — Request-Review

```
Agent A (coder)             Pluto              Agent B (reviewer)
  │                          │                         │
  │  ... finish coding ...   │                         │
  ├─ send review_request ───→│                         │
  │                          ├── inject message ──────→│
  │                          │     ... reviews code ...│
  │                          │←── send feedback ───────┤
  │←── inject message ───────│                         │
  │  ... apply feedback ...  │                         │
```

### Pattern 3 — Broadcast Announce

```
Agent A                    Pluto              All Agents
  │                          │                    │
  ├─ broadcast "done" ──────→│                    │
  │                          ├── inject to B ────→│
  │                          ├── inject to C ────→│
  │                          ├── inject to D ────→│
```

---

## Launching PlutoAgentFriend

This section is for the **user** (or for you to suggest to the user).

```bash
# Auto-detect which agent framework is installed
./PlutoAgentFriend.sh --agent-id my-agent

# Specify the framework
./PlutoAgentFriend.sh --agent-id coder-1 --framework copilot
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
./PlutoAgentFriend.sh --agent-id coder-1 --framework aider

# Custom command
./PlutoAgentFriend.sh --agent-id worker-1 -- python3 my_agent.py

# Confirm mode (user gets 10s to override)
./PlutoAgentFriend.sh --agent-id coder-1 --mode confirm

# Manual mode (notifications only)
./PlutoAgentFriend.sh --agent-id watcher-1 --mode manual
```

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--agent-id <name>` | *required* | Your identity in the Pluto network |
| `--framework <name>` | auto-detect | `claude`, `copilot`, `aider`, or `cursor` |
| `--mode <mode>` | `auto` | `auto`, `confirm`, or `manual` |
| `--host <ip>` | from config | Pluto server host |
| `--http-port <port>` | from config | Pluto HTTP port |
| `--ready-pattern <regex>` | per framework | Regex matching the agent's idle prompt |
| `--silence-timeout <sec>` | `3.0` | Seconds of silence before agent is considered idle |
| `--verbose` | off | Enable debug logging |

---

## Pluto Server Quick Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Server health check |
| `/ping` | GET | Simple ping |
| `/agents` | GET | List all connected agents |
| `/agents/register` | POST | Register (HTTP session mode) |
| `/agents/unregister` | POST | Unregister |
| `/agents/poll` | GET | Long-poll for messages (destructive; `?token=...&timeout=30`) |
| `/agents/peek` | GET | Non-destructive inbox read (`?token=...[&since_token=N]`) |
| `/agents/ack` | POST | Ack messages `{token, up_to_seq}` (idempotent) |
| `/agents/find` | POST | Find agents by attributes |
| `/message` | POST | Send a direct message |
| `/broadcast` | POST | Broadcast to all agents |
| `/agents/subscribe` | POST | Subscribe to a topic |
| `/agents/publish` | POST | Publish to a topic |
| `/lock` | POST | Acquire a lock |
| `/release` | POST | Release a lock |
| `/renew` | POST | Renew a lock TTL |
| `/task/assign` | POST | Assign a task |
| `/task/update` | POST | Update task status |
| `/task/list` | GET | List tasks |

---

## Example: Full Agent Session

Here is what a typical session looks like from your perspective:

```
# 1. You start up normally — the user launched you with PlutoAgentFriend.
#    You see your normal prompt and can work as usual.

# 2. While you're idle, you suddenly receive in stdin:

You have received the following Pluto coordination messages. Process them and take appropriate action.

[Pluto Task Assignment - TASK-3]
From: orchestrator
Description: Add input validation to the login endpoint in src/auth.py
Payload: {
  "priority": "high",
  "files": ["src/auth.py"],
  "requirements": "Validate email format and password length"
}

Work on this task. When done, update it with pluto_task_update("TASK-3", "completed", {"result": ...}).

# 3. You process this like a normal user request:
#    a. Lock the file
#    b. Edit it
#    c. Release the lock
#    d. Update the task status
#    e. Optionally notify other agents

# Example of what you might do:
curl -s -X POST http://localhost:9001/task/update \
  -d '{"task_id":"TASK-3","status":"in_progress"}'

curl -s -X POST http://localhost:9001/lock \
  -d '{"agent_id":"coder-1","resource":"file:/src/auth.py","mode":"write","ttl_ms":60000}'

# ... edit the file ...

curl -s -X POST http://localhost:9001/release \
  -d '{"lock_ref":"LOCK-15"}'

curl -s -X POST http://localhost:9001/task/update \
  -d '{"task_id":"TASK-3","status":"completed","result":{"summary":"Added email and password validation","files_changed":["src/auth.py"]}}'

curl -s -X POST http://localhost:9001/message \
  -d '{"from":"coder-1","to":"orchestrator","payload":{"type":"task_done","task_id":"TASK-3"}}'
```

---

## Summary

- You are running inside PlutoAgentFriend — a transparent PTY wrapper.
- Messages from other agents arrive as natural-language text in your stdin.
- Use `curl` against `localhost:9001` to send messages, acquire locks, and update tasks.
- Always lock resources before editing shared files.
- Always update task status when assigned work.
- The user can see everything — all injected messages are logged to stderr.
