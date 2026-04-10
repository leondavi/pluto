"""
3-Agent Cooperative Design — Real Copilot Agent Test
=====================================================

3 real Copilot CLI agents (architect, backend-dev, frontend-dev) cooperate
through Pluto to build a software design document:

  Phase 1 (Sequential):   architect designs the system architecture
  Phase 2 (Parallel):     backend-dev and frontend-dev work simultaneously,
                           contending for the shared status-board lock
  Phase 3 (Sequential):   architect collects completions and assembles
                           the final document

Each agent is a separate `copilot -p` process that autonomously writes and
executes a Python script using PlutoClient.

Requires the real Erlang Pluto server (auto-started via PlutoTestServer).
"""

import json
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

from agent_wrapper import AgentWrapper
from pluto_client import PlutoClient
from pluto_test_server import PlutoTestServer

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000
WORK_DIR = "/tmp/pluto_copilot_3agent_design"

COPILOT_AVAILABLE = shutil.which("copilot") is not None

# ── Task descriptions ────────────────────────────────────────────────────────

ARCHITECT_TASK = """\
You are the lead architect. Coordinate with 'backend-dev' and 'frontend-dev'.

1. Acquire a write lock on resource 'project:design'.
2. Create the file architecture.txt in the work directory with this content:
   ## System Architecture
   Designed by architect.

   Components:
   - Backend API service (REST + WebSocket)
   - Frontend SPA (React + TypeScript)
   - Shared data layer (PostgreSQL + Redis cache)

3. Release the 'project:design' lock.
4. Acquire a write lock on resource 'project:status-board'.
5. Create the file status.txt in the work directory containing:
   === Project Status Board ===
   architect: architecture design COMPLETE
6. Release the 'project:status-board' lock.
7. Send a message to 'backend-dev' with payload {"type": "start_work", "task": "implement backend API"}.
8. Send a message to 'frontend-dev' with payload {"type": "start_work", "task": "implement frontend SPA"}.
9. Wait for a completion message from 'backend-dev' (timeout 120s).
10. Wait for a completion message from 'frontend-dev' (timeout 120s).
11. Acquire a write lock on resource 'project:assembly'.
12. Read architecture.txt, backend.txt, and frontend.txt from the work directory.
13. Create the file final_design_doc.txt in the work directory that includes
    all three sections plus the footer line 'Assembly complete by architect'.
14. Release the lock.
15. Broadcast {"type": "all_done"} to all agents.
16. Disconnect.
"""

BACKEND_DEV_TASK = """\
You are 'backend-dev'. Wait for the architect's signal, then build the backend spec.

1. Register and wait for a message from 'architect' (timeout 120s).
2. Acquire a write lock on resource 'project:backend'.
3. Create the file backend.txt in the work directory containing:
   ## Backend API Specification
   Author: backend-dev

   Endpoints:
   - GET  /api/v1/users
   - POST /api/v1/users
   - GET  /api/v1/tasks

   Database: PostgreSQL with connection pooling
   Cache: Redis for session management
4. Release the 'project:backend' lock.
5. Acquire a write lock on resource 'project:status-board'.
6. APPEND this line to status.txt: 'backend-dev: backend implementation COMPLETE'
7. Release the 'project:status-board' lock.
8. Send a message to 'architect' with payload {"type": "work_complete", "section": "backend"}.
9. Disconnect.
"""

FRONTEND_DEV_TASK = """\
You are 'frontend-dev'. Wait for the architect's signal, then build the frontend spec.

1. Register and wait for a message from 'architect' (timeout 120s).
2. Acquire a write lock on resource 'project:frontend'.
3. Create the file frontend.txt in the work directory containing:
   ## Frontend SPA Specification
   Author: frontend-dev

   Framework: React with TypeScript
   State management: Redux Toolkit
   Routing: React Router v6
   UI components: Material UI

   Build: Vite + SWC
4. Release the 'project:frontend' lock.
5. Acquire a write lock on resource 'project:status-board'.
6. APPEND this line to status.txt: 'frontend-dev: frontend implementation COMPLETE'
7. Release the 'project:status-board' lock.
8. Send a message to 'architect' with payload {"type": "work_complete", "section": "frontend"}.
9. Disconnect.
"""


@unittest.skipUnless(COPILOT_AVAILABLE, "copilot CLI not installed")
class Test3AgentCooperativeDesign(unittest.TestCase):
    """3 real Copilot agents cooperate via Pluto to build a design doc."""

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

    def test_cooperative_design_document(self):
        """architect -> parallel(backend-dev, frontend-dev) -> architect assembles."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = [
            {"agent_id": "backend-dev",  "task": BACKEND_DEV_TASK,  "start_delay_s": 0},
            {"agent_id": "frontend-dev", "task": FRONTEND_DEV_TASK, "start_delay_s": 0},
            {"agent_id": "architect",    "task": ARCHITECT_TASK,    "start_delay_s": 5},
        ]

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=240)

        # Print agent output
        for agent in results["agents"]:
            print(f"\n{'=' * 60}")
            print(f"  Agent: {agent['agent_id']}  rc={agent['returncode']}")
            print(f"{'=' * 60}")
            print(agent["stdout"][:3000])
            if agent["stderr"]:
                print(f"STDERR:\n{agent['stderr'][:1000]}")

        # All agents must complete
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed (rc={agent['returncode']}): "
                f"{agent['stderr'][:500]}",
            )

        # architecture.txt
        with open(os.path.join(WORK_DIR, "architecture.txt")) as f:
            arch = f.read()
        self.assertIn("architect", arch)
        self.assertIn("Backend API", arch)
        self.assertIn("Frontend SPA", arch)

        # backend.txt
        with open(os.path.join(WORK_DIR, "backend.txt")) as f:
            be = f.read()
        self.assertIn("backend-dev", be)
        self.assertIn("/api/v1/users", be)
        self.assertIn("PostgreSQL", be)

        # frontend.txt
        with open(os.path.join(WORK_DIR, "frontend.txt")) as f:
            fe = f.read()
        self.assertIn("frontend-dev", fe)
        self.assertIn("React", fe)
        self.assertIn("TypeScript", fe)

        # final_design_doc.txt
        with open(os.path.join(WORK_DIR, "final_design_doc.txt")) as f:
            final = f.read()
        self.assertIn("architect", final)
        self.assertIn("backend-dev", final)
        self.assertIn("frontend-dev", final)
        self.assertIn("Assembly complete", final)

        # status.txt
        with open(os.path.join(WORK_DIR, "status.txt")) as f:
            status = f.read()
        self.assertIn("architect", status)
        self.assertIn("backend-dev", status)
        self.assertIn("frontend-dev", status)
        self.assertIn("COMPLETE", status)

    def test_stats_after_cooperation(self):
        """Run the cooperative flow and verify Pluto statistics."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = [
            {"agent_id": "backend-dev",  "task": BACKEND_DEV_TASK,  "start_delay_s": 0},
            {"agent_id": "frontend-dev", "task": FRONTEND_DEV_TASK, "start_delay_s": 0},
            {"agent_id": "architect",    "task": ARCHITECT_TASK,    "start_delay_s": 5},
        ]

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=240)
        self.assertTrue(results["success"], f"Flow failed: {results}")

        # Query stats from the real Pluto server
        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="stats-checker"
        ) as client:
            stats = client.stats()

        self.assertEqual(stats["status"], "ok")
        counters = stats["counters"]
        self.assertGreater(counters["locks_acquired"], 0)
        self.assertGreater(counters["locks_released"], 0)
        self.assertGreater(counters["agents_registered"], 0)
        self.assertGreater(counters["messages_sent"], 0)

        print("\n" + "=" * 60)
        print("  PLUTO SERVER STATISTICS")
        print("=" * 60)
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    unittest.main()
