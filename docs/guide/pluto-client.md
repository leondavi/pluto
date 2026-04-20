# PlutoClient.sh — Client CLI Guide

`PlutoClient.sh` is the command-line interface for interacting with a running Pluto
server. It automatically creates a Python virtual environment on first run, then
delegates to `src_py/pluto_client.py`.

---

## Quick Start

```bash
./PlutoClient.sh ping                         # Verify the server is reachable
./PlutoClient.sh list                         # List connected agents
./PlutoClient.sh stats                        # Show server statistics
./PlutoClient.sh guide --output agent_guide.md  # Generate the agent protocol guide
```

---

## Global Options

Place these **before** the command name:

| Option | Default | Description |
|--------|---------|-------------|
| `--host HOST` | `127.0.0.1` | Pluto server host |
| `--port PORT` | `9000` | Pluto TCP port |
| `--agent-id ID` | `pluto-cli` | Agent ID (used by `register`) |
| `--version` | — | Print the client version and exit |
| `-h`, `--help` | — | Show usage and exit |

---

## Commands

### `ping`

Verify connectivity to the Pluto server.

```bash
./PlutoClient.sh ping
./PlutoClient.sh --host 10.0.1.5 --port 9000 ping
```

### `list`

List all agents currently connected to the server.

```bash
./PlutoClient.sh list
```

### `stats`

Show live server statistics: active locks, message counts, deadlock events,
and per-agent counters. Does **not** require registration.

```bash
./PlutoClient.sh stats
./PlutoClient.sh --host 10.0.1.5 stats
```

### `guide`

Generate the Pluto agent protocol guide — a comprehensive Markdown reference
that you can feed to an AI agent as context.

```bash
# Print to stdout
./PlutoClient.sh guide

# Write to a file
./PlutoClient.sh guide --output ./agent_guide.md

# Custom output path
./PlutoClient.sh guide --output /tmp/pluto_guide.md
```

### `register`

Register an agent with the server and maintain its presence. Supports three
sub-modes:

#### TCP foreground (default)

Connects via TCP, registers, and sends heartbeats every 10 seconds until
Ctrl-C. The process keeps the session alive.

```bash
./PlutoClient.sh register --agent-id my-agent
./PlutoClient.sh register my-agent            # shorthand
```

#### HTTP / stateless

Registers via the HTTP API. Returns a token you can use to send heartbeats
and poll messages independently.

```bash
./PlutoClient.sh register --http --agent-id claude-workspace
./PlutoClient.sh register --stateless --ttl 300 --agent-id my-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `--http` | — | Use HTTP registration |
| `--stateless` | — | Register as stateless agent (longer TTL) |
| `--ttl SECONDS` | `300` | TTL for HTTP/stateless mode |
| `--http-port PORT` | `9001` | HTTP API port |

#### TCP daemon

Spawns a background process that maintains the TCP connection and
automatically reconnects on failure.

```bash
./PlutoClient.sh register --daemon --agent-id my-agent
```

The daemon writes its PID to `/tmp/pluto/daemon_<agent-id>.pid` and its
log to `/tmp/pluto/daemon_<agent-id>.log`.

---

## Examples

```bash
# Remote server
./PlutoClient.sh --host 10.0.1.5 --port 9000 ping
./PlutoClient.sh --host 10.0.1.5 --port 9000 list

# Generate guide to a project directory
./PlutoClient.sh guide --output ./docs/agent_guide.md

# Register via HTTP (suitable for Claude Code, CI jobs)
./PlutoClient.sh register --http --agent-id claude-workspace

# Background daemon — survives terminal closure
./PlutoClient.sh register --daemon --agent-id persistent-worker

# Stateless with custom TTL
./PlutoClient.sh register --stateless --ttl 600 --agent-id batch-job
```

---

## See Also

- [PlutoAgentFriend.sh guide](pluto-agent-friend.md) — wrap an AI CLI automatically
- [PlutoServer.sh guide](pluto-server.md) — manage the server
- [TCP / Python guide](tcp-connection.md) — full protocol reference
- [Pluto guide index](index.md)
