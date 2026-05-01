# Pluto — Usage Guide

This directory contains in-depth documentation for every way you can interact with the Pluto coordination server.

## Methods at a Glance

| Method | Best for | Guide |
|--------|----------|-------|
| [PlutoMCPFriend.sh](pluto-mcp-friend.md) | **Claude Code** — Pluto operations as native MCP tools | → [guide](pluto-mcp-friend.md) |
| [PlutoAgentFriend.sh](pluto-agent-friend.md) | Wrapping any other AI CLI via PTY injection (Cursor, Aider, Copilot, …) | → [guide](pluto-agent-friend.md) |
| [PlutoClient.sh](pluto-client.md) | Inspecting the server, registering agents via script, generating guides | → [guide](pluto-client.md) |
| [PlutoServer.sh](pluto-server.md) | Building, starting, and managing the Erlang server process | → [guide](pluto-server.md) |
| [TCP / Python library](tcp-connection.md) | Custom agents in any language; raw protocol details | → [guide](tcp-connection.md) |

## Recommended Flow

```
1. Install dependencies     →  ./PlutoInstall.sh
2. Start the server         →  ./PlutoServer.sh --daemon
3. Verify connectivity      →  ./PlutoClient.sh ping
4. Launch your agent        →  ./PlutoAgentFriend.sh --agent-id coder-1
```

All components read server address/port from `config/pluto_config.json`
(created by PlutoInstall or editable manually).

## Choosing the Right Method

**Use PlutoMCPFriend.sh** when you're running **Claude Code**. Pluto operations
show up as native tools (`pluto_send`, `pluto_lock_acquire`,
`pluto_task_update`, …) — no curl, no session token paste, no JSON-in-shell
escaping. The adapter auto-renews locks, surfaces inbox messages on tool
results, and applies your role automatically on the first turn via Claude's
`--append-system-prompt`. Only Claude Code is supported; for other agents see
PlutoAgentFriend below.

**Use PlutoAgentFriend.sh** when you want to wrap any other TUI agent
unchanged (Cursor, GitHub Copilot CLI, Aider, or a custom CLI) via a PTY
proxy that injects natural-language Pluto messages when the agent is idle.
Works without any MCP support in the agent.

**Use PlutoClient.sh** for operations and inspection: checking connectivity,
listing active agents, viewing server stats, or generating the agent protocol guide.
It also supports TCP and HTTP agent registration for scripts and CI environments.

**Use PlutoServer.sh** for all server lifecycle tasks: starting, stopping,
rebuilding, and opening an Erlang console.

**Use raw TCP or the Python library** when you are building your own agent from
scratch and need full control over the protocol. See the [TCP/Python guide](tcp-connection.md).
