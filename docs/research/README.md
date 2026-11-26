# Pluto Research Notes

Analysis and proposal documents — investigations into how Pluto behaves in the
field and how it should evolve. Nothing here describes shipped behavior; when a
proposal is implemented, its design graduates to `docs/technical/` and the
research doc gains a status note pointing there.

## Contents

| Document | Topic |
|---|---|
| [token-efficiency.md](token-efficiency.md) | Where Pluto-connected agents spend context-window tokens, a survey of state-of-the-art remedies (MCP 2026-07-28, prompt caching, code-mode tool calling, notification-driven delivery), and proposals grouped by whether they eliminate a token class outright (A), shrink what remains (B), or track protocol/interop (C). |
| [subagents-vs-pluto-communication.md](subagents-vs-pluto-communication.md) | A live experiment comparing a 4-worker team coordinated by native Claude Code subagents with one coordinated over Pluto. It measures relay latency, tokens, cost, wall time and correctness under lock contention, stale-lease fencing and dynamic fan-out. It also reports the queued-lock delivery bugs the experiment found, fixed in v0.5.1. |
| [claude-code-vs-pluto-coordination.md](claude-code-vs-pluto-coordination.md) | Mutexes, read/write locks, semaphores and barriers: what Claude Code documents, what the Pluto server has, and what PlutoSmartStreams v0.6.0 adds for jobs (`--slot`, `--after`, `--lock`), with live demonstrations and measurements. |

## Conventions

- Date each document and mark its status (`research / proposal`).
- Cite sources as links; cite repo code as `path:line`.
- State measurement methodology so numbers can be reproduced later.
