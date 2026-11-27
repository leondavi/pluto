#!/usr/bin/env python3
"""Write the private MCP config for one agent (used by PlutoSmartTasks.sh)."""

from __future__ import annotations

import argparse
import json
import os
import sys

#: Same inbox-socket pass-through PlutoMCPFriend.sh uses for wakeups.
_CLAUDE_ENV = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")

DEFAULT_TOOLS = "none"            # see TOOLSETS in constants.py


def build(*, python: str, entry: str, agent_id: str, host: str, port: str,
          framework: str, pluto_entry: str | None = None,
          home: str | None = None, tools: str = DEFAULT_TOOLS) -> dict:
    """With *pluto_entry* both adapters load; the Pluto one then owns the
    registration, so the agent id is never claimed twice."""
    common = ["--agent-id", agent_id, "--host", host, "--http-port", str(port)]

    def server(args: list) -> dict:
        env = {k: "${%s}" % k for k in _CLAUDE_ENV} if framework == "claude" else {}
        if home:
            env["PLUTO_SMARTTASKS_HOME"] = home
        out = {"command": python, "args": args, "env": env}
        if framework == "copilot":
            out.update(type="local", tools=["*"])
        return out

    # Both CLIs have a shell, and there pst does everything without schemas.
    extra = ["--framework", framework, "--tools", tools] + (["--attach"] if pluto_entry else [])
    servers = {"smarttasks": server([entry] + common + extra)}
    if pluto_entry:
        servers["pluto"] = server([pluto_entry] + common)
    return {"mcpServers": servers}


def write(path: str, config: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    for name in ("--out", "--python", "--entry", "--agent-id", "--host",
                 "--port", "--framework"):
        p.add_argument(name, required=True)
    p.add_argument("--pluto-entry")
    p.add_argument("--tools", default=os.environ.get("PST_TOOLS") or DEFAULT_TOOLS)
    a = p.parse_args(argv)
    write(a.out, build(python=a.python, entry=a.entry, agent_id=a.agent_id,
                       host=a.host, port=a.port, framework=a.framework,
                       pluto_entry=a.pluto_entry, tools=a.tools,
                       home=os.environ.get("PLUTO_SMARTTASKS_HOME")))
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
