# PlutoMCPFriend.sh — MCP Adapter Guide

PlutoMCPFriend exposes the Pluto coordination server as a native **MCP**
(Model Context Protocol) server. Any MCP-capable agent CLI — Claude
Code, Cursor, Aider, etc. — can call Pluto operations as ordinary
tools: `pluto_send`, `pluto_lock_acquire`, `pluto_task_update`, and
friends. No PTY, no `curl`, no copy-pasted session tokens, no JSON
heredoc gymnastics.

If you have used **PlutoAgentFriend** before, MCPFriend is the same idea
done over a structured wire protocol instead of stdin injection. Both
ship side-by-side; pick the one that fits your agent.

---

## Why MCP?

| Concern | PlutoAgentFriend (PTY) | PlutoMCPFriend (MCP) |
|---------|------------------------|----------------------|
| How the agent sends messages | curl + JSON heredoc | `pluto_send(...)` tool call |
| Session token handling | inlined into prompt; agent must paste it back | adapter holds it; agent never sees it |
| Inbound delivery | wrapper types text into stdin when idle | tool result includes `_pluto_inbox`; or call `pluto_recv` |
| Lock TTL renewal | agent must remember | adapter auto-renews at TTL/2 |
| Role on first turn | injected as natural-language prompt | MCP prompt + Claude `--append-system-prompt` |
| Works with non-Claude | yes (any TUI) | yes if framework supports MCP |

---

## Quick Start

```bash
# Prerequisite: Pluto server is running
./PlutoServer.sh --daemon

# One-shot launch (Claude Code, with the specialist role applied on turn 1)
./PlutoMCPFriend.sh --agent-id coder-1 --framework claude --role specialist

# Generate the .mcp.json without launching anything
./PlutoMCPFriend.sh --agent-id reviewer-1 --no-launch

# Cursor / Aider — adapter still works; role must be applied via slash menu
./PlutoMCPFriend.sh --agent-id worker-1 --framework cursor
```

The first run creates a Python virtual environment under `/tmp/pluto/.venv`
and installs the MCP SDK (`pip install -r requirements.txt`). Subsequent
launches reuse the venv.

---

## What ships out of the box

### 17 Pluto tools

Each is a thin wrapper around the existing `PlutoHttpClient` HTTP method;
the adapter injects the session token transparently.

| Tool | Wraps | Purpose |
|------|-------|---------|
| `pluto_send` | `POST /agents/send` | direct message to one agent |
| `pluto_broadcast` | `POST /agents/broadcast` | message to every agent |
| `pluto_recv` | drain in-memory inbox | explicit drain (call at start of turn) |
| `pluto_wait_for_messages` | event-driven block on inbox | long-poll until a message arrives (or timeout) — pair with a background sub-agent for chat-speed responsiveness |
| `pluto_publish` | `POST /agents/publish` | publish to a topic |
| `pluto_subscribe` | `POST /agents/subscribe` | subscribe to a topic |
| `pluto_list_agents` | `GET /agents?detailed=true` | discover peers |
| `pluto_find_agents` | `POST /agents/find` | filter peers by attribute |
| `pluto_lock_acquire` | `POST /locks/acquire` | grab a lock; auto-renews at TTL/2 |
| `pluto_lock_release` | `POST /locks/release` | release + cancel auto-renew |
| `pluto_lock_renew` | `POST /locks/renew` | manual renew (rarely needed) |
| `pluto_lock_info` | `GET /locks/resource` | who holds it / queue depth |
| `pluto_list_locks` | `GET /locks` | every active lock |
| `pluto_task_assign` | `POST /agents/task_assign` | assign a task |
| `pluto_task_update` | `POST /agents/task_update` | report task progress |
| `pluto_task_list` | `POST /agents/task_list` | enumerate tasks |
| `pluto_set_status` | `POST /agents/set_status` | custom status string |

### Prompts (slash commands)

In Claude Code these surface as slash commands. Two groups:

**Quick actions** — one-keystroke shortcuts; no need to type a request to the agent.

| Prompt | What it does |
|--------|-------------|
| `/pluto-check` | Calls `pluto_recv` and summarizes whatever arrived (one-shot, no blocking) |
| `/pluto-watch` | Starts a chat-speed inbox watcher: background Task on Claude Code, foreground long-poll on Cursor/Aider |
| `/pluto-status` | Reports my agent_id, connected peers, inbox depth, and locks held |

**Roles & reference** — inlined documents.

| Prompt | What it returns |
|--------|-----------------|
| `/pluto-protocol` | inlined `library/protocol.md` + live connection block |
| `/pluto-guide` | inlined agent guide + live connection block |
| `/pluto-role-<name>` | one prompt per role file in `library/roles/` (specialist, orchestrator, reviewer, qa, deployer, evaluator, experiment-runner, data-steward, ssh-bridge) |

### 4 resources

| URI | Content |
|-----|---------|
| `pluto://inbox` | pending messages (read-only; does not ack — use `pluto_recv` for that) |
| `pluto://locks` | locks currently held by this agent |
| `pluto://agents` | every agent connected to the Pluto server |
| `pluto://server` | server health / version |

---

## How inbox messages reach the agent

The MCP adapter runs a 1 s background loop calling `/agents/peek` against
the Pluto server. New actionable messages are buffered in memory and
acked the moment they're handed to the agent. There are **three** delivery
mechanisms; the role prompt teaches the agent which to use.

### 1. Tool-result piggyback (free)

Every Pluto tool result includes a `_pluto_inbox` field if there are
pending messages. So just by doing any Pluto-related work the agent
automatically picks up its inbox — no separate poll required.

### 2. Explicit drain — `pluto_recv`

Returns and acks every pending message. The role prompt instructs the
agent to call this at the start of any turn where it hasn't already
invoked another Pluto tool. Returns immediately even if the buffer is
empty.

### 3. Event-driven long-poll — `pluto_wait_for_messages`

Blocks until a message lands in the buffer (or until the timeout
elapses). Implemented with an `asyncio.Event` set by the peek loop, so
fanout is sub-second — it does **not** poll.

Two usage patterns:

#### Direct call (Cursor / Aider / any MCP client)

```
pluto_wait_for_messages(timeout_s=30)
```

The agent blocks server-side for up to 30 s at the tail of every turn.
When a message arrives, the call returns and the agent reasons over it
on the next turn.

#### Background sub-agent (Claude Code) — recommended

The main agent stays interactive with the user; a sub-agent does the
waiting. From inside the agent:

```
Task({
  description: "Watch Pluto inbox",
  prompt: "Call pluto_wait_for_messages(300) and return its result.",
  run_in_background: true
})
```

Claude Code notifies the parent agent when the Task completes (on the
parent's next turn, the result appears in context). Process the
messages, then spawn another watcher Task to keep listening. The user
never sees the agent stuck in a tool call — the watcher runs as a
silent side-effect.

This delivers chat-speed responsiveness without sacrificing the user's
ability to talk to the agent.

---

## Lock auto-renewal

`pluto_lock_acquire(resource, mode, ttl_ms, auto_renew=True)` registers
the granted lock with a background renewer that calls `/locks/renew` at
TTL/2 until the agent calls `pluto_lock_release` or the session ends. To
opt out, pass `auto_renew=False` and renew manually with
`pluto_lock_renew`.

If a renewal call fails (connectivity blip, lock revoked) the manager
stops retrying and logs a warning to stderr — the agent is responsible
for treating subsequent writes as unsafe.

---

## Role injection on startup

`PlutoMCPFriend.sh --role <name>` resolves the role file from
`library/roles/<name>.md` and:

- **Claude Code.** Launches with
  `claude --mcp-config .mcp.json --append-system-prompt "<role + protocol + connection>"`
  so turn 1 already has the role.
- **Cursor / Aider / Copilot.** No reliable system-prompt flag — the
  launcher prints `Run /pluto-role-<name>` and the user invokes the
  prompt once.

Either way, the role is also reachable mid-session via the slash menu
(`/pluto-role-specialist`, etc.). Switching roles is a single command.

---

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--agent-id <name>` | *required* | Pluto agent identity |
| `--framework <name>` | auto-detect | `claude`, `cursor`, `aider`, `copilot` |
| `--role <name|path>` | none | role to auto-apply on Claude; slash-tip on others |
| `--host <ip>` | from config / `localhost` | Pluto server host |
| `--http-port <port>` | from config / `9201` | Pluto HTTP port |
| `--ttl-ms <ms>` | `600000` | session TTL |
| `--log-level <lvl>` | `WARNING` | adapter stderr verbosity |
| `--no-launch` | off | write `.mcp.json`, don't start the framework |
| `--version` |  | print version |
| `--help` |  | show this help |
| `-- <cmd...>` |  | extra args forwarded verbatim to the agent CLI |

---

## .mcp.json layout

`PlutoMCPFriend.sh` writes (or merges into) `<repo>/.mcp.json`:

```json
{
  "mcpServers": {
    "pluto": {
      "command": "/tmp/pluto/.venv/bin/python",
      "args": [
        "/path/to/src_py/agent_mcp_friend/pluto_mcp_friend.py",
        "--agent-id", "coder-1",
        "--host", "127.0.0.1",
        "--http-port", "9201",
        "--ttl-ms", "600000",
        "--log-level", "WARNING"
      ]
    }
  }
}
```

Other `mcpServers` entries you have configured are preserved — only the
`pluto` key is overwritten. The file is gitignored (per-user).

To run the adapter from a custom MCP client:

```bash
claude --mcp-config /path/to/.mcp.json
```

---

## Architecture

```
   Claude Code / Cursor / Aider                    Pluto Erlang server
              │                                            │
              │  JSON-RPC stdio                            │  HTTP :9201
              │  (tools, prompts, resources,               │  (existing API,
              │   notifications/resources/updated)         │   unchanged)
              │                                            │
              └────────── PlutoMCPFriend (Python) ─────────┘
                              │
                              ├── FastMCP server (mcp SDK)
                              ├── PlutoHttpClient (existing)
                              ├── InboxManager
                              │     ├── 1 s peek loop
                              │     ├── seq dedup, noise filter
                              │     └── piggyback helper
                              └── LockManager
                                    └── per-lock TTL/2 renewal
```

The adapter is ~700 LOC of Python wrapping the existing HTTP client.
The Erlang server is unchanged.

---

## Comparison vs PlutoAgentFriend

Use **PlutoAgentFriend** when:

- The agent CLI does not speak MCP.
- You want to keep the agent completely unmodified, including its prompt
  format.
- You're using a custom or homegrown agent loop that talks to a TUI.

Use **PlutoMCPFriend** when:

- The agent supports MCP (Claude Code, Cursor, Aider with MCP plugin).
- You want structured tool calls instead of curl-from-shell.
- You want the wrapper to manage tokens, acks, and lock renewals
  automatically.

The two are not mutually exclusive — different team members can run
different wrappers against the same Pluto server.

---

## Troubleshooting

**`pip install` fails with externally-managed-environment.**
The script creates a project venv at `/tmp/pluto/.venv`. If your system
Python rejects pip via PEP 668, the venv side-steps it. If you've
deleted the venv, the next launch recreates it.

**`pluto_*` tools don't show up in Claude Code.**
Verify Claude was launched with `--mcp-config <path>/.mcp.json` (the
launcher does this automatically). Run `claude --mcp-config <path>` and
look at Claude's MCP startup log for errors from the `pluto` server.

**Inbox never delivers.**
Check `pluto://inbox` resource by reading it directly from Claude Code.
If it's empty, the adapter's peek loop is healthy but no actionable
messages have arrived. Set `--log-level DEBUG` for adapter chatter.

**Pluto server unreachable.**
Run `./PlutoServer.sh --status`. If down, `./PlutoServer.sh --daemon`.
The adapter starts even when the server is down so the agent doesn't
fail outright; tools will return errors until the server comes back.
