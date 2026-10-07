#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pluto_client.py — Python client for the Pluto coordination server.

Compatibility facade and CLI entry point. The implementation lives in the
``pluto_sdk`` package next to this file; every name that was importable
from here before the split still is, so existing agents keep working.

Basic usage (TCP):
    from pluto_client import PlutoClient

    client = PlutoClient(host="localhost", port=9000, agent_id="coder-1")
    client.connect()

    lock_ref = client.acquire("file:/repo/src/model.erl", ttl_ms=30000)
    # ... do work ...
    client.release(lock_ref)

    client.send("reviewer-2", {"type": "ready", "file": "model.erl"})
    client.disconnect()

Context manager:
    with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
        lock_ref = client.acquire("workspace:experiment-17")
        client.release(lock_ref)

Receiving async events:
    client.on_message(lambda e: print("msg:", e["payload"]))
    client.on_lock_granted(lambda e: print("lock granted:", e["lock_ref"]))
    client.connect()

HTTP (token session, used by the MCP adapter):
    from pluto_client import PlutoHttpClient

    with PlutoHttpClient(host="localhost", http_port=9001, agent_id="claude-1") as c:
        c.send("coder-1", {"type": "ping"})
        messages = c.peek()

Command line (see ``python pluto_client.py --help``):
    python pluto_client.py ping | list | stats | guide
"""

import os
import sys

# Allow `python /path/to/pluto_client.py` from any working directory: the
# sibling package and constants module must be importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pluto_sdk import (  # noqa: E402  (path setup must run first)
    HTTPConnectionPool,
    PlutoClient,
    PlutoError,
    PlutoHttpClient,
    generate_agent_guide,
    write_snapshot_files,
)
from pluto_sdk.cli import main  # noqa: E402

__all__ = [
    "HTTPConnectionPool",
    "PlutoClient",
    "PlutoError",
    "PlutoHttpClient",
    "generate_agent_guide",
    "main",
    "write_snapshot_files",
]


if __name__ == "__main__":
    main()
