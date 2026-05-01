"""Tests for PlutoMCPFriend — the MCP-server adapter for Pluto.

Most tests are pure unit tests using a fake PlutoHttpClient so they
don't need the Erlang server. The optional ``TestMCPLiveIntegration``
class brings up the real test server and exercises the adapter end-to-end
via the in-process MCP client.
"""

import asyncio
import json
import os
import sys
import unittest
from typing import Any
from unittest.mock import MagicMock

# Ensure src_py is importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
sys.path.insert(0, _SRC_PY)

try:
    from agent_mcp_friend.inbox import InboxManager, _is_noise
    from agent_mcp_friend.lock_manager import LockManager
    from agent_mcp_friend.prompts import (
        build_check_prompt_body,
        build_connection_block,
        build_role_prompt_body,
        build_status_prompt_body,
        build_watch_prompt_body,
        list_role_names,
        role_prompt_specs,
    )
    from agent_mcp_friend.server import PlutoMCPServer
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Fake PlutoHttpClient — captures calls, returns scripted responses.
# ────────────────────────────────────────────────────────────────────────────


class FakeHttpClient:
    """Minimal fake of PlutoHttpClient. Records calls in ``self.calls``."""

    def __init__(self):
        self.token = "FAKE-TOKEN-123"
        self.session_id = "fake-session"
        self.agent_id = "fake-agent"
        self.calls: list[tuple[str, tuple, dict]] = []
        self.peek_responses: list[list[dict]] = []
        self.acks: list[int] = []
        self.renew_calls: list[tuple[str, int]] = []
        self.release_calls: list[str] = []

    def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def peek(self, since_token: int = 0) -> list[dict]:
        self._record("peek", since_token)
        if self.peek_responses:
            return self.peek_responses.pop(0)
        return []

    def ack(self, up_to_seq: int) -> int:
        self._record("ack", up_to_seq)
        self.acks.append(int(up_to_seq))
        return 0

    def send(self, to: str, payload: dict) -> dict:
        self._record("send", to, payload)
        return {"status": "ok", "msg_id": "M-1"}

    def renew(self, lock_ref: str, ttl_ms: int) -> dict:
        self._record("renew", lock_ref, ttl_ms)
        self.renew_calls.append((lock_ref, ttl_ms))
        return {"status": "ok"}

    def release(self, lock_ref: str) -> dict:
        self._record("release", lock_ref)
        self.release_calls.append(lock_ref)
        return {"status": "ok"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ────────────────────────────────────────────────────────────────────────────
# Inbox: noise filtering and piggyback semantics
# ────────────────────────────────────────────────────────────────────────────


class TestInboxNoiseFiltering(unittest.TestCase):
    def test_actionable_message_kept(self):
        msg = {"event": "message", "from": "a", "payload": {"text": "hi"}}
        self.assertFalse(_is_noise(msg))

    def test_non_actionable_event_filtered(self):
        msg = {"event": "delivery_ack", "msg_id": "M1"}
        self.assertTrue(_is_noise(msg))

    def test_payload_delivery_ack_filtered(self):
        msg = {
            "event": "message",
            "from": "x",
            "payload": {"event": "delivery_ack", "msg_id": "M2"},
        }
        self.assertTrue(_is_noise(msg))

    def test_heartbeat_payload_filtered(self):
        msg = {
            "event": "message",
            "from": "x",
            "payload": {"event": "heartbeat", "ts": 1},
        }
        self.assertTrue(_is_noise(msg))


class TestInboxPiggyback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)

    async def test_piggyback_empty_returns_unchanged(self):
        result = {"status": "ok"}
        wrapped = await self.inbox.piggyback(result)
        self.assertEqual(wrapped, {"status": "ok"})
        self.assertNotIn("_pluto_inbox", wrapped)

    async def test_piggyback_attaches_messages_and_acks(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 5,
             "payload": {"text": "hi"}},
            {"event": "message", "from": "b", "seq_token": 7,
             "payload": {"text": "yo"}},
        ])
        wrapped = await self.inbox.piggyback({"status": "ok"})
        self.assertIn("_pluto_inbox", wrapped)
        self.assertEqual(len(wrapped["_pluto_inbox"]), 2)
        self.assertEqual(wrapped["status"], "ok")
        self.assertIn(7, self.client.acks)

    async def test_piggyback_wraps_non_dict_result(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"x": 1}},
        ])
        wrapped = await self.inbox.piggyback("plain-string-result")
        self.assertEqual(wrapped["result"], "plain-string-result")
        self.assertEqual(len(wrapped["_pluto_inbox"]), 1)

    async def test_drain_clears_buffer(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 9,
             "payload": {"x": 1}},
        ])
        msgs = await self.inbox.drain()
        self.assertEqual(len(msgs), 1)
        # Subsequent drain returns nothing
        self.assertEqual(await self.inbox.drain(), [])

    async def test_dedupe_by_seq_token(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"x": 1}},
        ])
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"x": 1}},  # duplicate
        ])
        self.assertEqual(len(await self.inbox.peek_only()), 1)

    async def test_wait_for_messages_returns_immediately_when_buffer_full(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"x": 1}},
        ])
        # No timeout needed — buffer already has content.
        msgs = await asyncio.wait_for(
            self.inbox.wait_for_messages(timeout_s=10), timeout=2,
        )
        self.assertEqual(len(msgs), 1)
        # Buffer drained and acked.
        self.assertEqual(await self.inbox.peek_only(), [])
        self.assertIn(1, self.client.acks)

    async def test_wait_for_messages_blocks_until_arrival(self):
        # Schedule an arrival 0.2 s in the future and assert wait returns
        # before its 5 s deadline.
        async def deliver_late():
            await asyncio.sleep(0.2)
            await self.inbox._absorb([
                {"event": "message", "from": "z", "seq_token": 42,
                 "payload": {"text": "late"}},
            ])

        delivery = asyncio.create_task(deliver_late())
        start = asyncio.get_event_loop().time()
        msgs = await self.inbox.wait_for_messages(timeout_s=5.0)
        elapsed = asyncio.get_event_loop().time() - start
        await delivery
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["seq_token"], 42)
        self.assertLess(elapsed, 1.0,
            "wait_for_messages should fire on event, not poll")

    async def test_wait_for_messages_returns_empty_on_timeout(self):
        start = asyncio.get_event_loop().time()
        msgs = await self.inbox.wait_for_messages(timeout_s=0.3)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertEqual(msgs, [])
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.0)

    async def test_noise_silently_acked_not_buffered(self):
        await self.inbox._absorb([
            {"event": "delivery_ack", "seq_token": 3, "msg_id": "M1"},
            {"event": "message", "from": "x", "seq_token": 4,
             "payload": {"event": "heartbeat"}},  # payload-noise
            {"event": "message", "from": "y", "seq_token": 5,
             "payload": {"text": "real"}},
        ])
        msgs = await self.inbox.peek_only()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["seq_token"], 5)
        # Noise seqs were acked
        self.assertIn(4, self.client.acks)


# ────────────────────────────────────────────────────────────────────────────
# Lock manager: register / unregister / shutdown
# ────────────────────────────────────────────────────────────────────────────


class TestLockManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.mgr = LockManager(self.client)

    async def test_register_then_unregister(self):
        await self.mgr.register("LOCK-1", "file:/foo", 30000)
        snapshot = self.mgr.held_locks()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["lock_ref"], "LOCK-1")
        self.assertEqual(snapshot[0]["resource"], "file:/foo")

        await self.mgr.unregister("LOCK-1")
        self.assertEqual(self.mgr.held_locks(), [])

    async def test_renewal_fires_at_ttl_over_two(self):
        # Tiny TTL so we observe at least one renewal in test time.
        # MIN_RENEW_INTERVAL_S is 1.0; pick TTL=2200 → interval ≈ 1.1s
        await self.mgr.register("LOCK-2", "file:/foo", 2200)
        await asyncio.sleep(1.5)
        await self.mgr.unregister("LOCK-2")
        self.assertGreaterEqual(len(self.client.renew_calls), 1)
        self.assertEqual(self.client.renew_calls[0][0], "LOCK-2")
        self.assertEqual(self.client.renew_calls[0][1], 2200)

    async def test_shutdown_cancels_all(self):
        await self.mgr.register("L1", "r1", 30000)
        await self.mgr.register("L2", "r2", 30000)
        self.assertEqual(len(self.mgr.held_locks()), 2)
        await self.mgr.shutdown()
        self.assertEqual(len(self.mgr.held_locks()), 0)


# ────────────────────────────────────────────────────────────────────────────
# Prompts: role assembly mirrors PlutoAgentFriend's _role_injection_loop
# ────────────────────────────────────────────────────────────────────────────


class TestPromptAssembly(unittest.TestCase):
    def test_list_role_names_finds_specialist(self):
        roles = list_role_names()
        # Expect at least specialist + orchestrator (always shipped).
        self.assertIn("specialist", roles)
        self.assertIn("orchestrator", roles)

    def test_role_prompt_specs_yield_pluto_role_prefix(self):
        specs = list(role_prompt_specs())
        self.assertTrue(specs)
        for prompt_name, role_name, _description in specs:
            self.assertTrue(prompt_name.startswith("pluto-role-"))
            self.assertEqual(prompt_name, f"pluto-role-{role_name}")

    def test_connection_block_contains_live_values(self):
        block = build_connection_block(
            host="my.server",
            http_port=12345,
            agent_id="coder-77",
        )
        self.assertIn("my.server", block)
        self.assertIn("12345", block)
        self.assertIn("coder-77", block)
        self.assertIn("PlutoMCPFriend", block)
        self.assertIn("_pluto_inbox", block)

    def test_role_prompt_includes_role_and_connection(self):
        body = build_role_prompt_body(
            "specialist",
            host="localhost",
            http_port=9001,
            agent_id="coder-42",
        )
        # Role file content (just check the heading is present).
        self.assertIn("specialist", body.lower())
        # Connection block at the bottom.
        self.assertIn("Agent ID:  coder-42", body)
        self.assertIn("Base URL:  http://localhost:9001", body)

    def test_role_prompt_inlines_protocol_when_referenced(self):
        # specialist.md references protocol.md → should inline it.
        body = build_role_prompt_body(
            "specialist",
            host="localhost",
            http_port=9001,
            agent_id="x",
        )
        self.assertIn("=== BEGIN protocol.md ===", body)
        self.assertIn("=== END protocol.md ===", body)

    def test_unknown_role_raises(self):
        with self.assertRaises(FileNotFoundError):
            build_role_prompt_body(
                "nonexistent-role-xyz",
                host="h", http_port=1, agent_id="a",
            )

    def test_check_prompt_invokes_pluto_recv(self):
        body = build_check_prompt_body()
        self.assertIn("pluto_recv", body)
        # Must tell the agent how to behave when inbox is empty.
        self.assertIn("inbox is empty", body)

    def test_watch_prompt_covers_both_paths(self):
        body = build_watch_prompt_body()
        # Claude Code path: background Task with run_in_background.
        self.assertIn("run_in_background", body)
        self.assertIn("pluto_wait_for_messages", body)
        # Fallback path: direct call.
        self.assertIn("60", body)  # the foreground long-poll timeout

    def test_status_prompt_lists_four_sections(self):
        body = build_status_prompt_body()
        self.assertIn("pluto_list_agents", body)
        self.assertIn("@pluto://inbox", body)
        self.assertIn("@pluto://locks", body)
        # And specifically warns NOT to call pluto_recv (which would drain).
        self.assertIn("do not call", body.lower())


# ────────────────────────────────────────────────────────────────────────────
# Server registration: tools / prompts / resources count
# ────────────────────────────────────────────────────────────────────────────


class TestServerCapabilities(unittest.IsolatedAsyncioTestCase):
    """Ensure the server registers the full surface area of capabilities."""

    async def test_capabilities_register(self):
        server = PlutoMCPServer(
            agent_id="cap-test",
            host="localhost",
            http_port=9999,  # unused — we don't call .run()
        )
        server.setup_capabilities()

        tools = await server.mcp.list_tools()
        prompts = await server.mcp.list_prompts()
        resources = await server.mcp.list_resources()

        tool_names = {t.name for t in tools}
        # Pluto operation tools
        for required in [
            "pluto_send", "pluto_broadcast", "pluto_recv",
            "pluto_wait_for_messages",
            "pluto_lock_acquire", "pluto_lock_release", "pluto_lock_renew",
            "pluto_lock_info", "pluto_list_locks",
            "pluto_task_assign", "pluto_task_update", "pluto_task_list",
            "pluto_list_agents", "pluto_find_agents",
            "pluto_publish", "pluto_subscribe", "pluto_set_status",
        ]:
            self.assertIn(required, tool_names, f"missing tool: {required}")

        prompt_names = {p.name for p in prompts}
        self.assertIn("pluto-protocol", prompt_names)
        self.assertIn("pluto-guide", prompt_names)
        self.assertIn("pluto-role-specialist", prompt_names)
        # Action prompts.
        self.assertIn("pluto-check", prompt_names)
        self.assertIn("pluto-watch", prompt_names)
        self.assertIn("pluto-status", prompt_names)

        resource_uris = {str(r.uri) for r in resources}
        self.assertIn("pluto://inbox", resource_uris)
        self.assertIn("pluto://locks", resource_uris)


# ────────────────────────────────────────────────────────────────────────────
# Tool wrappers: token injection + piggyback (mock the HTTP client)
# ────────────────────────────────────────────────────────────────────────────


class TestToolWrappers(unittest.IsolatedAsyncioTestCase):
    async def test_pluto_send_calls_client_and_piggybacks(self):
        from agent_mcp_friend.tools import register_tools
        from mcp.server.fastmcp import FastMCP

        client = FakeHttpClient()
        inbox = InboxManager(client)
        lock_mgr = LockManager(client)
        mcp = FastMCP(name="pluto-test")
        register_tools(mcp, client, inbox, lock_mgr)

        # Pre-populate inbox so we can check piggyback fires.
        await inbox._absorb([
            {"event": "message", "from": "alice", "seq_token": 99,
             "payload": {"text": "hi"}},
        ])

        result = await mcp.call_tool(
            "pluto_send",
            {"to": "bob", "payload": {"type": "ping"}},
        )
        # FastMCP wraps in (content, structured_output); structured is the dict.
        # Check the underlying call was made.
        self.assertTrue(any(c[0] == "send" for c in client.calls))
        # The returned content should reference the piggybacked seq.
        text_blob = json.dumps(result, default=str)
        self.assertIn("99", text_blob)
        self.assertIn("alice", text_blob)


if __name__ == "__main__":
    unittest.main()
