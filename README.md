<p align="center">
  <img src="assets/pluto.png" alt="Pluto Logo" width="280" />
</p>

<h1 align="center">Pluto</h1>

<p align="center">
  <strong>A coordination and messaging server for AI agents</strong><br>
  Resource locking · Agent discovery · Message routing · Deadlock detection
</p>

---

## What Is Pluto?

Pluto is a centralized coordination server built on Erlang/OTP that lets multiple AI agents share resources safely. It does **not** plan or assign tasks — that is the agent's job. Pluto exclusively handles:

- **Resource locking** — exclusive (write) and shared (read) locks with automatic expiration
- **Lease management** — every lock carries a TTL and must be renewed or it expires
- **Agent registry** — live directory of all connected agents
- **Message routing** — point-to-point and broadcast messaging between agents
- **Deadlock detection** — cycle detection in the wait-for graph with automatic victim selection
- **Fencing tokens** — monotonically increasing tokens that survive restarts, preventing stale writes
- **Event persistence** — append-only event log with queryable history

## Why Do You Need Pluto?

When multiple agents operate concurrently, they run into:

| Problem | Without Pluto | With Pluto |
|---------|--------------|------------|
| **File conflicts** | Two agents edit the same file at once | Exclusive lock ensures one writer at a time |
| **Resource races** | Agents compete for GPUs, ports, temp dirs | Locks with FIFO fairness prevent starvation |
| **Duplicate work** | Multiple agents attempt the same task | Agents discover peers and coordinate via messages |
| **Silent failures** | No way to know if a peer crashed | Heartbeat monitoring detects dead agents and releases their locks |
| **Deadlocks** | Circular waits cause permanent hangs | Automatic cycle detection breaks deadlocks immediately |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Pluto Server (Erlang/OTP)              │
│                                                          │
│  pluto_sup (supervisor)                                  │
│  ├── pluto_persistence    — state snapshots              │
│  ├── pluto_lock_mgr       — lock & lease management      │
│  ├── pluto_msg_hub        — agent registry & routing      │
│  ├── pluto_heartbeat      — liveness detection            │
│  └── pluto_listener_sup   — connection accept pool        │
│       ├── pluto_tcp_listener  — TCP sessions              │
│       │    └── pluto_session  — one process per conn      │
│       └── pluto_http_listener — REST/HTTP API             │
└──────────┬─────────────────────────┬────────────────────┘
           │  JSON over TCP (:9000)  │  REST/HTTP (:9001)
     ┌─────┼─────────┐              │
     │     │         │         ┌────┴────┐
┌────┴──┐ ┌┴──────┐ ┌┴──────┐ │Dashboard│
│Agent A│ │Agent B│ │Agent C│ │  / CLI  │
│(Python│ │(Python│ │ (any) │ │ (curl)  │
└───────┘ └───────┘ └───────┘ └─────────┘
```

Agents connect via **TCP** (port 9000, newline-delimited JSON) for persistent sessions with heartbeat and push events. A **REST/HTTP API** (port 9001) provides stateless access for dashboards, CLIs, and one-shot operations. A Python client library is included, but any language can participate.

## Quick Start

### 1. Install Dependencies

```bash
# macOS / Debian / Ubuntu — automated installer
./PlutoInstall.sh
```

Or install manually:
- **Erlang/OTP 26+** and **rebar3** (for the server)
- **Python 3.10+** (for the client)

### 2. Start the Server

```bash
# Foreground (logs to stdout)
./PlutoServer.sh

# Background daemon
./PlutoServer.sh --daemon

# Check status
./PlutoServer.sh --status

# Stop daemon
./PlutoServer.sh --kill
```

### 3. Use the Client

```bash
# Test connectivity
./PlutoClient.sh ping

# List connected agents
./PlutoClient.sh list

# Generate agent guide file
./PlutoClient.sh guide --output ./agent_guide.md
```

### 4. From Python Code

```python
from pluto_client import PlutoClient

with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
    # Acquire an exclusive lock
    lock_ref = client.acquire("file:/repo/src/model.py", ttl_ms=30000)

    # ... do work on the file ...

    # Release the lock
    client.release(lock_ref)

    # Send a message to another agent
    client.send("reviewer-2", {"type": "ready", "file": "model.py"})

    # Broadcast to all agents
    client.broadcast({"type": "build-complete", "status": "success"})

    # List connected peers
    peers = client.list_agents()
```

## Starting an Agent with Pluto

To build an agent that coordinates through Pluto, follow these steps:

### Step 1 — Start the server

```bash
./PlutoServer.sh --daemon      # start in background
./PlutoClient.sh ping           # verify it's reachable
```

### Step 2 — Generate the agent guide

```bash
./PlutoClient.sh guide --output agent_guide.md
```

This produces a comprehensive protocol reference your agent can use. Feed it to your AI agent as context or read it yourself.

### Step 3 — Connect and register

Open a TCP connection to `localhost:9000` and send:

```json
{"op": "register", "agent_id": "my-agent"}
```

The server responds with a session ID and the heartbeat interval:

```json
{"status": "ok", "session_id": "sess-...", "heartbeat_interval_ms": 15000}
```

### Step 4 — Coordinate

| What | How |
|------|-----|
| **Lock a resource** | `{"op": "acquire", "resource": "file:/src/main.py", "mode": "write", "ttl_ms": 30000}` |
| **Release a lock** | `{"op": "release", "lock_ref": "LOCK-42"}` |
| **Send a message** | `{"op": "send", "to": "other-agent", "payload": {...}}` |
| **Broadcast** | `{"op": "broadcast", "payload": {...}}` |
| **Stay alive** | Send `{"op": "ping"}` every 15 seconds |

### Step 5 — Use the Python client (optional)

If your agent is in Python, use the included client library:

```python
from pluto_client import PlutoClient

client = PlutoClient(host="localhost", port=9000, agent_id="my-agent")
client.connect()

lock = client.acquire("file:/shared/config.yaml", ttl_ms=30000)
# ... work with the file ...
client.release(lock)

client.send("peer-agent", {"status": "done"})
client.disconnect()
```

### Tips

- **Always release locks** when done — or they'll expire after the TTL.
- **Ping regularly** — the server kills sessions that go silent for 30 seconds.
- **Handle `lock_granted` events** — if a resource is busy, you'll get a `WAIT-*` reference and the lock arrives later as a push event.
- **Check `./PlutoClient.sh stats`** to monitor activity in real time.

## Protocol Reference

All communication uses newline-delimited JSON over TCP.

### Registration

```
→  {"op":"register","agent_id":"coder-1"}
←  {"status":"ok","session_id":"sess-a3f2...","heartbeat_interval_ms":15000}
```

### Lock Operations

```
→  {"op":"acquire","resource":"file:/src/main.py","mode":"write","agent":"coder-1","ttl_ms":30000}
←  {"status":"ok","lock_ref":"LOCK-42","fencing_token":17}

→  {"op":"renew","lock_ref":"LOCK-42","ttl_ms":30000}
←  {"status":"ok"}

→  {"op":"release","lock_ref":"LOCK-42"}
←  {"status":"ok"}
```

If a resource is already held, the server returns a wait reference and pushes a `lock_granted` event when the lock becomes available:

```
←  {"status":"wait","wait_ref":"WAIT-99"}
   ... later ...
←  {"event":"lock_granted","wait_ref":"WAIT-99","lock_ref":"LOCK-43","fencing_token":18}
```

### Lock Modes

| Mode | Behaviour |
|------|-----------|
| `write` | Exclusive — only one agent may hold it |
| `read` | Shared — multiple readers allowed if no writer holds it |

### Messaging

```
→  {"op":"send","from":"agent-A","to":"agent-B","payload":{"type":"review","file":"main.py"}}
←  {"status":"ok"}

→  {"op":"broadcast","from":"agent-A","payload":{"type":"done"}}
←  {"status":"ok"}
```

### Other Operations

| Operation | Request | Response |
|-----------|---------|----------|
| Ping | `{"op":"ping"}` | `{"status":"pong","ts":...,"heartbeat_interval_ms":15000}` |
| List agents | `{"op":"list_agents"}` | `{"status":"ok","agents":["a","b"]}` |
| Event history | `{"op":"event_history","since_token":40,"limit":50}` | `{"status":"ok","events":[...]}` |

### Server-Pushed Events

| Event | Description |
|-------|-------------|
| `lock_granted` | A queued lock request has been granted |
| `lock_expired` | A held lock expired (TTL elapsed without renewal) |
| `message` | Direct message from another agent |
| `broadcast` | Broadcast from another agent |
| `agent_joined` | A new agent connected |
| `agent_left` | An agent disconnected |
| `deadlock_detected` | Circular wait detected — victim is notified |
| `wait_timeout` | A queued lock request timed out |

## Resource Naming Convention

Resources are arbitrary strings. Recommended conventions:

```
file:/repo/src/model.py          # Source file
workspace:experiment-17           # Logical workspace
gpu:0                             # Hardware resource
artifact:build/output.bin         # Build artefact
port:8080                         # Network port
```

## HTTP API (REST)

Pluto exposes a lightweight REST API on port **9001** (configurable via `http_port`, set to `disabled` to turn off). All responses are JSON with CORS headers enabled.

### Health & Status

```bash
curl http://localhost:9001/health
# {"status":"ok","version":"0.1.0"}

curl http://localhost:9001/ping
# {"status":"pong","ts":1711234567890}
```

### Agents & Locks

```bash
# List connected agents
curl http://localhost:9001/agents
# {"status":"ok","agents":["coder-1","reviewer-2"]}

# List active locks
curl http://localhost:9001/locks
# {"status":"ok","locks":[{"lock_ref":"LOCK-1","resource":"file:/src/main.py","agent_id":"coder-1","mode":"write","fencing_token":5}]}
```

### Lock Operations

```bash
# Acquire a lock
curl -X POST -H "Content-Type: application/json" \
  -d '{"agent_id":"coder-1","resource":"file:/src/main.py","mode":"write","ttl_ms":30000}' \
  http://localhost:9001/locks/acquire
# {"status":"ok","lock_ref":"LOCK-42","fencing_token":17}

# Renew a lock
curl -X POST -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","ttl_ms":30000}' \
  http://localhost:9001/locks/renew
# {"status":"ok"}

# Release a lock
curl -X POST -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42","agent_id":"coder-1"}' \
  http://localhost:9001/locks/release
# {"status":"ok"}
```

### Events & Admin

```bash
# Query event history
curl "http://localhost:9001/events?since_token=0&limit=50"
# {"status":"ok","events":[...]}

# Get fencing sequence
curl http://localhost:9001/admin/fencing_seq
# {"status":"ok","fencing_seq":42}

# View deadlock wait graph
curl http://localhost:9001/admin/deadlock_graph
# {"status":"ok","edges":[]}

# Force-release a lock (admin)
curl -X POST -H "Content-Type: application/json" \
  -d '{"lock_ref":"LOCK-42"}' \
  http://localhost:9001/admin/force_release

# Run self-test
curl -X POST http://localhost:9001/selftest
```

### HTTP Endpoints Summary

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Server health check |
| `GET` | `/ping` | Ping with timestamp |
| `GET` | `/agents` | List connected agents |
| `GET` | `/locks` | List active locks |
| `POST` | `/locks/acquire` | Acquire a lock |
| `POST` | `/locks/release` | Release a lock |
| `POST` | `/locks/renew` | Renew lock TTL |
| `GET` | `/events` | Query event history |
| `GET` | `/admin/fencing_seq` | Current fencing sequence |
| `GET` | `/admin/deadlock_graph` | Wait-for graph edges |
| `POST` | `/admin/force_release` | Force-release a lock |
| `POST` | `/selftest` | Run built-in self-test |

## Configuration

Server configuration lives in `src_erl/config/sys.config`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `tcp_port` | `9000` | TCP protocol listening port |
| `http_port` | `9001` | HTTP API port (set to `disabled` to turn off) |
| `heartbeat_interval_ms` | `15000` | How often agents must ping |
| `heartbeat_timeout_ms` | `30000` | Declare dead after silence |
| `reconnect_grace_ms` | `30000` | Lock hold time after disconnect |
| `default_max_wait_ms` | `60000` | Max time in wait queue |
| `persistence_dir` | `/tmp/pluto/state` | State snapshot directory |
| `flush_interval` | `60000` | Snapshot frequency (ms) |
| `event_log_dir` | `/tmp/pluto/events` | Event log directory |
| `event_log_max_entries` | `100000` | Max events stored |
| `session_conflict_policy` | `strict` | `strict` rejects duplicate agent IDs; `takeover` replaces |

VM-level settings are in `src_erl/config/vm.args`:

```
-sname pluto
-setcookie pluto_secret
+P 1048576
+Q 65536
```

## Server Management

```bash
./PlutoServer.sh              # Build + start foreground
./PlutoServer.sh --daemon     # Build + start background
./PlutoServer.sh --kill       # Stop daemon
./PlutoServer.sh --status     # Check if running
./PlutoServer.sh --build      # Compile + release only
./PlutoServer.sh --clean      # Remove build artefacts
./PlutoServer.sh --console    # Interactive Erlang shell
```

Builds are placed under `/tmp/pluto/build` to keep the source tree clean. The release binary is at `/tmp/pluto/build/_build/default/rel/pluto/bin/pluto`.

## Project Structure

```
pluto/
├── PlutoServer.sh          # Server build & run wrapper
├── PlutoClient.sh          # Python client CLI wrapper
├── PlutoInstall.sh         # Automated installer (macOS / Debian / Ubuntu)
├── assets/
│   └── pluto.png           # Project logo
├── src_erl/                # Erlang server source
│   ├── src/                # Application modules (18 modules)
│   │   ├── pluto_app.erl           # OTP application entry
│   │   ├── pluto_sup.erl           # Top-level supervisor
│   │   ├── pluto_lock_mgr.erl      # Lock & lease management
│   │   ├── pluto_msg_hub.erl       # Agent registry & routing
│   │   ├── pluto_tcp_listener.erl   # TCP accept loop
│   │   ├── pluto_http_listener.erl  # REST/HTTP API
│   │   ├── pluto_session.erl       # Per-connection session
│   │   └── ...                      # 11 more modules
│   ├── include/            # Header files (.hrl)
│   ├── config/             # sys.config, vm.args
│   └── test/               # EUnit tests (58 tests)
├── src_py/                 # Python client
│   ├── pluto_client.py     # Client library + CLI
│   └── pluto_client_def.py # Client definitions
└── agent/                  # Agent integration docs
    └── README.md
```

## License

See [LICENSE](LICENSE) for details.
