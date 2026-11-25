"""Smaller tool schemas: they are paid for on every model turn."""

from __future__ import annotations

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
