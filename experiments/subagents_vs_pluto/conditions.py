"""Build the `claude -p` command lines for both experimental conditions.

Both conditions get the same model, the same built-in tools (Bash, Read,
Write) for workers, no user/project settings, and a per-process budget cap.
Condition A adds the Agent tool to the orchestrator; condition B adds the
Pluto MCP server (one identity per process) to every agent.
"""

import json
import os

from scenarios import WORKERS, orchestrator_prompt, worker_prompt

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRIEND = os.environ.get("PLUTO_FRIEND_SCRIPT",
                        os.path.join(REPO, "src_py", "agent_mcp_friend", "pluto_mcp_friend.py"))
FRIEND_PY = os.environ.get("PLUTO_FRIEND_PYTHON", "/tmp/pluto/.venv/bin/python")

WORKER_TOOLS = ["Bash", "Read", "Write"]


def _common(prompt, model, budget_usd):
    # The prompt goes first: --tools/--allowedTools are variadic and would
    # otherwise swallow a trailing positional prompt.
    return ["claude", "-p", prompt, "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--max-budget-usd", str(budget_usd)]


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    return path


def subagents_commands(scenario, run_dir, model, budget_usd):
    """Condition A: one orchestrator process with 4 worker subagents."""
    agents = {w: {"description": f"Team worker {w}.",
                  "prompt": worker_prompt(w, "subagents"),
                  "tools": WORKER_TOOLS,
                  "model": model}
              for w in WORKERS}
    empty = _write_json(os.path.join(run_dir, "mcp-empty.json"), {"mcpServers": {}})
    cmd = _common(orchestrator_prompt(scenario, "subagents"), model, budget_usd) + [
        "--mcp-config", empty,
        "--tools", ",".join(WORKER_TOOLS + ["Agent"]),
        "--agents", json.dumps(agents),
    ]
    return {"orch": cmd}


def _friend_config(run_dir, agent_id, host, http_port):
    cfg = {"mcpServers": {"pluto": {
        "command": FRIEND_PY,
        "args": [FRIEND, "--agent-id", agent_id, "--host", host,
                 "--http-port", str(http_port), "--ttl-ms", "600000",
                 "--wait-timeout-s", "30", "--log-level", "WARNING",
                 "--no-auto-snapshot"],
    }}}
    return _write_json(os.path.join(run_dir, f"mcp-{agent_id}.json"), cfg)


def pluto_commands(scenario, run_dir, model, budget_usd, host, http_port):
    """Condition B: orchestrator + 4 workers, each its own Pluto identity."""
    cmds = {}
    for agent_id in ["orch"] + WORKERS:
        cfg = _friend_config(run_dir, agent_id, host, http_port)
        prompt = (orchestrator_prompt(scenario, "pluto") if agent_id == "orch"
                  else worker_prompt(agent_id, "pluto") + " Begin now.")
        cmds[agent_id] = _common(prompt, model, budget_usd) + [
            "--mcp-config", cfg,
            "--tools", ",".join(WORKER_TOOLS),
            "--allowedTools", "mcp__pluto",
        ]
    return cmds
