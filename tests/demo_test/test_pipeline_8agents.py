"""
Pipeline test: 8 agents coordinate to build a reviewed document.

- 4 writer agents each write a section of a document
- 4 reviewer agents each review and approve their paired section
- Writers chain-pass control (writer-1 → writer-2 → writer-3 → writer-4)
- All agents contend for a shared status-board lock
- writer-1 collects all reviewer approvals and assembles the final document
- Final assertions check all sections, status board, final assembly, and stats

Requires the real Erlang Pluto server (auto-started if not already running).
"""

import json
import os
import sys
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
_TESTS = os.path.join(_PROJECT, "tests")
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from agent_wrapper import AgentWrapper
from pluto_client import PlutoClient
from pluto_test_server import PlutoTestServer

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000


class TestPipeline8Agents(unittest.TestCase):
    """Integration test: 8 agents cooperate through Pluto to build a reviewed document."""

    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_pipeline_document_assembly(self):
        """4 writers + 4 reviewers coordinate via locks and messages."""
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_pipeline_8agents.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # Print all agent logs for visibility
        for agent in results["agents"]:
            for line in agent["log"]:
                print(line)

        # All agents must complete successfully
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed: {agent['error']}",
            )

        # All assertions from the system flow must pass
        for a in results["assertions"]:
            self.assertTrue(a["passed"], f"Assertion failed: {a}")

        # Overall
        self.assertTrue(results["success"], f"System flow failed: {results}")

    def test_stats_after_pipeline(self):
        """Run the pipeline and verify statistics are tracked correctly."""
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_pipeline_8agents.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # Verify the pipeline succeeded
        self.assertTrue(results["success"], f"Pipeline failed: {results}")

        # Now query stats from the real server
        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="stats-checker"
        ) as client:
            stats = client.stats()

        self.assertEqual(stats["status"], "ok")

        counters = stats["counters"]
        # We should have acquired and released multiple locks
        self.assertGreater(counters["locks_acquired"], 0,
                           "Expected locks to have been acquired")
        self.assertGreater(counters["locks_released"], 0,
                           "Expected locks to have been released")
        # 8 agents registered per pipeline run (x2 runs now) + stats-checker
        self.assertGreater(counters["agents_registered"], 0)
        # Messages were exchanged
        self.assertGreater(counters["messages_sent"], 0)
        self.assertGreater(counters["messages_received"], 0)
        # At least one broadcast from writer-1
        self.assertGreater(counters["broadcasts_sent"], 0)

        # Per-agent stats should exist
        agent_stats = stats["agent_stats"]
        self.assertIn("writer-1", agent_stats)
        self.assertIn("reviewer-1", agent_stats)

        # Print stats for visibility
        print("\n=== Server Statistics ===")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    unittest.main()
