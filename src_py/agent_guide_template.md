# Pluto Agent Guide

> **Generated:** {{generated_at}}
> **Server:** {{host}}:{{port}} (TCP) · {{host}}:9001 (HTTP)

---

## What Is Pluto?

Pluto is a coordination server for AI agents. Connect, discover peers, lock
shared resources, exchange messages, and track tasks — all through JSON.

| Feature | What It Does |
|---|---|
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
|---|---|---|
| **TCP** | {{port}} | You can open TCP sockets — gives push events, real-time lock grants |
| **HTTP Sessions** | 9001 | Cannot maintain TCP (e.g. Claude Code, sandboxed envs) — register once, poll for events |
| **HTTP One-Shot** | 9001 | Quick lock/message operations without registering |
| **Python `PlutoClient`** | {{port}} | High-level wrapper over TCP with heartbeat and event dispatch |
| **Python `PlutoHttpClient`** | 9001 | High-level wrapper over HTTP sessions |

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

Send `{"op":"ping"}` every 15 s. Sessions expire after 30 s of silence.

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
|------|----------|
| `write` | Exclusive — no other locks allowed |
| `read` | Shared — multiple readers OK, writers wait |

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
|---|---|
| `message` | Direct message received |
| `broadcast` | Broadcast received |
| `topic_message` | Published to a topic you subscribed to |
| `lock_granted` | Queued lock was granted |
| `lock_expired` | Your lock's TTL elapsed |
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
6. Begin work — process tasks, respond to messages, ping every 15s
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

For agents that **cannot maintain a TCP socket** (CLI tools, sandboxed
environments, serverless functions). You register via HTTP and receive a
**token** for subsequent requests. Messages are retrieved by polling.

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

### Heartbeat

```bash
curl -s -X POST http://{{host}}:9001/agents/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37..."}'
```
Call periodically (before TTL expires) to keep the session alive.

### Poll Messages

```bash
curl -s "http://{{host}}:9001/agents/poll?token=PLUTO-A37..."
```
→ `{"status":"ok","count":2,"messages":[{"event":"message","from":"coder-1","payload":{...}},...]}`

Each poll also acts as a heartbeat. Messages are delivered once and removed from the inbox.

### Send / Broadcast / Subscribe

```bash
# Direct message
curl -s -X POST http://{{host}}:9001/agents/send \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","to":"coder-1","payload":{"text":"hello"}}'

# Broadcast
curl -s -X POST http://{{host}}:9001/agents/broadcast \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","payload":{"text":"announcement"}}'

# Subscribe to topic
curl -s -X POST http://{{host}}:9001/agents/subscribe \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37...","topic":"build-status"}'
```

### Unregister

```bash
curl -s -X POST http://{{host}}:9001/agents/unregister \
  -H "Content-Type: application/json" \
  -d '{"token":"PLUTO-A37..."}'
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
GET  /health                  → {"status":"ok","version":"0.2.1"}
GET  /agents                  → {"status":"ok","agents":["coder-1",...]}
GET  /agents/list/detailed    → full agent details with attributes
GET  /agents/<id>             → single agent status
GET  /locks                   → active locks
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

For agents that cannot maintain TCP connections:

```python
from pluto_client import PlutoHttpClient

with PlutoHttpClient(host="{{host}}", http_port=9001, agent_id="my-agent") as client:
    # Already registered — token managed automatically
    agents = client.list_agents()
    client.send("coder-1", {"text": "hello from HTTP"})
    messages = client.poll()      # also heartbeats
    client.heartbeat()            # explicit keepalive
```

### API Quick Reference

| Category | PlutoClient (TCP) | PlutoHttpClient (HTTP) |
|---|---|---|
| Connect | `connect()` / context manager | `register()` / context manager |
| Lock | `acquire()`, `try_acquire()`, `release()`, `renew()` | — (use one-shot HTTP) |
| Message | `send()`, `broadcast()`, `publish()` | `send()`, `broadcast()` |
| Subscribe | `subscribe()`, `unsubscribe()` | `subscribe()` |
| Discovery | `find_agents()`, `list_agents()`, `agent_status()` | `list_agents()`, `agent_status()` |
| Tasks | `task_assign()`, `task_update()`, `task_list()`, `task_batch()`, `task_progress()` | — (use one-shot HTTP) |
| Status | `set_status()` | — |
| Events | Push callbacks: `on_message()`, `on_broadcast()`, `on()` | `poll()` |
| Keepalive | Automatic heartbeat thread | `heartbeat()` or `poll()` |
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
2. **Heartbeat.** TCP: ping every 15 s. HTTP: call heartbeat or poll before TTL expires.
3. **Resource naming.** Use `file:/path/to/file` for files, `workspace:<name>` for logical scopes.
4. **Prefer topics over broadcast.** Subscribe to specific channels instead of broadcasting everything.
5. **Use task primitives** for work assignment — they are tracked and generate lifecycle events.
6. **Handle `tasks_orphaned` events** — pick up abandoned work when agents disconnect.
7. **Duplicate names.** If your `agent_id` is taken, the server appends a 6-character suffix. Always use the returned `agent_id`.

## Response Statuses

| Status | Meaning |
|---|---|
| `ok` | Success |
| `wait` | Lock queued — expect `lock_granted` event |
| `error` | Failed — see `reason` field |
| `pong` | Reply to ping |
| `unavailable` | `try_acquire`: resource is locked |

## Error Reasons

`bad_request` · `unknown_op` · `unknown_target` · `conflict` · `not_found` ·
`expired` · `wait_timeout` · `deadlock` · `already_registered` · `unauthorized` ·
`not_registered` · `internal_error`
