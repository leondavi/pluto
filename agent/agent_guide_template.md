# Pluto Agent Guide
Generated: {{generated_at}}

This file was written automatically by Pluto on startup.
Read it from top to bottom. By the end you will be registered and ready to coordinate.

---

## Connection Details

| Field    | Value        |
|----------|--------------|
| Host     | {{host}}     |
| Port     | {{port}}     |
| Protocol | Newline-delimited JSON over TCP |

Open one persistent TCP connection. Every request is a single JSON object followed
by a newline (`\n`). Every response or server-pushed event is also a single JSON
object followed by a newline.

---

## Step 1 — Choose Your Identity

Pick a stable, unique name for yourself (your `agent_id`). Use something that
describes your role, for example:

- `coder-1`
- `reviewer-main`
- `tester-gpu-0`

You will use the same `agent_id` on every reconnect. If you crash and come back
within the grace period your active locks are transferred to your new connection
automatically.

---

## Step 2 — Register

Send this immediately after opening the socket (replace `your-agent-id` with the
name you chose):

```json
{"op":"register","agent_id":"your-agent-id"}
```

Pluto responds with a server-generated session ID and the required heartbeat interval:

```json
{"status":"ok","session_id":"sess-a3f2b1c4-7e91-4b2d-8f3a-0d5e6c7b8a9f","heartbeat_interval_ms":15000}
```

Log the `session_id` for debugging. All subsequent requests on this connection are
automatically bound to this session — you do not need to include it in future messages.

If your `agent_id` is already taken (strict mode):

```json
{"status":"error","reason":"already_registered"}
```

---

## Step 2.5 — Keep Sending Heartbeats

Pluto monitors whether you are still alive. You must send a `ping` at least once
every `heartbeat_interval_ms / 2` milliseconds. Any message you send (acquire,
release, list_agents, etc.) also resets the liveness timer, so busy agents do not
need to ping separately.

```json
{"op":"ping"}
```

Response:
```json
{"status":"pong","ts":1743249600000,"heartbeat_interval_ms":15000}
```

If you go silent for longer than `heartbeat_interval_ms`, Pluto declares your session
dead, enters a grace period, and — if you do not reconnect in time — releases your
locks and unblocks the next waiting agents.

---

## Step 3 — Acquire a Lock Before Touching a Shared Resource

Never read or write a shared resource without holding a lock on it.

```json
{"op":"acquire","resource":"file:/repo/src/model.erl","mode":"write","agent":"your-agent-id","ttl_ms":30000}
```

**Granted immediately (includes a fencing token):**
```json
{"status":"ok","lock_ref":"LOCK-123","fencing_token":42}
```

The `fencing_token` is a monotonically increasing integer that you should embed in
any write you make to the guarded resource (as a header, version field, or metadata).
This prevents stale writes if your lease expires and another agent takes over while you
are paused.

**Queued (another agent holds it):**
```json
{"status":"wait","wait_ref":"WAIT-44"}
```
Keep the `wait_ref`. Pluto will push a `lock_granted` event to you when it is your
turn (see [Async Events](#async-events) below). The event includes the fencing token.

**Deadlock detected (you are the victim):**
```json
{"status":"error","reason":"deadlock","victim":true}
```
Retry with a different ordering or wait for the cycle to resolve.

**Wait timeout exceeded:**
```json
{"status":"error","reason":"wait_timeout"}
```

**Error (conflict, no queuing):**
```json
{"status":"error","reason":"conflict"}
```

### Lock modes
- `"write"` — exclusive. Only one agent may hold this at a time.
- `"read"` — shared. Multiple readers allowed, no writers.

### TTL and max wait
The `ttl_ms` field is the lease duration in milliseconds. The lock expires
automatically if you do not renew it. Call `renew` before the TTL runs out:

```json
{"op":"renew","lock_ref":"LOCK-123","ttl_ms":30000}
```

To limit how long you will wait in a queue, add `max_wait_ms`:

```json
{"op":"acquire","resource":"file:/repo/src/model.erl","mode":"write","agent":"your-agent-id","ttl_ms":30000,"max_wait_ms":10000}
```

---

## Step 4 — Release the Lock When Done

```json
{"op":"release","lock_ref":"LOCK-123"}
```

Response:
```json
{"status":"ok"}
```

Pluto will immediately notify the next queued agent that the lock is theirs.

---

## Step 5 — Communicate With Other Agents

### Direct message (one agent to one agent)

```json
{"op":"send","from":"your-agent-id","to":"reviewer-2","payload":{"type":"ready","file":"model.erl"}}
```

Response to you:
```json
{"status":"ok"}
```

Event pushed to `reviewer-2`:
```json
{"event":"message","from":"your-agent-id","payload":{"type":"ready","file":"model.erl"}}
```

### Broadcast (to all connected agents)

```json
{"op":"broadcast","from":"your-agent-id","payload":{"type":"event","name":"build-complete"}}
```

Response to you:
```json
{"status":"ok"}
```

Event pushed to every other agent:
```json
{"event":"broadcast","from":"your-agent-id","payload":{"type":"event","name":"build-complete"}}
```

---

## Step 6 — Discover Peers

```json
{"op":"list_agents"}
```

```json
{"status":"ok","agents":["coder-1","reviewer-2","tester-3"]}
```

---

## Async Events

Pluto pushes events to your socket at any time, interleaved with responses.
Check the `"event"` field to identify them.

| Event | When it arrives |
|---|---|
| `lock_granted` | A lock you were waiting for is now yours. Includes `fencing_token`. |
| `lock_expired` | One of your locks ran out of TTL before you released it. |
| `lock_released` | A lock you held was released (confirmation). |
| `wait_timeout` | A queued request exceeded `max_wait_ms` and was cancelled. |
| `deadlock_detected` | A wait cycle was detected; another agent was the victim; you may retry. |
| `message` | Another agent sent you a direct message. |
| `broadcast` | Another agent broadcast an event to everyone. |
| `agent_joined` | A new agent registered. |
| `agent_left` | An agent disconnected. |

### Examples

```json
{"event":"lock_granted","wait_ref":"WAIT-44","lock_ref":"LOCK-125","fencing_token":43,"resource":"file:/repo/src/model.erl"}
{"event":"lock_expired","lock_ref":"LOCK-123","resource":"file:/repo/src/model.erl"}
{"event":"wait_timeout","wait_ref":"WAIT-44","resource":"file:/repo/src/model.erl"}
{"event":"deadlock_detected","agents":["your-agent-id","other-agent"],"victim":"other-agent"}
{"event":"message","from":"coder-1","payload":{"type":"ready","file":"model.erl"}}
{"event":"agent_joined","agent_id":"tester-3"}
{"event":"agent_left","agent_id":"reviewer-2"}
```

---

## Error Reference

All errors follow this shape:

```json
{"status":"error","reason":"<reason>"}
```

| Reason | Meaning |
|---|---|
| `bad_request` | Missing or invalid fields in your request. |
| `unknown_op` | The `op` field is not a recognized operation. |
| `unknown_target` | The `to` agent is not currently connected. |
| `conflict` | Lock conflict and queueing is disabled. |
| `not_found` | The `lock_ref` or `wait_ref` does not exist. |
| `expired` | The lock TTL elapsed before this operation. |
| `wait_timeout` | Your queued request exceeded `max_wait_ms`. |
| `deadlock` | A wait cycle was detected; you were chosen as the victim. Retry. |
| `already_registered` | Your `agent_id` is already active (strict mode). |
| `unauthorized` | Auth token missing, invalid, or lacks permission for this resource. |
| `internal_error` | Unexpected server error. |

---

## Python Agents

If you are a Python agent, you do not need to manage sockets directly.
Use `pluto_client.py` (in the same directory as this file):

```python
from pluto_client import PlutoClient

with PlutoClient(host="{{host}}", port={{port}}, agent_id="your-agent-id") as client:
    lock_ref = client.acquire("file:/repo/src/model.erl", ttl_ms=30000)
    # ... do work ...
    client.release(lock_ref)
    client.send("reviewer-2", {"type": "done"})
```

---

## Resource Naming Convention

Resources are plain strings. Pick a scheme that makes sense for your team:

- Files: `file:/absolute/path/to/file.py`
- Workspaces: `workspace:experiment-17`
- Hardware: `gpu:0`, `port:8080`
- Artifacts: `artifact:build/output.bin`

All agents must use the same string for the same resource. Pluto normalizes
strings internally but does not interpret them.

---

## Quick Reference

| Operation | Minimum required fields |
|---|---|
| Register | `op`, `agent_id` (+ `token` if auth enabled) |
| Ping | `op` |
| Acquire | `op`, `resource`, `mode`, `agent`, `ttl_ms` (+ optional `max_wait_ms`) |
| Release | `op`, `lock_ref` |
| Renew | `op`, `lock_ref`, `ttl_ms` |
| Send | `op`, `from`, `to`, `payload` |
| Broadcast | `op`, `from`, `payload` |
| List agents | `op` |
| Event history | `op`, `since_token`, `limit` |
