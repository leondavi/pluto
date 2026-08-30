"""Tests for lock-loss surfacing and delivery-mode validation at the
tool layer.

A lock whose auto-renew fails is gone; the agent must find out via
``_pluto_lock_lost`` on its next tool result and via ``pluto_health``.
"""

import asyncio
import json
import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_PROJECT, "src_py"))
sys.path.insert(0, _THIS_DIR)  # sibling import of test_mcp_friend under pytest

try:
    from agent_mcp_friend.inbox import InboxManager
    from agent_mcp_friend.lock_manager import LockManager
    from agent_mcp_friend.tools import register_tools
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )

from test_mcp_friend import FakeHttpClient


def _fake_health_probe(body: dict):
    """Patch the /health GET that ``pluto_health`` performs, so epoch
    handling can be tested without a live Pluto server."""
    import contextlib
    import unittest.mock as mock

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *exc):
            return False

        def read(self_inner):
            return json.dumps(body).encode()

    @contextlib.contextmanager
    def _ctx():
        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            yield

    return _ctx()


class DenyRenewClient(FakeHttpClient):
    """FakeHttpClient whose renew can deny or raise on demand."""

    def __init__(self):
        super().__init__()
        self.renew_mode = "ok"  # "ok" | "denied" | "raise"

    def renew(self, lock_ref: str, ttl_ms: int) -> dict:
        super().renew(lock_ref, ttl_ms)
        if self.renew_mode == "denied":
            return {"status": "denied"}
        if self.renew_mode == "raise":
            raise RuntimeError("server unreachable")
        return {"status": "ok"}


class TestLockLossRecording(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DenyRenewClient()
        self.lock_mgr = LockManager(self.client)
        self.lock_mgr.MIN_RENEW_INTERVAL_S = 0.01

    async def asyncTearDown(self):
        await self.lock_mgr.shutdown()

    async def _wait_for_loss(self):
        for _ in range(100):
            if self.lock_mgr.lost_summary()["lost_total"]:
                return
            await asyncio.sleep(0.02)
        self.fail("renew loop never recorded the loss")

    async def test_renew_denied_recorded_as_lost(self):
        self.client.renew_mode = "denied"
        await self.lock_mgr.register("L-1", "src/main.py", 20)
        await self._wait_for_loss()
        lost = self.lock_mgr.take_lost()
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["lock_ref"], "L-1")
        self.assertEqual(lost[0]["resource"], "src/main.py")
        self.assertIn("renew_denied", lost[0]["reason"])
        # Drain-once: a second take returns nothing.
        self.assertEqual(self.lock_mgr.take_lost(), [])
        # Cumulative summary persists.
        self.assertEqual(self.lock_mgr.lost_summary()["lost_total"], 1)

    async def test_renew_exception_recorded_as_lost(self):
        self.client.renew_mode = "raise"
        await self.lock_mgr.register("L-2", "docs/x.md", 20)
        await self._wait_for_loss()
        lost = self.lock_mgr.take_lost()
        self.assertEqual(len(lost), 1)
        self.assertIn("renew_error", lost[0]["reason"])
        self.assertEqual(self.lock_mgr.held_locks(), [])


class TestLockLossToolSurfacing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DenyRenewClient()
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)
        self.mcp = FastMCP(name="pluto-test")
        register_tools(self.mcp, self.client, self.inbox, self.lock_mgr)

    async def test_lock_lost_piggybacked_on_next_tool_result(self):
        self.lock_mgr._record_lost("L-9", "src/a.py", "renew_denied: denied")
        result = await self.mcp.call_tool(
            "pluto_send", {"to": "bob", "payload": {"type": "ping"}},
        )
        blob = json.dumps(result, default=str)
        self.assertIn("_pluto_lock_lost", blob)
        self.assertIn("L-9", blob)
        # Delivered once — the next tool result is clean.
        result2 = await self.mcp.call_tool(
            "pluto_send", {"to": "bob", "payload": {"type": "ping"}},
        )
        self.assertNotIn("_pluto_lock_lost", json.dumps(result2, default=str))

    async def test_health_reports_lost_locks(self):
        self.lock_mgr._record_lost("L-9", "src/a.py", "renew_error: boom")
        result = await self.mcp.call_tool("pluto_health", {})
        blob = json.dumps(result, default=str)
        self.assertIn("lost_total", blob)
        self.assertIn("held", blob)
        self.assertIn("renew_error", blob)


class TestLockLossOnInboxTools(unittest.IsolatedAsyncioTestCase):
    """An agent driving a watcher/pipeline loop only ever calls the inbox
    tools. ``take_lost()`` is drain-once, so if those tools don't carry
    ``_pluto_lock_lost`` the agent never learns its lock is gone and
    keeps writing to the resource.
    """

    async def asyncSetUp(self):
        self.client = DenyRenewClient()
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)
        self.mcp = FastMCP(name="pluto-test")
        register_tools(self.mcp, self.client, self.inbox, self.lock_mgr)

    async def _assert_carries_loss(self, tool: str, args: dict):
        self.lock_mgr._record_lost("L-7", "src/a.py", "renew_denied: denied")
        blob = json.dumps(
            await self.mcp.call_tool(tool, args), default=str,
        )
        self.assertIn("_pluto_lock_lost", blob, f"{tool} dropped the loss")
        self.assertIn("L-7", blob)
        # Drain-once: the same tool called again is clean.
        blob2 = json.dumps(
            await self.mcp.call_tool(tool, args), default=str,
        )
        self.assertNotIn("_pluto_lock_lost", blob2)

    async def test_recv_carries_lock_loss(self):
        await self._assert_carries_loss("pluto_recv", {})

    async def test_pop_carries_lock_loss(self):
        await self._assert_carries_loss("pluto_pop", {"wait_s": 0})

    async def test_heartbeat_carries_lock_loss(self):
        await self._assert_carries_loss("pluto_heartbeat", {})

    async def test_wait_for_messages_carries_lock_loss(self):
        await self._assert_carries_loss(
            "pluto_wait_for_messages", {"timeout_s": 0},
        )

    async def test_pop_still_returns_exactly_one_message(self):
        """The lock-loss attachment must not drag the inbox piggyback in
        behind it — that would pull a second message off the buffer and
        break single-mode's one-message-per-pop invariant."""
        self.inbox.set_delivery_mode("single")
        await self.inbox._absorb([
            {"event": "message", "from": "a", "seq_token": 1, "payload": {}},
            {"event": "message", "from": "b", "seq_token": 2, "payload": {}},
        ])
        self.lock_mgr._record_lost("L-7", "src/a.py", "renew_denied: denied")
        blob = json.dumps(
            await self.mcp.call_tool("pluto_pop", {"wait_s": 0}), default=str,
        )
        self.assertIn("_pluto_lock_lost", blob)
        self.assertNotIn("_pluto_inbox", blob)
        # One consumed, one still buffered.
        self.assertEqual(len(await self.inbox.peek_only()), 1)


class TestHealthRecoveryHintPrecedence(unittest.IsolatedAsyncioTestCase):
    """An epoch mismatch is the *cause* of the 401s that trip the peek
    loop's unrecoverable threshold, so its hint must survive."""

    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)
        self.mcp = FastMCP(name="pluto-test")
        register_tools(self.mcp, self.client, self.inbox, self.lock_mgr)

    async def test_epoch_hint_survives_unrecoverable_peek_loop(self):
        self.client.server_epoch = "epoch-old"
        self.inbox._unrecoverable = True
        self.inbox._unrecoverable_reason = "session_lost x3 (last_error=401)"
        with _fake_health_probe({"status": "ok", "server_epoch": "epoch-new"}):
            result = await self.mcp.call_tool("pluto_health", {})
        blob = json.dumps(result, default=str)
        self.assertIn("server_restarted", blob)
        self.assertIn("Server epoch changed", blob)
        # The peek-loop diagnosis is still reported, just not as the hint.
        self.assertIn("unrecoverable", blob)

    async def test_unrecoverable_hint_used_when_no_epoch_mismatch(self):
        self.client.server_epoch = "epoch-same"
        self.inbox._unrecoverable = True
        self.inbox._unrecoverable_reason = "session_lost x3 (last_error=401)"
        with _fake_health_probe({"status": "ok", "server_epoch": "epoch-same"}):
            result = await self.mcp.call_tool("pluto_health", {})
        blob = json.dumps(result, default=str)
        self.assertNotIn("server_restarted", blob)
        self.assertIn("session_lost x3", blob)


class TestDeliveryModeToolValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)
        self.lock_mgr = LockManager(self.client)
        self.mcp = FastMCP(name="pluto-test")
        register_tools(self.mcp, self.client, self.inbox, self.lock_mgr)

    async def test_invalid_mode_returns_error_field(self):
        result = await self.mcp.call_tool(
            "pluto_set_delivery_mode", {"mode": "singel"},
        )
        blob = json.dumps(result, default=str)
        self.assertIn("invalid_mode", blob)
        self.assertIn("valid_modes", blob)
        self.assertEqual(self.inbox.delivery_mode, "batch")

    async def test_valid_mode_returns_ok(self):
        result = await self.mcp.call_tool(
            "pluto_set_delivery_mode", {"mode": "single"},
        )
        blob = json.dumps(result, default=str)
        self.assertIn("status", blob)
        self.assertNotIn("invalid_mode", blob)
        self.assertEqual(self.inbox.delivery_mode, "single")


if __name__ == "__main__":
    unittest.main()
