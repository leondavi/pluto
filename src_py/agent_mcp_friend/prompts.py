"""Prompt assembly for PlutoMCPFriend.

Each role file in ``library/roles/*.md`` becomes an MCP prompt
``pluto-role-<name>``. The prompt body is the same role + protocol +
live-connection block that ``PlutoAgentFriend._role_injection_loop``
builds today, lifted into a reusable :func:`build_role_prompt_body`.

A standalone ``pluto-protocol`` prompt exposes the shared protocol on
its own; ``pluto-guide`` exposes the agent guide. Users invoke any of
these via Claude Code's slash menu (``/pluto-role-specialist`` etc.).
"""

from __future__ import annotations

import os
from typing import Iterable

# Resolve the project root relative to this file. Layout:
#   <project>/src_py/agent_mcp_friend/prompts.py
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))


def project_path(*parts: str) -> str:
    return os.path.normpath(os.path.join(_PROJECT_ROOT, *parts))


def default_roles_dir() -> str:
    return project_path("library", "roles")


def default_protocol_path() -> str:
    return project_path("library", "protocol.md")


def default_guide_path() -> str:
    return project_path("agent_friend_guide.md")


def list_role_names(roles_dir: str | None = None) -> list[str]:
    """Return the bare names (no extension) of every ``*.md`` in *roles_dir*.

    Returns an empty list if the directory doesn't exist.
    """
    target = roles_dir or default_roles_dir()
    try:
        entries = sorted(os.listdir(target))
    except OSError:
        return []
    return [
        os.path.splitext(name)[0]
        for name in entries
        if name.endswith(".md") and not name.startswith("_") and name != "README.md"
    ]


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_connection_block(
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
    iterations: int = 15,
) -> str:
    """The live-connection block appended to every prompt body.

    *wait_timeout_s* is embedded into the recommended
    ``pluto_wait_for_messages(N)`` call so the watcher cycle matches
    the launcher's ``--wait-timeout-s`` value.

    *iterations* sets how many short-poll cycles the watcher subagent
    runs before exiting and letting the parent respawn it. Total
    subagent lifetime ≈ ``iterations * wait_timeout_s`` seconds.
    """
    return (
        f"---\n\n"
        f"**Live Pluto server connection** (injected by PlutoMCPFriend — "
        f"use these values; do not hardcode addresses from the role file):\n"
        f"  Host:      {host}\n"
        f"  HTTP port: {http_port}   (REST API base for /agents/* and /locks/*)\n"
        f"  Base URL:  http://{host}:{http_port}\n"
        f"  Agent ID:  {agent_id}\n\n"
        f"You are wrapped by **PlutoMCPFriend** running inside Claude Code.\n"
        f"The server has registered you and exposes Pluto operations as MCP\n"
        f"tools (``pluto_send``, ``pluto_lock_acquire``, etc.). Prefer those\n"
        f"tools over raw curl calls — the wrapper injects your session token\n"
        f"automatically and acks inbox messages on your behalf.\n\n"
        f"## 🔁 MANDATORY: drain the inbox at the start of every reply\n\n"
        f"At the start of every single reply you produce, **before any\n"
        f"other tool call or any other text**, call ``pluto_recv`` once\n"
        f"and process whatever comes back. Treat this as a non-negotiable\n"
        f"reflex — the user does not need to ask, and you do not need to\n"
        f"announce it.\n\n"
        f"This is the only delivery mechanism that works reliably on every\n"
        f"Claude Code setup: turn-driven pulling. The watcher pattern\n"
        f"below is best-effort and may not work in your configuration.\n\n"
        f"## ⚡ MANDATORY: auto-start a background inbox watcher\n\n"
        f"On your very first turn — before replying to the user with\n"
        f"anything beyond the role-acknowledgement — you MUST spawn a\n"
        f"background Pluto inbox watcher, and respawn one whenever the\n"
        f"previous watcher Task completes. This is not optional and\n"
        f"applies to every role; only stop re-arming if the watcher\n"
        f"fails entirely (see the failure section below). Use the Task\n"
        f"tool with **all** these parameters:\n\n"
        f"    description       = \"Pluto inbox watcher\"\n"
        f"    run_in_background = true\n"
        f"    subagent_type     = \"general-purpose\"\n"
        f"    prompt            = (the watcher prompt below — verbatim)\n\n"
        f"### The subagent prompt — peek-mode watcher + heartbeats\n\n"
        f"```\n"
        f"You are a Pluto inbox watcher. Loop these two tool calls, nothing\n"
        f"else, for up to {iterations} cycles:\n"
        f"  1. Call pluto_inbox_watch(inbox_id=\"default\",\n"
        f"     wait_timeout_s={wait_timeout_s}, drain=false). This blocks\n"
        f"     for up to {wait_timeout_s} s, then returns a non-consuming\n"
        f"     snapshot of the parent's inbox. drain=false is REQUIRED:\n"
        f"     this subagent shares the parent's MCP server, so consuming\n"
        f"     would steal the parent's messages.\n"
        f"  2. If the response has count > 0, STOP and return — the parent\n"
        f"     has unread messages and will drain them via pluto_recv. Do\n"
        f"     NOT loop further; do NOT respawn yourself. {{already_watching:\n"
        f"     true}} also means stop (an earlier watcher is in flight).\n"
        f"     Otherwise call pluto_heartbeat (free, no-network) and go\n"
        f"     back to step 1.\n"
        f"Do NOT add any sleep, delay, or pause commands — the watch call\n"
        f"itself provides all the waiting, and the heartbeat is free.\n"
        f"Do NOT produce conversational text; the tool calls are the output.\n"
        f"```\n\n"
        f"Why this works: each ``pluto_inbox_watch`` tool call returns\n"
        f"inside a single {wait_timeout_s} s slice (the server's\n"
        f"``max_total_s`` defaults to ``wait_timeout_s``), so Claude\n"
        f"Code's 600 s stream-silence watchdog sees fresh tool-call\n"
        f"activity at least every {wait_timeout_s} s. ``drain=false``\n"
        f"keeps the messages in the parent's buffer for the parent's\n"
        f"``pluto_recv`` to consume — the watcher is a *signal*, not a\n"
        f"delivery channel. ``(agent_id, inbox_id)`` dedupe protects\n"
        f"against accidental double-watchers. Total subagent lifetime is\n"
        f"bounded by {iterations} cycles (~{iterations * wait_timeout_s} s)\n"
        f"of empty slices before the parent respawns; in practice the\n"
        f"watcher exits early on the first non-empty slice.\n\n"
        f"### When the watcher fails entirely\n\n"
        f"On some Claude Code configurations subagents do **not** inherit\n"
        f"the parent's MCP server access. The MCP adapter probes for this\n"
        f"at startup via the ``PLUTO_MCP_INHERITED`` env var — call\n"
        f"``pluto_session`` to read the current verdict:\n\n"
        f"  • ``watcher_available: true``  → spawn watchers normally.\n"
        f"  • ``watcher_available: false`` → host has advertised that\n"
        f"    subagents will not see this MCP server; **skip the watcher\n"
        f"    entirely** and rely on the start-of-reply ``pluto_recv``.\n"
        f"  • ``mcp_inherited: null``       → unknown; try once, observe.\n\n"
        f"Runtime symptom of an undetected mismatch: the very first tool\n"
        f"call inside the subagent errors with \"tool unavailable\" or\n"
        f"similar, and the Task completes in seconds with no ``pluto_*``\n"
        f"activity at all. If you see that pattern, **stop spawning\n"
        f"watchers for the rest of the session** — re-arming them just\n"
        f"burns subagent budget. Tell the user once that the watcher\n"
        f"pattern isn't available in this setup, then rely entirely on\n"
        f"the mandatory ``pluto_recv`` at the start of every reply\n"
        f"(above) plus the user invoking ``/pluto-check`` when they want\n"
        f"to flush.\n\n"
        f"### When the Task completes normally\n\n"
        f"Whenever a watcher Task ends — whether it returned messages,\n"
        f"completed all {iterations} iterations empty, or timed out — **immediately\n"
        f"spawn another watcher Task** with the same parameters. The\n"
        f"subagent self-bounds at ~{iterations * wait_timeout_s} s; the parent\n"
        f"chain keeps a watcher in flight indefinitely.\n\n"
        f"### Watcher respawn — end-of-turn only\n\n"
        f"Respawn the watcher Task **only as the last thing you do before\n"
        f"yielding control back to the user**, never mid-turn. Concretely:\n\n"
        f"  • If a drain happens earlier in the turn (via ``pluto_recv``,\n"
        f"    piggybacked ``_pluto_inbox``, or a watcher returning) do not\n"
        f"    re-arm immediately. Finish your other work first.\n"
        f"  • As your final tool call before replying, check whether a\n"
        f"    watcher is in flight by calling ``pluto_session`` and\n"
        f"    reading ``watchers.active``. **If ``watchers.active >= 1``,\n"
        f"    DO NOT spawn another watcher** — one is already running\n"
        f"    (the grace window keeps the slot occupied between slices, so\n"
        f"    this check is reliable even when no tool call is in flight).\n"
        f"    Only spawn if ``watchers.active == 0``.\n"
        f"  • If the turn ends with another Pluto tool call as its last\n"
        f"    action, you may skip the respawn — that call's result has\n"
        f"    already piggybacked any inbox state and is itself a\n"
        f"    delivery point.\n\n"
        f"Why the explicit check: per-tool-call dedupe alone (the\n"
        f"``{{already_watching: true}}`` response) only catches overlapping\n"
        f"slices. Between slices a peer watcher subagent can sneak past,\n"
        f"giving you two long-lived watcher Tasks burning subagent budget\n"
        f"on the same inbox. The ``watchers.active`` field is grace-\n"
        f"windowed precisely to close that gap.\n\n"
        f"**Exception — ``watcher_stop``.** If the user has invoked the\n"
        f"``/pluto-watch-stop`` slash command (or said ``watcher_stop``)\n"
        f"this session, treat that as a session-level kill switch: stop\n"
        f"auto-respawning on drain, do not start new watcher Tasks, and\n"
        f"fall back to turn-driven ``pluto_recv`` only. The kill switch\n"
        f"stays in effect until the user invokes ``/pluto-watch`` to\n"
        f"explicitly resume the watcher chain.\n\n"
        f"## Payload conventions — keep messages lean\n\n"
        f"Pluto's envelope already carries routing fields (``from``,\n"
        f"``to``, ``seq_token``, ``event``). Do **not** duplicate them\n"
        f"into your payload. Specifically:\n\n"
        f"  • Drop ``from_role`` — the envelope's ``from`` plus a one-time\n"
        f"    ``spec_contract`` (see below) lets the recipient resolve\n"
        f"    your role without per-message overhead.\n"
        f"  • Drop protocol name, total-message counts, ack-semantics\n"
        f"    boilerplate, and round-robin order. These are **structural**\n"
        f"    facts about the session — publish them once.\n\n"
        f"### spec_contract — one per session, structural only\n\n"
        f"At the start of a multi-agent conversation, the convening agent\n"
        f"broadcasts (or directly sends) a single ``spec_contract``\n"
        f"message that captures everything which would otherwise repeat\n"
        f"on every turn:\n\n"
        f"```json\n"
        f"{{\n"
        f"  \"event\": \"spec_contract\",\n"
        f"  \"protocol\": \"pluto-roundtable/1\",\n"
        f"  \"conversation_id\": \"<short-id>\",\n"
        f"  \"participants\": [{{ \"agent_id\": \"...\", \"role\": \"...\" }}],\n"
        f"  \"order\": [\"agent-a\", \"agent-b\", ...],\n"
        f"  \"ack_semantics\": \"at-least-once via seq_token\",\n"
        f"  \"conventions\": [\"conv_seq monotonic per sender\", \"...\"]\n"
        f"}}\n"
        f"```\n\n"
        f"After you've seen the ``spec_contract`` for a conversation,\n"
        f"trust it — do not re-broadcast equivalents.\n\n"
        f"### conv_seq — per-conversation ordering, not the inbox seq\n\n"
        f"For ordering replies within a conversation, put a ``conv_seq``\n"
        f"integer in your **payload**, scoped to ``conversation_id`` and\n"
        f"monotonic per sender. Do NOT reuse the envelope's ``seq_token``\n"
        f"for this — that's Pluto's global ack cursor and is owned by the\n"
        f"server.\n\n"
        f"Rule of thumb: **structural info** (protocol, roles, order,\n"
        f"ack semantics) belongs in ``spec_contract`` once; **routing\n"
        f"info** (which conversation, which turn) belongs in each\n"
        f"per-message payload.\n\n"
        f"## Other ways messages can reach you\n\n"
        f"1. **Piggyback (free).** Any Pluto tool result includes any\n"
        f"   pending messages under ``_pluto_inbox``. Process those before\n"
        f"   continuing. (This counts as a drain — see the respawn rule\n"
        f"   above.)\n"
        f"2. **User-invoked**. The user can type ``/pluto-check`` to\n"
        f"   force-drain the inbox, ``/pluto-watch`` to (re)start the\n"
        f"   watcher chain, or ``/pluto-watch-stop`` to engage the\n"
        f"   ``watcher_stop`` kill switch and disable auto-respawn for\n"
        f"   the rest of the session.\n\n"
        f"## What to do if the MCP link drops\n\n"
        f"If a ``pluto_*`` tool call returns an error like \"tool not\n"
        f"available\", \"server disconnected\", or fails outright, the\n"
        f"stdio transport between Claude Code and the PlutoMCPFriend\n"
        f"adapter has dropped. This is a Claude Code transport issue,\n"
        f"not a Pluto server issue — the Pluto server may still be\n"
        f"running fine.\n\n"
        f"To self-diagnose, call ``pluto_session`` (a read-only,\n"
        f"network-free probe). If even that errors, the MCP transport\n"
        f"is the problem. Tell the user once, in one sentence:\n\n"
        f"    *The Pluto MCP transport has dropped — please run\n"
        f"     ``/mcp`` in Claude Code to refresh the connection,\n"
        f"     then I'll resume.*\n"
        f"\n"
        f"Do not keep retrying tools — the agent can't restore the\n"
        f"transport from inside; only the user's ``/mcp`` slash command\n"
        f"can. After they refresh, your next ``pluto_recv`` will\n"
        f"surface anything that queued up while we were offline."
    )


def build_role_prompt_body(
    role_name: str,
    *,
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
    iterations: int = 15,
    roles_dir: str | None = None,
    protocol_path: str | None = None,
) -> str:
    """Assemble the full text of the ``pluto-role-<role_name>`` prompt.

    Parts (in order):
      1. The role file content (inlined verbatim).
      2. The ``library/protocol.md`` content, if the role mentions it
         (matching ``_role_injection_loop``'s heuristic).
      3. The live-connection block from :func:`build_connection_block`.
    """
    roles = roles_dir or default_roles_dir()
    role_path = os.path.join(roles, f"{role_name}.md")
    if not os.path.isfile(role_path):
        raise FileNotFoundError(f"Role file not found: {role_path}")

    role_content = _read_file(role_path).strip()

    protocol_block = ""
    proto = protocol_path or default_protocol_path()
    if "protocol.md" in role_content and os.path.isfile(proto):
        try:
            protocol_text = _read_file(proto)
            protocol_block = (
                "\n\n---\n\n"
                "Your role above references `protocol.md`. The full shared "
                "coordination protocol is inlined below for convenience "
                f"(source: {proto}). Treat this as authoritative — do NOT "
                "attempt to re-read the file from disk; your CWD may not "
                "contain it.\n\n"
                "=== BEGIN protocol.md ===\n\n"
                f"{protocol_text}\n\n"
                "=== END protocol.md ==="
            )
        except OSError:
            pass

    connection_block = build_connection_block(
        host, http_port, agent_id,
        wait_timeout_s=wait_timeout_s, iterations=iterations,
    )

    role_basename = os.path.basename(role_path)
    return (
        f"You have been assigned a specific role for this session.\n"
        f"Read and internalize the following role description from "
        f"{role_basename}, then confirm briefly that you understand your "
        f"role and are ready to begin:\n\n"
        f"{role_content}{protocol_block}\n\n"
        f"{connection_block}"
    )


def build_protocol_prompt_body(
    *,
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
    iterations: int = 15,
    protocol_path: str | None = None,
) -> str:
    """Standalone ``pluto-protocol`` prompt: just protocol + connection."""
    proto = protocol_path or default_protocol_path()
    try:
        text = _read_file(proto)
    except OSError as exc:
        text = f"(could not read {proto}: {exc})"
    return (
        f"=== BEGIN protocol.md ===\n\n"
        f"{text}\n\n"
        f"=== END protocol.md ===\n\n"
        f"{build_connection_block(host, http_port, agent_id, wait_timeout_s, iterations)}"
    )


def build_guide_prompt_body(
    *,
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
    iterations: int = 15,
    guide_path: str | None = None,
) -> str:
    """Standalone ``pluto-guide`` prompt: agent guide + connection."""
    path = guide_path or default_guide_path()
    try:
        text = _read_file(path)
    except OSError as exc:
        text = f"(could not read {path}: {exc})"
    return (
        f"{text}\n\n"
        f"{build_connection_block(host, http_port, agent_id, wait_timeout_s, iterations)}"
    )


def build_check_prompt_body() -> str:
    """One-shot inbox check — invoked via the /pluto-check slash command."""
    return (
        "Call ``pluto_recv`` with no arguments.\n\n"
        "If the response's ``count`` is greater than zero, summarize each "
        "message in one line each: ``[from -> event] one-line summary of "
        "payload``. Then ask whether to act on them.\n\n"
        "If ``count`` is zero, reply exactly: ``inbox is empty``.\n\n"
        "**At end of this turn**, if no Pluto inbox watcher is currently "
        "in flight and the ``watcher_stop`` kill switch is not engaged, "
        "spawn a fresh one (same parameters as your role's default "
        "watcher) as your last tool call. Do NOT spawn it before "
        "summarizing the drained messages — end-of-turn only."
    )


def build_watch_prompt_body(
    wait_timeout_s: int = 300, iterations: int = 15,
) -> str:
    """Start a long-poll watcher — invoked via the /pluto-watch slash command."""
    return (
        "Start watching the Pluto inbox so messages arrive without me "
        "having to ask. **Invoking this also clears any in-effect "
        "``watcher_stop`` kill switch** — the auto-respawn-on-drain rule "
        "from your role connection block is back on.\n\n"
        "Spawn a background Task using the Task tool with **all** these "
        "parameters:\n\n"
        "  • ``run_in_background=true``\n"
        "  • ``subagent_type=\"general-purpose\"``\n"
        "  • ``description=\"Pluto inbox watcher\"``\n"
        "  • ``prompt`` = the looping subagent prompt below, verbatim:\n\n"
        "```\n"
        "You are a Pluto inbox watcher. Loop these two tool calls for up\n"
        f"to {iterations} cycles:\n"
        f"  1. pluto_inbox_watch(inbox_id=\"default\",\n"
        f"     wait_timeout_s={wait_timeout_s}, drain=false). Blocks for\n"
        f"     up to {wait_timeout_s} s, then returns a non-consuming\n"
        "     snapshot. drain=false is REQUIRED — this subagent shares\n"
        "     the parent's MCP server, so consuming would steal messages.\n"
        "  2. If count > 0, STOP and return — the parent will drain via\n"
        "     pluto_recv. {already_watching: true} also means stop.\n"
        "     Otherwise call pluto_heartbeat (free) and loop.\n"
        "Do NOT add sleep/delay; the watch call provides all the waiting.\n"
        "Do NOT emit conversational text — tool calls are the output.\n"
        "```\n\n"
        f"Each watch slice blocks for up to {wait_timeout_s} s and then\n"
        f"the tool call returns, so Claude Code's 600 s stream-silence\n"
        f"watchdog sees fresh activity every cycle. The watcher exits\n"
        f"early on the first non-empty slice (count > 0) — the parent's\n"
        f"pluto_recv handles delivery. Total subagent lifetime is bounded\n"
        f"by {iterations} cycles of empty slices.\n\n"
        f"When the Task completes (any reason — messages, empty after {iterations} "
        "iterations, or watchdog), **immediately spawn another watcher "
        "Task** with the same parameters. The chain keeps a watcher in "
        "flight indefinitely.\n\n"
        "**Failure detection.** If the Task completes in seconds with "
        "no evidence of any ``pluto_inbox_watch`` call (meaning "
        "the subagent doesn't have MCP access here), **stop re-arming** "
        "— tell me the watcher pattern isn't available in this setup "
        "and rely on the start-of-every-reply ``pluto_recv`` from your "
        "role.\n\n"
        "Reply briefly with ``watcher started`` once the first Task is "
        "in flight, or ``watcher unavailable — falling back to "
        "pluto_recv`` if the failure pattern shows up."
    )


def build_watch_stop_prompt_body() -> str:
    """Disable watcher auto-respawn — invoked via /pluto-watch-stop."""
    return (
        "The user is engaging the **``watcher_stop`` kill switch** for "
        "this session.\n\n"
        "From now on:\n\n"
        "1. Do NOT spawn any new Pluto inbox watcher Tasks, even after a "
        "drain via ``pluto_recv``, piggyback, or a watcher firing.\n"
        "2. If a watcher Task is currently in flight, let it run out its "
        "remaining iterations and exit naturally — do not re-arm when it "
        "completes.\n"
        "3. Continue draining the inbox the cheap way: ``pluto_recv`` at "
        "the start of every reply, plus the ``_pluto_inbox`` piggyback on "
        "any Pluto tool result.\n\n"
        "The kill switch stays in effect until the user invokes "
        "``/pluto-watch`` to explicitly resume the watcher chain.\n\n"
        "Reply exactly: ``watcher_stop engaged — turn-driven drain only "
        "until /pluto-watch``."
    )


def build_status_prompt_body() -> str:
    """Snapshot of current Pluto state — invoked via /pluto-status."""
    return (
        "Report the current Pluto coordination state in a tight summary:\n\n"
        "1. **Me**: my ``agent_id`` and the Pluto server host:port (from "
        "the connection block in your role).\n"
        "2. **Peers**: call ``pluto_list_agents`` and list every other "
        "connected agent_id, one per line. Note any with custom_status "
        "values.\n"
        "3. **Inbox**: read ``@pluto://inbox`` (do NOT call ``pluto_recv`` "
        "— it would drain). Report just the count of pending messages.\n"
        "4. **Locks**: read ``@pluto://locks``. Report each held lock as "
        "``lock_ref -> resource``.\n\n"
        "Format as four numbered lines, no preamble."
    )


def role_prompt_specs(roles_dir: str | None = None) -> Iterable[tuple[str, str, str]]:
    """Yield ``(prompt_name, role_name, description)`` for every available role.

    ``prompt_name`` is what shows up in slash menus
    (e.g. ``pluto-role-specialist``); ``role_name`` is the bare file name
    (``specialist``); ``description`` is suitable for an MCP prompt.
    """
    for name in list_role_names(roles_dir):
        yield (
            f"pluto-role-{name}",
            name,
            f"Apply the '{name}' role from library/roles/{name}.md "
            f"plus the shared protocol and live Pluto connection info.",
        )
