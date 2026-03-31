# Pluto Agent Guide

> **Generated:** {{generated_at}}
> **Server:** {{host}}:{{port}}

---

## Overview

Pluto is a centralized coordination server for AI agents. It provides resource
locking (exclusive and shared with TTL), lease management, an agent registry,
point-to-point and broadcast messaging, deadlock detection with automatic victim
selection, and fencing tokens.

**Three access methods** (in order of preference):

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

### 1.1 Register

Connect to `{{host}}:{{port}}` via TCP and send:
```json
{"op":"register","agent_id":"my-agent"}
```
Response:
```json
{"status":"ok","session_id":"sess-a3f2...","heartbeat_interval_ms":15000}
```

Optional authentication:
```json
{"op":"register","agent_id":"my-agent","token":"secret"}
```

### 1.2 Heartbeat

Send a `ping` every `heartbeat_interval_ms` (default 15 s) to keep the session
alive. Sessions expire after 30 s of silence.

```json
{"op":"ping"}
```
```json
{"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

### 1.3 Acquire a Lock

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

Optional field: `"max_wait_ms"` — maximum time to wait in the queue.

### 1.4 Release a Lock

```json
{"op":"release","lock_ref":"LOCK-42"}
```
```json
{"status":"ok"}
```

### 1.5 Renew a Lock (Extend TTL)

```json
{"op":"renew","lock_ref":"LOCK-42","ttl_ms":30000}
```
```json
{"status":"ok"}
```

### 1.6 Send a Direct Message

```json
{"op":"send","to":"reviewer-1","payload":{"type":"ready","file":"main.py"}}
```
```json
{"status":"ok"}
```

The recipient receives a push event:
```json
{"event":"message","from":"my-agent","payload":{"type":"ready","file":"main.py"}}
```

### 1.7 Broadcast

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

### 1.8 List Agents

```json
{"op":"list_agents"}
```
```json
{"status":"ok","agents":["coder-1","reviewer-2"]}
```

### 1.9 Stats

```json
{"op":"stats"}
```
Returns server statistics (locks, messages, deadlocks, per-agent counters).

### 1.10 Server-Pushed Events

Events arrive asynchronously on the TCP socket at any time. They are identified
by the `"event"` key (no `"status"` key). Route them accordingly:

| Event               | Example                                                                            |
|---------------------|------------------------------------------------------------------------------------|
| `lock_granted`      | `{"event":"lock_granted","wait_ref":"WAIT-99","lock_ref":"LOCK-43","fencing_token":18}` |
| `lock_expired`      | `{"event":"lock_expired","lock_ref":"LOCK-42","resource":"file:/src/main.py"}`     |
| `lock_released`     | `{"event":"lock_released","lock_ref":"LOCK-42","resource":"file:/src/main.py"}`    |
| `message`           | `{"event":"message","from":"agent-A","payload":{...}}`                             |
| `broadcast`         | `{"event":"broadcast","from":"agent-A","payload":{...}}`                           |
| `agent_joined`      | `{"event":"agent_joined","agent_id":"coder-2"}`                                    |
| `agent_left`        | `{"event":"agent_left","agent_id":"coder-2"}`                                      |
| `deadlock_detected` | `{"event":"deadlock_detected",...}`                                                 |
| `wait_timeout`      | `{"event":"wait_timeout","wait_ref":"WAIT-99"}`                                    |

**Routing rule:** if the received JSON has an `"event"` key → async event;
otherwise → response to your last request.

### 1.11 Full TCP Session Example

```
→  {"op":"register","agent_id":"coder-1"}
←  {"status":"ok","session_id":"sess-abc","heartbeat_interval_ms":15000}

→  {"op":"acquire","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}
←  {"status":"ok","lock_ref":"LOCK-1","fencing_token":1}

    ... do work ...

→  {"op":"release","lock_ref":"LOCK-1"}
←  {"status":"ok"}

→  {"op":"send","to":"reviewer-1","payload":{"type":"done","file":"main.py"}}
←  {"status":"ok"}

→  {"op":"ping"}
←  {"status":"pong","ts":1711234567890,"heartbeat_interval_ms":15000}
```

### 1.12 Response Statuses

| Status  | Meaning                                        |
|---------|------------------------------------------------|
| `ok`    | Request succeeded.                             |
| `wait`  | Lock is queued; expect a `lock_granted` event. |
| `error` | Request failed; see the `reason` field.        |
| `pong`  | Reply to `ping`.                               |

### 1.13 Error Reasons

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

| Method    | Signature                                                            | Returns                             |
|-----------|----------------------------------------------------------------------|-------------------------------------|
| `acquire` | `(resource: str, mode: str = "write", ttl_ms: int = 30000) -> str`  | `lock_ref` (granted) or `wait_ref` (queued) |
| `release` | `(lock_ref: str)`                                                    | None                                |
| `renew`   | `(lock_ref: str, ttl_ms: int = 30000)`                              | None                                |

**Messaging:**

| Method      | Signature                  | Description                         |
|-------------|----------------------------|-------------------------------------|
| `send`      | `(to: str, payload: dict)` | Send a direct message to one agent. |
| `broadcast` | `(payload: dict)`          | Broadcast a message to all agents.  |

**Discovery & Stats:**

| Method        | Signature          | Description                            |
|---------------|--------------------|----------------------------------------|
| `list_agents` | `() -> List[str]`  | List connected agent IDs.              |
| `stats`       | `() -> dict`       | Query server statistics.               |

**Event handlers:**

```python
client.on_message(lambda e: print("Direct message:", e["payload"]))
client.on_broadcast(lambda e: print("Broadcast:", e["payload"]))
client.on_lock_granted(lambda e: print("Lock granted:", e["lock_ref"]))
client.on("deadlock_detected", lambda e: print("Deadlock!", e))
```

### 2.3 CLI Reference

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
# → {"status":"ok","version":"0.1.0"}

GET  http://{{host}}:9001/agents
# → {"status":"ok","agents":["coder-1","reviewer-2"]}

GET  http://{{host}}:9001/locks
# → {"status":"ok","locks":[{"lock_ref":"LOCK-1","resource":"...","agent_id":"...","mode":"write","fencing_token":5}]}
```

### 3.2 Lock Operations

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

### 3.3 Events (Polling)

```bash
GET  http://{{host}}:9001/events?since_token=0&limit=50
# → {"status":"ok","events":[...]}
```

### 3.4 Admin

```bash
GET   http://{{host}}:9001/admin/fencing_seq
GET   http://{{host}}:9001/admin/deadlock_graph
POST  http://{{host}}:9001/admin/force_release  {"lock_ref":"LOCK-42"}
POST  http://{{host}}:9001/selftest
```

---

## Best Practices

1. **Always release locks** when your work is done. Use try/finally or ensure
   release on every code path to avoid leaking locks.
2. **Set reasonable TTLs.** A TTL that is too short risks expiration mid-work;
   too long delays other agents. 30 seconds is a good default.
3. **Renew before expiry** if an operation takes longer than expected.
4. **Send heartbeats** (TCP only) — ping every 15 s to keep the session alive.
5. **Use resource naming conventions** — e.g., `file:/path/to/file` for file
   locks, `workspace:<name>` for logical workspaces.
6. **Handle `wait` responses** — your lock may not be granted immediately.
   Listen for the `lock_granted` event or poll `/events`.
7. **React to deadlocks** — the server automatically resolves deadlocks by
   selecting a victim. Handle the `deadlock_detected` event and retry.
8. **Prefer direct messages** (`send`) for targeted communication and
   `broadcast` for announcements to all agents.
9. **Use fencing tokens** to detect stale locks — each lock grant includes a
   monotonically increasing `fencing_token`.
