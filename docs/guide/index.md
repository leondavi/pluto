# Pluto — Usage Guide

This directory contains in-depth documentation for every way you can interact with the Pluto coordination server.

## Methods at a Glance

| Method | Best for | Guide |
|--------|----------|-------|
| [PlutoAgentFriend.sh](pluto-agent-friend.md) | Wrapping an existing AI CLI (Claude, Copilot, Aider…) — **recommended** | → [guide](pluto-agent-friend.md) |
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

**Use PlutoAgentFriend.sh** when you want to run Claude Code, GitHub Copilot CLI,
Aider, Cursor, or any other AI CLI tool alongside other agents. PlutoAgentFriend
wraps the CLI in a PTY proxy, registers it with Pluto, and delivers incoming
messages as natural-language prompts — no changes to the agent itself.

**Use PlutoClient.sh** for operations and inspection: checking connectivity,
listing active agents, viewing server stats, or generating the agent protocol guide.
It also supports TCP and HTTP agent registration for scripts and CI environments.

**Use PlutoServer.sh** for all server lifecycle tasks: starting, stopping,
rebuilding, and opening an Erlang console.

**Use raw TCP or the Python library** when you are building your own agent from
scratch and need full control over the protocol. See the [TCP/Python guide](tcp-connection.md).
