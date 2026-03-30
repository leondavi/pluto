"""
Demo test: two agents coordinate editing a shared file through Pluto.

- editor-1 acquires a lock, writes line 1, releases, and signals editor-2.
- editor-2 waits for the signal, acquires the lock, appends line 2, releases.
- The test asserts both lines are present in the final file.

Uses MockPlutoServer so no running Erlang server is needed.
"""

import os
import sys
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from agent_wrapper import AgentWrapper, MockPlutoServer


class TestSharedFileEdit(unittest.TestCase):
    """Integration test: two agents coordinate via Pluto to edit a shared file."""

    @classmethod
    def setUpClass(cls):
        cls.server = MockPlutoServer(host="127.0.0.1", port=0)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_two_editors_coordinate(self):
        """editor-1 writes, editor-2 appends — both via Pluto lock coordination."""
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_shared_edit.json")

        wrapper = AgentWrapper(host="127.0.0.1", port=self.server.port)
        results = wrapper.run_system_flow(sys_flow)

        # Print agent logs for visibility
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


if __name__ == "__main__":
    unittest.main()
