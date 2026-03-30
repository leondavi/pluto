"""
Q&A Conversation test: 5 agents exchange 10 general knowledge questions
through the REAL Pluto Erlang server.

Agents: alice, bob, charlie, diana, eve
- Each agent asks 2 questions and answers 2 questions
- All Q&A entries are logged to a shared transcript file protected by a Pluto lock
- Messages are exchanged via Pluto's send/wait_message mechanism

Requires the real Pluto server running on 127.0.0.1:9000.
Start with: ./PlutoServer.sh --daemon
"""

import json
import os
import socket
import sys
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_wrapper import AgentWrapper
from pluto_client import PlutoClient

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000


def _server_reachable():
    """Quick TCP connect check to see if the real server is up."""
    try:
        s = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


@unittest.skipUnless(_server_reachable(), "Real Pluto server not running on port 9000")
class TestQAConversation(unittest.TestCase):
    """5 agents exchange 10 Q&A pairs through the real Pluto server."""

    def _reset_stats(self):
        """Reset server stats before the test."""
        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="test-setup"
        ) as client:
            client._send_and_wait({"op": "admin_reset_stats"})

    def test_10_questions_exchanged(self):
        """All 10 questions and answers are exchanged and logged to transcript."""
        self._reset_stats()

        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_qa_conversation.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # Print all agent logs
        for agent in results["agents"]:
            for line in agent["log"]:
                print(line)

        # All 5 agents must complete successfully
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed: {agent['error']}",
            )

        # All assertions (20 transcript lines + all_agents_completed)
        for a in results["assertions"]:
            self.assertTrue(a["passed"], f"Assertion failed: {a}")

        self.assertTrue(results["success"], f"System flow failed: {results}")

    def test_stats_reflect_conversation(self):
        """After Q&A, stats show correct message and lock counts."""
        self._reset_stats()

        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_qa_conversation.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)
        self.assertTrue(results["success"], f"Q&A flow failed: {results}")

        # Query stats from the real server
        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="stats-verifier"
        ) as client:
            stats = client.stats()

        self.assertEqual(stats["status"], "ok")
        counters = stats["counters"]

        # 5 agents registered
        self.assertGreaterEqual(counters["agents_registered"], 5)
        # 10 questions + 10 answers = 20 send operations (plus 1 broadcast from alice)
        self.assertGreaterEqual(counters["messages_sent"], 20)
        self.assertGreaterEqual(counters["messages_received"], 20)
        # Each Q&A pair requires 2 lock acquire/release cycles for transcript logging
        # 20 transcript entries = 20 lock cycles
        self.assertGreaterEqual(counters["locks_acquired"], 20)
        self.assertGreaterEqual(counters["locks_released"], 20)
        # At least 1 broadcast
        self.assertGreaterEqual(counters["broadcasts_sent"], 1)

        # Print stats summary
        print("\n=== Server Statistics After Q&A ===")
        print(f"  Agents registered:  {counters['agents_registered']}")
        print(f"  Messages sent:      {counters['messages_sent']}")
        print(f"  Messages received:  {counters['messages_received']}")
        print(f"  Locks acquired:     {counters['locks_acquired']}")
        print(f"  Locks released:     {counters['locks_released']}")
        print(f"  Broadcasts sent:    {counters['broadcasts_sent']}")
        print(f"  Total requests:     {counters['total_requests']}")


if __name__ == "__main__":
    unittest.main()
