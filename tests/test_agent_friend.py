"""
Tests for PlutoAgentFriend — the PTY-based agent wrapper.

Tests cover:
    1. ANSI stripping
    2. Framework detection (detect_available_frameworks)
    3. AgentStateDetector (BUSY / ASKING_USER / READY)
    4. MessageFormatter — all event types
    5. PlutoConnection — connect / drain
    6. Pluto config loading & server status
    7. PlutoAgentFriend.sh bash script (--help, interactive)
    8. Integration: connect, register, format, disconnect
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest

# Ensure src_py is importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
sys.path.insert(0, _SRC_PY)

from agent_friend.pluto_agent_friend import (
    AGENT_STATE_ASKING_USER,
    AGENT_STATE_BUSY,
    AGENT_STATE_READY,
    FOCUS_IN_EVENT,
    FOCUS_OUT_EVENT,
    FOCUS_TRACKING_DISABLE,
    FOCUS_TRACKING_ENABLE,
    AgentStateDetector,
    MessageFormatter,
    PlutoAgentFriend,
    PlutoConnection,
    TerminalProxy,
    check_pluto_status,
    detect_available_frameworks,
    get_framework_cmd,
    get_framework_ready_pattern,
    load_pluto_config,
    strip_ansi,
)


class TestStripAnsi(unittest.TestCase):
    """Test ANSI escape sequence stripping."""

    def test_plain_text(self):
        self.assertEqual(strip_ansi("hello world"), "hello world")

    def test_color_codes(self):
        self.assertEqual(strip_ansi("\033[0;32mOK\033[0m"), "OK")

    def test_bold_and_dim(self):
        self.assertEqual(strip_ansi("\033[1mBold\033[2mDim\033[0m"), "BoldDim")

    def test_empty(self):
        self.assertEqual(strip_ansi(""), "")


class TestFocusEventFiltering(unittest.TestCase):
    """Test TerminalProxy focus-event interception for Ink-based TUIs."""

    def _make_proxy(self):
        """Create a TerminalProxy without spawning a child."""
        proxy = TerminalProxy(["echo", "test"])
        proxy._master_fd = -1  # not used for these tests
        return proxy

    def test_filter_agent_output_intercepts_focus_enable(self):
        proxy = self._make_proxy()
        data = b"some-output" + FOCUS_TRACKING_ENABLE + b"more-output"
        result = proxy._filter_agent_output(data)
        self.assertNotIn(FOCUS_TRACKING_ENABLE, result)
        self.assertIn(b"some-output", result)
        self.assertIn(b"more-output", result)
        self.assertTrue(proxy._focus_tracking_active)
        # Focus-in should have been injected into _ibuf
        self.assertIn(FOCUS_IN_EVENT, proxy._ibuf)

    def test_filter_agent_output_intercepts_focus_disable(self):
        proxy = self._make_proxy()
        proxy._focus_tracking_active = True
        data = FOCUS_TRACKING_DISABLE + b"rest"
        result = proxy._filter_agent_output(data)
        self.assertNotIn(FOCUS_TRACKING_DISABLE, result)
        self.assertFalse(proxy._focus_tracking_active)

    def test_filter_agent_output_passes_normal_data(self):
        proxy = self._make_proxy()
        data = b"\x1b[?1049h\x1b[c\x1b[>1u"
        result = proxy._filter_agent_output(data)
        self.assertEqual(result, data)
        self.assertFalse(proxy._focus_tracking_active)

    def test_filter_stdin_drops_focus_out(self):
        proxy = self._make_proxy()
        data = FOCUS_OUT_EVENT
        result = proxy._filter_stdin(data)
        self.assertEqual(result, b"")

    def test_filter_stdin_drops_focus_out_mixed(self):
        proxy = self._make_proxy()
        data = b"hello" + FOCUS_OUT_EVENT + b"world"
        result = proxy._filter_stdin(data)
        self.assertEqual(result, b"helloworld")

    def test_filter_stdin_passes_focus_in(self):
        proxy = self._make_proxy()
        data = FOCUS_IN_EVENT
        result = proxy._filter_stdin(data)
        self.assertEqual(result, data)

    def test_filter_stdin_passes_normal_keys(self):
        proxy = self._make_proxy()
        data = b"hello world\r"
        result = proxy._filter_stdin(data)
        self.assertEqual(result, data)

    def test_focus_in_only_injected_once(self):
        proxy = self._make_proxy()
        # First enable → injects focus-in
        proxy._filter_agent_output(FOCUS_TRACKING_ENABLE)
        self.assertEqual(proxy._ibuf, FOCUS_IN_EVENT)
        # Second enable → no duplicate injection
        proxy._ibuf = b""
        proxy._filter_agent_output(FOCUS_TRACKING_ENABLE)
        self.assertEqual(proxy._ibuf, b"")


class TestFrameworkDetection(unittest.TestCase):
    """Test agent framework detection."""

    def test_detect_returns_list(self):
        result = detect_available_frameworks()
        self.assertIsInstance(result, list)
        # Each entry should have key, display, cmd, path
        for fw in result:
            self.assertIn("key", fw)
            self.assertIn("display", fw)
            self.assertIn("cmd", fw)
            self.assertIn("path", fw)

    def test_get_framework_cmd_known(self):
        cmd = get_framework_cmd("claude")
        self.assertEqual(cmd, ["claude"])

    def test_get_framework_cmd_unknown(self):
        cmd = get_framework_cmd("some-unknown-agent")
        self.assertEqual(cmd, ["some-unknown-agent"])

    def test_get_ready_pattern_claude(self):
        # Claude has no specific pattern (uses silence timeout)
        pattern = get_framework_ready_pattern("claude")
        self.assertIsNone(pattern)

    def test_get_ready_pattern_aider(self):
        pattern = get_framework_ready_pattern("aider")
        self.assertIsNotNone(pattern)
        self.assertTrue(re.compile(pattern).search("> "))


class TestStateDetection(unittest.TestCase):
    """Test AgentStateDetector state classification."""

    def _make_detector(self, ready_pattern=None):
        """Create an AgentStateDetector with test-friendly patterns."""
        return AgentStateDetector(
            ready_pattern=ready_pattern,
            ask_patterns=[
                r"\?\s*$",
                r"\[y/n\]",
                r"\[Y/n\]",
                r"Continue\?",
                r"Proceed\?",
            ],
            silence_timeout=3.0,
        )

    def test_busy_on_output(self):
        d = self._make_detector()
        d.analyse_output(b"Processing files...\n")
        self.assertEqual(d.state, AGENT_STATE_BUSY)

    def test_asking_user_question_mark(self):
        d = self._make_detector()
        d.analyse_output(b"Would you like to continue?\n")
        self.assertEqual(d.state, AGENT_STATE_ASKING_USER)

    def test_asking_user_yn(self):
        d = self._make_detector()
        d.analyse_output(b"Save changes? [y/n]\n")
        self.assertEqual(d.state, AGENT_STATE_ASKING_USER)

    def test_ready_pattern_match(self):
        d = self._make_detector(ready_pattern=r"^> $")
        d.analyse_output(b"Done.\n> ")
        self.assertEqual(d.state, AGENT_STATE_READY)

    def test_ready_pattern_no_match(self):
        d = self._make_detector(ready_pattern=r"^> $")
        d.analyse_output(b"Still working...\n")
        self.assertEqual(d.state, AGENT_STATE_BUSY)

    def test_injection_blocked_when_asking(self):
        d = self._make_detector()
        d.state = AGENT_STATE_ASKING_USER
        self.assertFalse(d.is_ready_for_injection())

    def test_injection_blocked_when_user_typing(self):
        d = self._make_detector()
        d.state = AGENT_STATE_READY
        d._user_typing_time = time.monotonic()  # just typed
        self.assertFalse(d.is_ready_for_injection())

    def test_injection_ready_pattern(self):
        d = self._make_detector()
        d.state = AGENT_STATE_READY
        d._user_typing_time = 0.0  # long ago
        self.assertTrue(d.is_ready_for_injection())

    def test_injection_ready_silence(self):
        d = self._make_detector()
        d.state = AGENT_STATE_BUSY
        d._last_output_time = time.monotonic() - 10.0  # 10s ago
        d._user_typing_time = 0.0
        self.assertTrue(d.is_ready_for_injection())

    def test_ink_redraw_does_not_reset_silence_timer(self):
        """Ink-style screen redraws (same visible text) must not restart
        the silence timer, otherwise is_ready_for_injection() never fires."""
        d = self._make_detector()
        d.analyse_output(b"Describe a task to get started.\n> ")
        first_time = d._last_output_time
        # Feed the exact same content again (simulates Ink screen redraw)
        d.analyse_output(b"Describe a task to get started.\n> ")
        self.assertEqual(d._last_output_time, first_time,
                         "Silence timer should NOT reset on identical content")

    def test_new_content_does_reset_silence_timer(self):
        """Genuinely new output must still update the silence timer."""
        d = self._make_detector()
        d.analyse_output(b"Hello world\n")
        old_time = d._last_output_time
        d._last_output_time -= 5.0  # pretend time passed
        old_time = d._last_output_time
        d.analyse_output(b"Something new happened\n")
        self.assertGreater(d._last_output_time, old_time,
                           "Silence timer should reset on new content")

    def test_pure_ansi_chunk_ignored(self):
        """Chunks containing only ANSI escape sequences (cursor moves,
        colors with no visible text) should be completely ignored."""
        d = self._make_detector()
        d.analyse_output(b"Initial output\n")
        d.state = AGENT_STATE_READY
        d._last_output_time -= 5.0
        old_time = d._last_output_time
        # Feed pure ANSI (cursor move + style reset, no visible text)
        d.analyse_output(b"\x1b[2;1H\x1b[0m\x1b[K")
        self.assertEqual(d._last_output_time, old_time,
                         "Pure ANSI should not update timer")
        # State should remain unchanged (READY — not flipped to BUSY)
        self.assertEqual(d.state, AGENT_STATE_READY)


class TestMessageFormatting(unittest.TestCase):
    """Test MessageFormatter output for each event type."""

    def test_format_direct_message(self):
        messages = [
            {"event": "message", "from": "reviewer-1",
             "payload": {"type": "review_done", "file": "main.py"}}
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("Pluto msg from reviewer-1", result)
        self.assertIn("review_done", result)
        self.assertIn("main.py", result)

    def test_format_broadcast(self):
        messages = [
            {"event": "broadcast", "from": "lead",
             "payload": {"type": "build_complete"}}
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("Pluto bcast from lead", result)

    def test_format_task_assigned(self):
        messages = [
            {"event": "task_assigned", "task_id": "TASK-42",
             "from": "architect", "description": "Fix the bug",
             "payload": {"file": "api.py"}}
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("TASK-42", result)
        self.assertIn("Fix the bug", result)
        self.assertIn("pluto_task_update", result)

    def test_format_topic_message(self):
        messages = [
            {"event": "topic_message", "topic": "builds",
             "from": "ci", "payload": {"status": "passed"}}
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("topic 'builds'", result)

    def test_format_multiple_messages(self):
        messages = [
            {"event": "message", "from": "a", "payload": {"x": 1}},
            {"event": "message", "from": "b", "payload": {"x": 2}},
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("from a", result)
        self.assertIn("from b", result)

    def test_format_unknown_event(self):
        """Unknown/infrastructure events are silently skipped."""
        messages = [{"event": "delivery_ack", "data": "hello"}]
        result = MessageFormatter.format(messages)
        self.assertNotIn("delivery_ack", result)
        # Only the header should remain
        self.assertIn("Pluto coordination msgs", result)

    def test_format_filters_ack_keeps_real(self):
        """Formatter skips acks but keeps actionable messages."""
        messages = [
            {"event": "delivery_ack", "msg_id": "123"},
            {"event": "message", "from": "alice", "payload": {"text": "hi"}},
            {"event": "delivery_ack", "msg_id": "456"},
        ]
        result = MessageFormatter.format(messages)
        self.assertIn("alice", result)
        self.assertNotIn("delivery_ack", result)

    def test_format_filters_ack_wrapped_as_message(self):
        """Delivery acks wrapped as regular messages (server behaviour)
        must be filtered out by checking the payload."""
        messages = [
            {
                "event": "message",
                "from": "keren",
                "payload": {
                    "event": "delivery_ack",
                    "msg_id": "MSG-123",
                    "to": "keren",
                    "delivered": True,
                    "acked_at": 1776691754861,
                },
            },
            {
                "event": "message",
                "from": "keren",
                "payload": {"text": "Hello david!"},
            },
        ]
        result = MessageFormatter.format(messages)
        self.assertNotIn("delivery_ack", result)
        self.assertNotIn("MSG-123", result)
        self.assertIn("Hello david!", result)


class TestPlutoConfig(unittest.TestCase):
    """Test config loading."""

    def test_load_config_returns_dict(self):
        config = load_pluto_config()
        self.assertIsInstance(config, dict)

    def test_load_config_has_server_section(self):
        config = load_pluto_config()
        # Config may or may not exist depending on environment
        if config:
            self.assertIn("pluto_server", config)


class TestPlutoServerStatus(unittest.TestCase):
    """Test Pluto server status check."""

    def test_check_status_returns_dict_or_none(self):
        # This tests against the real server if running, or returns None
        result = check_pluto_status("localhost", 9001)
        if result is not None:
            self.assertIn("status", result)
        # If None, server is just not running — that's OK


class TestBashScript(unittest.TestCase):
    """Test PlutoAgentFriend.sh script basics."""

    def test_help_flag(self):
        script = os.path.join(_PROJECT, "PlutoAgentFriend.sh")
        if not os.path.isfile(script):
            self.skipTest("PlutoAgentFriend.sh not found")
        result = subprocess.run(
            [script, "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("PlutoAgentFriend", result.stdout)
        self.assertIn("--agent-id", result.stdout)
        self.assertIn("--framework", result.stdout)
        self.assertIn("--mode", result.stdout)

    def test_missing_agent_id_interactive(self):
        """When no --agent-id, script prompts interactively; empty input → exit 1."""
        script = os.path.join(_PROJECT, "PlutoAgentFriend.sh")
        if not os.path.isfile(script):
            self.skipTest("PlutoAgentFriend.sh not found")
        result = subprocess.run(
            [script],
            input="",  # empty stdin → interactive prompt gets empty answer
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        # Should show the interactive prompt or the error about empty agent-id
        combined = result.stdout + result.stderr
        self.assertTrue(
            "agent" in combined.lower(),
            f"Expected 'agent' in output, got:\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )


class TestIntegrationWithEchoAgent(unittest.TestCase):
    """
    Integration test: use PlutoConnection and MessageFormatter with
    the real Pluto server.

    Requires the Pluto server to be running.
    """

    def test_pluto_connect_and_format(self):
        """Test PlutoConnection lifecycle and MessageFormatter together."""
        # Check if Pluto server is running
        status = check_pluto_status("localhost", 9001)
        if status is None:
            self.skipTest("Pluto server not running")

        agent_id = f"test-friend-{int(time.time())}"
        conn = PlutoConnection(
            agent_id=agent_id,
            host="localhost",
            http_port=9001,
        )

        # Connect
        self.assertTrue(conn.connect(), "Should connect to Pluto")
        self.assertTrue(conn.connected)

        # Verify registration via a second client
        from pluto_client import PlutoHttpClient
        checker = PlutoHttpClient(
            host="localhost", http_port=9001,
            agent_id=f"test-checker-{int(time.time())}",
        )
        checker.register()
        agents = checker.list_agents()
        self.assertIn(agent_id, agents)
        checker.unregister()

        # Test MessageFormatter
        test_msg = {
            "event": "message",
            "from": "test-sender",
            "payload": {"action": "test", "value": 42},
        }
        formatted = MessageFormatter.format([test_msg])
        self.assertIn("test-sender", formatted)
        self.assertIn("42", formatted)

        # Disconnect
        conn.disconnect()
        self.assertFalse(conn.connected)


if __name__ == "__main__":
    unittest.main()
