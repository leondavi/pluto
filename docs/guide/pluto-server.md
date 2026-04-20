# PlutoServer.sh — Server Management Guide

`PlutoServer.sh` builds, starts, and manages the Pluto coordination server
(an Erlang/OTP application). All build artefacts are placed under `/tmp/pluto/build`
to keep the source tree clean.

---

## Quick Start

```bash
# Install dependencies first (one-time)
./PlutoInstall.sh

# Build and start in the foreground (logs to stdout)
./PlutoServer.sh

# Build and start as a background daemon
./PlutoServer.sh --daemon

# Check whether the daemon is running
./PlutoServer.sh --status

# Stop the daemon
./PlutoServer.sh --kill
```

---

## Commands

| Command | Description |
|---------|-------------|
| *(none)* | Build (if needed) and start in the **foreground**. Logs stream to stdout. Press Ctrl-C to stop. |
| `--daemon` | Build (if needed) and start as a **background daemon**. PID and log files are written to `/tmp/pluto/`. |
| `--kill` | Stop a running daemon gracefully. |
| `--status` | Print whether the server is running (and its PID). |
| `--build` | Compile and assemble the release only — do not start. |
| `--clean` | Remove all build artefacts under `/tmp/pluto/build`. |
| `--console` | Start an **interactive Erlang shell** with Pluto loaded, suitable for debugging and introspection. |
| `--version` | Print the Pluto version string and exit. |
| `-h`, `--help` | Show usage and exit. |

---

## File Locations

| Path | Contents |
|------|----------|
| `/tmp/pluto/build/` | Compiled Erlang release |
| `/tmp/pluto/pluto.pid` | Daemon PID file |
| `/tmp/pluto/pluto.log` | Daemon stdout/stderr log |
| `/tmp/pluto/state/` | State snapshots (persistence) |
| `/tmp/pluto/events/` | Event log (append-only) |
| `src_erl/config/sys.config` | Server configuration |
| `src_erl/config/vm.args` | Erlang VM flags |

---

## Configuration

Edit `src_erl/config/sys.config` to change server behaviour:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `tcp_port` | `9000` | TCP listening port |
| `http_port` | `9001` | HTTP API port (set to `disabled` to turn off) |
| `heartbeat_interval_ms` | `15000` | How often agents must send a ping |
| `heartbeat_timeout_ms` | `30000` | Declare an agent dead after this silence |
| `reconnect_grace_ms` | `30000` | Hold locks for this long after a disconnect |
| `default_max_wait_ms` | `60000` | Maximum time a lock request waits in queue |
| `persistence_dir` | `/tmp/pluto/state` | State snapshot directory |
| `flush_interval` | `60000` | Snapshot frequency (ms) |
| `event_log_dir` | `/tmp/pluto/events` | Event log directory |
| `event_log_max_entries` | `100000` | Maximum stored events (ring buffer) |
| `session_conflict_policy` | `strict` | `strict` rejects duplicate agent IDs; `takeover` replaces |

VM-level settings are in `src_erl/config/vm.args`:

```
-sname pluto
-setcookie pluto_secret
+P 1048576
+Q 65536
```

---

## Prerequisites

- **Erlang/OTP 26+** — install via `./PlutoInstall.sh` or your system package manager.
- **rebar3** — build tool, also installed by `./PlutoInstall.sh`.

---

## Troubleshooting

**Server won't start — port already in use**

```bash
# Find what is using port 9000
lsof -i :9000
# Then either kill that process or change tcp_port in sys.config
```

**Build fails**

```bash
./PlutoServer.sh --clean   # remove stale artefacts
./PlutoServer.sh --build   # fresh compile
```

**Inspect a running node**

```bash
./PlutoServer.sh --console
# Inside the shell:
pluto_lock_mgr:list_locks().
pluto_msg_hub:list_agents().
```

---

## See Also

- [PlutoAgentFriend.sh guide](pluto-agent-friend.md) — launch agents
- [PlutoClient.sh guide](pluto-client.md) — verify and inspect
- [TCP / Python guide](tcp-connection.md) — protocol reference
- [Pluto guide index](index.md)
