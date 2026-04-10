"""
3-Agent Cooperative Design Test
================================

3 agents (architect, backend-dev, frontend-dev) cooperate through Pluto
to build a software design document:

  Phase 1 (Sequential):  architect designs the system architecture
  Phase 2 (Parallel):    backend-dev and frontend-dev implement their
                         sections simultaneously, contending for the
                         shared status-board lock
  Phase 3 (Sequential):  architect collects both completions and
                         assembles the final document

Runs against the real Erlang Pluto server (auto-started via PlutoTestServer).
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


class Test3AgentCooperativeDesign(unittest.TestCase):
    """Integration test: 3 agents cooperate via Pluto to build a design doc."""

    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_cooperative_design_document(self):
        """architect → parallel(backend-dev, frontend-dev) → architect assembles."""
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_3agent_design.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # Print all agent logs for visibility
        print("\n" + "=" * 70)
        print("  3-AGENT COOPERATIVE DESIGN — AGENT LOGS")
        print("=" * 70)
        for agent in results["agents"]:
            print(f"\n--- {agent['agent_id']} ---")
            for line in agent["log"]:
                print(f"  {line}")

        # All agents must complete successfully
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed: {agent['error']}",
            )

        # All assertions from the system flow must pass
        for a in results["assertions"]:
            self.assertTrue(a["passed"], f"Assertion failed: {a}")

        # Overall success
        self.assertTrue(results["success"], f"System flow failed: {results}")

    def test_stats_after_cooperation(self):
        """Run the cooperative flow and verify Pluto statistics."""
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_3agent_design.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)
        self.assertTrue(results["success"], f"Flow failed: {results}")

        # Query stats from the real Pluto server
        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="stats-checker"
        ) as client:
            stats = client.stats()

        self.assertEqual(stats["status"], "ok")

        counters = stats["counters"]
        # Locks were acquired and released
        self.assertGreater(counters["locks_acquired"], 0,
                           "Expected locks to have been acquired")
        self.assertGreater(counters["locks_released"], 0,
                           "Expected locks to have been released")
        # 3 agents (x2 test runs) + stats-checker registered
        self.assertGreater(counters["agents_registered"], 0)
        # Messages exchanged: architect→backend-dev, architect→frontend-dev,
        # backend-dev→architect, frontend-dev→architect = 4 per run
        self.assertGreater(counters["messages_sent"], 0)
        # At least one broadcast from architect
        self.assertGreater(counters["broadcasts_sent"], 0)

        # Per-agent stats should include our agents
        agent_stats = stats.get("agent_stats", {})

        # Print full statistics
        print("\n" + "=" * 70)
        print("  PLUTO SERVER STATISTICS")
        print("=" * 70)
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    unittest.main()
