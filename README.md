<p align="center">
  <img src="assets/pluto.png" alt="Pluto Logo" width="280" />
</p>

<h1 align="center">Pluto</h1>

<p align="center">
  <strong>Multi-Agent Coordination and Messaging Server</strong><br>
  Resource locking · Agent discovery · Message routing · Deadlock detection
</p>

<p align="center">
  <em>By using Pluto you accept the terms of the <a href="#disclaimer--liability">Disclaimer &amp; Liability</a> and <a href="CONSENT.md">CONSENT.md</a>.</em>
</p>

---

## What Is Pluto?

Pluto is a coordination server built on Erlang/OTP that gives multiple AI agents a shared infrastructure for safe concurrent operation. It provides resource locking with deadlock detection, lease-based ownership, fencing tokens, agent discovery, and inter-agent messaging.

It does **not** plan or assign tasks — that is the agent's job. Pluto exclusively handles:

- **Resource locking** — exclusive (write) and shared (read) locks with automatic expiration
- **Lease management** — every lock carries a TTL and must be renewed or it expires
- **Agent registry** — live directory of all connected agents
- **Message routing** — point-to-point and broadcast messaging between agents
- **Deadlock detection** — cycle detection in the wait-for graph with automatic victim selection
- **Fencing tokens** — monotonically increasing tokens that survive restarts, preventing stale writes
- **Event persistence** — append-only event log with queryable history

## Why Do You Need Pluto?

Most multi-agent frameworks today (LangGraph, CrewAI, AutoGen, OpenAI Agents SDK, Claude's agent SDK) focus on task planning, tool use, and orchestrating agent-to-agent calls. What they generally do not provide is a coordination layer for agents that run as separate processes — especially when those agents are heterogeneous (different models, runtimes, or languages).

Concretely, what is missing and what Pluto adds:

| Gap in current frameworks | What Pluto provides |
|--------------------------|---------------------|
| Agents share state through memory or files with no conflict prevention | Distributed resource locks (exclusive / shared) with FIFO fairness |
| No mechanism to detect or break circular waits between agents | Automatic deadlock detection with cycle analysis and victim selection |
| Locks held by a crashed agent are never released | Heartbeat monitoring releases locks when an agent goes silent |
| No protection against a stale agent writing after a lock was revoked | Monotonically increasing fencing tokens tied to each lock grant |
| Agents can only communicate via shared state or framework-internal calls | Language-agnostic TCP and HTTP messaging bus any runtime can join |
| No live view of which agents are running and what they hold | Agent registry with live discovery and event history |

Pluto does not replace orchestration frameworks — it complements them by providing the missing concurrency primitives that make multi-process, multi-runtime agent systems safe to run.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Pluto Server (Erlang/OTP)              │
│                                                          │
│  pluto_sup (supervisor)                                  │
│  ├── pluto_persistence    — state snapshots              │
│  ├── pluto_lock_mgr       — lock & lease management      │
│  ├── pluto_msg_hub        — agent registry & routing     │
│  ├── pluto_heartbeat      — liveness detection           │
│  └── pluto_listener_sup   — connection accept pool       │
│       ├── pluto_tcp_listener  — TCP sessions             │
│       │    └── pluto_session  — one process per conn     │
│       └── pluto_http_listener — REST/HTTP API            │
└──────────┬─────────────────────────┬────────────────────┘
           │  JSON over TCP (:9000)  │  REST/HTTP (:9001)
     ┌─────┼─────────┐         ┌────┴────────────────────┐
     │     │         │         │                          │
┌────┴──┐ ┌┴──────┐ ┌┴──────┐ │ ┌──────────────────────┐ │
│Agent A│ │Agent B│ │Agent C│ │ │  PlutoAgentFriend.sh  │ │
│(Python│ │(Python│ │ (any) │ │ │ ┌────────────────────┐│ │
└───────┘ └───────┘ └───────┘ │ │ │   Agent CLI (PTY)  ││ │
                               │ │ │ Claude / Copilot / ││ │
                               │ │ │  Aider / custom    ││ │
                               │ │ └─────────↑──────────┘│ │
                               │ │    stdin injection     │ │
                               │ └──────────────────────┘ │
                               │ ┌──────────────────────┐ │
                               │ │   Dashboard / CLI     │ │
                               │ │       (curl)          │ │
                               │ └──────────────────────┘ │
                               └──────────────────────────┘
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

### 3. Launch an Agent with PlutoAgentFriend

The simplest way to start a coordinated AI agent — wraps Claude Code, Copilot CLI, Aider, or any custom command with Pluto messaging automatically:

```bash
# Auto-detect installed agent framework
./PlutoAgentFriend.sh --agent-id coder-1

# Target a specific framework
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
./PlutoAgentFriend.sh --agent-id coder-1 --framework copilot

# Wrap a custom command
./PlutoAgentFriend.sh --agent-id worker-1 -- python3 my_agent.py
```

> See the [PlutoAgentFriend guide](docs/guide/pluto-agent-friend.md) for full options, injection modes, and advanced configuration.

### 4. Verify & Inspect with the Client

```bash
# Test connectivity
./PlutoClient.sh ping

# List connected agents
./PlutoClient.sh list

# Show live statistics
./PlutoClient.sh stats

# Generate agent guide file
./PlutoClient.sh guide --output ./agent_guide.md
```

> See the [PlutoClient guide](docs/guide/pluto-client.md) for all commands and options.

### 5. From Python Code

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

There are four ways to connect an agent to Pluto. **PlutoMCPFriend** is the smoothest path on Claude Code; **PlutoAgentFriend** is the universal fallback for any TUI agent without code changes.

| Method | Best for | Guide |
|--------|----------|-------|
| **PlutoMCPFriend.sh** | **Claude Code** — Pluto operations as native MCP tools, no curl, no token paste | [docs/guide/pluto-mcp-friend.md](docs/guide/pluto-mcp-friend.md) |
| **PlutoAgentFriend.sh** | Wrapping any other AI CLI (Cursor, Aider, Copilot, Claude…) via PTY injection — zero protocol code in your agent | [docs/guide/pluto-agent-friend.md](docs/guide/pluto-agent-friend.md) |
| **Python client library** | Custom Python agents | [docs/guide/tcp-connection.md](docs/guide/tcp-connection.md) |
| **Raw TCP / HTTP** | Any language, maximum control | [docs/guide/tcp-connection.md](docs/guide/tcp-connection.md) |

### Recommended — PlutoAgentFriend.sh

```bash
# 1. Start the server
./PlutoServer.sh --daemon
./PlutoClient.sh ping

# 2. Launch your agent wrapped by Pluto
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
```

PlutoAgentFriend registers the agent, injects incoming Pluto messages as natural-language prompts when the agent is idle, and handles all heartbeating — no protocol changes needed in your agent.

> Full reference: [docs/guide/pluto-agent-friend.md](docs/guide/pluto-agent-friend.md)

### Alternative — Python client library

If your agent is written in Python, use the built-in client:

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

> Full reference: [docs/guide/tcp-connection.md](docs/guide/tcp-connection.md)

### Alternative — Raw TCP / HTTP

Any language can talk to Pluto directly. Open a TCP connection to `localhost:9000` and send newline-delimited JSON:

```json
{"op": "register", "agent_id": "my-agent"}
```

| What | How |
|------|-----|
| **Lock a resource** | `{"op": "acquire", "resource": "file:/src/main.py", "mode": "write", "ttl_ms": 30000}` |
| **Release a lock** | `{"op": "release", "lock_ref": "LOCK-42"}` |
| **Send a message** | `{"op": "send", "to": "other-agent", "payload": {...}}` |
| **Broadcast** | `{"op": "broadcast", "payload": {...}}` |
| **Stay alive** | `{"op": "ping"}` every 15 seconds |

> Full reference: [docs/guide/tcp-connection.md](docs/guide/tcp-connection.md)

### Tips

- **Always release locks** when done — or they'll expire after the TTL.
- **Ping regularly** — the server kills sessions that go silent for 30 seconds.
- **Handle `lock_granted` events** — if a resource is busy, you'll get a `WAIT-*` reference and the lock arrives later as a push event.
- **Check `./PlutoClient.sh stats`** to monitor activity in real time.

## PlutoAgentFriend — Agent Wrapper

> **Guide:** [docs/guide/pluto-agent-friend.md](docs/guide/pluto-agent-friend.md)

PlutoAgentFriend wraps any AI agent CLI (Claude, Copilot, Aider, etc.) in a PTY proxy that transparently injects Pluto messages when the agent is idle. No protocol changes needed — the agent sees natural-language input.

### Architecture & Design

```
User terminal  ←→  TerminalProxy  ←→  Agent CLI (via PTY)
                        │
                AgentStateDetector   (parses stdout → BUSY / ASKING / READY)
                        │
                PlutoConnection      (long-polls Pluto server for messages)
                        │
                MessageFormatter     (formats messages as natural-language)
                        │
                InjectionGate        (decides when/how to inject)
```

**Class hierarchy** (`src_py/agent_friend/pluto_agent_friend.py`):

| Class | Responsibility |
|-------|---------------|
| `TerminalProxy` | Low-level PTY management, raw mode, non-blocking buffered I/O loop (modelled after Python's `pty.spawn()`). Sets master fd to non-blocking so writes never stall the event loop. |
| `AgentStateDetector` | Watches agent output and classifies state: **BUSY** (producing output), **ASKING_USER** (waiting for human answer), or **READY** (idle, safe to inject). Uses configurable regex patterns + silence timeout. |
| `MessageFormatter` | Converts Pluto protocol messages (JSON dicts) into natural-language prompts any LLM agent can process. Handles message, broadcast, task_assigned, topic_message, and unknown event types. |
| `PlutoConnection` | Manages HTTP session with Pluto: register, background long-poll, thread-safe message queue, graceful unregister. |
| `PlutoAgentFriend` | Top-level orchestrator (inherits `TerminalProxy`). Composes all the above, owns the `run()` lifecycle, and coordinates injection timing. |

### How It Works

1. `PlutoAgentFriend.run()` prints a banner and connects to Pluto (or continues standalone)
2. The agent CLI is spawned inside a PTY via `TerminalProxy.spawn()`
3. The terminal is set to raw mode and the non-blocking I/O copy loop starts
4. A background thread long-polls Pluto for incoming messages (`PlutoConnection`)
5. An injection thread checks `AgentStateDetector.is_ready_for_injection()` every 0.5 s
6. When ready, `MessageFormatter.format()` converts messages to text and `inject_input()` enqueues it

### Injection Modes

| Mode | Behaviour |
|------|-----------|
| `auto` | Inject as soon as the agent is idle (default) |
| `confirm` | Show notification, auto-inject after 10 seconds if still idle |
| `manual` | Show notification only — user handles input |

### Quick Start

```bash
# Auto-detect installed agent framework
./PlutoAgentFriend.sh --agent-id coder-1

# Use a specific framework
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
./PlutoAgentFriend.sh --agent-id coder-1 --framework copilot

# Custom agent command
./PlutoAgentFriend.sh --agent-id coder-1 -- python3 my_agent.py

# Confirm mode (show before injecting)
./PlutoAgentFriend.sh --agent-id reviewer-1 --framework claude --mode confirm

# Full help
./PlutoAgentFriend.sh --help
```

The wrapper reads Pluto server settings from `config/pluto_config.json` automatically.

### Safety Rules

- **User always wins** — user input takes priority over injections; the non-blocking I/O loop never stalls on writes
- **No injection during questions** — if the agent is asking the user something, messages are held
- **Transparency** — every injection is announced with `[pluto-friend]` in the terminal
- **No injection while typing** — a 5-second cooldown after the last keystroke prevents interrupting the user

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

> **Guide:** [docs/guide/pluto-server.md](docs/guide/pluto-server.md)

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
├── PlutoAgentFriend.sh     # Agent wrapper — injects Pluto messages into agent CLIs
├── PlutoInstall.sh         # Automated installer (macOS / Debian / Ubuntu)
├── config/
│   └── pluto_config.json   # Server IP/port configuration
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
│   ├── pluto_client_def.py # Client definitions
│   └── agent_friend/       # Agent integration tools
│       ├── pluto_agent_friend.py # PTY wrapper (used by PlutoAgentFriend.sh)
│       ├── agent_wrapper.py      # Copilot agent launcher
│       └── flow_runner.py        # JSON flow executor
└── tests/                  # Integration & demo tests
```

## Disclaimer & Liability

> **Read this before using Pluto.**

Pluto is provided **as-is**, without warranty of any kind — express or implied — including but not limited to warranties of merchantability, fitness for a particular purpose, or non-infringement.

**The maintainers and developers of this repository bear no responsibility or liability** of any kind — direct, indirect, incidental, special, exemplary, or consequential — for any damages, losses, security incidents, data corruption, system damage, or any other harm arising from the use, misuse, or inability to use this software.

**You, the user, are solely and fully responsible for:**

- Any harm, damage, data loss, or security incident caused by running Pluto
- Granting consent to AI agents to receive injected messages
- Running automated injections into agent input streams
- Exposing the Pluto message bus to networks you do not fully control
- Coordinating AI agents that take destructive, irreversible, or harmful actions on your systems
- Any wrong, unauthorized, or unintended usage of Pluto

### Purpose & Intent

Pluto is built with entirely **positive intentions**, solely for **research and development** in legitimate multi-agent AI coordination scenarios. The code injection capability — writing messages directly into an AI agent's input stream — is a **powerful and potentially dangerous action**. Users must carefully inspect and understand what Pluto does before enabling it.

The maintainers and developers of Pluto have **no malicious intent**. This project exists for beneficial, experimental, and research purposes only. Nevertheless, this tool can cause unintended effects if misused, and you are responsible for how you use it.

Use Pluto only in environments you own and control. Do not use it on production systems, shared machines, or with agents that have access to sensitive resources, unless you fully understand the risks.

---

## License

See [LICENSE](LICENSE) for details.

## Citation

Pluto is developed by David Leon. Academic researchers can use Pluto for
free, provided they cite this repository.

If you use Pluto in your research, please cite it as:

```bibtex
@software{leon_pluto,
  author  = {Leon, David},
  title   = {Pluto: A coordination and messaging server for AI agents},
  url     = {https://github.com/leondavi/pluto},
  version = {0.2.42},
  year    = {2026}
}
```

A machine-readable [CITATION.cff](CITATION.cff) file is also provided —
GitHub renders a "Cite this repository" button in the sidebar.

