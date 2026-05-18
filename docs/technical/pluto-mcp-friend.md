# PlutoMCPFriend — Technical Reference

This page is the implementation-level companion to the user-facing
`docs/guide/pluto-mcp-friend.md`. It documents what the adapter is, how
it delivers Pluto messages into a request/response agent without async
push, and the v0.2.9+ "medium-bucket" hardening of the inbox watcher.

---

## 1. Overview

`PlutoMCPFriend` is the MCP-server adapter that wraps the Pluto
coordination server for MCP-capable agent clients (Claude Code primarily).
It runs as a long-lived stdio subprocess and exposes Pluto operations
(`pluto_send`, `pluto_lock_*`, `pluto_task_*`, …) as MCP tools.

### Process layout

```
   ┌──────────────────────┐    stdio JSON-RPC    ┌──────────────────────┐
   │ Claude Code (host)   │ ───────────────────► │ pluto_mcp_friend.py  │
   │  ↳ model / subagents │ ◄─────────────────── │  PlutoMCPServer      │
   └──────────────────────┘                      │   ├── FastMCP        │
                                                 │   ├── InboxManager   │
                                                 │   ├── LockManager    │
                                                 │   └── HTTP client    │
                                                 └──────────┬───────────┘
                                                            │ HTTP
                                                            ▼
                                                  ┌───────────────────┐
                                                  │  Pluto Erlang     │
                                                  │  coordination svc │
                                                  └───────────────────┘
```

### Responsibilities owned by the adapter

- **Session lifecycle** — registers with Pluto on startup, holds the
  session token, unregisters cleanly on shutdown. The agent never sees
  the token.
- **Inbox loop** — a background coroutine peeks Pluto at ~1 Hz, filters
  noise, dedupes by `seq_token`, and buffers actionable messages
  (`InboxManager`).
- **Tool-result piggyback** — every Pluto tool's return is wrapped with
  any pending buffered messages under `_pluto_inbox`, and those messages
  are acked. The wrap shape is governed by the active **delivery mode**
  (see §1.1): `"batch"` (default) attaches all buffered messages,
  `"single"` attaches the head message plus an
  `_pluto_inbox_remaining` counter.
- **Lock auto-renewal** — locks acquired with `auto_renew=true` get
  background TTL/2 renewals (`LockManager`).
- **Snapshots / restore** — `pluto_snapshot_self` / `--restore <.plut>`
  for crash-recovery.
- **Prompt assembly** — exposes `pluto-role-*`, `pluto-protocol`,
  `pluto-guide`, `pluto-check`, `pluto-watch`, `pluto-watch-stop`,
  `pluto-status` as MCP prompts the user invokes via slash menu.

### The fundamental constraint

MCP-capable agents (Claude Code today) are **request/response**. There
is no in-band channel by which the adapter can push a message into a
mid-turn model context. Every delivery mechanism is layered on top of
that constraint:

| Channel | Latency | Reliability | Requires |
|---|---|---|---|
| `pluto_recv` at turn start | turn-driven | high | model follows role |
| `pluto_pop` per inbox event | per-message | high | model in single-mode + watcher / notification wake |
| `_pluto_inbox` piggyback on any Pluto tool call | turn-driven | high | model uses a Pluto tool that turn |
| Background watcher subagent calling `pluto_inbox_watch` | chat-speed | medium | subagent inherits the MCP server |
| `notifications/inboxMessage` (phase 2, flagged) | chat-speed | host-dependent | host surfaces MCP notifications to model |

`pluto_recv` and `_pluto_inbox` are the correctness path. The watcher
is a chat-speed optimization. Notifications are a future optimization
behind `PLUTO_MCP_NOTIFICATIONS`. `pluto_pop` is the pipeline /
event-driven counterpart to `pluto_recv` — see §1.1.

---

## 1.1 Delivery modes — batch vs. single

`InboxManager.delivery_mode` toggles how the buffer is surfaced to the
agent. Switch at runtime via `pluto_set_delivery_mode(mode)`; observe
the active value in `pluto_session.delivery_mode`.

| Mode | `pluto_recv` / `drain()` | `piggyback` wrap | Canonical consumer | Best for |
|---|---|---|---|---|
| `"batch"` (default) | returns ALL buffered messages, acks the batch | `{_pluto_inbox: [...all msgs...]}` | `pluto_recv` + piggyback | turn-driven / interactive agents |
| `"single"` | unchanged (still drains all — fallback) | `{_pluto_inbox: [head], _pluto_inbox_remaining: N}` | `pluto_pop` (one at a time) | pipeline / event-driven agents |

**Why a mode flag and not just `pluto_pop`?** Every Pluto tool result
routes through `piggyback()`. In batch mode, an unrelated `pluto_send`
or `pluto_lock_*` call would silently bulk-drain the buffer alongside
the agent's pop loop — breaking the one-message-per-turn invariant.
Single-mode rewires `piggyback` to attach exactly one message + a
`remaining` counter, so the one-at-a-time contract holds across the
whole tool surface.

**`pluto_pop(wait_s=0.0)`** — pop and ack a single message; returns
`{message, remaining, empty, delivery_mode}`. When the buffer is empty
and `wait_s > 0`, blocks on `_new_message_event` up to that many
seconds (event-driven, no polling). The intended loop:

```
notification / watcher wake
  → pluto_pop(wait_s=<short>)
  → process msg
  → while remaining > 0: pluto_pop() (no wait — head must be there)
  → yield, wait for next wake
```

At-least-once semantics match the existing drain path: the message is
removed from the in-memory buffer before the server-side ack, so a
crash between pop and ack relies on process restart clearing
`_seen_seqs` to allow re-absorption on the next peek.

---

## 2. Medium-bucket watcher hardening (v0.2.9+)

The legacy watcher pattern asked the **subagent** to own the long-poll
loop: it called `pluto_wait_for_messages(N)` in a tight Python-style
loop, breaking out on messages, looping on empty. Three problems:

1. **Stacked loops on respawn.** Parent re-arms the watcher while an
   older Task is still finishing → two subagents both calling
   `pluto_wait_for_messages` independently, duplicating work and
   wakeups.
2. **Brittle iteration math.** Iteration count was a fixed CLI arg,
   not derived from the current `wait_timeout_s`, so changing one
   silently drifted the budget.
3. **No error backoff.** A transient Pluto failure caused the subagent
   to either return immediately (empty result) or hammer the server.

The v0.2.9+ medium-bucket rewrite moves loop ownership into the
adapter. Watcher state and policy live server-side; the subagent
contracts down to two tool calls in alternation.

### 2.1 Server-owned durable loop — `pluto_inbox_watch`

New MCP tool, defined in `src_py/agent_mcp_friend/tools.py` and backed
by `InboxManager.watch_durable` in `inbox.py`.

```
pluto_inbox_watch(inbox_id="default",
                  wait_timeout_s=<launcher default>,
                  max_total_s=1800)
```

Semantics:

- **Adaptive iteration.** Internally iterates
  `wait_for_messages(timeout_s=wait_timeout_s)` slices.
  `max_iters = ceil(max_total_s / wait_timeout_s)`, recomputed every
  call so changes to `--wait-timeout-s` track automatically.
- **Soft condition — empty slice.** Immediately re-enter the next
  slice with no sleep. The blocking `wait_for_messages` itself
  provides the wait.
- **Hard condition — raised exception.** Exponential backoff capped at
  30 s, then continue. After 10 consecutive errors the call returns
  with `{error: "too_many_errors", last_error: ...}` so the client can
  decide whether to retry.
- **Return shape.** Always a dict with `messages`, `count`, and a
  `watcher_id` (equal to `inbox_id`). Includes `iterations` and either
  `timeout: true` or an `error` field when applicable.

### 2.2 Server-side dedupe — `(agent_id, inbox_id)`

`InboxManager._active_watchers` is a `set[str]` of currently-running
durable watcher keys (the `inbox_id`). On `watch_durable` entry:

```python
if key in self._active_watchers:
    return {"already_watching": True,
            "watcher_id": key,
            "messages": [], "count": 0}
self._active_watchers.add(key)
try:
    ...
finally:
    self._active_watchers.discard(key)
```

The agent_id half of the key is implicit — each adapter process is
already scoped to one agent. The behavior is **keep + swap**: the
existing waiter keeps running and continues to receive the message;
the new caller exits immediately so it can resume its own loop (call
heartbeat, retry watch, etc.) without stacking a second blocking call
into the shared peek path.

This makes subagent respawn cheap and idempotent. The parent can
re-arm the watcher Task without worrying that the prior watcher is
still in flight.

### 2.3 Heartbeat tool — `pluto_heartbeat`

```
pluto_heartbeat()  →  {ok, ts, agent_id, connected, mcp_inherited}
```

No network call. Its only job is to produce a tool-call result so
Claude Code's 600 s stream-silence watchdog never kills a subagent
that is legitimately idle between `pluto_inbox_watch` slices. The
subagent contract is the two-call loop:

```
loop {
  pluto_inbox_watch(...)
  pluto_heartbeat()
}
```

### 2.4 MCP-inheritance probe — `PLUTO_MCP_INHERITED`

On startup the adapter reads `PLUTO_MCP_INHERITED` and surfaces a
tri-state through `pluto_session` and `pluto_heartbeat`:

| Env value | `mcp_inherited` | `watcher_available` | Meaning |
|---|---|---|---|
| `1` / `true` / `yes` / `on` | `True` | `True` | Host advertises subagents inherit this MCP server |
| `0` / `false` / `no` / `off` | `False` | `False` | Host advertises they do **not** inherit |
| unset / other | `None` | `True` (assume yes) | Unknown — try once, observe |

When `watcher_available` is `False` the role prompt instructs the
agent to skip the watcher subagent entirely and fall back to
`pluto_recv` at turn start. This replaces the previous "model
discovers via error-string sniffing" approach, which was brittle.

The probe is **advisory only**. Hosts that don't set the variable get
the legacy best-effort behavior: try once, observe the failure pattern
in the subagent's tool-call log, and disable for the session.

### 2.5 Notifications — `PLUTO_MCP_NOTIFICATIONS` (phase 2)

When the env var is truthy, `PlutoMCPServer` instantiates a
`Notifier` (`notifier.py`) and attaches it to `InboxManager`. The
notifier captures the live `ServerSession` on every tool call
(`tools.py:_bind_session`) so background paths — the inbox peek loop,
the watcher's hard-error branch — can fire frames without their own
request context.

**Wire format.** Each event fires two MCP notification frames:

| Event | Standard frame | Structured frame |
|---|---|---|
| New inbox messages | `notifications/resources/updated` for `pluto://inbox` | `notifications/message` (`level=info`, `logger=pluto.inbox`, `data.event=pluto.inboxMessage`) |
| Hard watcher failure | — | `notifications/message` (`level=warning`, `logger=pluto.watcher`, `data.event=pluto.watcherError`) |

The standard frame lets MCP-compliant clients refresh subscribed
resources without knowing anything Pluto-specific. The structured
frame embeds a discriminated payload (`data.event`) so Pluto-aware
hosts can route on it. Hosts that understand neither silently drop —
the long-poll path remains the correctness channel either way.

**Telemetry.** Whether the host actually converts notifications into
new model turns is host-side and unobservable from the server. We
instead measure delivery latency: `InboxManager._landed_at[seq_token]`
records when a message lands in the buffer, and `_ack_messages`
computes the delta to drain time, feeding `Notifier.record_drain_latency_ms`.
A capped 1000-sample ring is summarized in `pluto_session` as
`notifications.drain_mean_ms` / `drain_p95_ms` plus fired-event
counters. Comparing those numbers with notifications on vs. off tells
us whether the host is consuming them.

**Failure handling.** All notifier sends are wrapped in try/except;
failures bump `send_failures` and are debug-logged but never propagate
to the delivery path.

---

## 3. End-to-end delivery sequence

A message arriving at the Pluto server while an agent is idle:

```
1. Pluto server         → enqueues message for <agent_id>
2. InboxManager loop    → peek() returns the new message
3. InboxManager._absorb → dedupe by seq_token, append to buffer,
                          fire _new_message_event
4. (a) watch_durable    → slice returns the message → tool result lands
       in subagent      → bubbles to parent on Task completion
                          (chat-speed delivery)
   (b) OR single-mode   → notification / watcher wake → pluto_pop()
       agent            → one message per call + remaining counter
                          (per-message pipeline delivery)
   (c) OR no watcher    → message stays buffered until next pluto_recv
       active           → or _pluto_inbox piggyback on next Pluto tool
                          (turn-speed delivery)
5. piggyback() / drain() / pop_one()
                        → ack(seq_token) → server marks delivered
```

If the adapter crashes between steps 3 and 5, Pluto's at-least-once
delivery resurfaces the message on the next session's peek, keyed off
the last acked seq.

---

## 4. Configuration matrix

| Knob | Source | Default | Effect |
|---|---|---|---|
| `--agent-id` | CLI | required | Pluto identity to register |
| `--host` / `--http-port` | CLI / `pluto_server.json` | `127.0.0.1` / `9201` | Pluto HTTP endpoint |
| `--ttl-ms` | CLI | `600_000` | Session TTL; renewed by inbox loop |
| `--wait-timeout-s` | CLI | `60` | Per-slice block in watcher loop |
| `--iterations` | CLI | `15` | Subagent respawn budget for `/pluto-watch` |
| `--restore <path>` | CLI | unset | Apply a `.plut` after register |
| `PLUTO_MCP_INHERITED` | env | unset | Inheritance probe verdict |
| `PLUTO_MCP_NOTIFICATIONS` | env | unset | Enables notification seam (no-op today) |
| `delivery_mode` | `pluto_set_delivery_mode` runtime call | `"batch"` | `"batch"` drains the whole buffer per `pluto_recv` / piggyback; `"single"` surfaces one message per call (head + `_pluto_inbox_remaining`), making `pluto_pop` the canonical consumer |

---

## 5. Files of interest

| Path | Role |
|---|---|
| `src_py/agent_mcp_friend/pluto_mcp_friend.py` | CLI entry point |
| `src_py/agent_mcp_friend/server.py` | `PlutoMCPServer`, lifecycle, notification seam |
| `src_py/agent_mcp_friend/tools.py` | All `pluto_*` MCP tools, inheritance probe |
| `src_py/agent_mcp_friend/inbox.py` | `InboxManager`, durable watcher, dedupe |
| `src_py/agent_mcp_friend/lock_manager.py` | Lock auto-renewal |
| `src_py/agent_mcp_friend/prompts.py` | Role + connection prompt assembly |
| `src_py/agent_mcp_friend/resources.py` | `pluto://inbox`, `pluto://locks` resources |

---

## 6. Rollout phases

| Phase | Scope | Flag | Status |
|---|---|---|---|
| 1 | Server-owned durable watcher, dedupe, adaptive iterations, error backoff, heartbeat, inheritance probe | — | shipped v0.2.9 |
| 2 | `notifications/resources/updated` + structured `notifications/message` for inboxMessage + watcherError; drain-latency telemetry; payload conventions (drop `from_role`, `spec_contract` once, `conv_seq` per-message); end-of-turn-only watcher respawn | `PLUTO_MCP_NOTIFICATIONS` | shipped |
| 3 | Optional tuning (longer `wait_timeout_s`, relaxed heartbeat) once phase 2 telemetry shows host consumption | — | not started |

The phase ordering is deliberate: correctness path first, optimization
on top. Notifications are not a replacement for the long-poll loop;
they are an opportunistic wakeup that arrives on top of an already-
boring delivery channel.
