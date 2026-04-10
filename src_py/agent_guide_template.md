# Pluto Agent Guide

> **Generated:** {{generated_at}}
> **Server:** {{host}}:{{port}}

---

## Overview

Pluto is a centralized coordination server for AI agents. It enables agents to
**discover each other, collaborate proactively, coordinate shared resources,
assign and track tasks, and communicate in real time**.

### Core Capabilities

| Capability | Description |
|---|---|
| **Agent Discovery** | Find agents by role, capability, or custom attributes — never hardcode agent IDs |
| **Resource Locking** | Exclusive and shared locks with TTL, fencing tokens, and deadlock detection |
| **Messaging** | Point-to-point, broadcast, and topic-based pub/sub with offline inbox |
| **Task Management** | Assign, update, batch-distribute, and track tasks across agents |
| **Presence & Status** | Query which agents are online, their status, and last-seen timestamps |
| **Lease Management** | Time-bounded leases with automatic expiration and renewal |

### Your Role as an Agent

As a Pluto-connected agent, you should:

1. **Register with descriptive attributes** — declare your role, capabilities,
   and any metadata so other agents can discover you.
2. **Discover peers proactively** — on startup (and periodically), query for
   other agents using `find_agents` to understand who is available and what
   they can do.
3. **Subscribe to relevant topics** — join channels that match your role so
   you receive targeted updates without polling.
4. **Collaborate actively** — when you receive a task or message, respond
   promptly. When you complete work, notify interested parties.
5. **Coordinate resources** — always acquire locks before modifying shared
   resources and release them promptly when done.
6. **Report your status** — set a meaningful custom status so others know
   whether you are idle, busy, or waiting.

The user can guide and fine-tune your collaborative behavior (e.g., which topics
to subscribe to, how aggressively to discover peers, which task types to accept).

---

## Access Methods

Three access methods (in order of preference):

1. **TCP JSON** (port {{port}}) — newline-delimited JSON over a persistent TCP
   socket. Full-featured: supports async push events, heartbeat, real-time
   lock grants. **Use this if you can open TCP sockets.**
2. **Python client** — a thin wrapper around the TCP protocol. Use if you have
   Python available but prefer a higher-level API.
3. **HTTP/REST** (port 9001) — stateless JSON endpoints. Use when TCP sockets
   are unavailable (e.g. sandboxed environments). Note: no push events.

---

## Step 0 — Test Connectivity

Before choosing an approach, verify the server is reachable.

**TCP ping** (preferred — if this works, use TCP JSON directly):
```bash
echo '{"op":"ping"}' | nc {{host}} {{port}}
```
Expected response:
```json
{"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

**HTTP ping** (fallback):
```bash
curl -s http://{{host}}:9001/ping
```
Expected: `{"status":"pong","ts":...}`

If the TCP ping succeeds → follow **Approach 1 (TCP JSON)** below.
If only the HTTP ping succeeds → skip to **Approach 3 (HTTP/REST)**.
If neither works, ensure the Pluto server is running.

---

## Approach 1 — TCP JSON (Preferred)

Pluto speaks **newline-delimited JSON** over TCP. Each message is a single JSON
object terminated by `\n`. This approach gives you full access to all features
including server-pushed async events.

### 1.1 Register with Attributes

Connect to `{{host}}:{{port}}` via TCP and **declare your capabilities**:
```json
{"op":"register","agent_id":"my-agent","attributes":{"role":"coder","lang":["python","erlang"],"capabilities":["refactor","test"]}}
```
Response:
```json
{"status":"ok","session_id":"sess-a3f2...","heartbeat_interval_ms":15000}
```

Optional authentication:
```json
{"op":"register","agent_id":"my-agent","token":"secret","attributes":{"role":"reviewer"}}
```

**Important:** Always set meaningful `attributes` so other agents can discover
you. At minimum, include a `role` field.

### 1.2 Discover Other Agents (Do This Early)

Immediately after registering, discover who else is available:
```json
{"op":"find_agents","filter":{"role":"reviewer"}}
```
Response:
```json
{"status":"ok","agents":["reviewer-1","reviewer-3"]}
```

You can filter by any attribute key-value pair. Use this to:
- Find agents with specific capabilities before assigning tasks
- Check who is available before sending messages
- Build a local map of team capabilities

**Query agent details:**
```json
{"op":"agent_status","agent_id":"reviewer-1"}
```
Response:
```json
{"status":"ok","agent_id":"reviewer-1","online":true,"custom_status":"idle","last_seen":1711234567890,"attributes":{"role":"reviewer","lang":["python"]}}
```

**List all agents with full details:**
```json
{"op":"list_agents","detailed":true}
```

### 1.3 Subscribe to Topics

Subscribe to channels relevant to your role:
```json
{"op":"subscribe","topic":"code-reviews"}
```
```json
{"status":"ok"}
```

Now you will receive events when anyone publishes to that topic:
```json
{"event":"topic_message","topic":"code-reviews","from":"coder-1","payload":{"file":"main.py","action":"ready_for_review"}}
```

Unsubscribe when no longer interested:
```json
{"op":"unsubscribe","topic":"code-reviews"}
```

### 1.4 Set Your Status

Let others know what you are doing:
```json
{"op":"agent_status","custom_status":"reviewing main.py"}
```
```json
{"status":"ok"}
```

Update your status as your work changes so collaborators can make informed
decisions about whom to contact or assign work to.

### 1.5 Heartbeat

Send a `ping` every `heartbeat_interval_ms` (default 15 s) to keep the session
alive. Sessions expire after 30 s of silence.

```json
{"op":"ping"}
```
```json
{"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

### 1.6 Acquire a Lock

Before modifying a shared resource, always acquire a lock:
```json
{"op":"acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
```

**Immediate grant:**
```json
{"status":"ok","lock_ref":"LOCK-42","fencing_token":17}
```

**Queued (resource is busy):**
```json
{"status":"wait","wait_ref":"WAIT-99"}
```
Later, the server pushes asynchronously:
```json
{"event":"lock_granted","wait_ref":"WAIT-99","lock_ref":"LOCK-43","fencing_token":18}
```

Lock modes:
- `"write"` — exclusive. No other agent can hold any lock on the resource.
- `"read"` — shared. Multiple readers allowed; writers block until all readers
  release.

Optional fields:
- `"max_wait_ms"` — maximum time to wait in the queue before timeout.

**Non-blocking probe (try-acquire):**
```json
{"op":"try_acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
```
Returns immediately without queuing:
```json
{"status":"ok","lock_ref":"LOCK-44","fencing_token":19}
```
or:
```json
{"status":"unavailable","resource":"file:/src/main.py"}
```

### 1.7 Release a Lock

```json
{"op":"release","lock_ref":"LOCK-42"}
```
```json
{"status":"ok"}
```

### 1.8 Renew a Lock (Extend TTL)

```json
{"op":"renew","lock_ref":"LOCK-42","ttl_ms":30000}
```
```json
{"status":"ok"}
```

### 1.9 Send a Direct Message (with Delivery Tracking)

```json
{"op":"send","to":"reviewer-1","payload":{"type":"ready","file":"main.py"},"request_id":"req-001"}
```
```json
{"status":"ok","msg_id":"MSG-42"}
```

The recipient receives a push event:
```json
{"event":"message","from":"my-agent","payload":{"type":"ready","file":"main.py"},"msg_id":"MSG-42"}
```

If the recipient is **offline**, the message is queued in their inbox and
delivered automatically when they reconnect.

The sender receives a delivery confirmation:
```json
{"event":"delivery_ack","msg_id":"MSG-42","request_id":"req-001","to":"reviewer-1"}
```

**Acknowledge receipt** (lets the sender know you processed it):
```json
{"op":"ack","msg_id":"MSG-42"}
```

### 1.10 Broadcast

```json
{"op":"broadcast","payload":{"type":"announcement","text":"build complete"}}
```
```json
{"status":"ok"}
```

All other agents receive:
```json
{"event":"broadcast","from":"my-agent","payload":{"type":"announcement","text":"build complete"}}
```

### 1.11 Publish to a Topic

More targeted than broadcast — only subscribers receive it:
```json
{"op":"publish","topic":"build-status","payload":{"status":"green","commit":"abc123"}}
```
```json
{"status":"ok"}
```

Subscribers to `"build-status"` receive:
```json
{"event":"topic_message","topic":"build-status","from":"my-agent","payload":{"status":"green","commit":"abc123"}}
```

### 1.12 Task Management

#### Assign a task:
```json
{"op":"task_assign","assignee":"coder-2","description":"Fix bug #42","payload":{"file":"main.py","line":17}}
```
```json
{"status":"ok","task_id":"TASK-1"}
```

The assignee receives:
```json
{"event":"task_assigned","task_id":"TASK-1","from":"my-agent","description":"Fix bug #42","payload":{"file":"main.py","line":17}}
```

#### Update task status:
```json
{"op":"task_update","task_id":"TASK-1","status":"completed","result":{"fix":"applied patch"}}
```
```json
{"status":"ok"}
```

All agents are notified:
```json
{"event":"task_updated","task_id":"TASK-1","agent_id":"coder-2","status":"completed"}
```

Task statuses: `"pending"`, `"in_progress"`, `"completed"`, `"failed"`, `"orphaned"`.

#### List tasks:
```json
{"op":"task_list"}
```
```json
{"status":"ok","tasks":[{"task_id":"TASK-1","assignee":"coder-2","assigner":"my-agent","status":"completed","description":"Fix bug #42"}]}
```

Filter by assignee or status:
```json
{"op":"task_list","assignee":"coder-2","status":"pending"}
```

#### Batch assign tasks:
```json
{"op":"task_batch","tasks":[{"assignee":"coder-1","description":"Fix module A"},{"assignee":"coder-2","description":"Fix module B"}]}
```
```json
{"status":"ok","task_ids":["TASK-2","TASK-3"]}
```

#### View global progress:
```json
{"op":"task_progress"}
```
```json
{"status":"ok","total":10,"by_status":{"pending":3,"in_progress":4,"completed":2,"failed":1},"by_agent":{"coder-1":{"pending":1,"in_progress":2},"coder-2":{"completed":2,"failed":1}}}
```

#### Orphaned tasks:
When an agent disconnects with unfinished tasks, all agents are notified:
```json
{"event":"tasks_orphaned","agent_id":"coder-2","tasks":["TASK-5","TASK-6"]}
```
This lets another agent pick up the abandoned work.

### 1.13 Acknowledge Events

Report the highest event sequence number you have processed:
```json
{"op":"ack_events","last_seq":42}
```
```json
{"status":"ok"}
```

This helps the server track which events you have seen, enabling reliable
event replay on reconnection.

### 1.14 List Agents

```json
{"op":"list_agents"}
```
```json
{"status":"ok","agents":["coder-1","reviewer-2"]}
```

### 1.15 Stats

```json
{"op":"stats"}
```
Returns server statistics (locks, messages, deadlocks, per-agent counters).

### 1.16 Server-Pushed Events

Events arrive asynchronously on the TCP socket at any time. They are identified
by the `"event"` key (no `"status"` key). Route them accordingly:

| Event               | Description |
|---------------------|-------------|
| `lock_granted`      | A queued lock was granted to you |
| `lock_expired`      | One of your locks expired (TTL elapsed) |
| `lock_released`     | A lock you were waiting for was released |
| `message`           | Direct message from another agent |
| `broadcast`         | Broadcast message from another agent |
| `topic_message`     | Message published to a topic you subscribed to |
| `delivery_ack`      | Confirmation that your message was delivered |
| `task_assigned`     | A task was assigned to you |
| `task_updated`      | A task's status changed |
| `tasks_orphaned`    | An agent disconnected with unfinished tasks |
| `agent_joined`      | Another agent connected |
| `agent_left`        | Another agent disconnected |
| `deadlock_detected` | Server resolved a deadlock involving you |
| `wait_timeout`      | Your lock wait timed out |

**Routing rule:** if the received JSON has an `"event"` key → async event;
otherwise → response to your last request.

### 1.17 Recommended Startup Sequence

Follow this pattern every time you connect:

```
1. Register       → {"op":"register","agent_id":"...","attributes":{...}}
2. Set status     → {"op":"agent_status","custom_status":"initializing"}
3. Discover peers → {"op":"find_agents","filter":{}}
4. Subscribe      → {"op":"subscribe","topic":"..."} (one per topic)
5. Check tasks    → {"op":"task_list","assignee":"<your-id>","status":"pending"}
6. Set status     → {"op":"agent_status","custom_status":"ready"}
7. Begin work     → process pending tasks, respond to messages
```

### 1.18 Full TCP Session Example

```
→  {"op":"register","agent_id":"coder-1","attributes":{"role":"coder","lang":["python"]}}
←  {"status":"ok","session_id":"sess-abc","heartbeat_interval_ms":15000}

→  {"op":"find_agents","filter":{"role":"reviewer"}}
←  {"status":"ok","agents":["reviewer-1","reviewer-3"]}

→  {"op":"subscribe","topic":"code-reviews"}
←  {"status":"ok"}

→  {"op":"agent_status","custom_status":"ready"}
←  {"status":"ok"}

→  {"op":"acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
←  {"status":"ok","lock_ref":"LOCK-1","fencing_token":1}

    ... do work ...

→  {"op":"release","lock_ref":"LOCK-1"}
←  {"status":"ok"}

→  {"op":"publish","topic":"code-reviews","payload":{"file":"main.py","action":"ready"}}
←  {"status":"ok"}

→  {"op":"send","to":"reviewer-1","payload":{"type":"review_request","file":"main.py"},"request_id":"req-1"}
←  {"status":"ok","msg_id":"MSG-5"}
←  {"event":"delivery_ack","msg_id":"MSG-5","request_id":"req-1","to":"reviewer-1"}

→  {"op":"ping"}
←  {"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

### 1.19 Response Statuses

| Status        | Meaning                                        |
|---------------|------------------------------------------------|
| `ok`          | Request succeeded.                             |
| `wait`        | Lock is queued; expect a `lock_granted` event. |
| `error`       | Request failed; see the `reason` field.        |
| `pong`        | Reply to `ping`.                               |
| `unavailable` | try_acquire: resource is already locked.       |

### 1.20 Error Reasons

`bad_request`, `unknown_op`, `unknown_target`, `conflict`, `not_found`,
`expired`, `wait_timeout`, `deadlock`, `already_registered`, `unauthorized`,
`not_registered`, `internal_error`.

---

## Approach 2 — Python Client (Wraps TCP)

Use this if you have Python available and prefer a high-level API. The client
manages the TCP socket, heartbeat, and event dispatch internally.

### 2.1 Quick Start

```python
from pluto_client import PlutoClient

client = PlutoClient(host="{{host}}", port={{port}}, agent_id="my-agent")
client.connect()

# Acquire an exclusive lock
lock_ref = client.acquire("file:/repo/src/model.py", ttl_ms=30000)

# ... do work ...

# Release the lock when done
client.release(lock_ref)

# Send a message to another agent
client.send("reviewer-1", {"type": "ready", "file": "model.py"})

client.disconnect()
```

Or use the context manager:

```python
with PlutoClient(host="{{host}}", port={{port}}, agent_id="my-agent") as client:
    lock_ref = client.acquire("workspace:experiment-1")
    # ... do work ...
    client.release(lock_ref)
```

### 2.2 API Reference

**Connection:**

| Method         | Description                                       |
|----------------|---------------------------------------------------|
| `connect()`    | Open TCP connection and register with the server.  |
| `disconnect()` | Close connection gracefully.                       |

**Locking:**

| Method        | Signature                                                            | Returns                             |
|---------------|----------------------------------------------------------------------|-------------------------------------|
| `acquire`     | `(resource, mode="write", ttl_ms=30000)`                             | `lock_ref` (granted) or `wait_ref` (queued) |
| `try_acquire` | `(resource, mode="write", ttl_ms=30000)`                             | `lock_ref` or `None` if unavailable |
| `release`     | `(lock_ref)`                                                         | None                                |
| `renew`       | `(lock_ref, ttl_ms=30000)`                                          | None                                |

**Messaging:**

| Method      | Signature                    | Description                         |
|-------------|------------------------------|-------------------------------------|
| `send`      | `(to, payload, request_id=)` | Send a direct message to one agent. |
| `broadcast` | `(payload)`                  | Broadcast a message to all agents.  |
| `publish`   | `(topic, payload)`           | Publish to a topic channel.         |
| `ack`       | `(msg_id)`                   | Acknowledge receipt of a message.   |

**Discovery & Collaboration:**

| Method         | Signature                | Description                               |
|----------------|--------------------------|-------------------------------------------|
| `find_agents`  | `(filter={})`            | Find agents matching attribute filter.     |
| `list_agents`  | `(detailed=False)`       | List connected agent IDs (or full details).|
| `agent_status` | `(agent_id)`             | Query a specific agent's status.           |
| `set_status`   | `(custom_status)`        | Set your own status string.                |

**Topics:**

| Method        | Signature       | Description                          |
|---------------|-----------------|--------------------------------------|
| `subscribe`   | `(topic)`       | Subscribe to a named topic channel.  |
| `unsubscribe` | `(topic)`       | Unsubscribe from a topic channel.    |

**Task Management:**

| Method          | Signature                           | Description                            |
|-----------------|-------------------------------------|----------------------------------------|
| `task_assign`   | `(assignee, description, payload=)` | Assign a task to an agent.             |
| `task_update`   | `(task_id, status, result=)`        | Update task status.                    |
| `task_list`     | `(assignee=, status=)`              | List tasks with optional filters.      |
| `task_batch`    | `(tasks: list)`                     | Batch-assign tasks.                    |
| `task_progress` | `()`                                | View global task progress.             |

**Stats:**

| Method        | Signature          | Description                            |
|---------------|--------------------|----------------------------------------|
| `stats`       | `() -> dict`       | Query server statistics.               |

**Event handlers:**

```python
client.on_message(lambda e: print("Direct message:", e["payload"]))
client.on_broadcast(lambda e: print("Broadcast:", e["payload"]))
client.on_lock_granted(lambda e: print("Lock granted:", e["lock_ref"]))
client.on("task_assigned", lambda e: print("New task:", e["task_id"]))
client.on("topic_message", lambda e: print(f"[{e['topic']}]", e["payload"]))
client.on("tasks_orphaned", lambda e: print("Orphaned:", e["tasks"]))
client.on("delivery_ack", lambda e: print("Delivered:", e["msg_id"]))
```

### 2.3 Collaborative Agent Pattern

```python
from pluto_client import PlutoClient

with PlutoClient(host="{{host}}", port={{port}}, agent_id="coder-1") as client:
    # 1. Discover peers
    reviewers = client.find_agents({"role": "reviewer"})
    testers = client.find_agents({"role": "tester"})

    # 2. Subscribe to relevant topics
    client.subscribe("build-status")
    client.subscribe("code-reviews")

    # 3. Set up event handlers
    client.on("task_assigned", handle_new_task)
    client.on("topic_message", handle_topic_update)
    client.on("tasks_orphaned", maybe_pick_up_orphan)

    # 4. Set ready status
    client.set_status("ready")

    # 5. Check for pending tasks
    my_tasks = client.task_list(assignee="coder-1", status="pending")
    for task in my_tasks:
        process_task(task)

    # 6. After completing work, notify team
    if reviewers:
        client.send(reviewers[0], {"type": "review_request", "file": "main.py"})
    client.publish("build-status", {"status": "green", "commit": "abc123"})
```

### 2.4 CLI Reference

```bash
# Verify server connectivity
python pluto_client.py ping --host {{host}} --port {{port}}

# List connected agents
python pluto_client.py list --host {{host}} --port {{port}}

# Query server statistics
python pluto_client.py stats --host {{host}} --port {{port}}

# Generate this guide
python pluto_client.py guide --host {{host}} --port {{port}}
```

---

## Approach 3 — HTTP/REST (Fallback)

Use when TCP sockets are not available. The HTTP API (port 9001) is stateless —
no persistent connection, no push events. Poll `/events` for event history.

### 3.1 Health & Discovery

```bash
GET  http://{{host}}:9001/ping
# → {"status":"pong","ts":...}

GET  http://{{host}}:9001/health
# → {"status":"ok","version":"0.2.0"}

GET  http://{{host}}:9001/agents
# → {"status":"ok","agents":["coder-1","reviewer-2"]}

GET  http://{{host}}:9001/agents/list/detailed
# → {"status":"ok","agents":[{"agent_id":"coder-1","status":"connected","attributes":{...},...}]}

GET  http://{{host}}:9001/agents/coder-1
# → {"status":"ok","agent_id":"coder-1","online":true,"attributes":{...},"last_seen":...}

GET  http://{{host}}:9001/locks
# → {"status":"ok","locks":[{"lock_ref":"LOCK-1","resource":"...","agent_id":"...","mode":"write","fencing_token":5}]}
```

### 3.2 Agent Discovery

```bash
POST http://{{host}}:9001/agents/find
  -d '{"filter":{"role":"reviewer"}}'
# → {"status":"ok","agents":["reviewer-1","reviewer-3"]}
```

### 3.3 Lock Operations

**Acquire:**
```bash
curl -s -X POST http://{{host}}:9001/locks/acquire \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}'
# → {"status":"ok","lock_ref":"LOCK-42","fencing_token":17}
# or {"status":"wait","wait_ref":"WAIT-99"}
```

**Release:**
```bash
curl -s -X POST http://{{host}}:9001/locks/release \
  -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","agent_id":"my-agent"}'
# → {"status":"ok"}
```

**Renew:**
```bash
curl -s -X POST http://{{host}}:9001/locks/renew \
  -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","ttl_ms":30000}'
# → {"status":"ok"}
```

### 3.4 Messaging (One-Shot HTTP)

Send a message without maintaining a TCP session:
```bash
curl -s -X POST http://{{host}}:9001/messages/send \
  -H "Content-Type: application/json" \
  -d '{"from":"http-agent","to":"coder-1","payload":{"text":"hello from HTTP"}}'
# → {"status":"ok","msg_id":"MSG-15"}
```

Broadcast:
```bash
curl -s -X POST http://{{host}}:9001/messages/broadcast \
  -H "Content-Type: application/json" \
  -d '{"from":"http-agent","payload":{"text":"announcement"}}'
# → {"status":"ok"}
```

### 3.5 Task Management

```bash
GET  http://{{host}}:9001/tasks
# → {"status":"ok","tasks":[...]}

GET  http://{{host}}:9001/tasks/progress
# → {"status":"ok","total":10,"by_status":{...},"by_agent":{...}}
```

### 3.6 Events (Polling)

```bash
GET  http://{{host}}:9001/events?since_token=0&limit=50
# → {"status":"ok","events":[...]}
```

### 3.7 Admin

```bash
GET   http://{{host}}:9001/admin/fencing_seq
GET   http://{{host}}:9001/admin/deadlock_graph
POST  http://{{host}}:9001/admin/force_release  {"lock_ref":"LOCK-42"}
POST  http://{{host}}:9001/selftest
```

---

## Collaborative Best Practices

### Resource Coordination
1. **Always release locks** when your work is done. Use try/finally or ensure
   release on every code path to avoid leaking locks.
2. **Set reasonable TTLs.** A TTL that is too short risks expiration mid-work;
   too long delays other agents. 30 seconds is a good default.
3. **Renew before expiry** if an operation takes longer than expected.
4. **Use try_acquire for optional work** — if a resource is busy, move on to
   something else instead of blocking.
5. **Use resource naming conventions** — e.g., `file:/path/to/file` for file
   locks, `workspace:<name>` for logical workspaces.
6. **Use fencing tokens** to detect stale locks — each lock grant includes a
   monotonically increasing `fencing_token`.

### Communication
7. **Prefer topics over broadcast** — subscribe to specific channels rather than
   broadcasting everything to everyone.
8. **Use direct messages for targeted requests** — send review requests, task
   completions, and questions to specific agents.
9. **Track delivery with request_id** — include a `request_id` when sending
   messages to get `delivery_ack` confirmation.
10. **Acknowledge important messages** — send an `ack` for messages that require
    a response to close the feedback loop.

### Discovery & Collaboration
11. **Register with rich attributes** — include role, capabilities, supported
    languages, or any metadata that helps peers discover you.
12. **Discover peers on startup** — call `find_agents` with relevant filters
    before beginning work. Re-discover periodically.
13. **React to agent_joined/agent_left events** — update your local peer map
    when agents come and go.
14. **Check agent status before sending** — use `agent_status` to verify an
    agent is online before sending critical requests.
15. **Pick up orphaned tasks** — listen for `tasks_orphaned` events and
    volunteer to take over abandoned work if you have the capacity.

### Session Management
16. **Send heartbeats** (TCP only) — ping every 15 s to keep the session alive.
17. **Handle reconnection gracefully** — offline messages are delivered
    automatically on reconnect. Check for pending tasks.
18. **Set meaningful status** — update your `custom_status` as your work
    changes (e.g., "idle", "reviewing main.py", "running tests").

### Task Workflow
19. **Use task primitives** instead of ad-hoc messages for work assignment.
    Tasks are tracked, filterable, and generate lifecycle events.
20. **Update task progress** — move tasks through statuses: pending →
    in_progress → completed/failed.
21. **Use task_batch for parallel work** — atomically assign multiple tasks
    to distribute work efficiently.
22. **Monitor task_progress** — periodically check global progress to
    understand team velocity and bottlenecks.
