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
) -> str:
    """The live-connection block injected at the bottom of every prompt.

    Mirrors the block ``_role_injection_loop`` builds in
    ``pluto_agent_friend.py`` so agents see consistent connection info
    regardless of which integration path they're running under.
    """
    return (
        f"---\n\n"
        f"**Live Pluto server connection** (injected by PlutoMCPFriend — "
        f"use these values; do not hardcode addresses from the role file):\n"
        f"  Host:      {host}\n"
        f"  HTTP port: {http_port}   (REST API base for /agents/* and /locks/*)\n"
        f"  Base URL:  http://{host}:{http_port}\n"
        f"  Agent ID:  {agent_id}\n\n"
        f"You are wrapped by **PlutoMCPFriend**. The server has registered\n"
        f"you and exposes Pluto operations as MCP tools (``pluto_send``,\n"
        f"``pluto_lock_acquire``, etc.). Prefer those tools over raw curl\n"
        f"calls — the wrapper injects your session token automatically and\n"
        f"acks inbox messages on your behalf.\n\n"
        f"**Inbox delivery — three ways messages reach you:**\n\n"
        f"1. **Piggyback (free).** Any Pluto tool result may include a\n"
        f"   non-empty ``_pluto_inbox`` array. Process those before\n"
        f"   continuing.\n"
        f"2. **Pull (on demand).** Call ``pluto_recv`` at the start of any\n"
        f"   turn where you have not otherwise touched Pluto.\n"
        f"3. **Watch (event-driven).** ``pluto_wait_for_messages(timeout_s)``\n"
        f"   blocks until a message arrives.\n\n"
        f"**To stay responsive to the user while watching the inbox**, prefer\n"
        f"the background sub-agent pattern (Claude Code only): at the tail of\n"
        f"every turn, spawn a background Task with ``run_in_background=true``\n"
        f"whose prompt is *\"Call ``pluto_wait_for_messages(300)`` and return\n"
        f"its result\"*. The main conversation stays free; when the Task\n"
        f"completes, its result appears in your next turn — process the\n"
        f"messages, then spawn another watcher Task to keep listening.\n\n"
        f"If your client lacks a background Task tool (Cursor, Aider), call\n"
        f"``pluto_wait_for_messages(30)`` directly at the tail of every turn\n"
        f"as a foreground long-poll."
    )


def build_role_prompt_body(
    role_name: str,
    *,
    host: str,
    http_port: int,
    agent_id: str,
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

    connection_block = build_connection_block(host, http_port, agent_id)

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
        f"{build_connection_block(host, http_port, agent_id)}"
    )


def build_guide_prompt_body(
    *,
    host: str,
    http_port: int,
    agent_id: str,
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
        f"{build_connection_block(host, http_port, agent_id)}"
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


def build_watch_prompt_body() -> str:
    """Start a long-poll watcher — invoked via the /pluto-watch slash command."""
    return (
        "Start watching the Pluto inbox at chat speed so messages arrive "
        "without me having to ask.\n\n"
        "**Preferred (Claude Code):** spawn a background Task using the "
        "Task tool with ``run_in_background=true``, "
        "``description=\"Pluto inbox watcher\"``, and "
        "``prompt=\"Call pluto_wait_for_messages(300) and return its result "
        "as JSON.\"``. When the Task completes, process any messages and "
        "spawn another watcher Task to keep listening.\n\n"
        "**Fallback (Cursor / Aider / no background Task tool):** call "
        "``pluto_wait_for_messages(60)`` directly in this turn instead. "
        "You'll block for up to 60 s; on return, process the messages, "
        "then re-issue the call.\n\n"
        "Reply briefly: ``watcher started`` (preferred path) or "
        "``foreground long-poll engaged`` (fallback)."
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
