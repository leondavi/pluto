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
        build_watch_stop_prompt_body,
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
        self.host = "127.0.0.1"
        self.http_port = 9201
        self.base_url = f"http://{self.host}:{self.http_port}"
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

    def test_connection_block_mandates_pluto_recv_per_reply(self):
        """The reliable fallback path — pluto_recv at start of every reply —
        must be marked as mandatory, not aspirational."""
        block = build_connection_block(
            host="h", http_port=1, agent_id="a", wait_timeout_s=300,
        )
        self.assertIn("pluto_recv", block)
        self.assertIn("MANDATORY", block)
        self.assertIn("every reply", block)

    def test_connection_block_describes_watcher_as_best_effort(self):
        """The subagent watcher pattern can fail when Claude Code doesn't
        propagate MCP to subagents — the prompt must say so explicitly so
        the agent doesn't get stuck in a re-arm loop."""
        block = build_connection_block(
            host="h", http_port=1, agent_id="a", wait_timeout_s=60,
        )
        # Best-effort framing.
        self.assertIn("best-effort", block.lower())
        # Stop-spawning-on-failure rule (when subagent lacks MCP entirely).
        self.assertIn("stop spawning", block.lower())
        # Required Task parameters still spelled out.
        self.assertIn("run_in_background", block)
        self.assertIn("subagent_type", block)
        # New contract: subagent calls pluto_inbox_watch (server-owned
        # durable loop), not pluto_wait_for_messages directly.
        self.assertIn("pluto_inbox_watch", block)
        self.assertIn("wait_timeout_s=60", block)
        # Watcher subagents share the parent's MCP server on Claude Code,
        # so they MUST use peek mode (drain=false) — otherwise they steal
        # the parent's inbox out from under pluto_recv.
        self.assertIn("drain=false", block)

    def test_connection_block_uses_short_poll_loop(self):
        """A single 300+ s block in a subagent gets killed by Claude Code's
        stream watchdog (~600s silence cutoff). The subagent prompt must
        instruct a loop of short polls so output is produced regularly."""
        block = build_connection_block(
            host="h", http_port=1, agent_id="a", wait_timeout_s=60,
        )
        # The subagent prompt must describe a loop, not a single call.
        self.assertIn("loop", block.lower())
        # Bounded number of cycles so the subagent self-terminates
        # before any platform-level cleanup. Default is 15.
        self.assertIn("15 cycles", block)
        # Must explain the watchdog reason so future maintainers don't
        # "simplify" it back to a single block.
        self.assertIn("watchdog", block.lower())

    def test_connection_block_honours_custom_iterations(self):
        """Orchestrators tune the iteration count via --iterations to trade
        respawn-gap latency against subagent budget."""
        block = build_connection_block(
            host="h", http_port=1, agent_id="a",
            wait_timeout_s=60, iterations=30,
        )
        self.assertIn("30 cycles", block)
        # Total lifetime calculation reflects the new value.
        self.assertIn(f"{30 * 60} s", block)
        # Default value should not appear when overridden.
        self.assertNotIn("15 cycles", block)

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

    def test_watch_prompt_uses_background_task(self):
        body = build_watch_prompt_body()
        # Claude-only path: background Task with run_in_background.
        self.assertIn("run_in_background", body)
        # New contract: subagent calls pluto_inbox_watch in peek mode.
        self.assertIn("pluto_inbox_watch", body)
        self.assertIn("drain=false", body)
        # Must specify subagent_type — Claude Code's Task tool requires it.
        self.assertIn("subagent_type", body)
        # Default timeout shows up.
        self.assertIn("300", body)
        # Failure-detection heuristic must be present so the agent doesn't
        # re-arm a non-functional watcher in a tight loop.
        self.assertIn("stop re-arming", body.lower())
        # No more Cursor/Aider fallback wording.
        self.assertNotIn("Cursor", body)
        self.assertNotIn("Aider", body)

    def test_watch_prompt_uses_short_poll_loop(self):
        """The subagent prompt must instruct a short-poll loop so the
        stream watchdog never fires."""
        body = build_watch_prompt_body(wait_timeout_s=60)
        self.assertIn("loop", body.lower())
        # Default cycle count is 15.
        self.assertIn("15 cycles", body)
        self.assertIn("watchdog", body.lower())

    def test_watch_prompt_honours_custom_iterations(self):
        """``/pluto-watch`` must propagate the configured iteration count
        so orchestrators can tune subagent lifetime without code changes."""
        body = build_watch_prompt_body(wait_timeout_s=60, iterations=30)
        self.assertIn("30 cycles", body)
        self.assertNotIn("15 cycles", body)

    def test_watch_prompt_honours_custom_timeout(self):
        body = build_watch_prompt_body(wait_timeout_s=120)
        # New contract: pluto_inbox_watch with wait_timeout_s parameter.
        self.assertIn("wait_timeout_s=120", body)
        self.assertIn("pluto_inbox_watch", body)
        # Should not still mention the previous default we replaced.
        self.assertNotIn("wait_timeout_s=60", body)

    def test_connection_block_honours_custom_timeout(self):
        block = build_connection_block(
            host="h", http_port=1, agent_id="a", wait_timeout_s=900,
        )
        # New contract.
        self.assertIn("wait_timeout_s=900", block)
        self.assertIn("pluto_inbox_watch", block)
        # No fallback wording for non-Claude clients.
        self.assertNotIn("Cursor", block)
        self.assertNotIn("Aider", block)

    def test_role_prompt_propagates_wait_timeout(self):
        body = build_role_prompt_body(
            "specialist",
            host="h", http_port=1, agent_id="a", wait_timeout_s=42,
        )
        self.assertIn("wait_timeout_s=42", body)
        self.assertIn("pluto_inbox_watch", body)

    def test_status_prompt_lists_four_sections(self):
        body = build_status_prompt_body()
        self.assertIn("pluto_list_agents", body)
        self.assertIn("@pluto://inbox", body)
        self.assertIn("@pluto://locks", body)
        # And specifically warns NOT to call pluto_recv (which would drain).
        self.assertIn("do not call", body.lower())

    # ── /pluto-watch-stop and the respawn-on-drain rule ─────────────────

    def test_watch_stop_prompt_engages_kill_switch(self):
        """``/pluto-watch-stop`` must engage the watcher_stop kill switch:
        no new watcher Tasks until ``/pluto-watch`` resumes."""
        body = build_watch_stop_prompt_body()
        # Names the kill switch by its session-level token.
        self.assertIn("watcher_stop", body)
        # Tells the agent explicitly NOT to spawn new watcher Tasks.
        self.assertIn("Do NOT spawn", body)
        # Allows the in-flight watcher to drain naturally — agent must
        # not kill it, just not re-arm.
        self.assertIn("do not re-arm", body)
        # Falls back to turn-driven pluto_recv + piggyback.
        self.assertIn("pluto_recv", body)
        self.assertIn("_pluto_inbox", body)
        # Resume path is documented.
        self.assertIn("/pluto-watch", body)
        # Exact ack reply so an orchestrator can detect the engaged state.
        self.assertIn("watcher_stop engaged", body)

    def test_check_prompt_respawns_watcher_after_drain(self):
        """``/pluto-check`` is a drain — the agent must end-of-turn re-arm
        the watcher, unless the kill switch is in effect."""
        body = build_check_prompt_body()
        # Existing contract still holds.
        self.assertIn("pluto_recv", body)
        # New respawn instruction: end-of-turn only.
        self.assertIn("end of this turn", body.lower())
        self.assertIn("last tool call", body.lower())
        # Honours the kill switch.
        self.assertIn("watcher_stop", body)

    def test_connection_block_drain_respawn_rule(self):
        """The always-on role connection block must spell out the
        end-of-turn-only respawn rule and call out the kill switch."""
        block = build_connection_block(
            host="h", http_port=1, agent_id="a",
        )
        # New end-of-turn rule.
        self.assertIn("end-of-turn", block.lower())
        # The kill-switch exception still documented.
        self.assertIn("watcher_stop", block)
        # Old "respawn immediately after every drain" wording is gone.
        self.assertNotIn(
            "respawn the watcher immediately after every drain",
            block.lower(),
        )
        self.assertIn("/pluto-watch-stop", block)
        # Resume path is mentioned so the agent doesn't get stuck.
        self.assertIn("/pluto-watch", block)

    def test_watch_prompt_clears_kill_switch(self):
        """``/pluto-watch`` is the documented resume path — invoking it
        must implicitly clear any in-effect ``watcher_stop``."""
        body = build_watch_prompt_body()
        self.assertIn("watcher_stop", body)
        # Wording specifically says "clears" so the agent treats this as
        # a state reset, not just a one-shot watcher start.
        self.assertIn("clears", body.lower())


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
            "pluto_session",
        ]:
            self.assertIn(required, tool_names, f"missing tool: {required}")

        prompt_names = {p.name for p in prompts}
        self.assertIn("pluto-protocol", prompt_names)
        self.assertIn("pluto-guide", prompt_names)
        self.assertIn("pluto-role-specialist", prompt_names)
        # Action prompts.
        self.assertIn("pluto-check", prompt_names)
        self.assertIn("pluto-watch", prompt_names)
        self.assertIn("pluto-watch-stop", prompt_names)
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

    async def test_pluto_session_reports_state_without_network(self):
        """pluto_session must work even when the HTTP server is down —
        it's the agent's "is MCP alive?" probe and must never depend
        on Pluto-side reachability."""
        from agent_mcp_friend.tools import register_tools
        from mcp.server.fastmcp import FastMCP

        client = FakeHttpClient()
        client.token = "TOK-ABC"
        inbox = InboxManager(client)
        lock_mgr = LockManager(client)
        mcp = FastMCP(name="pluto-test")
        register_tools(mcp, client, inbox, lock_mgr)

        result = await mcp.call_tool("pluto_session", {})
        text_blob = json.dumps(result, default=str)
        self.assertIn("agent_id", text_blob)
        self.assertIn("connected", text_blob)
        self.assertIn("buffered_messages", text_blob)
        # Crucially, pluto_session must not have made an HTTP call —
        # otherwise it can't serve as a transport-only probe.
        for call_name, *_ in client.calls:
            self.assertNotIn(call_name, {"register", "peek", "ack",
                                         "send", "broadcast", "renew",
                                         "release", "list_agents_detailed"})


# ────────────────────────────────────────────────────────────────────────────
# Phase-1 + phase-2 hardening: durable watcher, dedupe, notifier, telemetry.
# ────────────────────────────────────────────────────────────────────────────


from agent_mcp_friend.notifier import Notifier  # noqa: E402
from agent_mcp_friend.tools import _mcp_inherited  # noqa: E402


class TestWatcherDurable(unittest.IsolatedAsyncioTestCase):
    """``watch_durable`` semantics — dedupe, adaptive iteration, soft retry."""

    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)

    async def test_durable_returns_messages_when_they_arrive(self):
        async def deliver_after_delay():
            await asyncio.sleep(0.05)
            await self.inbox._absorb([
                {"event": "message", "from": "x", "seq_token": 11,
                 "payload": {"text": "hi"}},
            ])

        asyncio.create_task(deliver_after_delay())
        resp = await self.inbox.watch_durable(
            inbox_id="default", wait_timeout_s=1.0, max_total_s=2.0,
        )
        self.assertEqual(resp["count"], 1)
        self.assertEqual(resp["watcher_id"], "default")
        self.assertEqual(resp["messages"][0]["seq_token"], 11)

    async def test_durable_dedupes_concurrent_callers(self):
        # First watcher blocks on an empty inbox.
        first = asyncio.create_task(
            self.inbox.watch_durable(
                inbox_id="default", wait_timeout_s=0.5, max_total_s=1.0,
            )
        )
        # Give it a moment to enter the loop and register the key.
        await asyncio.sleep(0.05)
        # Concurrent second call must short-circuit.
        second = await self.inbox.watch_durable(
            inbox_id="default", wait_timeout_s=0.5, max_total_s=1.0,
        )
        self.assertTrue(second.get("already_watching"))
        self.assertEqual(second["watcher_id"], "default")
        # First eventually returns on timeout.
        first_resp = await first
        self.assertTrue(first_resp.get("timeout"))

    async def test_durable_releases_slot_after_return(self):
        # Run, return, run again — second call must NOT see already_watching.
        await self.inbox.watch_durable(
            inbox_id="default", wait_timeout_s=0.1, max_total_s=0.1,
        )
        self.assertNotIn("default", self.inbox._active_watchers)
        again = await self.inbox.watch_durable(
            inbox_id="default", wait_timeout_s=0.1, max_total_s=0.1,
        )
        self.assertFalse(again.get("already_watching", False))
        self.assertTrue(again.get("timeout"))

    async def test_durable_adaptive_iteration_math(self):
        # Run with a tiny budget and verify iterations done matches the cap.
        resp = await self.inbox.watch_durable(
            inbox_id="default", wait_timeout_s=0.05, max_total_s=0.15,
        )
        self.assertTrue(resp.get("timeout"))
        # max_iters = ceil(0.15 / 0.05) = 3
        self.assertEqual(resp.get("max_iterations"), 3)
        self.assertLessEqual(resp.get("iterations"), 3)

    async def test_durable_distinct_inbox_ids_not_deduped(self):
        first = asyncio.create_task(
            self.inbox.watch_durable(
                inbox_id="conv-a", wait_timeout_s=0.3, max_total_s=0.5,
            )
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(
            self.inbox.watch_durable(
                inbox_id="conv-b", wait_timeout_s=0.3, max_total_s=0.5,
            )
        )
        a, b = await asyncio.gather(first, second)
        # Neither should see already_watching — different keys.
        self.assertFalse(a.get("already_watching", False))
        self.assertFalse(b.get("already_watching", False))


class TestPeekModeAndDrainEvent(unittest.IsolatedAsyncioTestCase):
    """``wait_for_messages(drain=False)`` and the new ``drain()`` event
    clear. These guarantee a watcher subagent sharing the parent's
    ``InboxManager`` cannot steal messages from the parent's ``pluto_recv``."""

    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)

    async def test_peek_mode_returns_snapshot_without_consuming(self):
        await self.inbox._absorb([
            {"event": "message", "from": "x", "seq_token": 5,
             "payload": {"text": "hi"}},
        ])
        snap = await self.inbox.wait_for_messages(timeout_s=1.0, drain=False)
        # Snapshot reflects buffered content.
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["seq_token"], 5)
        # Buffer still holds the message; no ack was sent.
        self.assertEqual(len(await self.inbox.peek_only()), 1)
        self.assertNotIn(5, self.client.acks)

    async def test_peek_mode_blocks_until_arrival_then_returns_snapshot(self):
        async def deliver_late():
            await asyncio.sleep(0.1)
            await self.inbox._absorb([
                {"event": "message", "from": "y", "seq_token": 7,
                 "payload": {"text": "yo"}},
            ])

        asyncio.create_task(deliver_late())
        snap = await self.inbox.wait_for_messages(timeout_s=2.0, drain=False)
        self.assertEqual(len(snap), 1)
        # Still buffered, still un-acked.
        self.assertEqual(len(await self.inbox.peek_only()), 1)
        self.assertNotIn(7, self.client.acks)

    async def test_drain_clears_new_message_event(self):
        # _absorb sets the event; drain must clear it so a subsequent
        # peek-mode wait re-arms instead of returning immediately.
        await self.inbox._absorb([
            {"event": "message", "from": "z", "seq_token": 11,
             "payload": {"x": 1}},
        ])
        self.assertTrue(self.inbox._new_message_event.is_set())
        await self.inbox.drain()
        self.assertFalse(self.inbox._new_message_event.is_set())

    async def test_peek_then_parent_drain_round_trip(self):
        # End-to-end: watcher peeks (no consume), parent drains, next
        # watcher peek blocks again until a fresh arrival.
        await self.inbox._absorb([
            {"event": "message", "from": "p", "seq_token": 21,
             "payload": {"x": 1}},
        ])
        snap = await self.inbox.wait_for_messages(timeout_s=1.0, drain=False)
        self.assertEqual(len(snap), 1)

        drained = await self.inbox.drain()
        self.assertEqual(len(drained), 1)
        self.assertIn(21, self.client.acks)

        # Next peek-mode call must time out — no new arrivals.
        start = asyncio.get_event_loop().time()
        snap2 = await self.inbox.wait_for_messages(timeout_s=0.3, drain=False)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertEqual(snap2, [])
        self.assertGreaterEqual(elapsed, 0.25)

    async def test_watch_durable_drain_false_does_not_consume(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 30,
             "payload": {"x": 1}},
        ])
        resp = await self.inbox.watch_durable(
            inbox_id="default",
            wait_timeout_s=0.5,
            max_total_s=0.5,
            drain=False,
        )
        self.assertEqual(resp["count"], 1)
        # Buffer preserved, ack not sent.
        self.assertEqual(len(await self.inbox.peek_only()), 1)
        self.assertNotIn(30, self.client.acks)


class TestSinglePopDelivery(unittest.IsolatedAsyncioTestCase):
    """Single-pop / pipeline mode: pop_one + delivery_mode-aware piggyback."""

    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)

    async def test_default_mode_is_batch(self):
        self.assertEqual(self.inbox.delivery_mode, "batch")

    async def test_set_delivery_mode_accepts_known_modes(self):
        self.assertEqual(self.inbox.set_delivery_mode("single"), "single")
        self.assertEqual(self.inbox.delivery_mode, "single")
        self.assertEqual(self.inbox.set_delivery_mode("batch"), "batch")
        self.assertEqual(self.inbox.delivery_mode, "batch")

    async def test_set_delivery_mode_ignores_unknown(self):
        self.inbox.set_delivery_mode("single")
        result = self.inbox.set_delivery_mode("firehose")
        self.assertEqual(result, "single")
        self.assertEqual(self.inbox.delivery_mode, "single")

    async def test_pop_one_returns_head_and_remaining(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"n": 1}},
            {"event": "message", "from": "a", "seq_token": 2,
             "payload": {"n": 2}},
            {"event": "message", "from": "a", "seq_token": 3,
             "payload": {"n": 3}},
        ])
        msg, remaining = await self.inbox.pop_one()
        self.assertEqual(msg["seq_token"], 1)
        self.assertEqual(remaining, 2)
        self.assertIn(1, self.client.acks)
        msg, remaining = await self.inbox.pop_one()
        self.assertEqual(msg["seq_token"], 2)
        self.assertEqual(remaining, 1)
        msg, remaining = await self.inbox.pop_one()
        self.assertEqual(msg["seq_token"], 3)
        self.assertEqual(remaining, 0)

    async def test_pop_one_empty_returns_none_immediately(self):
        start = asyncio.get_event_loop().time()
        msg, remaining = await self.inbox.pop_one()
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNone(msg)
        self.assertEqual(remaining, 0)
        self.assertLess(elapsed, 0.1)

    async def test_pop_one_blocks_until_arrival(self):
        async def deliver_late():
            await asyncio.sleep(0.1)
            await self.inbox._absorb([
                {"event": "message", "from": "z", "seq_token": 77,
                 "payload": {"text": "late"}},
            ])

        asyncio.create_task(deliver_late())
        start = asyncio.get_event_loop().time()
        msg, remaining = await self.inbox.pop_one(wait_s=2.0)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNotNone(msg)
        self.assertEqual(msg["seq_token"], 77)
        self.assertEqual(remaining, 0)
        self.assertLess(elapsed, 1.0,
            "pop_one(wait_s) should fire on event, not poll")

    async def test_pop_one_times_out_when_no_arrival(self):
        start = asyncio.get_event_loop().time()
        msg, remaining = await self.inbox.pop_one(wait_s=0.3)
        elapsed = asyncio.get_event_loop().time() - start
        self.assertIsNone(msg)
        self.assertEqual(remaining, 0)
        self.assertGreaterEqual(elapsed, 0.25)

    async def test_pop_one_clears_event_when_buffer_emptied(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 5,
             "payload": {"x": 1}},
        ])
        self.assertTrue(self.inbox._new_message_event.is_set())
        await self.inbox.pop_one()
        self.assertFalse(self.inbox._new_message_event.is_set())

    async def test_pop_one_keeps_event_set_when_buffer_nonempty(self):
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 5,
             "payload": {"x": 1}},
            {"event": "message", "from": "a", "seq_token": 6,
             "payload": {"x": 2}},
        ])
        await self.inbox.pop_one()
        self.assertTrue(self.inbox._new_message_event.is_set())

    async def test_piggyback_single_mode_attaches_one_message(self):
        self.inbox.set_delivery_mode("single")
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 10,
             "payload": {"n": 1}},
            {"event": "message", "from": "a", "seq_token": 11,
             "payload": {"n": 2}},
            {"event": "message", "from": "a", "seq_token": 12,
             "payload": {"n": 3}},
        ])
        wrapped = await self.inbox.piggyback({"status": "ok"})
        self.assertEqual(len(wrapped["_pluto_inbox"]), 1)
        self.assertEqual(wrapped["_pluto_inbox"][0]["seq_token"], 10)
        self.assertEqual(wrapped["_pluto_inbox_remaining"], 2)
        # Only the head was acked; rest stays buffered for later pops.
        self.assertEqual(self.client.acks, [10])
        self.assertEqual(len(await self.inbox.peek_only()), 2)

    async def test_piggyback_batch_mode_unchanged(self):
        # Belt-and-braces: confirm the existing batch path is undisturbed.
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"n": 1}},
            {"event": "message", "from": "a", "seq_token": 2,
             "payload": {"n": 2}},
        ])
        wrapped = await self.inbox.piggyback({"status": "ok"})
        self.assertEqual(len(wrapped["_pluto_inbox"]), 2)
        self.assertNotIn("_pluto_inbox_remaining", wrapped)
        self.assertEqual(len(await self.inbox.peek_only()), 0)

    async def test_piggyback_single_mode_empty_buffer_no_marker(self):
        self.inbox.set_delivery_mode("single")
        wrapped = await self.inbox.piggyback({"status": "ok"})
        # Empty buffer → unchanged result, no remaining marker.
        self.assertEqual(wrapped, {"status": "ok"})

    async def test_pluto_pop_tool_returns_single_message(self):
        from agent_mcp_friend.tools import register_tools
        from mcp.server.fastmcp import FastMCP

        lock_mgr = LockManager(self.client)
        mcp = FastMCP(name="pluto-test")
        register_tools(mcp, self.client, self.inbox, lock_mgr)

        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 21,
             "payload": {"n": 1}},
            {"event": "message", "from": "a", "seq_token": 22,
             "payload": {"n": 2}},
        ])
        result = await mcp.call_tool("pluto_pop", {})
        text = json.dumps(result, default=str)
        self.assertIn("21", text)
        self.assertIn("remaining", text)
        # Only one message popped — the second one stays buffered.
        self.assertEqual(len(await self.inbox.peek_only()), 1)

    async def test_pluto_set_delivery_mode_tool_switches_mode(self):
        from agent_mcp_friend.tools import register_tools
        from mcp.server.fastmcp import FastMCP

        lock_mgr = LockManager(self.client)
        mcp = FastMCP(name="pluto-test")
        register_tools(mcp, self.client, self.inbox, lock_mgr)

        result = await mcp.call_tool(
            "pluto_set_delivery_mode", {"mode": "single"},
        )
        text = json.dumps(result, default=str)
        self.assertIn("single", text)
        self.assertEqual(self.inbox.delivery_mode, "single")


class TestMcpInheritedProbe(unittest.TestCase):
    """Tri-state behavior of the PLUTO_MCP_INHERITED env var."""

    def setUp(self):
        self._prev = os.environ.pop("PLUTO_MCP_INHERITED", None)

    def tearDown(self):
        os.environ.pop("PLUTO_MCP_INHERITED", None)
        if self._prev is not None:
            os.environ["PLUTO_MCP_INHERITED"] = self._prev

    def test_unset_returns_none(self):
        self.assertIsNone(_mcp_inherited())

    def test_truthy_returns_true(self):
        for v in ["1", "true", "TRUE", "yes", "on"]:
            os.environ["PLUTO_MCP_INHERITED"] = v
            self.assertTrue(_mcp_inherited(), f"failed on {v!r}")

    def test_falsy_returns_false(self):
        for v in ["0", "false", "no", "off"]:
            os.environ["PLUTO_MCP_INHERITED"] = v
            self.assertEqual(_mcp_inherited(), False, f"failed on {v!r}")

    def test_unrecognized_returns_none(self):
        os.environ["PLUTO_MCP_INHERITED"] = "maybe"
        self.assertIsNone(_mcp_inherited())


class _FakeSession:
    """Records calls instead of actually sending MCP frames."""

    def __init__(self):
        self.resource_updates: list = []
        self.log_messages: list[dict] = []
        self.fail_resource_updated = False
        self.fail_log_message = False

    async def send_resource_updated(self, uri):
        if self.fail_resource_updated:
            raise RuntimeError("simulated transport failure")
        self.resource_updates.append(str(uri))

    async def send_log_message(self, level, data, logger=None):
        if self.fail_log_message:
            raise RuntimeError("simulated transport failure")
        self.log_messages.append(
            {"level": level, "data": data, "logger": logger}
        )


class TestNotifier(unittest.IsolatedAsyncioTestCase):
    """Notifier fan-out + drain-latency telemetry."""

    async def test_disabled_notifier_is_noop(self):
        n = Notifier(enabled=False)
        sess = _FakeSession()
        n.bind_session(sess)
        await n.inbox_message([{"event": "message", "from": "a", "seq_token": 1}])
        self.assertEqual(sess.resource_updates, [])
        self.assertEqual(sess.log_messages, [])
        self.assertEqual(n.inbox_message_fired, 0)

    async def test_enabled_notifier_fires_both_channels(self):
        n = Notifier(enabled=True)
        sess = _FakeSession()
        n.bind_session(sess)
        await n.inbox_message([
            {"event": "message", "from": "a", "seq_token": 1,
             "payload": {"text": "hi"}},
            {"event": "message", "from": "b", "seq_token": 2,
             "payload": {"text": "yo"}},
        ])
        self.assertEqual(sess.resource_updates, ["pluto://inbox"])
        self.assertEqual(len(sess.log_messages), 1)
        log = sess.log_messages[0]
        self.assertEqual(log["level"], "info")
        self.assertEqual(log["logger"], "pluto.inbox")
        self.assertEqual(log["data"]["event"], "pluto.inboxMessage")
        self.assertEqual(log["data"]["count"], 2)
        self.assertEqual(sorted(log["data"]["from"]), ["a", "b"])
        self.assertEqual(n.inbox_message_fired, 1)

    async def test_notifier_without_session_is_noop(self):
        n = Notifier(enabled=True)
        await n.inbox_message([{"event": "message", "from": "a", "seq_token": 1}])
        self.assertEqual(n.inbox_message_fired, 0)

    async def test_notifier_survives_send_failures(self):
        n = Notifier(enabled=True)
        sess = _FakeSession()
        sess.fail_resource_updated = True
        sess.fail_log_message = True
        n.bind_session(sess)
        # Should not raise; should record send_failures.
        await n.inbox_message([{"event": "message", "from": "a", "seq_token": 1}])
        self.assertEqual(n.send_failures, 2)
        self.assertEqual(n.inbox_message_fired, 0)

    async def test_watcher_error_fires_warning(self):
        n = Notifier(enabled=True)
        sess = _FakeSession()
        n.bind_session(sess)
        await n.watcher_error("default", "boom")
        self.assertEqual(len(sess.log_messages), 1)
        self.assertEqual(sess.log_messages[0]["level"], "warning")
        self.assertEqual(
            sess.log_messages[0]["data"]["event"], "pluto.watcherError",
        )

    def test_summary_has_expected_shape(self):
        n = Notifier(enabled=True)
        n.record_drain_latency_ms(10.0)
        n.record_drain_latency_ms(20.0)
        n.record_drain_latency_ms(30.0)
        s = n.summary()
        self.assertEqual(s["enabled"], True)
        self.assertEqual(s["drain_samples"], 3)
        self.assertEqual(s["drain_mean_ms"], 20.0)
        self.assertIsNotNone(s["drain_p95_ms"])


class TestInboxTelemetry(unittest.IsolatedAsyncioTestCase):
    """Drain-latency telemetry: _absorb → _ack_messages delta is recorded."""

    async def test_drain_latency_recorded_through_notifier(self):
        client = FakeHttpClient()
        inbox = InboxManager(client)
        notifier = Notifier(enabled=True)
        inbox.set_notifier(notifier)

        await inbox._absorb([
            {"event": "message", "from": "x", "seq_token": 42,
             "payload": {"text": "hi"}},
        ])
        # A tiny delay so the recorded latency is > 0.
        await asyncio.sleep(0.01)
        msgs = await inbox.drain()
        self.assertEqual(len(msgs), 1)
        # One sample recorded, in milliseconds, > 0.
        self.assertEqual(notifier.summary()["drain_samples"], 1)
        self.assertGreater(notifier.summary()["drain_mean_ms"], 0.0)

    async def test_absorb_fires_notifier_inbox_message(self):
        client = FakeHttpClient()
        inbox = InboxManager(client)
        notifier = Notifier(enabled=True)
        sess = _FakeSession()
        notifier.bind_session(sess)
        inbox.set_notifier(notifier)

        await inbox._absorb([
            {"event": "message", "from": "x", "seq_token": 99,
             "payload": {"text": "hi"}},
        ])
        self.assertEqual(notifier.inbox_message_fired, 1)
        self.assertEqual(sess.resource_updates, ["pluto://inbox"])


class TestPromptsPhase2(unittest.TestCase):
    """Prompts surface the new conventions and end-of-turn respawn rule."""

    def test_connection_block_describes_end_of_turn_respawn(self):
        body = build_connection_block(
            host="127.0.0.1", http_port=9201, agent_id="a",
            wait_timeout_s=60, iterations=15,
        )
        self.assertIn("end-of-turn", body.lower())
        # And the old "respawn after every drain" mandate is gone.
        self.assertNotIn("respawn the watcher immediately after every drain",
                         body.lower())

    def test_connection_block_mentions_spec_contract(self):
        body = build_connection_block(
            host="127.0.0.1", http_port=9201, agent_id="a",
            wait_timeout_s=60, iterations=15,
        )
        self.assertIn("spec_contract", body)
        self.assertIn("conv_seq", body)
        # And tells the agent to drop from_role.
        self.assertIn("from_role", body)

    def test_connection_block_describes_inheritance_probe(self):
        body = build_connection_block(
            host="127.0.0.1", http_port=9201, agent_id="a",
            wait_timeout_s=60, iterations=15,
        )
        self.assertIn("PLUTO_MCP_INHERITED", body)
        self.assertIn("watcher_available", body)


if __name__ == "__main__":
    unittest.main()
