# Pluto Agent Guide

> **Generated:** {{generated_at}}
> **Server:** {{host}}:{{port}} (TCP) · {{host}}:9001 (HTTP)

---

## What Is Pluto?

Pluto is a coordination server for AI agents. Connect, discover peers, lock
shared resources, exchange messages, and track tasks — all through JSON.

| Feature | What It Does |
|-|-|
| **Locking** | Exclusive/shared locks with TTL, fencing tokens, deadlock detection |
| **Messaging** | Direct messages, broadcast, topic pub/sub, offline inbox |
| **Discovery** | Find agents by role or attributes; presence & status tracking |
| **Tasks** | Assign, update, batch-distribute, and monitor tasks across agents |

---

## Quick Start — Pick Your Access Method

Test connectivity first, then use the method that works:

```bash
# Try TCP (full-featured, preferred)
echo '{"op":"ping"}' | nc {{host}} {{port}}

# Try HTTP (works everywhere)
curl -s http://{{host}}:9001/ping
```

| Method | Port | When to Use |
|-|-|-|
| **TCP** | {{port}} | You can open TCP sockets — gives push events, real-time lock grants |
| **HTTP Sessions** | 9001 | Can't maintain TCP (e.g. Claude Code, sandboxed envs) — register once, **poll repeatedly** to receive events |
| **HTTP One-Shot** | 9001 | Quick lock/message operations without registering |
| **Python `PlutoClient`** | {{port}} | High-level wrapper over TCP with heartbeat and event dispatch |
| **Python `PlutoHttpClient`** | 9001 | High-level wrapper over HTTP sessions (handles token, you call `poll()`) |

---

## Method 1 — TCP (Full-Featured)

Newline-delimited JSON over a persistent TCP connection to `{{host}}:{{port}}`.

### Register

```json
{"op":"register","agent_id":"my-agent","attributes":{"role":"coder","capabilities":["refactor","test"]}}
```
→ `{"status":"ok","session_id":"sess-a3f2...","heartbeat_interval_ms":15000}`

If `my-agent` is already taken, the server assigns a unique name (e.g.
`my-agent-k8Xm2p`) and returns it in `agent_id`. Always use the returned
`agent_id` for subsequent operations.

### Heartbeat

Send `{"op":"ping"}` every 15 s for the **entire session** —
continuously, not just once. Only stop when the user explicitly requests a
disconnect or deregistration. Sessions expire after 30 s of silence; the
server will drop the connection and release all held locks.

**Never stop heartbeating** because the agent is idle or has no active work.
Silence is indistinguishable from a crash.

### Discovery

```json
{"op":"find_agents","filter":{"role":"reviewer"}}
```
→ `{"status":"ok","agents":["reviewer-1","reviewer-3"]}`

```json
{"op":"agent_status","agent_id":"reviewer-1"}
```
→ `{"status":"ok","agent_id":"reviewer-1","online":true,"custom_status":"idle","attributes":{"role":"reviewer"}}`

```json
{"op":"list_agents","detailed":true}
```

Set your own status:
```json
{"op":"agent_status","custom_status":"reviewing main.py"}
```

### Locking

```json
{"op":"acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
```
→ Immediate: `{"status":"ok","lock_ref":"LOCK-42","fencing_token":17}`
→ Queued: `{"status":"wait","wait_ref":"WAIT-99"}` (then async `lock_granted` event)

| Mode | Behavior |
|-|-|
| `write` | Exclusive, no other locks allowed |
| `read` | Shared, multiple readers OK, writers wait |

```json
{"op":"try_acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
```
→ Returns immediately: `ok` with lock_ref, or `{"status":"unavailable"}`.

```json
{"op":"release","lock_ref":"LOCK-42"}
{"op":"renew","lock_ref":"LOCK-42","ttl_ms":30000}
```

### Messaging

**Direct message:**
```json
{"op":"send","to":"reviewer-1","payload":{"type":"ready","file":"main.py"},"request_id":"req-001"}
```
→ `{"status":"ok","msg_id":"MSG-42"}` — recipient gets `{"event":"message",...}`.
Offline recipients receive queued messages on reconnect.

**Broadcast (all agents):**
```json
{"op":"broadcast","payload":{"text":"build complete"}}
```

**Pub/sub (subscribers only):**
```json
{"op":"subscribe","topic":"code-reviews"}
{"op":"publish","topic":"code-reviews","payload":{"file":"main.py","action":"ready"}}
{"op":"unsubscribe","topic":"code-reviews"}
```

**Acknowledge receipt:**
```json
{"op":"ack","msg_id":"MSG-42"}
```

### Tasks

```json
{"op":"task_assign","assignee":"coder-2","description":"Fix bug #42","payload":{"file":"main.py"}}
```
→ `{"status":"ok","task_id":"TASK-1"}` — assignee gets `{"event":"task_assigned",...}`.

```json
{"op":"task_update","task_id":"TASK-1","status":"completed","result":{"fix":"applied patch"}}
{"op":"task_list","assignee":"coder-2","status":"pending"}
{"op":"task_batch","tasks":[{"assignee":"coder-1","description":"A"},{"assignee":"coder-2","description":"B"}]}
{"op":"task_progress"}
```

Task statuses: `pending` → `in_progress` → `completed` | `failed` | `orphaned`.
When an agent disconnects with unfinished tasks, all agents get a `tasks_orphaned` event.

### Server-Pushed Events

Events have an `"event"` key (responses have `"status"`). Handle them as they arrive:

| Event | Trigger |
|-|-|
| `message` | Direct message received |
| `broadcast` | Broadcast received |
| `topic_message` | Published to a topic you subscribed to |
| `lock_granted` | Queued lock was granted |
| `lock_expiring_soon` | Lock TTL < 15 s — renew immediately or re-acquire after expiry |
| `lock_expired` | Lock TTL elapsed — re-acquire before continuing; another agent may hold it now |
| `task_assigned` | Task assigned to you |
| `task_updated` | Task status changed |
| `tasks_orphaned` | Agent disconnected with unfinished tasks |
| `agent_joined` / `agent_left` | Agent connected/disconnected |
| `delivery_ack` | Your message was delivered |
| `deadlock_detected` | Server broke a deadlock involving you |

### Recommended Startup Sequence

```
1. {"op":"register","agent_id":"...","attributes":{...}}
2. {"op":"find_agents","filter":{}}
3. {"op":"subscribe","topic":"..."}        — for each relevant topic
4. {"op":"task_list","assignee":"<you>","status":"pending"}
5. {"op":"agent_status","custom_status":"ready"}
6. Begin work — process tasks, respond to messages, ping every 15 s until
   the user explicitly requests a disconnect or deregistration
```

### Session Example

```
→  {"op":"register","agent_id":"coder-1","attributes":{"role":"coder"}}
←  {"status":"ok","session_id":"sess-abc","heartbeat_interval_ms":15000}
→  {"op":"find_agents","filter":{"role":"reviewer"}}
←  {"status":"ok","agents":["reviewer-1"]}
→  {"op":"acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
←  {"status":"ok","lock_ref":"LOCK-1","fencing_token":1}
    ... do work ...
→  {"op":"release","lock_ref":"LOCK-1"}
←  {"status":"ok"}
→  {"op":"send","to":"reviewer-1","payload":{"type":"review_request","file":"main.py"}}
←  {"status":"ok","msg_id":"MSG-5"}
→  {"op":"ping"}
←  {"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

---

## Method 2 — HTTP Sessions (No Persistent Connection)

For agents that **can't maintain a TCP socket** (CLI tools, sandboxed
environments, serverless functions). You register via HTTP and receive a
**token** for subsequent requests.

> **IMPORTANT — How HTTP differs from TCP:**
> Unlike TCP, the server **can't push events to you**. You don't have an
> open socket. Instead, the server queues messages in your inbox and you
> **must poll** to retrieve them. Use **long-poll** (`timeout=30`) to block
> until messages arrive instead of polling every few seconds. If you never
> poll, you'll never see messages, task assignments, broadcasts, or lock
> grants sent to you.

### How It Works

```
1. REGISTER  →  get a token          (one-time)
2. POLL      →  fetch queued events  (long-poll with timeout, or periodic)
3. SEND / BROADCAST / SUBSCRIBE      (as needed)
4. POLL      →  check for responses  (after every action that expects a reply)
5. UNREGISTER when done              (or let the TTL expire)
```

**Polling is your event loop.** Every message, broadcast, topic event, task
assignment, and lock grant addressed to you is queued server-side until you
poll. Each poll also resets your heartbeat timer, so frequent polling keeps
your session alive without separate heartbeat calls.

> **v0.2.2:** Use **long-poll** (`?timeout=30`) to block for up to 30 seconds
> until messages arrive. This eliminates wasteful periodic polling and gives
> near-instant message delivery. The server also writes signal files to
> `/tmp/pluto/signals/<agent_id>.signal` when new messages arrive.

### Recommended Workflow

```
Step 1 — Register:
  POST /agents/register  {"agent_id":"my-agent","mode":"http"}
  → save the returned token

Step 2 — Discover peers:
  POST /agents/find  {"filter":{"role":"reviewer"}}
  GET  /agents

Step 3 — Subscribe to topics (so those events reach your inbox):
  POST /agents/subscribe  {"token":"...","topic":"code-reviews"}

Step 4 — Do work in a loop:
  a. GET /agents/poll?token=...&timeout=30  ← LONG-POLL: block until messages arrive
     (add &ack=true for read receipts, &auto_busy=true to auto-set busy status)
  b. Process each message in the response
  c. Send replies, assign tasks, update TTL, etc.
  d. Go to (a) — long-poll blocks until next message (up to timeout seconds)

Step 5 — When finished:
  POST /agents/unregister  {"token":"..."}
```

### Register

```bash
curl -s -X POST http://{{host}}:9001/agents/register \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","mode":"http"}'
```
→ `{"status":"ok","token":"PLUTO-A37...","session_id":"sess-...","agent_id":"my-agent","mode":"http","ttl_ms":300000}`

Modes:
- `http` — standard HTTP session (default TTL: 5 min)
- `stateless` — for fire-and-forget agents (same TTL, but semantically different)

Custom TTL: add `"ttl_ms":600000` to the request body.

**Save the returned `token`** — it authenticates all subsequent requests.

### Poll Messages (Your Event Loop)

**This is the most important operation for HTTP agents.**

#### Long-Poll (Recommended)

Block until messages arrive or timeout expires (max 60 seconds):

```bash
curl -s "http://{{host}}:9001/agents/poll?token=PLUTO-A37...&timeout=30"
```
→ `{"status":"ok","count":2,"messages":[{"event":"message","from":"coder-1","payload":{...}},...]}`

The server holds the connection open for up to `timeout` seconds. If a message
arrives during that time, it responds immediately. If nothing arrives, it
returns `{"count":0}` after the timeout.

#### Optional Query Parameters

| Parameter | Default | Description |
|-|-|-|
| `timeout` | `0` | Long-poll timeout in seconds (max 60). `0` = immediate return. |
| `ack` | `false` | Send delivery receipts back to message senders. |
| `auto_busy` | `false` | Auto-set your status to "processing" for 30s after receiving messages. |

Example with all options:
```bash
curl -s "http://{{host}}:9001/agents/poll?token=PLUTO-A37...&timeout=30&ack=true&auto_busy=true"
```

#### Periodic Poll (Fallback)

If you cannot use long-poll, call without timeout every 1–5 seconds:

```bash
curl -s "http://{{host}}:9001/agents/poll?token=PLUTO-A37..."
```

What you will receive in the `messages` array:
- Direct messages from other agents (`"event":"message"`)
- Broadcasts (`"event":"broadcast"`)
- Topic messages you subscribed to (`"event":"topic_message"`)
- Task assignments (`"event":"task_assigned"`)
- Task updates (`"event":"task_updated"`)
- Lock grants (`"event":"lock_granted"`)

Msgs are delivered **once** and removed from the inbox after polling.
If `count` is 0, there are no new events — poll again later.

**Each poll also acts as a heartbeat**, resetting your session TTL timer.

#### Signal Files

When a message is queued for your agent, the server writes a signal file at:
```
/tmp/pluto/signals/<agent_id>.signal
```
The file is deleted when you poll. You can watch this file to detect new
messages without polling (useful for shell-based agents with `inotifywait`).

### Heartbeat

Keep the session alive — send heartbeats (or poll) continuously until the
user explicitly requests a disconnect or deregistration. If you stop, the
server treats it as a crash and evicts the agent.

If you are not polling frequently, send explicit heartbeats to prevent
session expiry:

```bash
curl -s -X POST http://{{host}}:9001/agents/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37..."}'
```

You do **not** need this if you are already polling regularly — each poll
counts as a heartbeat.

### Send / Broadcast / Subscribe

```bash
# Direct message — then POLL to check for replies
curl -s -X POST http://{{host}}:9001/agents/send \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","to":"coder-1","payload":{"text":"hello"}}'

# Broadcast to all agents
curl -s -X POST http://{{host}}:9001/agents/broadcast \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","payload":{"text":"announcement"}}'

# Subscribe to topic — future topic messages will appear when you POLL
curl -s -X POST http://{{host}}:9001/agents/subscribe \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","topic":"build-status"}'
```

### Update TTL

Dynamically extend or shorten your session lifetime:

```bash
curl -s -X POST http://{{host}}:9001/agents/update_ttl \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","ttl_ms":600000}'
```
→ `{"status":"ok","ttl_ms":600000}`

### Set Status

Set a custom status visible to other agents:

```bash
curl -s -X POST http://{{host}}:9001/agents/set_status \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","custom_status":"reviewing code"}'
```
→ `{"status":"ok","custom_status":"reviewing code"}`

### Task Management via HTTP

HTTP agents can now assign, update, and query tasks directly using their token:

```bash
# Assign a task
curl -s -X POST http://{{host}}:9001/agents/task_assign \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","assignee":"worker-1","description":"Review PR #42","payload":{"pr":42}}'
→ {"status":"ok","task_id":"TASK-..."}

# Update a task
curl -s -X POST http://{{host}}:9001/agents/task_update \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","task_id":"TASK-...","status":"done","result":{"approved":true}}'

# List tasks (optional filters: assignee, status)
curl -s -X POST http://{{host}}:9001/agents/task_list \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","assignee":"worker-1","status":"pending"}'

# Task progress overview
curl -s -X POST http://{{host}}:9001/agents/task_progress \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37..."}'
```

### Unregister

```bash
curl -s -X POST http://{{host}}:9001/agents/unregister \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37..."}'
```

### Complete HTTP Session Example

```bash
# 1. Register
TOKEN=$(curl -s -X POST http://{{host}}:9001/agents/register \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","mode":"http"}' | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['token'])")

# 2. Discover peers
curl -s http://{{host}}:9001/agents
curl -s -X POST http://{{host}}:9001/agents/find \
  -H "Content-Type: application/json" \
  -d '{"filter":{"role":"reviewer"}}'

# 3. Subscribe to a topic
curl -s -X POST http://{{host}}:9001/agents/subscribe \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\",\"topic\":\"code-reviews\"}"

# 4. Send a message
curl -s -X POST http://{{host}}:9001/agents/send \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\",\"to\":\"reviewer-1\",\"payload\":{\"type\":\"review_request\"}}"

# 5. POLL for responses (long-poll blocks until messages arrive)
curl -s "http://{{host}}:9001/agents/poll?token=$TOKEN&timeout=30&ack=true"
#    → process messages, then long-poll again...

# 6. Done — unregister
curl -s -X POST http://{{host}}:9001/agents/unregister \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$TOKEN\"}"
```

### Shell Registration (PlutoClient.sh)

```bash
# HTTP registration
./PlutoClient.sh register --http --agent-id my-agent

# Stateless with custom TTL
./PlutoClient.sh register --stateless --ttl 600 --agent-id my-agent

# Background TCP daemon (maintains heartbeat for you)
./PlutoClient.sh register --daemon --agent-id my-agent
```

---

## Method 3 — HTTP One-Shot (No Registration)

For quick operations without registering. Use `agent_id` or `from` in the
request body to identify yourself.

### Health & Info

```bash
GET  /ping                    → {"status":"pong","ts":...}
GET  /health                  → {"status":"ok","version":"0.2.2"}
GET  /agents                  → {"status":"ok","agents":["coder-1",...]}
GET  /agents/list/detailed    → full agent details with attributes
GET  /agents/<id>             → single agent status
GET  /locks                   → active locks
GET  /locks/resource?resource=<name>    → holders + last holder + queue
GET  /locks/last_holder?resource=<name> → most recent holder (or null)
GET  /locks/queue?resource=<name>       → queue_length + waiters (FIFO)
GET  /tasks                   → all tasks
GET  /tasks/progress          → task counts by status and agent
GET  /events?since_token=0    → event history (polling)
```
All URLs are prefixed with `http://{{host}}:9001`.

### Lock Operations

```bash
POST /locks/acquire   {"agent_id":"me","resource":"file:/x","mode":"write","ttl_ms":30000}
POST /locks/release   {"lock_ref":"LOCK-42","agent_id":"me"}
POST /locks/renew     {"lock_ref":"LOCK-42","ttl_ms":30000}
```

### Messaging (One-Shot)

```bash
POST /messages/send       {"from":"me","to":"coder-1","payload":{"text":"hello"}}
POST /messages/broadcast  {"from":"me","payload":{"text":"announcement"}}
```

### Agent Discovery

```bash
POST /agents/find   {"filter":{"role":"reviewer"}}
```

### Admin

```bash
GET   /admin/fencing_seq
GET   /admin/deadlock_graph
POST  /admin/force_release   {"lock_ref":"LOCK-42"}
POST  /selftest
```

---

## Python Clients

### PlutoClient (TCP)

```python
from pluto_client import PlutoClient

with PlutoClient(host="{{host}}", port={{port}}, agent_id="my-agent") as client:
    # Discover peers
    reviewers = client.find_agents({"role": "reviewer"})

    # Lock → work → release
    lock_ref = client.acquire("file:/src/main.py", ttl_ms=30000)
    # ... do work ...
    client.release(lock_ref)

    # Notify
    client.send(reviewers[0], {"type": "review_request", "file": "main.py"})
```

**Event handlers** (install before `connect()`):
```python
client.on_message(lambda e: print("Message:", e["payload"]))
client.on_broadcast(lambda e: print("Broadcast:", e["payload"]))
client.on_lock_granted(lambda e: print("Lock granted:", e["lock_ref"]))
client.on("task_assigned", lambda e: print("Task:", e["task_id"]))
client.on("topic_message", lambda e: print(f"[{e['topic']}]", e["payload"]))
```

**Blocking wait for a message** (useful for turn-based coordination):
```python
import threading, time

messages = []
msg_event = threading.Event()
_lock = threading.Lock()

def on_msg(event):
    with _lock:
        messages.append(event)
    msg_event.set()

def wait_msg(from_agent, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            for i, m in enumerate(messages):
                if m.get("from") == from_agent:
                    return messages.pop(i)
        msg_event.clear()
        msg_event.wait(timeout=1)
    raise TimeoutError(f"No message from {from_agent} within {timeout}s")

client = PlutoClient(host="{{host}}", port={{port}}, agent_id="agent-a")
client.on_message(on_msg)
client.connect()

# Handshake with peer
client.send("agent-b", {"type": "ready"})
wait_msg("agent-b", timeout=30)
```

### PlutoHttpClient (HTTP Sessions)

For agents that can't maintain TCP connections. Use **`long_poll()`** (v0.2.2)
to block until messages arrive, or `poll()` for periodic polling:

```python
from pluto_client import PlutoHttpClient

with PlutoHttpClient(host="{{host}}", http_port=9001, agent_id="my-agent") as client:
    # Registration happens automatically — token is managed for you

    # Discover peers
    agents = client.list_agents()

    # Subscribe to a topic (events will appear when you poll)
    client.subscribe("code-reviews")

    # Send a message
    client.send("coder-1", {"text": "hello from HTTP"})

    # LONG-POLL — blocks until messages arrive (up to 30s)
    # This is the recommended event loop for v0.2.2+
    while working:
        messages = client.long_poll(timeout=30, ack=True)  # blocks until msgs arrive
        for msg in messages:
            handle(msg)

    # --- Additional v0.2.2 capabilities ---

    # Set your status (visible to other agents)
    client.set_status("reviewing code")

    # Extend your session TTL dynamically
    client.update_ttl(ttl_ms=600000)  # 10 minutes

    # Assign a task to another agent
    task_id = client.task_assign("worker-1", "Review PR #42", {"pr": 42})

    # Update task status
    client.task_update(task_id, "done", {"approved": True})

    # Query tasks
    tasks = client.task_list(assignee="worker-1", status="pending")
    progress = client.task_progress()

    # Check for signal file (alternative to polling)
    if client.check_signal_file():
        messages = client.poll()  # messages are waiting
```

### API Quick Reference

| Category | PlutoClient (TCP) | PlutoHttpClient (HTTP) |
|-|-|-|
| Connect | `connect()` / context manager | `register()` / context manager |
| Lock | `acquire()`, `try_acquire()`, `release()`, `renew()` | — (use one-shot HTTP) |
| Message | `send()`, `broadcast()`, `publish()` | `send()`, `broadcast()` |
| Subscribe | `subscribe()`, `unsubscribe()` | `subscribe()` |
| Discovery | `find_agents()`, `list_agents()`, `agent_status()` | `list_agents()`, `agent_status()` |
| Tasks | `task_assign()`, `task_update()`, `task_list()`, `task_batch()`, `task_progress()` | `task_assign()`, `task_update()`, `task_list()`, `task_progress()` |
| Status | `set_status()` | `set_status()` |
| TTL | — | `update_ttl()` |
| Events | Push callbacks: `on_message()`, `on_broadcast()`, `on()` | `long_poll()`, `poll()` |
| Keepalive | Automatic heartbeat thread | `heartbeat()`, `poll()`, or `long_poll()` |
| Signal | — | `check_signal_file()` |
| Stats | `stats()` | — |

### CLI

```bash
./PlutoClient.sh ping                          # test connectivity
./PlutoClient.sh list                          # list agents
./PlutoClient.sh stats                         # server statistics
./PlutoClient.sh guide --output agent_guide.md # generate this guide
./PlutoClient.sh register --http --agent-id X  # HTTP session
./PlutoClient.sh register --daemon --agent-id X # TCP daemon
```

---

## Key Rules

1. **Always release locks.** Use try/finally. Default TTL: 30 s.
   - You'll receive a `lock_expiring_soon` event ~15 s before the TTL runs out. Renew immediately with `{"op":"renew","lock_ref":"<ref>","ttl_ms":30000}`.
   - If the lock expires, you'll receive `lock_expired`. **Stop writing** and re-acquire before continuing — another agent may have been granted the lock.
2. **Heartbeat continuously.** Keep sending pings (TCP) or polls/heartbeats (HTTP). Only stop when the user explicitly requests a disconnect or deregistration — silence is treated as a crash and the server will evict the agent and release all its locks.
3. **Resource naming.** Use `file:/path/to/file` for files, `workspace:<name>` for logical scopes.
4. **Prefer topics over broadcast.** Subscribe to specific channels instead of broadcasting everything.
5. **Use task primitives** for work assignment — they are tracked and generate lifecycle events. HTTP agents can use `task_assign()` and `task_update()` directly.
6. **Handle `tasks_orphaned` events** — pick up abandoned work when agents disconnect.
7. **Duplicate names.** If your `agent_id` is taken, the server appends a 6-character suffix. Always use the returned `agent_id`.

## Response Statuses

| Status | Meaning |
|-|-|
| `ok` | Success |
| `wait` | Lock queued — expect `lock_granted` event |
| `error` | Failed — see `reason` field |
| `pong` | Reply to ping |
| `unavailable` | `try_acquire`: resource is locked |

## Error Reasons

`bad_request` · `unknown_op` · `unknown_target` · `conflict` · `not_found` ·
`expired` · `wait_timeout` · `deadlock` · `already_registered` · `unauthorized` ·
`not_registered` · `internal_error`
