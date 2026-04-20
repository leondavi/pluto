"""
Pipeline 8-Agent Test — Real Copilot Agents
=============================================

8 real Copilot CLI agents coordinate through Pluto to build a reviewed document:

  4 writers:    each writes a section, acquires section + status-board locks
  4 reviewers:  each reviews one section after the writer signals readiness

  writer-1 waits for all 4 reviewer approvals and assembles the final document.

Each agent is a separate `copilot -p` process.

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

from agent_friend import AgentWrapper
from pluto_client import PlutoClient
from pluto_test_server import PlutoTestServer

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000
WORK_DIR = "/tmp/pluto_copilot_pipeline_8agents"

COPILOT_AVAILABLE = shutil.which("copilot") is not None

# ── Writer tasks ──────────────────────────────────────────────────────────────

def _writer_task(n, section_title, section_body, is_assembler=False):
    """Generate task description for writer-N."""
    task = (
        f"You are 'writer-{n}'. Write section {n} of a collaborative document.\n\n"
        f"1. Register with Pluto as 'writer-{n}'.\n"
        f"2. Acquire a write lock on resource 'doc:section-{n}'.\n"
        f"3. Create the file section{n}.txt in the work directory with this content:\n"
        f"   ## {section_title}\n"
        f"   Author: writer-{n}\n\n"
        f"   {section_body}\n\n"
        f"4. Release the 'doc:section-{n}' lock.\n"
        f"5. Acquire a write lock on resource 'doc:status-board'.\n"
        f"6. APPEND the line 'writer-{n}: section {n} COMPLETE' (followed by a newline) "
        f"to the file status.txt in the work directory (create it if it doesn't exist).\n"
        f"7. Release the 'doc:status-board' lock.\n"
        f"8. Send a message to 'reviewer-{n}' with payload "
        f'{{"type": "ready_for_review", "section": {n}}}.\n'
        f"9. Wait for a message from 'reviewer-{n}' (timeout 120s).\n"
    )
    if is_assembler:
        task += (
            "10. After reviewer-1 approves, also wait for messages from "
            "'reviewer-2', 'reviewer-3', and 'reviewer-4' (timeout 120s each).\n"
            "11. Read section1.txt, section2.txt, section3.txt, section4.txt "
            "from the work directory.\n"
            "12. Create final_document.txt in the work directory containing all "
            "four sections concatenated, plus the footer line "
            "'=== Final assembly by writer-1 ==='.\n"
            "13. Broadcast {\"type\": \"pipeline_complete\"} to all agents.\n"
        )
    task += "14. Disconnect from Pluto.\n"
    return task


WRITER_TASKS = [
    _writer_task(1, "Introduction", "This document covers the full system design.\nTopics: architecture, API, security, deployment.", is_assembler=True),
    _writer_task(2, "API Design", "REST endpoints for /api/v1/* resources.\nAuthentication via JWT tokens.\nRate limiting: 100 req/min."),
    _writer_task(3, "Security Model", "TLS 1.3 for all connections.\nOAuth 2.0 authorization.\nRole-based access control (RBAC)."),
    _writer_task(4, "Deployment Plan", "Kubernetes cluster with 3 nodes.\nCI/CD via GitHub Actions.\nBlue-green deployment strategy."),
]

# ── Reviewer tasks ────────────────────────────────────────────────────────────

def _reviewer_task(n):
    """Generate task description for reviewer-N."""
    return (
        f"You are 'reviewer-{n}'. Review section {n} of the document.\n\n"
        f"1. Register with Pluto as 'reviewer-{n}'.\n"
        f"2. Wait for a message from 'writer-{n}' (timeout 120s).\n"
        f"3. Acquire a write lock on resource 'doc:section-{n}'.\n"
        f"4. APPEND the line '\\n[Reviewed and approved by reviewer-{n}]\\n' "
        f"to section{n}.txt in the work directory.\n"
        f"5. Release the 'doc:section-{n}' lock.\n"
        f"6. Acquire a write lock on resource 'doc:status-board'.\n"
        f"7. APPEND the line 'reviewer-{n}: review {n} COMPLETE' (followed by a newline) "
        f"to status.txt in the work directory.\n"
        f"8. Release the 'doc:status-board' lock.\n"
        f"9. Send a message to 'writer-{n}' with payload "
        f'{{"type": "review_approved", "section": {n}}}.\n'
        f"10. Also send a message to 'writer-1' with payload "
        f'{{"type": "review_approved", "reviewer": "reviewer-{n}", "section": {n}}}.\n'
        f"11. Disconnect from Pluto.\n"
    )


REVIEWER_TASKS = [_reviewer_task(n) for n in range(1, 5)]


@unittest.skipUnless(COPILOT_AVAILABLE, "copilot CLI not installed")
class TestPipeline8Agents(unittest.TestCase):
    """8 real Copilot agents build a reviewed document via Pluto."""

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

    def test_pipeline_document_assembly(self):
        """4 writers + 4 reviewers coordinate via locks and messages."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = []
        # Reviewers start first (they wait for writer messages)
        for n in range(1, 5):
            agents.append({
                "agent_id": f"reviewer-{n}",
                "task": REVIEWER_TASKS[n - 1],
                "start_delay_s": 0,
            })
        # Writers start slightly later
        for n in range(1, 5):
            agents.append({
                "agent_id": f"writer-{n}",
                "task": WRITER_TASKS[n - 1],
                "start_delay_s": 5,
            })

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=300)

        # Print agent output
        for agent in results["agents"]:
            print(f"\n{'=' * 60}")
            print(f"  Agent: {agent['agent_id']}  rc={agent['returncode']}")
            print(f"{'=' * 60}")
            print(agent["stdout"][:2000])
            if agent["stderr"]:
                print(f"STDERR:\n{agent['stderr'][:500]}")

        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed (rc={agent['returncode']}): "
                f"{agent['stderr'][:500]}",
            )

        # Each section file exists and has correct content
        for n in range(1, 5):
            path = os.path.join(WORK_DIR, f"section{n}.txt")
            self.assertTrue(os.path.exists(path), f"section{n}.txt missing")
            with open(path) as f:
                content = f.read()
            self.assertIn(f"writer-{n}", content)
            self.assertIn(f"reviewer-{n}", content)

        # Final document
        final = os.path.join(WORK_DIR, "final_document.txt")
        self.assertTrue(os.path.exists(final), "final_document.txt missing")
        with open(final) as f:
            content = f.read()
        self.assertIn("writer-1", content)
        self.assertIn("Final assembly", content)

        # Status board
        status = os.path.join(WORK_DIR, "status.txt")
        self.assertTrue(os.path.exists(status), "status.txt missing")
        with open(status) as f:
            content = f.read()
        for n in range(1, 5):
            self.assertIn(f"writer-{n}", content)
            self.assertIn(f"reviewer-{n}", content)

    def test_stats_after_pipeline(self):
        """Run the pipeline and verify statistics are tracked."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = []
        for n in range(1, 5):
            agents.append({"agent_id": f"reviewer-{n}", "task": REVIEWER_TASKS[n - 1], "start_delay_s": 0})
        for n in range(1, 5):
            agents.append({"agent_id": f"writer-{n}", "task": WRITER_TASKS[n - 1], "start_delay_s": 5})

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=300)
        self.assertTrue(results["success"], f"Pipeline failed")

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

        print("\n=== Server Statistics ===")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    unittest.main()
