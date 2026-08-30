# Claude Code Integration

How Pluto plugs into Claude Code's native multi-agent capabilities —
what works today, what ships with Pluto, and where the two systems
complement each other.

Requirements noted per feature; the push path needs Claude Code
≥ 2.1.224 (macOS/Linux). Official docs referenced throughout:
[MCP](https://code.claude.com/docs/en/mcp.md),
[subagents](https://code.claude.com/docs/en/sub-agents.md),
[cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging.md),
[agent teams](https://code.claude.com/docs/en/agent-teams.md),
[hooks](https://code.claude.com/docs/en/hooks-guide.md).

## 1. Per-session MCP server (the standard path)

`./PlutoMCPFriend.sh --agent-id <id> --role <role>` writes a
project-scoped `.mcp.json` and launches Claude Code with the Pluto MCP
adapter attached: 25 `pluto_*` tools, `pluto://` resources, and
role/protocol prompts, with the role injected via
`--append-system-prompt`. Each session is one Pluto agent; the Erlang
server is the shared source of truth for identity, messages, locks, and
tasks. See the [PlutoMCPFriend guide](pluto-mcp-friend.md) and
[technical reference](../technical/pluto-mcp-friend.md).

## 2. Push wakeups via the session inbox socket (v0.4.0)

Claude Code exports a per-session Unix-socket inbox to child processes
(`CLAUDE_CODE_MESSAGING_SOCKET` / `CLAUDE_CODE_MESSAGING_TOKEN`). The
Pluto adapter auto-detects it and pushes a metadata-only wakeup when
messages land: an idle session starts a new turn and drains with
`pluto_recv` — no watcher subagents, no heartbeat turns, no token cost
while waiting. Gated by the tri-state `PLUTO_MCP_PUSH` env var
(unset = auto-on when the socket is present). Details, wire format, and
failure semantics: [technical reference §2.6](../technical/pluto-mcp-friend.md).

On hosts without the socket (other CLIs, older Claude Code, Windows)
nothing changes — the long-poll/piggyback path remains the correctness
channel.

## 3. Pluto roles as Claude Code subagents

`.claude/agents/pluto-{specialist,reviewer,qa}.md` (shipped in-repo)
package worker-shaped Pluto roles as native subagents. In a
Pluto-connected session you can say *"use the pluto-reviewer subagent to
review t-001's diff"* — the subagent arrives with the role, the protocol
digest, and the shared-identity rules baked in.

Two identity models:

- **Inherited (default, checked in):** the subagent shares the parent
  session's MCP server and Pluto identity. It must never drain the
  parent's inbox (`pluto_recv`/`pluto_pop` are forbidden; snapshots via
  `pluto_inbox_watch(drain=false)`), and its sends go out under the
  parent's `agent_id`.
- **Own identity (generated locally):** subagent frontmatter can declare
  its own `mcpServers` block spawning a dedicated PlutoMCPFriend with
  its own `agent_id` and inbox. Generate with
  `python -m agent_mcp_friend.subagents --own-identity` — the output
  embeds machine-specific paths, so it is never checked in.

The checked-in files are generated from `library/roles/*.md`
(`python -m agent_mcp_friend.subagents`); a CI test fails on drift.

## 4. Pluto vs. Agent Teams

Claude Code's experimental agent teams
(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) give a lead session
teammates with mailbox files and a shared task list. Overlapping
surface, different guarantees:

| | Agent Teams | Pluto |
|---|---|---|
| Agents | Claude Code only | any CLI (Claude, Aider, Gemini, MCP, PTY) |
| Lifetime | team dies with the lead session | server outlives sessions; snapshots/`--resume` |
| Messaging | mailbox files, session-scoped | central bus, at-least-once, ack cursors |
| Concurrency control | none | locks, leases, fencing tokens, deadlock detection |
| Task list | shared JSON per team | server-side tasks + protocol (`task_assigned`…) |
| Setup | zero | run the Pluto server + launcher |

Rule of thumb: parallel work inside one Claude Code session → teams or
subagents; durable cross-vendor coordination with real mutual-exclusion
guarantees → Pluto. They compose — a Pluto-connected lead can still
spawn teams/subagents for local fan-out.

## 5. Future paths

- **Cross-session messaging** (`ListAgents`/`SendMessage`): Claude
  sessions can already message each other directly; a Pluto bridge could
  mirror its registry into that namespace.
- **Hooks**: `SessionStart`/`PostToolUse` hooks can bridge Pluto state
  into sessions on hosts where the MCP adapter can't (e.g. re-exporting
  the messaging socket, auto-registering roles).
- **Channels / Routines**: Claude Code's channels research preview and
  cloud routines' API trigger offer push-into-session and always-on
  externally-triggered agents; both are natural Pluto transports once
  stable.
