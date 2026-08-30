"""Byte-stability tests for the agent-facing surfaces that feed
prompt-prefix caches.

Tool descriptions/schemas and the shared connection-block prose must be
identical across deployments and across agents: interpolating config
(wait timeouts) or identity (agent_id/host/port) into them invalidates
the cache for every request of every agent. The protocol digest must
also keep covering every message type the full protocol defines.
"""

import json
import os
import re
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_PROJECT, "src_py"))
sys.path.insert(0, _THIS_DIR)  # sibling import of test_mcp_friend under pytest

try:
    from agent_mcp_friend.inbox import InboxManager
    from agent_mcp_friend.lock_manager import LockManager
    from agent_mcp_friend.prompts import (
        _shared_connection_body,
        build_connection_block,
        default_protocol_digest_path,
        default_protocol_path,
    )
    from agent_mcp_friend.tools import register_tools
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )

from test_mcp_friend import FakeHttpClient


async def _tools_fingerprint(wait_timeout_s: int) -> str:
    client = FakeHttpClient()
    mcp = FastMCP(name="pluto-test")
    register_tools(
        mcp, client, InboxManager(client), LockManager(client),
        wait_timeout_s=wait_timeout_s,
    )
    tools = await mcp.list_tools()
    return json.dumps(
        [
            {"name": t.name, "description": t.description,
             "inputSchema": t.inputSchema}
            for t in sorted(tools, key=lambda t: t.name)
        ],
        sort_keys=True,
    )


class TestCacheStability(unittest.IsolatedAsyncioTestCase):
    async def test_tools_list_config_independent(self):
        # The full tool surface (names, descriptions, AND schemas —
        # signature defaults land in the schema) must not vary with the
        # launcher's --wait-timeout-s.
        self.assertEqual(
            await _tools_fingerprint(60),
            await _tools_fingerprint(300),
        )

    async def test_tools_list_deterministic_across_instances(self):
        self.assertEqual(
            await _tools_fingerprint(60),
            await _tools_fingerprint(60),
        )

    def test_connection_block_deterministic(self):
        a = build_connection_block(
            host="127.0.0.1", http_port=9201, agent_id="agent-a",
            wait_timeout_s=60, iterations=15,
        )
        b = build_connection_block(
            host="127.0.0.1", http_port=9201, agent_id="agent-a",
            wait_timeout_s=60, iterations=15,
        )
        self.assertEqual(a, b)

    def test_shared_connection_body_contains_no_identity(self):
        # The prose after the identity header must be byte-identical for
        # every agent of a deployment — no agent_id/host/port leakage.
        body = _shared_connection_body(wait_timeout_s=300, iterations=15)
        for marker in ("MARKER-AGENT", "MARKER-HOST", "4242"):
            self.assertNotIn(marker, body)
        # And the identity header + shared body is exactly the public block.
        full = build_connection_block(
            host="MARKER-HOST", http_port=4242, agent_id="MARKER-AGENT",
            wait_timeout_s=300, iterations=15,
        )
        self.assertTrue(full.endswith(body))
        self.assertIn("MARKER-AGENT", full.replace(body, ""))


class TestProtocolDigestDrift(unittest.TestCase):
    def test_digest_covers_all_message_events(self):
        # Every `### 4.x \`event\`` heading in protocol.md must appear in
        # the digest — adding a message type without updating the digest
        # would hide it from every role prompt.
        protocol = open(default_protocol_path(), encoding="utf-8").read()
        digest = open(default_protocol_digest_path(), encoding="utf-8").read()
        events = re.findall(r"^### 4\.\d+ `([^`]+)`", protocol, re.MULTILINE)
        self.assertTrue(events, "no message-type headings found in protocol.md")
        for heading in events:
            # Headings like "remote_task` / `remote_result" split into
            # individual event names.
            for event in re.split(r"`\s*/\s*`", heading):
                self.assertIn(
                    f"`{event}`", digest,
                    f"protocol.md event {event!r} missing from protocol-digest.md",
                )


if __name__ == "__main__":
    unittest.main()
