# Token Efficiency in Pluto Multi-Agent Communication

**Status:** research / proposal — nothing in this document is shipped behavior.
**Scope:** the PlutoMCPFriend (MCP stdio) and PlutoAgentFriend (PTY) adapters, the role/protocol prompt library, and the wire formats used between agents. The Erlang server's CPU/memory costs are out of scope except where they shape what reaches a model's context window.
**Date:** August 2026.

---

## 1. Introduction and problem statement

Pluto gives AI coding agents runtime coordination primitives — locks, leases, fencing tokens, deadlock detection, an agent registry, and a messaging bus. None of those primitives are expensive by themselves: the Erlang hub exchanges compact JSON over HTTP, and the adapter's 1 Hz peek loop costs zero model tokens because it runs in the adapter process, not the model.

The cost shows up in the **model context window**. Every Pluto-connected agent pays for:

- a large injected system prompt (role + protocol + connection instructions),
- the schemas of every Pluto MCP tool, re-sent with every request,
- watcher subagents that burn full model turns while idling on an empty inbox,
- message payloads and tool results that carry uncompacted, unbounded JSON.

In practice this is the main friction reported when working with Pluto: a three-agent team spends roughly **24K tokens before any work happens**, and long sessions accumulate watcher-turn and piggyback overhead on top. This document measures where those tokens go (§3), surveys the state of the art in August 2026 for reducing them (§4), and maps each applicable technique to a concrete Pluto change (§5).

**Methodology.** Token figures were obtained by executing the prompt builders in `src_py/agent_mcp_friend/prompts.py` in the repo venv and approximating tokens as `chars / 4`. That approximation is coarse (±15% for English/markdown/JSON mixes) but consistent, so *relative* comparisons hold. Anyone can re-measure the same way; see §7.

---

## 2. Background: the token cost model of an agentic session

Not all tokens are equal. Three cost classes matter, and they multiply differently:

| Cost class | Paid | Example in Pluto | Growth |
|---|---|---|---|
| **Session-once** | Once per agent per session | `--append-system-prompt` role/connection block | O(agents) |
| **Per-request** | In every model request | MCP tool schemas, conversation history | O(agents × turns) |
| **Per-model-turn** | Each time a model is *woken* to run | Watcher subagent iterations, heartbeat turns | O(agents × wake-ups) — **unbounded** if wake-ups are periodic rather than event-driven |

Two economic facts shape every recommendation below:

1. **Prompt caching makes stable prefixes nearly free.** Anthropic (and equivalents at other providers) price cached prefix reads at roughly one-tenth of normal input. The system prompt and tool schemas are re-*sent* every request, but if they are byte-identical to the previous request they are cache-*reads*, not full-price input. The corollary is brutal: **any byte that changes early in the prefix invalidates everything after it.** A tool description that embeds a timestamp, a port number that varies, or a reordered tool list turns ~10K tokens of cheap cache reads into full-price input on every turn.
2. **A model turn has a floor cost regardless of what it does.** A watcher subagent that wakes, calls `pluto_inbox_watch`, gets `timeout`, and loops has still paid for its inherited tool schemas, its subagent prompt, and its growing tool-result history. Turns that exist only to keep a connection alive (heartbeats) or to poll are pure overhead — the only structural fix is to move waiting out of the model entirely.

The implication: durable wins come from **removing token load from the model path entirely** — schemas the model never needed in context, prompt text it could fetch by reference, waiting it should never do in-band — and only then from compacting what remains. Tuning frequencies (longer polls, fewer heartbeats) merely dilutes a recurring cost while leaving the cost class in place; §5 treats such tuning as a stopgap, never a fix.

---

## 3. Analysis: where Pluto spends tokens today

### 3.1 System-prompt injection (session-once, per agent)

Measured from `src_py/agent_mcp_friend/prompts.py`:

| Builder | Size | ≈ Tokens |
|---|---|---|
| `build_connection_block()` (`prompts.py:60`) | 11,402 chars | ~2,850 |
| `build_role_prompt_body("specialist")` (`prompts.py:303`) | 31,590 chars | ~7,900 |
| `build_role_prompt_body("orchestrator")` | 35,571 chars | ~8,900 |
| `build_guide_prompt_body()` | 32,582 chars | ~8,100 |

The role body is composed of the role file + the **entire `library/protocol.md` (13,875 chars) inlined** whenever the role file references it (`prompts.py:329-344`) + the connection block. The connection block is byte-identical across every agent in a team, and the bulk of it describes the *watcher mechanism* — an optimization, not the correctness path.

A three-agent team (orchestrator + two specialists) therefore starts at ≈ 24K tokens of injected prompt. Via `--append-system-prompt` (`PlutoMCPFriend.sh`) this is at least cache-friendly; on the PTY path (§3.5) it is not.

### 3.2 MCP tool schemas (per-request, per agent)

The 19 tools registered in `src_py/agent_mcp_friend/tools.py` carry ~5,906 chars ≈ **1,476 tokens of descriptions alone**, plus JSON-schema parameter blocks, plus the `FastMCP(instructions=...)` block (`server.py:117`) and four resource descriptions (`resources.py`). This sits in every request of every connected agent for the whole session. The most verbose descriptions belong to the delivery-mode tools (`pluto_pop`, `pluto_inbox_watch`, `pluto_set_delivery_mode`, `pluto_wait_for_messages`) — documentation that arguably belongs in a prompt or resource, not in schemas paid for per-request.

### 3.3 The watcher subagent chain (per-model-turn, unbounded)

The connection block instructs the agent to spawn a `general-purpose` subagent that alternates `pluto_inbox_watch` (`inbox.py:333`, default `wait_timeout_s=60.0`) with `pluto_heartbeat`, for up to 15 iterations, and instructs the parent to **respawn the chain indefinitely** (`prompts.py:165-170`). Each iteration is a full model turn inheriting the ~1.5K-token tool schema block plus its own accumulated history. `pluto_heartbeat` (`tools.py:271`) exists purely to defeat Claude Code's 600-second stream-silence watchdog — a turn whose only product is "still here."

This is the **only cost in the design that grows without bound** in session length, and it grows even when no messages flow. The docs already identify the fix as Phase 3 — "longer `wait_timeout_s`, relaxed heartbeat" — and mark it **not started** (`docs/technical/pluto-mcp-friend.md`, rollout table).

### 3.4 Payload and result verbosity (per-turn, variable)

- `pluto_send(to, payload)` accepts an **unbounded** dict — no size cap, truncation, or schema anywhere on the path (`tools.py:104`, `pluto_msg_hub.erl` send handler).
- `piggyback` attaches buffered messages to every Pluto tool result with full envelopes — `msg_id`, `seq`, `seq_token`, `from`, `event` per message (`inbox.py:173-218`), uncompacted.
- MCP resources are serialized with `json.dumps(..., indent=2)` (`resources.py`) — pretty-printing costs ~20-30% extra tokens versus compact separators. The PTY formatter already knows better: `message_formatter.py:42` uses compact separators explicitly "to save tokens."
- `pluto_list_agents`, `pluto_task_list`, `pluto_list_locks` return full detail objects with no field projection (`tools.py:313-440`).
- `pluto_snapshot_self` / `pluto_restore_from_snapshot` can pull an entire snapshot file into context (`tools.py:468-508`).

### 3.5 The PTY path re-sends everything, every request

PlutoAgentFriend types the role/guide text into the agent's **conversation** (`pluto_agent_friend.py`, `_role_injection_loop` / `_guide_injection_loop`). Conversation history is re-sent with every subsequent request, so an injected ~8K-token guide is not a session-once cost there — it is a per-request cost for the rest of the session, and being mid-history it also isn't a cacheable stable prefix once anything before it changes.

### 3.6 What Pluto already does right

Credit where due — several mitigations exist and should be preserved and generalized:

- **Spec contracts** (`library/protocol.md` §7): factoring repeated task boilerplate into a broadcast-once contract, with a measured claim of 1–2K tokens saved per dispatch. This is the strongest prior art in the repo.
- **Payload conventions** (`prompts.py:201-238`): drop `from_role`, avoid re-stating protocol boilerplate, use `conv_seq` instead of echoing `seq_token`.
- **Compact JSON separators** on the PTY wire format (`message_formatter.py:42`).
- **Content offload by reference**: the `ssh-bridge` role truncates outputs to 8 KiB tails and writes full logs to disk, sending only the path (`library/roles/ssh-bridge.md`). This pattern is the seed of §5 B2.
- The 1 Hz peek loop and HTTP keep-alive pool keep *transport* costs off the model entirely — the architecture already separates "network chatter" from "context spend" correctly. The problem is confined to what crosses into context.

---

## 4. Survey of available solutions (state of the art, August 2026)

### 4.1 MCP specification 2026-07-28

The [2026-07-28 MCP release](https://blog.modelcontextprotocol.io/posts/2026-07-28/) ([spec](https://modelcontextprotocol.io/specification/2026-07-28)) is the largest protocol revision since Streamable HTTP, and several changes bear directly on Pluto:

- **Stateless protocol core.** The `initialize`/`initialized` handshake and `Mcp-Session-Id` are retired; each request carries protocol version, client identity, and capabilities in `_meta`. Pluto's HTTP path is already pull-based and session-token oriented, so this aligns well — but PlutoMCPFriend's stdio transport keeps its own session assumptions that will need revisiting as hosts adopt the new revision.
- **Cacheable list results.** `tools/list`, `prompts/list`, `resources/list`, and `resources/read` responses now carry `ttlMs` and `cacheScope`. A server that declares its tool list stable is explicitly telling the host "this prefix will not change" — protocol-level support for the cache-stability discipline in §2.
- **Tasks extension** (`io.modelcontextprotocol/tasks`): a tool call may return a task handle; the client polls `tasks/get`, and change notifications consolidate into a single `subscriptions/listen` stream. This is a standardized replacement for exactly the job Pluto's watcher subagent does by burning model turns.
- **Multi Round-Trip Requests** replace server-initiated requests over open streams (`resultType: "input_required"` / `inputResponses`).
- **Deprecations**: sampling, roots, and logging capabilities are deprecated (12-month window), as is legacy HTTP+SSE.

### 4.2 Prompt-caching-aware design

All major hosts (Claude Code, Copilot CLI, Cursor) sit on providers with prefix caching. The design discipline it implies:

- **Byte-stable prefixes**: system prompt and tool list must be deterministic across a session — no timestamps, counters, or environment-dependent strings in descriptions.
- **Append-only context**: mutating or reordering earlier content (rather than appending) invalidates the KV cache from the mutation point. Research systems such as [TokenPilot](https://arxiv.org/html/2606.17016v1) (cache-efficient context management) and Continuum (KV-cache TTL scheduling for multi-turn agents) formalize this: context-management operations should be chosen for *cache alignment*, not just raw token count.
- Consequence for adapters like Pluto: volatile data (inbox contents, watcher state) belongs at the **end** of context (tool results), never inside tool descriptions, instructions, or resources that the host may re-fetch and diff.

### 4.3 Deferred / searchable tool schemas, and tool consolidation

Anthropic's **Tool Search** pattern (shipped in Claude Code and the API) keeps most tool schemas out of context; the model loads a schema on demand when it actually needs the tool. Where deferral isn't available, the ecosystem trend is **consolidation**: fewer, parameterized tools (`action="lock"|"unlock"|...`) with terse descriptions, because every schema is a per-request tax and large tool menus also degrade tool-selection accuracy. Cursor and Copilot CLI both expose MCP but neither yet defers schemas, so consolidation is the portable technique; deferral is a host-specific bonus.

### 4.4 Code-mode / programmatic tool calling

Anthropic's "code execution with MCP" direction — echoed by Cloudflare's code-mode and Claude Code's workflow/sandbox facilities — has the agent **write code that calls tools inside a sandbox**, with only the final result entering the model context. Instead of five tool-call turns (send → peek → pop → ack → status), the model writes one script against a client library and reads one result. Pluto is unusually well positioned here: `src_py/pluto_client.py` is already a complete, standalone HTTP/TCP client with no MCP dependency — it *is* the code-mode surface, undocumented as such.

### 4.5 Notification-driven delivery vs. polling and watcher subagents

The delivery-mechanism landscape, ordered by model-token cost:

| Mechanism | Model tokens while idle | Who supports it |
|---|---|---|
| Adapter-side polling + piggyback on next natural turn | zero | everything (Pluto today, correctness path) |
| MCP `notifications/*` wakeup | zero | Claude Code (Pluto has this behind `PLUTO_MCP_NOTIFICATIONS`) |
| MCP Tasks / `subscriptions/listen` | zero | 2026-07-28 hosts, rolling out |
| Long-poll tool call (one turn per timeout window) | ~1 turn / window | everything |
| Watcher subagent loop (Pluto's current push path) | 1 turn / iteration + heartbeats | Claude Code |

Claude Code's own **Agent Teams** (experimental, Feb 2026) confirms the direction: teammates idle on a mailbox without burning turns, waking on delivery. The lesson for Pluto: the watcher subagent was a workaround for hosts that couldn't wake on notifications; as hosts close that gap, the watcher should shrink to a fallback and eventually disappear.

### 4.6 Payload governance patterns

Established patterns from production agent systems:

- **Size caps with offload-by-reference**: payloads over a threshold are written to shared storage; the message carries a path/URI plus a short head/tail excerpt. (Pluto's ssh-bridge role already does this manually; A2A does this protocol-wide with artifact references.)
- **Field projection**: list/status endpoints accept a `fields=` or `detail=compact|full` parameter and default to compact.
- **Compact serialization**: `separators=(",", ":")`, no pretty-printing anywhere a model will read it.
- **Envelope minimization**: per-message metadata trimmed to what the consumer acts on (`from`, `msg_id`, payload); bookkeeping fields (`seq_token`) surfaced once per batch, not per message.

### 4.7 Compression and KV-reuse research

For completeness — the academic frontier, with an honest assessment of applicability:

- **Prompt compression** (LLMLingua family): learned token pruning achieving 2-5× compression on instruction-heavy prompts. Applicable to Pluto's role/guide texts offline (compress once, ship the short version), less so to live payloads (adds a model call to save a model call).
- **KV-cache-aware context management** ([TokenPilot 2026](https://arxiv.org/html/2606.17016v1), Continuum): scheduling and truncation policies chosen to preserve cache prefixes. The *discipline* transfers to Pluto (§4.2); the mechanisms live in the inference stack.
- **Cross-agent KV reuse** (DroidSpeak lineage, [persistent multi-agent KV caches](https://arxiv.org/html/2603.04428v1)): agents sharing attention states instead of re-prefilling shared context. Not actionable from an adapter — Pluto talks to hosted models through hosts it does not control — but it signals where the ecosystem is heading: *shared context should be paid for once, not once per agent*. Pluto can approximate this today at the prompt layer by making shared blocks byte-identical so each agent's provider cache at least dedups within that agent's session.

### 4.8 Native peer messaging and interop protocols

- **Claude Code Agent Teams**: separate Claude Code sessions with a shared task list and a peer mailbox. This overlaps Pluto's messaging bus for Claude-only teams. Pluto's differentiation is (a) **cross-CLI reach** — Gemini CLI, Copilot CLI, Cursor CLI, Aider-via-PTY in one team — and (b) **correctness primitives** Agent Teams lacks: distributed locks with FIFO fairness, leases, fencing tokens, deadlock detection.
- **A2A v1** (Google → Linux Foundation, 2026): Agent Cards (JSON metadata: capabilities, skills, endpoints) for discovery, task delegation over HTTP/SSE. Pluto's agent registry already stores role/capability data; exporting it as Agent Cards would make Pluto agents discoverable by A2A-speaking orchestrators.
- **ACP** (IBM) and **ANP** round out the field but have less coding-agent traction.
- Host status: Cursor and Copilot CLI are full MCP hosts (Copilot CLI GA Feb 2026); **Aider still lacks native MCP** — the PTY friend remains the honest integration path there, which keeps §3.5 relevant.

---

## 5. Proposals: mapping solutions to Pluto changes

The grouping follows what a proposal does to the cost, not its effort. **Group A eliminates a token class outright** — those tokens stop entering the model path at all. **Group B shrinks what remains.** **Group C tracks protocol and positioning.** Frequency tuning (longer polls, fewer heartbeats) appears only as a stopgap inside A2: dividing a recurring cost by nine still leaves it recurring, which is why it is not a headline item here. Effort: S (< 1 day), M (days), L (weeks / host-gated).

### Group A — remove tokens from the model path entirely

#### A1 — Make `pluto_client.py` the high-traffic interface (code-mode) — **S (docs) → M (support)**
The deepest fix for per-interaction cost. For hosts with sandboxed code execution (§4.4), a scripted sequence (`register → send → wait → ack → status`) through `PlutoClient` costs **one tool result** instead of N tool-call turns, and needs **zero Pluto MCP schemas in context**. The client already exists, is standalone, and has no MCP dependency — what's missing is documentation presenting it as the primary interface for message-heavy work, with the MCP tools reserved for occasional/interactive use. Start with a recipe + worked example in `docs/guide/`; then consider a single `pluto_run_script`-style entry point so even non-code-mode hosts can batch a sequence into one call. Addresses §3.2 (schemas) and per-message turn overhead at their root.

#### A2 — Event-driven delivery: retire the watcher chain and heartbeat — **L, host-gated**
Idle-waiting should cost zero model tokens. Promote notification-driven wakeup (`PLUTO_MCP_NOTIFICATIONS` from opt-in to default where the host consumes it) and adopt the MCP Tasks extension with `subscriptions/listen` (§4.1) as hosts ship 2026-07-28 support. The watcher subagent becomes a fallback for notification-blind hosts; `pluto_heartbeat` is deleted along with the stream-silence workaround, and the ~2.9K-token connection block shrinks dramatically once it no longer teaches watcher choreography. This eliminates the unbounded cost class of §3.3 entirely. Files: `notifier.py`, `server.py`, `inbox.py`, `prompts.py`.
*Stopgap only:* while host support lands, raising `wait_timeout_s` from 60 s toward ~570 s (under Claude Code's 600 s watchdog) cuts watcher iterations ~9× — worth taking because it is nearly free, but it dilutes the idle-turn cost rather than removing it. A bridge, not a destination.

#### A3 — Prompts by reference, not by value — **S/M**
Stop inlining all 13.9 KB of `library/protocol.md` into role bodies. Ship a ~1K-token digest inline (the rules agents actually act on per-message) and expose the full text as an MCP resource (`pluto://protocol`) fetched on demand. Cuts orchestrator injection ~8.9K → ~5.5K tokens; a 3-agent team's startup drops ~15%. Apply the same principle to the PTY guide (§3.5), where the win is per-request rather than session-once. Files: `prompts.py:329-344`, `resources.py`, `agent_friend` injection loops.

#### A4 — Shrink the tool-schema surface — **M**
Consolidate 19 tools to ~10-12 parameterized ones (lock/lease variants, low-frequency admin ops behind an `action=` parameter); move delivery-mode *documentation* out of tool descriptions into the connection block or a resource, keeping descriptions terse; where the host supports deferred tool loading (§4.3), mark admin tools deferrable so their schemas never load unless used. Saves ~600-800 tokens per request prefix per agent and improves tool-selection accuracy. Files: `tools.py`, prompts referencing tool names.

### Group B — shrink what remains

#### B1 — Compact serialization and envelope trimming — **S**
Drop `indent=2` in `resources.py`; use `separators=(",", ":")` for anything a model reads; trim piggyback envelopes to `from`/`msg_id`/payload with batch-level `seq_token`; extend noise-event filtering. Saves 20-30% on every tool result carrying inbox data, every turn, every agent. Files: `resources.py`, `inbox.py` (`piggyback`), `tools.py` result shaping.

#### B2 — Payload governance — **M**
Server-side size cap on `pluto_send` (reject or auto-offload above ~8 KiB); generalize the ssh-bridge pattern into first-class **blob offload-by-reference** (large payloads written to a hub-managed spool, message delivers path + excerpt); `detail=compact|full` projection on `pluto_list_agents` / `pluto_task_list` / `pluto_list_locks`, defaulting compact. Bounds worst-case blowups rather than average cost — insurance. Files: `pluto_msg_hub.erl`, `pluto_http_listener.erl`, `tools.py`, `pluto_client.py`.

#### B3 — Cache-stability guarantees — **S/M**
Make byte-stability a tested invariant: assert `tools/list` output and `build_connection_block()` are deterministic for fixed config (no timestamps, no env-dependent strings); document the append-only rule for anything adapters put into context. Protects the Group A/B gains at cache-read pricing; with 2026-07-28 hosts, declare stability via `ttlMs`/`cacheScope` (see C1). Files: `tests/test_mcp_friend.py` (new assertions), `prompts.py`, `tools.py`.

### Group C — protocol and positioning

#### C1 — Adopt MCP 2026-07-28 — **M/L**
Track the stateless core (identity via `_meta`), declare cacheable list results, migrate off deprecated capabilities within the 12-month window (audit `notifier.py`, which sits adjacent to deprecated logging surfaces). Modest direct savings; prerequisite for A2's Tasks route and required maintenance regardless. Files: `server.py`, `notifier.py`, FastMCP version bump.

#### C2 — Interop positioning: Agent Teams complement, A2A Agent Cards — **M, strategic**
Document the complement story (Pluto = cross-CLI messaging + locks/fencing/deadlock underneath native team features such as Claude Code Agent Teams); export A2A Agent Cards from the agent registry (`/agents` HTTP endpoint → card JSON) so Pluto agents are discoverable by A2A orchestrators. No direct token savings; keeps Pluto relevant as native peer messaging matures.

### Sequencing

A1 (documentation-first) and A3 remove the largest token volumes and need no host changes — start there, together with B1. B3 immediately after, to lock the gains in at cache-read pricing. A4 and B2 next. A2 proceeds as host notification/Tasks support lands, taking the long-poll stopgap only if watcher costs bite before then. C1/C2 can proceed anytime.

---

## 6. Open questions and risks

- **Correctness is not negotiable for tokens.** At-least-once delivery (peek/ack), fencing tokens, and the no-silent-re-register contract (`inbox.py`, commit 8d0ca13) must survive every optimization. B2's size cap needs an explicit failure mode (reject with error vs. auto-offload) so senders never silently lose data.
- **Host watchdog empiricism (A2 stopgap).** The 600 s figure is Claude Code's documented stream-silence limit; other hosts (Gemini CLI, Copilot CLI) have their own tolerances. Any long-poll default may need per-host profiles.
- **Cache-invalidation trap.** A well-intended "small" dynamic touch — embedding the agent id in a tool description, a version string in instructions — silently converts ~10K tokens/request from cache-read to full price. This is why B3 is a *test*, not a guideline.
- **Deferral vs. consolidation tension (A4/A1).** If deferred tool loading and code-mode become universal across hosts, aggressive consolidation loses value (schemas would be lazy-loaded or bypassed anyway). Consolidation is still worth doing now; revisit if host coverage reaches Gemini/Copilot/Cursor.
- **Measurement debt.** All figures here use `chars/4`. Before/after numbers for the Group A/B items should be re-measured with a real tokenizer and recorded in this directory.

---

## 7. References

**Protocol and vendor**
- MCP 2026-07-28 specification — https://modelcontextprotocol.io/specification/2026-07-28 and release post — https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP 2026 roadmap — https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/
- Claude Code Agent Teams (experimental multi-session teams; see e.g. https://shipyard.build/blog/claude-code-multi-agent/)
- A2A protocol v1, Linux Foundation — overview: https://onereach.ai/blog/what-is-a2a-agent-to-agent-protocol/

**Research**
- TokenPilot: Cache-Efficient Context Management for LLM Agents — https://arxiv.org/html/2606.17016v1
- Persistent Q4 KV Cache for Multi-Agent LLM Inference — https://arxiv.org/html/2603.04428v1
- Communication-Centric Survey of LLM-Based Multi-Agent Systems — https://arxiv.org/pdf/2502.14321

**In-repo prior art**
- `library/protocol.md` §7 — Spec Contracts (token-saving boilerplate factoring)
- `src_py/agent_mcp_friend/prompts.py:201-238` — payload conventions
- `library/roles/ssh-bridge.md` — output truncation + logs-by-path
- `docs/technical/pluto-mcp-friend.md` — delivery mechanisms and rollout phases (Phase 3 = the A2 stopgap here)

**Re-measuring.** From the repo venv: import `src_py/agent_mcp_friend/prompts.py`, call the builders with representative config, and take `len(text)/4`; for tool schemas, sum description lengths in `tools.py` registrations. Prefer a real tokenizer (e.g. the provider's counting endpoint) for publishable numbers.
