# PlutoAgentFriend.sh — Agent Wrapper Guide

PlutoAgentFriend wraps any AI agent CLI (Claude Code, GitHub Copilot CLI, Aider,
Cursor, or a custom command) in a **PTY-based I/O proxy** that:

- Registers the agent with the Pluto server automatically.
- Long-polls for incoming messages (direct, broadcast, tasks, topics) in the background.
- Injects pending messages into the agent's stdin as natural-language prompts
  when the agent is idle — without modifying the agent itself.

No Pluto protocol code is required in your agent.

---

## Quick Start

```bash
# Prerequisite: server must be running
./PlutoServer.sh --daemon

# Auto-detect which agent framework is installed
./PlutoAgentFriend.sh --agent-id coder-1

# Specify a framework explicitly
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
./PlutoAgentFriend.sh --agent-id coder-1 --framework copilot
./PlutoAgentFriend.sh --agent-id coder-1 --framework aider

# Wrap a fully custom command
./PlutoAgentFriend.sh --agent-id worker-1 -- python3 my_agent.py

# Full option reference
./PlutoAgentFriend.sh --help
```

---

## Options

### Required

| Flag | Description |
|------|-------------|
| `--agent-id <name>` | Unique agent ID for Pluto registration (e.g. `coder-1`) |

### Optional

| Flag | Default | Description |
|------|---------|-------------|
| `--framework <name>` | auto-detect | Agent framework: `claude`, `copilot`, `aider`, `cursor` |
| `--mode <mode>` | `auto` | Injection mode — see [Injection Modes](#injection-modes) |
| `--host <ip>` | from config | Pluto server host |
| `--http-port <port>` | from config | Pluto HTTP port (default: 9001) |
| `--ready-pattern <regex>` | framework default | Regex matching the agent's "ready for input" prompt |
| `--silence-timeout <sec>` | `3.0` | Seconds of output silence treated as agent idle |
| `--poll-timeout <sec>` | `15` | Pluto long-poll timeout in seconds |
| `--guide <path>` | auto-discover | Path to a guide file injected on startup |
| `--no-guide` | — | Disable automatic guide injection |
| `--verbose` | — | Enable debug logging |

---

## Injection Modes

| Mode | Behaviour |
|------|-----------|
| `auto` | Inject Pluto messages as soon as the agent is detected idle (default) |
| `confirm` | Show a notification and auto-inject after 10 seconds if still idle |
| `manual` | Show a notification only — user is responsible for forwarding the message |

Choose `confirm` or `manual` in interactive sessions where you want to review
messages before they reach the agent.

---

## How It Works

```
User terminal  ←→  TerminalProxy  ←→  Agent CLI (via PTY)
                        │
                AgentStateDetector   (stdout → BUSY / ASKING / READY)
                        │
                PlutoConnection      (long-polls Pluto for messages)
                        │
                MessageFormatter     (JSON → natural-language prompt)
                        │
                InjectionGate        (decides when/how to inject)
```

1. `PlutoAgentFriend` prints a startup banner and registers with Pluto via HTTP.
2. The agent CLI is spawned inside a **PTY** (pseudo-terminal) — the user's
   terminal is set to raw mode and every keystroke is forwarded transparently.
3. A background thread long-polls `POST /agents/poll` for incoming messages.
4. An injection thread checks `AgentStateDetector.is_ready_for_injection()` every 0.5 s.
5. When the agent is idle, `MessageFormatter` converts the queued messages into
   readable text and `inject_input()` writes them to the agent's PTY stdin.

### State Detection

`AgentStateDetector` classifies the agent's state from its stdout:

| State | Meaning |
|-------|---------|
| `BUSY` | Agent is actively producing output |
| `ASKING_USER` | Agent posed a question — injection is held |
| `READY` | Output has been silent for `--silence-timeout` seconds |

### Safety Rules

- **User always wins** — keystrokes are forwarded before injected messages.
- **No injection while asking** — messages queue until the agent returns to idle.
- **5-second cooldown** — no injection within 5 seconds of the last keystroke.
- **Transparency** — every injection is announced with `[pluto-friend]` in the terminal.

---

## Configuration File

PlutoAgentFriend reads `config/pluto_config.json` for defaults:

```json
{
  "host": "127.0.0.1",
  "http_port": 9001
}
```

Create or edit this file to avoid repeating `--host` / `--http-port` on every launch.

---

## Examples

```bash
# Claude Code, confirm mode — review messages before injection
./PlutoAgentFriend.sh --agent-id reviewer-1 --framework claude --mode confirm

# Copilot CLI, verbose debug output
./PlutoAgentFriend.sh --agent-id coder-2 --framework copilot --verbose

# Custom Python agent on a remote server
./PlutoAgentFriend.sh --agent-id worker-3 --host 10.0.1.5 --http-port 9001 \
  -- python3 my_agent.py

# Inject a custom skill guide on startup, manual injection mode
./PlutoAgentFriend.sh --agent-id analyst-1 --guide ./my_skill_guide.md --mode manual
```

---

## Supported Frameworks

| Framework | Detected by | Default ready pattern |
|-----------|-------------|----------------------|
| `claude` | `claude` on PATH | `>` prompt after silence |
| `copilot` | `gh copilot` on PATH | silence timeout |
| `aider` | `aider` on PATH | `>` prompt |
| `cursor` | `cursor` on PATH | silence timeout |
| custom | `--` separator | silence timeout |

---

## Copilot CLI — Not Supported

> **PlutoAgentFriend does not work with GitHub Copilot CLI.**

GitHub Copilot CLI enforces an internal **safe layer** that rejects
unsolicited text written to its input stream. This restriction applies
**even when the user has explicitly given consent** — the safe layer
operates at the CLI level and is outside Pluto's control.

### What this means

- Guide and role files **cannot** be injected into Copilot.
- Pluto messages **cannot** be delivered to a Copilot agent automatically.
- Automated agent coordination through prompt injection is **not available**.
- The consent handshake (previous versions of this doc) does not overcome
  the restriction — Copilot will reject the injected handshake question
  itself, so the gate can never be opened.

### What to do instead

All communication with a Copilot-based agent must go through
**PlutoClient** with **active polling** from the agent side:

1. The Copilot agent (or a human using Copilot) periodically calls
   `./PlutoClient.sh poll` (or the equivalent HTTP endpoint) to fetch
   pending messages from its Pluto inbox.
2. The agent reads the messages and responds or acts accordingly —
   there is no push/injection path.
3. Replies are sent back via `./PlutoClient.sh send` or the HTTP API.

This polling pattern works reliably with Copilot because it does not
require writing anything to Copilot's stdin.

### Other agents

Claude Code, Cursor, Aider, and custom commands are **fully
supported** — prompt injection works as documented in the sections
above. `--require-consent` is not needed for these frameworks.

---

## Disclaimer & Liability

Pluto is provided **as-is**, without warranty of any kind — express or
implied. The repository maintainers and developers bear **no
responsibility or liability** of any kind for any damages, losses,
security incidents, or harm arising from the use or misuse of this
software.

**You, the user, are solely responsible** for any harm, damage, data
loss, security incident, or other issue caused by your use or misuse of
Pluto — including granting consent to AI agents, running automated
injections, exposing the local message bus to untrusted networks, or
coordinating agents that take destructive actions on your systems.

The code injection capability is a **powerful and potentially dangerous
action**. Carefully inspect what Pluto injects before enabling automated
mode, and use it only in environments you own and control.

Pluto is built with entirely positive intentions for legitimate
multi-agent research and development. See [CONSENT.md](../../CONSENT.md)
for the full disclaimer and user-responsibility statement.

---

## See Also

- [PlutoClient.sh guide](pluto-client.md) — inspect agents, stats, locks
- [PlutoServer.sh guide](pluto-server.md) — start and manage the server
- [TCP / Python guide](tcp-connection.md) — build a fully custom agent
- [Pluto guide index](index.md)
