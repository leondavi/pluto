"""
Q&A Conversation — Real Copilot Agent Test
============================================

3 real Copilot CLI agents (alice, bob, charlie) exchange questions and
answers through the Pluto server, logging each exchange to a shared
transcript file protected by a Pluto lock.

  alice   → asks bob a question, answers charlie's question
  bob     → answers alice's question, asks charlie a question
  charlie → answers bob's question, asks alice a question

Each agent is a separate `copilot -p` process using PlutoClient.

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
WORK_DIR = "/tmp/pluto_copilot_qa_conversation"

COPILOT_AVAILABLE = shutil.which("copilot") is not None

# ── Task descriptions ────────────────────────────────────────────────────────

ALICE_TASK = """\
You are 'alice'. You will ask bob a question and answer charlie's question.

1. Register with Pluto as 'alice' and set up message handling.
2. Acquire a write lock on resource 'qa:transcript'.
3. APPEND the line '[Q1] alice asks bob: What is the capital of France?' followed by
   a newline to the file transcript.txt in the work directory (create it if it doesn't exist).
4. Release the lock.
5. Send a message to 'bob' with payload {"type": "question", "id": "Q1", "text": "What is the capital of France?"}.
6. Wait for a message from 'bob' (timeout 120s). This is bob's answer to Q1.
7. Acquire a write lock on 'qa:transcript'.
8. APPEND the line '[A1] bob answers alice: Paris' followed by a newline to transcript.txt.
9. Release the lock.
10. Wait for a message from 'charlie' (timeout 120s). This is charlie's question Q3.
11. Acquire a write lock on 'qa:transcript'.
12. APPEND the line '[A3] alice answers charlie: The speed of light is approximately 300,000 km/s' followed by a newline to transcript.txt.
13. Release the lock.
14. Send a message to 'charlie' with payload {"type": "answer", "id": "A3", "text": "approximately 300,000 km/s"}.
15. Disconnect from Pluto.
"""

BOB_TASK = """\
You are 'bob'. You will answer alice's question and ask charlie a question.

1. Register with Pluto as 'bob' and set up message handling.
2. Wait for a message from 'alice' (timeout 120s). This is alice's question Q1.
3. Acquire a write lock on resource 'qa:transcript'.
4. APPEND the line '[A1-note] bob received Q1 from alice' followed by a newline
   to the file transcript.txt in the work directory.
5. Release the lock.
6. Send a message to 'alice' with payload {"type": "answer", "id": "A1", "text": "Paris"}.
7. Acquire a write lock on 'qa:transcript'.
8. APPEND the line '[Q2] bob asks charlie: What is 2+2?' followed by a newline to transcript.txt.
9. Release the lock.
10. Send a message to 'charlie' with payload {"type": "question", "id": "Q2", "text": "What is 2+2?"}.
11. Wait for a message from 'charlie' (timeout 120s). This is charlie's answer to Q2.
12. Acquire a write lock on 'qa:transcript'.
13. APPEND the line '[A2] charlie answers bob: 4' followed by a newline to transcript.txt.
14. Release the lock.
15. Disconnect from Pluto.
"""

CHARLIE_TASK = """\
You are 'charlie'. You will answer bob's question and ask alice a question.

1. Register with Pluto as 'charlie' and set up message handling.
2. Wait for a message from 'bob' (timeout 120s). This is bob's question Q2.
3. Acquire a write lock on resource 'qa:transcript'.
4. APPEND the line '[A2-note] charlie received Q2 from bob' followed by a newline
   to the file transcript.txt in the work directory.
5. Release the lock.
6. Send a message to 'bob' with payload {"type": "answer", "id": "A2", "text": "4"}.
7. Acquire a write lock on 'qa:transcript'.
8. APPEND the line '[Q3] charlie asks alice: What is the speed of light?' followed by
   a newline to transcript.txt.
9. Release the lock.
10. Send a message to 'alice' with payload {"type": "question", "id": "Q3", "text": "What is the speed of light?"}.
11. Wait for a message from 'alice' (timeout 120s). This is alice's answer A3.
12. Acquire a write lock on 'qa:transcript'.
13. APPEND the line '[A3-note] charlie received A3 from alice' followed by a newline to transcript.txt.
14. Release the lock.
15. Disconnect from Pluto.
"""


@unittest.skipUnless(COPILOT_AVAILABLE, "copilot CLI not installed")
class TestQAConversation(unittest.TestCase):
    """3 real Copilot agents exchange Q&A through Pluto."""

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

    def test_questions_exchanged(self):
        """All questions and answers are exchanged and logged to transcript."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = [
            {"agent_id": "bob",     "task": BOB_TASK,     "start_delay_s": 0},
            {"agent_id": "charlie", "task": CHARLIE_TASK, "start_delay_s": 0},
            {"agent_id": "alice",   "task": ALICE_TASK,   "start_delay_s": 5},
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

        # Transcript must contain all Q&A entries
        transcript = os.path.join(WORK_DIR, "transcript.txt")
        self.assertTrue(os.path.exists(transcript), "transcript.txt not created")

        with open(transcript) as f:
            content = f.read()

        # Core conversation markers
        self.assertIn("[Q1]", content, f"Q1 missing from transcript:\n{content}")
        self.assertIn("[Q2]", content, f"Q2 missing from transcript:\n{content}")
        self.assertIn("[Q3]", content, f"Q3 missing from transcript:\n{content}")
        self.assertIn("alice", content)
        self.assertIn("bob", content)
        self.assertIn("charlie", content)

        print(f"\n{'=' * 60}")
        print("  FINAL TRANSCRIPT")
        print(f"{'=' * 60}")
        print(content)

    def test_stats_reflect_conversation(self):
        """After Q&A, stats show message and lock activity."""
        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)

        agents = [
            {"agent_id": "bob",     "task": BOB_TASK,     "start_delay_s": 0},
            {"agent_id": "charlie", "task": CHARLIE_TASK, "start_delay_s": 0},
            {"agent_id": "alice",   "task": ALICE_TASK,   "start_delay_s": 5},
        ]

        results = wrapper.run_copilot_agents(agents, WORK_DIR, timeout=240)
        self.assertTrue(results["success"], f"Q&A flow failed")

        with PlutoClient(
            host=PLUTO_HOST, port=PLUTO_PORT, agent_id="stats-verifier"
        ) as client:
            stats = client.stats()

        self.assertEqual(stats["status"], "ok")
        counters = stats["counters"]
        self.assertGreaterEqual(counters["agents_registered"], 3)
        self.assertGreater(counters["messages_sent"], 0)
        self.assertGreater(counters["locks_acquired"], 0)
        self.assertGreater(counters["locks_released"], 0)

        print("\n=== Server Statistics After Q&A ===")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    unittest.main()
