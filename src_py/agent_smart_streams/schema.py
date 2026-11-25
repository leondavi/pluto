"""Smaller tool schemas: they are paid for on every model turn."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP


def slim(node):
    """Drop generated titles, null alternatives and null defaults. Argument
    validation does not use this schema, so explicit nulls stay accepted."""
    if isinstance(node, list):
        return [slim(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: ({n: slim(p) for n, p in v.items()} if k == "properties" else slim(v))
           for k, v in node.items()
           if not (k == "title" and isinstance(v, str)
                   or k == "default" and v is None)}
    options = [o for o in out.get("anyOf", []) if o != {"type": "null"}]
    if len(options) == 1:
        del out["anyOf"]
        out = {**options[0], **out}
    return out


def slim_tools(mcp: FastMCP) -> None:
    for tool in mcp._tool_manager.list_tools():
        tool.parameters = slim(tool.parameters)


def fold(mcp: FastMCP, keep: tuple) -> None:
    """Hide all tools but *keep* behind one job_more tool (none at all when
    keep is empty): rarely used schemas should not be paid for."""
    manager = mcp._tool_manager
    hidden = {t.name: t for t in manager.list_tools() if t.name not in keep}
    for name in hidden:
        del manager._tools[name]
    if not keep:
        return
    ops = {n.removeprefix("job_"): t for n, t in hidden.items()}

    async def job_more(op: str, args: dict | None = None) -> str:
        tool = ops.get(op.removeprefix("job_"))
        if tool is None:
            return json.dumps({"error": f"op must be one of {sorted(ops)}"})
        if args is None:
            return json.dumps({"use": tool.description,
                               "args": slim(tool.parameters)["properties"]})
        try:
            return await tool.run(args)
        except Exception as exc:     # bad arguments: tell the agent which
            return json.dumps({"error": str(exc)[:500]})

    mcp.tool(structured_output=False, description=(
        f"Less common operations: {', '.join(sorted(ops))}. Without args it "
        "explains the operation."))(job_more)
