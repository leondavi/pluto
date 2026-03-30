# Pluto

## Quick Start for Agents

When Pluto starts, it writes an `agent_guide.md` file to its working directory. This file contains the server's address, the full protocol, and copy-paste-ready examples with the correct host and port already filled in.

**To onboard an agent, point it to that file:**

> Read `agent_guide.md` in the Pluto working directory. Follow the registration steps, then use the protocol described there to coordinate with other agents.

The agent reads the guide, registers itself, and knows how to acquire locks, send messages, and list peers — no additional configuration required.

If you are writing a Python agent and do not want to manage raw TCP yourself, use the bundled `pluto_client.py` instead. See [Connecting Without Raw TCP](#connecting-without-raw-tcp) below.

---

## What is Pluto?

Pluto is a centralized coordination and messaging server for AI agents, built on Erlang/OTP.

It acts as a **single coordination point** when multiple agents run concurrently and need to share resources without stepping on each other. Pluto does not plan tasks, reason, or make decisions — that is the agent's job. Pluto is solely responsible for:

- Tracking which agent currently owns a shared resource.
- Managing who is waiting for a resource.
- Knowing which agents are connected at any given moment.
- Routing messages between agents.
- Expiring and renewing resource leases.
- Recovering to a consistent state after a crash or restart.

## What Does Pluto Allow?

Without a coordination layer, concurrent agents can:

- Edit the same file simultaneously, producing conflicts.
- Duplicate work on the same task.
- Race for limited resources like GPU slots, ports, or build artifacts.
- Have no reliable way to notify each other when work starts, completes, or fails.

Pluto eliminates these problems by giving agents a shared protocol they can all speak to:

| Capability | Description |
|---|---|
| **Resource locking** | An agent acquires an exclusive (or shared) lock on a named resource before touching it. Others wait or are rejected until the lock is released. |
| **Lease management** | Every lock carries a TTL. Locks expire automatically if an agent crashes and does not renew. |
| **Agent registry** | Pluto maintains a live list of connected agents so any agent can discover its peers. |
| **Direct messaging** | One agent can send a structured message to a specific peer through Pluto. |
| **Broadcast** | An agent can broadcast an event to all currently connected agents at once. |
| **Session identity** | Pluto distinguishes between a logical `agent_id` (stable, operator-chosen) and a `session_id` (per-connection, server-generated), so agents can reconnect and reclaim their identity and locks. |

## How an End User Coordinates Agents with Pluto

Pluto speaks **line-delimited JSON over TCP**. Any agent that can open a TCP socket and read/write JSON can connect.

### 1. Start Pluto

Run the Pluto server on a known host and port before launching any agents:

```bash
# From the project root
rebar3 shell
```

### 2. Register Each Agent

When an agent starts, it connects and declares its identity:

```json
{"op":"register","agent_id":"coder-1"}
```

Pluto responds with a session ID:

```json
{"status":"ok","session_id":"sess-a3f2b1c4-..."}
```

From this point, `coder-1` is visible to every other connected agent.

### 3. Acquire a Lock Before Touching a Shared Resource

Before any agent reads or writes a shared resource, it asks Pluto for a lock:

```json
{"op":"acquire","resource":"file:/repo/src/model.erl","mode":"write","agent":"coder-1","ttl_ms":30000}
```

- If the resource is free, Pluto grants the lock immediately:
  ```json
  {"status":"ok","lock_ref":"LOCK-123"}
  ```
- If another agent holds it, Pluto puts the requester in a wait queue:
  ```json
  {"status":"wait","wait_ref":"WAIT-44"}
  ```

The agent holds the `lock_ref` and uses it to release or renew the lock when done.

### 4. Release the Lock When Done

```json
{"op":"release","lock_ref":"LOCK-123"}
```

Pluto releases the lock and automatically notifies the next waiting agent:

```json
{"event":"lock_granted","wait_ref":"WAIT-44","lock_ref":"LOCK-125","resource":"file:/repo/src/model.erl"}
```

### 5. Communicate Between Agents

Agents can send targeted messages to each other without building their own routing:

```json
{"op":"send","from":"coder-1","to":"reviewer-2","payload":{"type":"ready","file":"model.erl"}}
```

Or broadcast to everyone:

```json
{"op":"broadcast","from":"coder-1","payload":{"type":"event","name":"build-complete"}}
```

### 6. Discover Peers

At any time, an agent can ask who else is online:

```json
{"op":"list_agents"}
```

```json
{"status":"ok","agents":["coder-1","reviewer-2","tester-3"]}
```

---

## A Complete Example Flow

```
coder-1  connects and registers.
reviewer-2 connects and registers.

coder-1  → acquire lock on file:/repo/src/model.erl (granted, LOCK-123)
reviewer-2 → acquire lock on file:/repo/src/model.erl (waiting, WAIT-44)

coder-1  → sends message to reviewer-2: "I am editing model.erl"
coder-1  → finishes edits, releases LOCK-123

Pluto    → notifies reviewer-2: lock_granted (LOCK-125)
reviewer-2 → begins review with exclusive access
reviewer-2 → releases LOCK-125 when done
```

---

## Resources Are Just Strings

Pluto does not know or care what a resource actually is. Any unique string works:

- `file:/repo/src/model.erl`
- `workspace:experiment-17`
- `gpu:0`
- `artifact:build/output.bin`

The agents and their operators define the naming convention. Pluto enforces access.

---

## Coordination Guarantees

### Liveness detection and dead agent cleanup

Pluto continuously monitors connected agents. Each agent must send a `{"op":"ping"}` at regular intervals (the required interval is advertised in the register response). If an agent goes silent — due to a crash, network partition, or hang — the liveness sweeper declares it dead, terminates its session, and releases its locks so waiting agents are unblocked. Locks are not released instantly; they enter a short grace period first, giving the agent a chance to reconnect and reclaim them.

### Deadlock prevention

When two agents each hold a lock the other needs, they create a cycle that neither can break alone. Pluto detects this automatically by maintaining a wait-for graph. On every lock request that enters a queue, Pluto checks for a cycle. If one is found, a victim is chosen (the most recent waiter) and its request is rejected with a `deadlock` error. The other agents in the cycle receive an event and can proceed.

Operators can also set a `max_wait_ms` on any acquire request to cap queuing time independently of deadlock detection:

```json
{"op":"acquire","resource":"file:/repo/a.txt","mode":"write","agent":"coder-1","ttl_ms":30000,"max_wait_ms":10000}
```

### Fencing tokens

Even when a lock is held correctly, an agent that pauses — due to garbage collection, CPU starvation, or a slow tool call — can resume after its lease has already expired and another agent has taken over. Without a guard, both agents would write concurrently.

Every lock grant includes a monotonically increasing fencing token that survives restarts:

```json
{"status":"ok","lock_ref":"LOCK-123","fencing_token":42}
```

Agents embed this token when writing to guarded resources. The resource (a file gatekeeper, database, or shared store) rejects writes carrying a token lower than the last accepted one, making stale writes safe to ignore.

### Fairness

Wait queues are strict FIFO per resource. The agent that has been waiting longest is always granted the lock next. No agent can be skipped or starved. When a write lock is released and multiple readers are queued ahead of the next writer, all of them are granted simultaneously.

### Durable event history

Pluto writes an append-only log of all coordination events — lock acquisitions, releases, expirations, messages, deadlocks, agent joins and leaves — to disk. Agents can query recent history after a reconnect:

```json
{"op":"event_history","since_token":40,"limit":50}
```

This gives agents context about what happened while they were disconnected.

### Observability

Pluto exposes Prometheus-compatible metrics (locks active, wait queue depth, deadlocks detected, fencing token value, heartbeat timeouts, messages routed) on an optional HTTP endpoint. All significant events are also emitted as structured JSON log lines. An admin API provides live introspection and force-release operations.

### Permissions and policy

Agents authenticate with a per-agent bearer token in the register step. Resource access is governed by ACL rules that map agent ID patterns to resource prefixes and allowed lock modes:

- A coder agent can read/write source files but not artifacts.
- A reviewer agent can read source files but not write.
- A tester agent can read/write its own workspace.

Denied requests receive `{"status":"error","reason":"unauthorized"}`. All authentication failures and admin actions are written to a separate audit log.

---

When Pluto starts it writes a file named `agent_guide.md` into its working directory (configurable via `guide_path` in `sys.config`). The file is regenerated on every start so the host, port, and any example agent IDs always reflect the live server.

**What is inside `agent_guide.md`:**

- The exact server address and port the agent must connect to.
- A step-by-step registration walkthrough.
- The full operation reference with copy-paste JSON examples.
- The complete list of async events Pluto can push to the agent.
- Error codes and what they mean.

**Directing an agent to the file:**

Include a line like the following in your agent's system prompt or task description:

> Before doing any shared work, read the file at `/path/to/pluto/agent_guide.md` and follow the registration steps. Use the protocol described there for all resource coordination.

After reading the file the agent knows everything it needs: where to connect, its agent ID, and how every operation works. No additional glue code or human explanation is required.

**Example of a generated `agent_guide.md` header:**

```markdown
# Pluto Agent Guide
Generated: 2026-03-29T08:00:00Z

## Connection
- Host: localhost
- Port: 9000
- Protocol: newline-delimited JSON over TCP

## Your Identity
Choose a stable agent_id (e.g. `coder-1`). You will present this name when you register.
If you crash and reconnect with the same agent_id within the grace period, your active
locks are transferred to your new session automatically.
...
```

---

## Connecting Without Raw TCP

Some agents — particularly those running as Python scripts or inside LLM tool loops — cannot or should not manage a raw TCP socket directly. Pluto ships a self-contained Python client, `pluto_client.py`, that handles the TCP connection, JSON encoding, registration, and async event dispatch for you.

### Installation

No package installation needed. Copy `pluto_client.py` next to your agent script.

### Basic usage

```python
from pluto_client import PlutoClient

client = PlutoClient(host="localhost", port=9000, agent_id="coder-1")
client.connect()  # opens TCP connection and registers automatically

# Acquire an exclusive lock
lock_ref = client.acquire("file:/repo/src/model.erl", ttl_ms=30000)

# ... do work on the file ...

# Release when done
client.release(lock_ref)

# Send a message to another agent
client.send("reviewer-2", {"type": "ready", "file": "model.erl"})

# Broadcast to everyone
client.broadcast({"type": "event", "name": "build-complete"})

# Discover peers
peers = client.list_agents()

client.disconnect()
```

### Receiving async events

Pluto pushes events to the agent (lock granted, incoming messages, broadcasts). Register handlers before connecting:

```python
def on_message(event):
    print(f"Message from {event['from']}: {event['payload']}")

def on_lock_granted(event):
    print(f"Lock granted: {event['lock_ref']} for {event['resource']}")

client.on_message(on_message)
client.on_lock_granted(on_lock_granted)
client.connect()
```

### Using as a context manager

```python
with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
    lock_ref = client.acquire("workspace:experiment-17")
    # ... work ...
    client.release(lock_ref)
# disconnects automatically on exit
```

### Wait-queue flow

When a lock is contested, `acquire()` returns immediately with a `wait_ref`. The lock will be granted asynchronously via a `lock_granted` event:

```python
def on_lock_granted(event):
    if event.get("wait_ref") == my_wait_ref:
        # Now safe to proceed
        do_work(event["lock_ref"])

client.on_lock_granted(on_lock_granted)
my_wait_ref = client.acquire("gpu:0")  # returns wait_ref if contested
```

### API reference

| Method | Description |
|---|---|
| `connect()` | Open connection and register with Pluto. |
| `disconnect()` | Close connection gracefully. |
| `acquire(resource, mode="write", ttl_ms=30000)` | Acquire a lock. Returns `lock_ref` or `wait_ref`. |
| `release(lock_ref)` | Release a held lock. |
| `renew(lock_ref, ttl_ms=30000)` | Extend an active lock's lease. |
| `send(to, payload)` | Send a direct message to another agent. |
| `broadcast(payload)` | Send an event to all connected agents. |
| `list_agents()` | Return a list of currently connected agent IDs. |
| `on(event, handler)` | Register a callback for a named event type. |
| `on_message(handler)` | Shorthand for `on("message", handler)`. |
| `on_broadcast(handler)` | Shorthand for `on("broadcast", handler)`. |
| `on_lock_granted(handler)` | Shorthand for `on("lock_granted", handler)`. |
