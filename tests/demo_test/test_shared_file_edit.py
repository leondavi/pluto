"""
Shared File Edit — Real Copilot Agent Test
============================================

Two real Copilot CLI agents coordinate editing a shared file through Pluto.

- editor-1: acquires lock, writes line 1, releases, signals editor-2.
- editor-2: waits for signal, acquires lock, appends line 2, confirms.

Each agent is a separate `copilot -p` process that autonomously writes and
executes a Python script using PlutoClient.  The test verifies that real AI
agents can understand and correctly use Pluto's locking and messaging APIs.

Requires the real Erlang Pluto server (auto-started via PlutoTestServer).
"""

import os
import shutil
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

from agent_friend import AgentWrapper
from pluto_test_server import PlutoTestServer

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000
WORK_DIR = "/tmp/pluto_copilot_shared_edit"

COPILOT_AVAILABLE = shutil.which("copilot") is not None


@unittest.skipUnless(COPILOT_AVAILABLE, "copilot CLI not installed")
class TestSharedFileEdit(unittest.TestCase):
    """Two real Copilot agents coordinate via Pluto to edit a shared file."""

    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        if os.path.exists(WORK_DIR):
            shutil.rmtree(WORK_DIR)
        os.makedirs(WORK_DIR, exist_ok=True)

    def test_two_editors_coordinate(self):
        """editor-1 writes, editor-2 appends — both via Pluto lock coordination."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = [
            {
                "agent_id": "editor-2",
                "task": (
                    "1. Register with Pluto as 'editor-2' and set up message handling.\n"
                    "2. Wait for a message from agent 'editor-1' (timeout 120s).\n"
                    "3. After receiving it, acquire a write lock on resource "
                    "'file:/demo/shared.txt'.\n"
                    "4. APPEND the text 'Line 2: Written by editor-2\\n' to the "
                    "file shared.txt in the work directory.\n"
                    "5. Release the lock.\n"
                    "6. Send a message to 'editor-1' with payload "
                    '{"type": "done", "file": "shared.txt"}.\n'
                    "7. Disconnect from Pluto.\n"
                ),
                "start_delay_s": 0,
            },
            {
                "agent_id": "editor-1",
                "task": (
                    "1. Register with Pluto as 'editor-1' and set up message handling.\n"
                    "2. Acquire a write lock on resource 'file:/demo/shared.txt'.\n"
                    "3. CREATE the file shared.txt in the work directory and write "
                    "'Line 1: Written by editor-1\\n' to it.\n"
                    "4. Release the lock.\n"
                    "5. Send a message to agent 'editor-2' with payload "
                    '{"type": "your_turn", "file": "shared.txt"}.\n'
                    "6. Wait for a response message from 'editor-2' (timeout 120s).\n"
                    "7. Disconnect from Pluto.\n"
                ),
                "start_delay_s": 3,
            },
        ]

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=180)

        # ── Print agent output ────────────────────────────────────────────
        for agent in results["agents"]:
            print(f"\n{'=' * 60}")
            print(f"  Agent: {agent['agent_id']}  rc={agent['returncode']}")
            print(f"{'=' * 60}")
            print(agent["stdout"][:3000])
            if agent["stderr"]:
                print(f"STDERR:\n{agent['stderr'][:1000]}")

        # ── Assertions ────────────────────────────────────────────────────
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed (rc={agent['returncode']}): "
                f"{agent['stderr'][:500]}",
            )

        shared = os.path.join(WORK_DIR, "shared.txt")
        self.assertTrue(os.path.exists(shared), f"shared.txt not created in {WORK_DIR}")

        with open(shared) as f:
            content = f.read()
        self.assertIn("editor-1", content,
                       f"editor-1 content missing.\nFile:\n{content}")
        self.assertIn("editor-2", content,
                       f"editor-2 content missing.\nFile:\n{content}")


if __name__ == "__main__":
    unittest.main()
