<p align="center">
  <img src="assets/pluto.png" alt="Pluto Logo" width="240" />
</p>

<h1 align="center">Pluto</h1>

<p align="center">
  <strong>Multi-Agent Coordination &amp; Messaging Server</strong><br>
  <sub>Resource locking · Agent discovery · Message routing · Deadlock detection</sub>
</p>

<p align="center">
  <a href="docs/guide/installation.md">Install</a> ·
  <a href="docs/guide/index.md">Guides</a> ·
  <a href="docs/demos/">Demos</a> ·
  <a href="library/protocol.md">Protocol</a> ·
  <a href="CONSENT.md">Consent &amp; Disclaimer</a>
</p>

<p align="center">
  <em>By using Pluto you accept the <a href="CONSENT.md">disclaimer and liability terms</a>.</em>
</p>

---

## What Is Pluto?

Pluto is an Erlang/OTP server that gives multiple AI agents a shared
runtime for safe concurrent work. It does **not** plan tasks — agents
do that. Pluto provides the coordination primitives an agent team
needs: locks, leases, fencing tokens, agent registry, message routing,
and deadlock detection.

## Why Pluto?

Today's agent frameworks (LangGraph, CrewAI, AutoGen, Claude Agent SDK,
OpenAI Agents SDK) focus on planning and tool use. **They don't give
you a runtime where heterogeneous agent processes can safely share
state.** Pluto fills that gap.

| Problem in multi-agent systems | What Pluto adds |
|-|-|
| Agents share state with no conflict prevention | Distributed read/write locks with FIFO fairness |
| Crashed agents leave locks held forever | Heartbeat monitoring; auto-release on silence |
| Stale writes after a lock was revoked | Monotonically increasing fencing tokens |
| No mechanism to break circular waits | Deadlock detection with victim selection |
| Agents must share a runtime to communicate | Language-agnostic TCP + HTTP messaging bus |
| No live view of who is running | Agent registry with discovery and event history |

Pluto **complements** orchestration frameworks; it doesn't replace them.

## Quick Start

```bash
# 1. Install dependencies (Erlang/OTP, rebar3, Python 3.10+)
./PlutoInstall.sh

# 2. Start the server
./PlutoServer.sh --daemon
./PlutoServer.sh --status        # confirms version + ports

# 3. Wrap your agent with Pluto and go
./PlutoAgentFriend.sh --agent-id coder-1 --framework claude
```

That's the whole loop. Full setup details and troubleshooting are in
the [Installation guide](docs/guide/installation.md).

## Documentation

| Doc | What it covers | Best for |
|-|-|-|
| [Installation](docs/guide/installation.md) | Prerequisites, install paths, verification, troubleshooting | Everyone, first time |
| [Usage guide index](docs/guide/index.md) | Map of every way to talk to Pluto | Quick orientation |
| [PlutoMCPFriend](docs/guide/pluto-mcp-friend.md) | Pluto operations as native MCP tools — **recommended for Claude Code** | Claude Code users |
| [PlutoAgentFriend](docs/guide/pluto-agent-friend.md) | PTY wrapper that injects messages into any TUI agent | Cursor / Aider / generic CLI |
| [PlutoClient](docs/guide/pluto-client.md) | Inspection CLI and Python client library | Anyone scripting against Pluto |
| [PlutoServer](docs/guide/pluto-server.md) | Build, daemon, console, and lifecycle management | Operators |
| [TCP / Protocol](docs/guide/tcp-connection.md) | Wire-level details for custom agents | Building from scratch in any language |
| [Collaboration protocol](library/protocol.md) | Shared message schemas every role speaks | Multi-agent teams |
| [Roles](library/roles/) | Predefined agent roles (orchestrator, specialist, reviewer, …) | Multi-agent teams |

## Demos

Worked examples that exercise the full stack end-to-end:

| Demo | What it shows |
|-|-|
| [fractal_collaboration](docs/demos/fractal_collaboration.md) | Three agents (architect, specialist, reviewer) building a Mandelbrot pipeline |
| [multiagent_vs_solo](docs/demos/multiagent_vs_solo.md) | A/B comparison: solo Claude vs. a Pluto-coordinated team on the same task |
| [stress_collaboration](docs/demos/stress_collaboration_demo.md) | High-frequency lock contention under heavy load |
| [task_claim_race](docs/demos/task_claim_race_demo.md) | Two agents racing for the same task; correctness via fencing tokens |
| [weather_chat](docs/demos/weather_chat_demo.md) | Minimal two-agent conversation pattern |

## Architecture in 30 seconds

```
                +---------------------------------+
                |     Pluto Server (Erlang/OTP)    |
                |                                  |
                |   lock_mgr · msg_hub · session   |
                |   heartbeat · persistence · sup  |
                +-----------+----------+-----------+
       JSON over TCP :9200  |          |  HTTP :9201
                            |          |
        +-------------------+          +-----------------+
        |                                                 |
   Agents (Claude / Aider / Cursor / Copilot / Python    Dashboards
   / custom) connect via PlutoMCPFriend, PlutoAgentFriend,   & curl
   PlutoClient, or raw TCP/HTTP.
```

Erlang/OTP supervises every component; one process per connection;
ETS-backed lock table; persistent fencing counter survives restarts.
Implementation details in [docs/guide/pluto-server.md](docs/guide/pluto-server.md).

## Disclaimer

Pluto is provided **as-is** for research and development. The
maintainers bear no responsibility or liability for any harm arising
from its use. **You are solely responsible** for what your agents do
under Pluto. Read [CONSENT.md](CONSENT.md) before running it on
anything you care about.

## License & Citation

[LICENSE](LICENSE). Academic researchers may use Pluto for free
provided they cite this repository:

```bibtex
@software{leon_pluto,
  author  = {Leon, David},
  title   = {Pluto: A coordination and messaging server for AI agents},
  url     = {https://github.com/leondavi/pluto},
  version = {0.2.8},
  year    = {2026}
}
```

A machine-readable [CITATION.cff](CITATION.cff) is provided so GitHub
shows a "Cite this repository" button in the sidebar.
