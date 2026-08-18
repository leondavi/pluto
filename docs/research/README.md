# Pluto Research Notes

Analysis and proposal documents — investigations into how Pluto behaves in the
field and how it should evolve. Nothing here describes shipped behavior; when a
proposal is implemented, its design graduates to `docs/technical/` and the
research doc gains a status note pointing there.

## Contents

| Document | Topic |
|---|---|
| [token-efficiency.md](token-efficiency.md) | Where Pluto-connected agents spend context-window tokens, a survey of state-of-the-art remedies (MCP 2026-07-28, prompt caching, code-mode tool calling, notification-driven delivery), and proposals grouped by whether they eliminate a token class outright (A), shrink what remains (B), or track protocol/interop (C). |

## Conventions

- Date each document and mark its status (`research / proposal`).
- Cite sources as links; cite repo code as `path:line`.
- State measurement methodology so numbers can be reproduced later.
