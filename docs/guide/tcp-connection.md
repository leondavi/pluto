# TCP Connection & Python Library Guide

This guide covers connecting to Pluto directly — either via **raw TCP** (any
language) or via the **Python client library** included in `src_py/`.

Both approaches give you full control over every protocol operation.

---

## Ports

| Transport | Default port | Use case |
|-----------|-------------|----------|
| TCP (newline-delimited JSON) | `9000` | Persistent agents with heartbeat + push events |
| HTTP (REST JSON) | `9001` | One-shot operations, dashboards, CLI tools |

---

## Raw TCP Protocol

All messages are newline-delimited JSON (`\n` terminated). Connect with any
language that can open a TCP socket.

### 1. Register

```json
→  {"op": "register", "agent_id": "my-agent"}
←  {"status": "ok", "session_id": "sess-a3f2...", "heartbeat_interval_ms": 15000}
```

### 2. Stay Alive — Ping

Send a ping at least every `heartbeat_interval_ms` milliseconds (default 15 s).
The server disconnects sessions silent for 30 s.

```json
→  {"op": "ping"}
←  {"status": "pong", "ts": 1711234567890, "heartbeat_interval_ms": 15000}
```

### 3. Acquire a Lock

```json
→  {"op": "acquire", "resource": "file:/src/main.py", "mode": "write", "agent": "my-agent", "ttl_ms": 30000}
←  {"status": "ok", "lock_ref": "LOCK-42", "fencing_token": 17}
```

If the resource is already held:

```json
←  {"status": "wait", "wait_ref": "WAIT-99"}
   ... later (server push) ...
←  {"event": "lock_granted", "wait_ref": "WAIT-99", "lock_ref": "LOCK-43", "fencing_token": 18}
```

### 4. Renew a Lock

```json
→  {"op": "renew", "lock_ref": "LOCK-42", "ttl_ms": 30000}
←  {"status": "ok"}
```

### 5. Release a Lock

```json
→  {"op": "release", "lock_ref": "LOCK-42"}
←  {"status": "ok"}
```

### 6. Send a Message

```json
→  {"op": "send", "from": "agent-A", "to": "agent-B", "payload": {"type": "review", "file": "main.py"}}
←  {"status": "ok"}
```

### 7. Broadcast to All Agents

```json
→  {"op": "broadcast", "from": "agent-A", "payload": {"type": "done"}}
←  {"status": "ok"}
```

### 8. List Agents

```json
→  {"op": "list_agents"}
←  {"status": "ok", "agents": ["agent-A", "agent-B"]}
```

### 9. Query Event History

```json
→  {"op": "event_history", "since_token": 40, "limit": 50}
←  {"status": "ok", "events": [...]}
```

---

## Lock Modes

| Mode | Behaviour |
|------|-----------|
| `write` | Exclusive — one holder at a time |
| `read` | Shared — multiple readers allowed when no writer holds it |

---

## Server-Pushed Events

The server pushes events without a request. Read them from the socket continuously.

| Event | Description |
|-------|-------------|
| `lock_granted` | A queued lock request has been granted |
| `lock_expired` | A held lock expired (TTL elapsed) |
| `message` | Direct message from another agent |
| `broadcast` | Broadcast from another agent |
| `agent_joined` | A new agent connected |
| `agent_left` | An agent disconnected |
| `deadlock_detected` | Circular wait detected — victim notified |
| `wait_timeout` | A queued lock request timed out |

---

## Resource Naming Convention

Resources are arbitrary strings. Recommended conventions:

```
file:/repo/src/model.py          # Source file
workspace:experiment-17           # Logical workspace
gpu:0                             # Hardware resource
artifact:build/output.bin         # Build artefact
port:8080                         # Network port
```

---

## Python Client Library

The library at `src_py/pluto_client.py` wraps the TCP protocol.

### Basic Usage

```python
from pluto_client import PlutoClient

client = PlutoClient(host="localhost", port=9000, agent_id="my-agent")
client.connect()

# Acquire an exclusive lock
lock_ref = client.acquire("file:/repo/src/model.py", ttl_ms=30000)

# ... do work ...

# Release the lock
client.release(lock_ref)

# Send a message to another agent
client.send("reviewer-2", {"type": "ready", "file": "model.py"})

# Broadcast to all agents
client.broadcast({"type": "build-complete", "status": "success"})

# List connected peers
peers = client.list_agents()

client.disconnect()
```

### Context Manager

```python
with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
    lock_ref = client.acquire("file:/src/main.py", ttl_ms=30000)
    # ... work ...
    client.release(lock_ref)
    client.send("reviewer-2", {"type": "done"})
```

The context manager calls `disconnect()` automatically on exit.

---

## HTTP API (Quick Reference)

The HTTP API at port 9001 is suitable for one-shot operations and dashboards.

```bash
# Health check
curl http://localhost:9001/health

# List agents
curl http://localhost:9001/agents

# List active locks
curl http://localhost:9001/locks

# Acquire a lock
curl -X POST -H "Content-Type: application/json" \
  -d '{"agent_id":"coder-1","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}' \
  http://localhost:9001/locks/acquire

# Release a lock
curl -X POST -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","agent_id":"coder-1"}' \
  http://localhost:9001/locks/release

# Query event history
curl "http://localhost:9001/events?since_token=0&limit=50"

# Register via HTTP (returns a session token)
curl -X POST -H "Content-Type: application/json" \
  -d '{"agent_id":"my-agent","mode":"http","ttl_ms":300000}' \
  http://localhost:9001/agents/register

# Heartbeat (keep HTTP session alive)
curl -X POST -H "Content-Type: application/json" \
  -d '{"token":"YOUR-TOKEN"}' \
  http://localhost:9001/agents/heartbeat

# Poll for messages (HTTP agents)
curl "http://localhost:9001/agents/poll?token=YOUR-TOKEN"
```

### Full HTTP Endpoint Table

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Server health check |
| `GET` | `/ping` | Ping with timestamp |
| `GET` | `/agents` | List connected agents |
| `GET` | `/locks` | List active locks |
| `POST` | `/locks/acquire` | Acquire a lock |
| `POST` | `/locks/release` | Release a lock |
| `POST` | `/locks/renew` | Renew lock TTL |
| `POST` | `/agents/register` | HTTP agent registration |
| `POST` | `/agents/heartbeat` | Renew HTTP session |
| `GET` | `/agents/poll` | Poll for queued messages |
| `GET` | `/events` | Query event history |
| `GET` | `/admin/fencing_seq` | Current fencing sequence |
| `GET` | `/admin/deadlock_graph` | Wait-for graph edges |
| `POST` | `/admin/force_release` | Force-release a lock (admin) |
| `POST` | `/selftest` | Run built-in self-test |

---

## Tips

- **Always release locks** — or they expire after the TTL (default 30 s).
- **Ping every 10–15 s** — the server kills sessions silent for 30 s.
- **Handle `lock_granted` push events** — if a resource is busy, poll the
  socket after receiving `wait`; the lock arrives asynchronously.
- **Use fencing tokens** to detect stale writes: reject writes with a token
  lower than the last seen value.

---

## See Also

- [PlutoAgentFriend.sh guide](pluto-agent-friend.md) — zero-code agent wrapping
- [PlutoClient.sh guide](pluto-client.md) — CLI for inspection and registration
- [PlutoServer.sh guide](pluto-server.md) — server lifecycle
- [Pluto guide index](index.md)
