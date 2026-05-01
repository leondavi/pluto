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
) -> str:
    """The live-connection block appended to every prompt body.

    *wait_timeout_s* is embedded into the recommended
    ``pluto_wait_for_messages(N)`` call so the watcher cycle matches
    the launcher's ``--wait-timeout-s`` value.
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
        f"## ⚡ Best-effort: spawn a background inbox watcher\n\n"
        f"On your first turn (and again whenever the previous watcher\n"
        f"completes), try to spawn a background Pluto inbox watcher. Use\n"
        f"the Task tool with **all** these parameters:\n\n"
        f"    description       = \"Pluto inbox watcher\"\n"
        f"    run_in_background = true\n"
        f"    subagent_type     = \"general-purpose\"\n"
        f"    prompt            = (the looping prompt below — verbatim)\n\n"
        f"### The subagent prompt — looping short polls\n\n"
        f"```\n"
        f"You are a Pluto inbox watcher. Repeat this loop:\n"
        f"  1. Call pluto_wait_for_messages({wait_timeout_s}).\n"
        f"  2. If the response's count > 0, return the response JSON and stop.\n"
        f"  3. If count == 0, go back to step 1.\n"
        f"Stop after 5 iterations regardless and return whatever you\n"
        f"have. Each iteration produces a tool-call output, which keeps\n"
        f"this subagent visible to Claude Code's stream watchdog —\n"
        f"a single long block would get killed at the 600 s silence\n"
        f"threshold. Do not produce conversational text between calls;\n"
        f"the tool calls themselves are the output.\n"
        f"```\n\n"
        f"Why this works: each ``pluto_wait_for_messages({wait_timeout_s})``\n"
        f"call returns within {wait_timeout_s} s with messages or empty,\n"
        f"so the subagent emits a tool-call result every <= {wait_timeout_s} s\n"
        f"— nowhere near the 600 s watchdog cutoff. Total subagent\n"
        f"lifetime is bounded by 5 iterations (~{5 * wait_timeout_s} s) before\n"
        f"the parent respawns. This pattern survives both watchdog\n"
        f"timeouts and idle cleanup.\n\n"
        f"### When the watcher fails entirely\n\n"
        f"On some Claude Code configurations subagents do **not** inherit\n"
        f"the parent's MCP server access. The symptom: the very first\n"
        f"tool call inside the subagent errors with \"tool unavailable\"\n"
        f"or similar, and the Task completes in seconds with no\n"
        f"``pluto_*`` activity at all.\n\n"
        f"If you see that pattern once, **stop spawning watchers for the\n"
        f"rest of the session** — re-arming them just burns subagent\n"
        f"budget. Tell the user once that the watcher pattern isn't\n"
        f"available in this setup, then rely entirely on the mandatory\n"
        f"``pluto_recv`` at the start of every reply (above) plus the\n"
        f"user invoking ``/pluto-check`` when they want to flush.\n\n"
        f"### When the Task completes normally\n\n"
        f"Whenever a watcher Task ends — whether it returned messages,\n"
        f"completed all 5 iterations empty, or timed out — **immediately\n"
        f"spawn another watcher Task** with the same parameters. The\n"
        f"subagent self-bounds at ~{5 * wait_timeout_s} s; the parent\n"
        f"chain keeps a watcher in flight indefinitely.\n\n"
        f"## Other ways messages can reach you\n\n"
        f"1. **Piggyback (free).** Any Pluto tool result includes any\n"
        f"   pending messages under ``_pluto_inbox``. Process those before\n"
        f"   continuing.\n"
        f"2. **User-invoked**. The user can type ``/pluto-check`` at any\n"
        f"   time to force-drain the inbox, or ``/pluto-watch`` to retry\n"
        f"   the watcher pattern."
    )


def build_role_prompt_body(
    role_name: str,
    *,
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
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
        host, http_port, agent_id, wait_timeout_s=wait_timeout_s,
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
        f"{build_connection_block(host, http_port, agent_id, wait_timeout_s)}"
    )


def build_guide_prompt_body(
    *,
    host: str,
    http_port: int,
    agent_id: str,
    wait_timeout_s: int = 300,
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
        f"{build_connection_block(host, http_port, agent_id, wait_timeout_s)}"
    )


def build_check_prompt_body() -> str:
    """One-shot inbox check — invoked via the /pluto-check slash command."""
    return (
        "Call ``pluto_recv`` with no arguments.\n\n"
        "If the response's ``count`` is greater than zero, summarize each "
        "message in one line each: ``[from -> event] one-line summary of "
        "payload``. Then ask whether to act on them.\n\n"
        "If ``count`` is zero, reply exactly: ``inbox is empty``."
    )


def build_watch_prompt_body(wait_timeout_s: int = 300) -> str:
    """Start a long-poll watcher — invoked via the /pluto-watch slash command."""
    return (
        "Start watching the Pluto inbox so messages arrive without me "
        "having to ask.\n\n"
        "Spawn a background Task using the Task tool with **all** these "
        "parameters:\n\n"
        "  • ``run_in_background=true``\n"
        "  • ``subagent_type=\"general-purpose\"``\n"
        "  • ``description=\"Pluto inbox watcher\"``\n"
        "  • ``prompt`` = the looping subagent prompt below, verbatim:\n\n"
        "```\n"
        "You are a Pluto inbox watcher. Repeat this loop:\n"
        f"  1. Call pluto_wait_for_messages({wait_timeout_s}).\n"
        "  2. If response.count > 0, return the response JSON and stop.\n"
        "  3. If count == 0, go back to step 1.\n"
        "Stop after 5 iterations regardless and return whatever you\n"
        "have. Each iteration produces a tool-call output that keeps\n"
        "this subagent visible to Claude Code's stream watchdog — a\n"
        "single long block would get killed at the 600 s silence cutoff.\n"
        "```\n\n"
        f"Each call blocks for at most {wait_timeout_s} s, so the subagent "
        f"emits a tool-call result every <= {wait_timeout_s} s — well "
        f"under the 600 s watchdog. Total subagent lifetime ~{5 * wait_timeout_s} s.\n\n"
        "When the Task completes (any reason — messages, empty after 5 "
        "iterations, or watchdog), **immediately spawn another watcher "
        "Task** with the same parameters. The chain keeps a watcher in "
        "flight indefinitely.\n\n"
        "**Failure detection.** If the Task completes in seconds with "
        "no evidence of any ``pluto_wait_for_messages`` call (meaning "
        "the subagent doesn't have MCP access here), **stop re-arming** "
        "— tell me the watcher pattern isn't available in this setup "
        "and rely on the start-of-every-reply ``pluto_recv`` from your "
        "role.\n\n"
        "Reply briefly with ``watcher started`` once the first Task is "
        "in flight, or ``watcher unavailable — falling back to "
        "pluto_recv`` if the failure pattern shows up."
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
