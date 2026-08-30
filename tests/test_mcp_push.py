"""Tests for the Claude Code push-wakeup path (SocketNotifier).

Pure unit tests: a local capture Unix server stands in for the Claude
Code session inbox socket, and a fake PlutoHttpClient backs the
InboxManager integration tests — no Erlang server, no Claude Code.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure src_py is importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
sys.path.insert(0, _SRC_PY)
sys.path.insert(0, _THIS_DIR)  # sibling import of test_mcp_friend under pytest

try:
    from agent_mcp_friend.inbox import InboxManager
    from agent_mcp_friend.socket_notifier import SocketNotifier
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )

from test_mcp_friend import FakeHttpClient


class TestSocketNotifier(unittest.IsolatedAsyncioTestCase):
    """Unit tests against a local capture Unix server (no Claude Code)."""

    async def asyncSetUp(self):
        # macOS caps AF_UNIX paths at ~104 bytes — keep the dir short.
        self.tmpdir = tempfile.mkdtemp(prefix="pluto-push-", dir="/tmp")
        self.received: list[str] = []
        self.connections = 0
        self.sock_path = os.path.join(self.tmpdir, "s.sock")

        async def handle(reader, writer):
            self.connections += 1
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.received.append(line.decode().rstrip("\n"))
            writer.close()

        self.server = await asyncio.start_unix_server(handle, path=self.sock_path)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _notifier(self, token="tok-1", enabled=True) -> SocketNotifier:
        n = SocketNotifier(self.sock_path, token, "agent-x", enabled=enabled)
        n.DEBOUNCE_S = 0.05  # keep tests fast
        return n

    async def _settle(self):
        # debounce + server read slack
        await asyncio.sleep(0.3)

    # ── from_env gating ───────────────────────────────────────────────────

    def test_from_env_disabled_when_socket_env_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            n = SocketNotifier.from_env("a")
        self.assertFalse(n.available)
        self.assertIn(
            "CLAUDE_CODE_MESSAGING_SOCKET", n.summary()["unavailable_reason"],
        )

    def test_from_env_auto_enabled_when_socket_env_present(self):
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": self.sock_path,
               "CLAUDE_CODE_MESSAGING_TOKEN": "t"}
        with patch.dict(os.environ, env, clear=True):
            n = SocketNotifier.from_env("a")
        self.assertTrue(n.available)

    def test_pluto_mcp_push_false_disables_despite_socket(self):
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": self.sock_path,
               "PLUTO_MCP_PUSH": "off"}
        with patch.dict(os.environ, env, clear=True):
            n = SocketNotifier.from_env("a")
        self.assertFalse(n.available)

    def test_unexpanded_env_placeholder_treated_as_unset(self):
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": "${CLAUDE_CODE_MESSAGING_SOCKET}",
               "CLAUDE_CODE_MESSAGING_TOKEN": "${CLAUDE_CODE_MESSAGING_TOKEN}"}
        with patch.dict(os.environ, env, clear=True):
            n = SocketNotifier.from_env("a")
        self.assertFalse(n.available)

    def test_windows_marks_unavailable(self):
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": self.sock_path}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(sys, "platform", "win32"):
            n = SocketNotifier.from_env("a")
        self.assertFalse(n.available)
        self.assertEqual(
            n.summary()["unavailable_reason"], "windows_named_pipe_unsupported",
        )

    # ── Wire format ───────────────────────────────────────────────────────

    async def test_wakeup_sends_auth_line_first_then_user_frame(self):
        n = self._notifier(token="tok-9")
        await n.notify_new_messages([{"from": "peer-1", "payload": {"x": 1}}])
        await self._settle()
        self.assertEqual(len(self.received), 2)
        auth = json.loads(self.received[0])
        self.assertEqual(auth, {"type": "auth", "token": "tok-9"})
        frame = json.loads(self.received[1])
        self.assertEqual(frame["type"], "user")
        self.assertEqual(frame["message"]["role"], "user")
        self.assertIn("pluto_recv", frame["message"]["content"])

    async def test_wakeup_text_has_count_and_senders_no_payloads(self):
        n = self._notifier()
        secret = "payload-secret-do-not-leak"
        await n.notify_new_messages([
            {"from": "peer-1", "payload": {"text": secret}},
            {"from": "peer-2", "payload": {"text": secret}},
        ])
        await self._settle()
        content = json.loads(self.received[-1])["message"]["content"]
        self.assertIn("2 new Pluto message(s)", content)
        self.assertIn("peer-1", content)
        self.assertIn("peer-2", content)
        self.assertNotIn(secret, content)

    async def test_no_auth_line_without_token(self):
        n = self._notifier(token=None)
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(json.loads(self.received[0])["type"], "user")

    # ── Debounce / suppression ────────────────────────────────────────────

    async def test_debounce_coalesces_burst_into_one_connection(self):
        n = self._notifier()
        for i in range(5):
            await n.notify_new_messages([{"from": f"p{i}", "payload": {}}])
        await self._settle()
        self.assertEqual(self.connections, 1)
        self.assertEqual(n.summary()["wakeups_sent"], 1)
        content = json.loads(self.received[-1])["message"]["content"]
        self.assertIn("5 new Pluto message(s)", content)

    async def test_second_wakeup_suppressed_until_drained(self):
        n = self._notifier()
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 1)
        # New arrival while the first wakeup is outstanding → suppressed.
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 1)
        self.assertEqual(n.summary()["suppressed"], 1)
        # Drain re-arms; the next arrival wakes again.
        n.notify_drained()
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 2)

    async def test_drain_cancels_pending_flush(self):
        n = self._notifier()
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        n.notify_drained()  # agent drained before the debounce fired
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 0)
        self.assertEqual(self.connections, 0)

    # ── Failure handling ──────────────────────────────────────────────────

    async def test_send_failure_swallowed_counted_and_degrades(self):
        n = SocketNotifier(
            os.path.join(self.tmpdir, "missing.sock"), "t", "a", enabled=True,
        )
        n.DEBOUNCE_S = 0.05
        for _ in range(3):
            await n.notify_new_messages([{"from": "p", "payload": {}}])
            await self._settle()
            n.notify_drained()
        s = n.summary()
        self.assertEqual(s["send_failures"], 3)
        self.assertTrue(s["degraded"])
        self.assertIsNotNone(s["last_error"])
        # Degraded + inside cooldown → flush suppressed, no 4th failure.
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["send_failures"], 3)

    async def test_success_after_failures_rearms(self):
        n = self._notifier()
        n.RETRY_COOLDOWN_S = 0.0
        # Break the path temporarily by pointing at a missing socket.
        good_path = n._socket_path
        n._socket_path = os.path.join(self.tmpdir, "missing.sock")
        for _ in range(3):
            await n.notify_new_messages([{"from": "p", "payload": {}}])
            await self._settle()
            n.notify_drained()
        self.assertTrue(n.summary()["degraded"])
        n._socket_path = good_path
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        s = n.summary()
        self.assertFalse(s["degraded"])
        self.assertEqual(s["wakeups_sent"], 1)

    async def test_disabled_notifier_sends_nothing(self):
        n = self._notifier(enabled=False)
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(self.connections, 0)

    async def test_failed_wakeup_retries_and_delivers(self):
        # A transient send failure must not strand the wakeup: pending
        # state is restored and the flush rescheduled.
        n = self._notifier()
        # Longer than one _settle() so exactly one failure lands before
        # the socket "comes back", and the retry fires in the second.
        n.RETRY_BACKOFF_S = 0.4
        good_path = n._socket_path
        n._socket_path = os.path.join(self.tmpdir, "missing.sock")
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()  # first flush fails
        self.assertEqual(n.summary()["send_failures"], 1)
        n._socket_path = good_path  # socket comes back
        await self._settle()  # retry fires without any new arrival
        s = n.summary()
        self.assertEqual(s["wakeups_sent"], 1)
        self.assertIn("1 new Pluto message(s)",
                      json.loads(self.received[-1])["message"]["content"])

    async def test_min_rewake_elapsed_allows_second_wakeup(self):
        n = self._notifier()
        n.MIN_REWAKE_S = 0.0
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 1)
        # Still awaiting drain, but MIN_REWAKE_S has elapsed → re-wake.
        await n.notify_new_messages([{"from": "p", "payload": {}}])
        await self._settle()
        self.assertEqual(n.summary()["wakeups_sent"], 2)


class TestInboxPushIntegration(unittest.IsolatedAsyncioTestCase):
    """InboxManager fires the push notifier on absorb and re-arms on drain."""

    class RecordingPush:
        def __init__(self):
            self.notified: list[list[dict]] = []
            self.drained = 0

        async def notify_new_messages(self, messages):
            self.notified.append(list(messages))

        def notify_drained(self):
            self.drained += 1

    async def asyncSetUp(self):
        self.client = FakeHttpClient()
        self.inbox = InboxManager(self.client)
        self.push = self.RecordingPush()
        self.inbox.set_push_notifier(self.push)

    def _msg(self, seq: int, sender: str = "peer") -> dict:
        return {
            "event": "message", "from": sender,
            "payload": {"text": f"m{seq}"}, "seq_token": seq,
        }

    async def test_absorb_fires_push_with_fresh_messages(self):
        await self.inbox._absorb([self._msg(1), self._msg(2)])
        self.assertEqual(len(self.push.notified), 1)
        self.assertEqual(len(self.push.notified[0]), 2)

    async def test_noise_does_not_fire_push(self):
        await self.inbox._absorb([
            {"event": "delivery_ack", "seq_token": 3},
        ])
        self.assertEqual(self.push.notified, [])

    async def test_drain_rearms_push(self):
        await self.inbox._absorb([self._msg(1)])
        await self.inbox.drain()
        self.assertEqual(self.push.drained, 1)

    async def test_pop_one_rearms_push_only_when_buffer_empties(self):
        await self.inbox._absorb([self._msg(1), self._msg(2)])
        await self.inbox.pop_one()
        self.assertEqual(self.push.drained, 0)
        await self.inbox.pop_one()
        self.assertEqual(self.push.drained, 1)

    async def test_piggyback_batch_rearms_push(self):
        await self.inbox._absorb([self._msg(1)])
        await self.inbox.piggyback({"status": "ok"})
        self.assertEqual(self.push.drained, 1)

    async def test_piggyback_single_mode_rearms_only_when_empty(self):
        self.inbox.set_delivery_mode("single")
        await self.inbox._absorb([self._msg(1), self._msg(2)])
        await self.inbox.piggyback({"status": "ok"})  # pops head, 1 left
        self.assertEqual(self.push.drained, 0)
        await self.inbox.piggyback({"status": "ok"})  # pops last
        self.assertEqual(self.push.drained, 1)

    async def test_wait_for_messages_drain_rearms_push(self):
        await self.inbox._absorb([self._msg(1)])
        await self.inbox.wait_for_messages(timeout_s=0.1, drain=True)
        self.assertEqual(self.push.drained, 1)


if __name__ == "__main__":
    unittest.main()
