"""
Demo: File corruption WITHOUT vs WITH Pluto coordination.

Part 1 — Without lock coordination (TestCorruptionWithoutPluto):
    4 agents do concurrent read-modify-write on the same file WITHOUT
    acquiring locks.  The read-modify-write race causes lost updates:
    the final file has fewer lines than expected.

Part 2 — With lock coordination (TestCorruptionWithPluto):
    4 agents each append 5 lines to a shared file, acquiring Pluto locks
    before every write.  All 20 lines are present and intact.

Both parts require the real Erlang Pluto server.
Start with:  ./PlutoServer.sh --daemon
"""

import os
import shutil
import socket
import sys
import tempfile
import time
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

NUM_WRITERS = 4
LINES_PER_WRITER = 25          # enough iterations to reliably trigger races
TOTAL_EXPECTED = NUM_WRITERS * LINES_PER_WRITER


# ── Helpers ───────────────────────────────────────────────────────────────────

def _server_reachable():
    """Quick TCP check for the real Pluto server."""
    try:
        s = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1: WITHOUT lock coordination — concurrent writes corrupt the file
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(
    _server_reachable(),
    "Real Pluto server not running on port 9000 — start with ./PlutoServer.sh --daemon",
)
class TestCorruptionWithoutPluto(unittest.TestCase):
    """Demonstrate data loss when multiple agents share a file with no coordination."""

    def test_concurrent_read_modify_write_causes_lost_updates(self):
        """
        4 agents each read-modify-write 25 lines via FlowRunner with NO
        lock coordination.  Expected total: 100 lines.  Actual: fewer,
        because agents overwrite each other's changes.
        """
        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_uncoordinated_write.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # All agents should complete (they just don't coordinate)
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed: {agent['error']}",
            )

        target = os.path.join(
            tempfile.gettempdir(), "pluto_corruption_no_server", "data.txt",
        )
        with open(target) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]

        unique_lines = set(lines)
        print(f"\n{'='*60}")
        print("WITHOUT PLUTO — uncoordinated concurrent writes (agents)")
        print(f"{'='*60}")
        print(f"  Writers:            {NUM_WRITERS}")
        print(f"  Lines per writer:   {LINES_PER_WRITER}")
        print(f"  Expected total:     {TOTAL_EXPECTED}")
        print(f"  Actual lines:       {len(lines)}")
        print(f"  Unique lines:       {len(unique_lines)}")
        print(f"  Lines LOST:         {TOTAL_EXPECTED - len(unique_lines)}")

        # Count per-writer survivals
        for wid in range(1, NUM_WRITERS + 1):
            count = sum(1 for l in unique_lines if l.startswith(f"[writer-{wid}]"))
            print(f"  writer-{wid} lines:    {count} / {LINES_PER_WRITER}")

        print(f"{'='*60}")

        # The whole point: corruption means we lost data
        self.assertLess(
            len(unique_lines),
            TOTAL_EXPECTED,
            "Expected lost updates due to uncoordinated writes, "
            f"but all {TOTAL_EXPECTED} lines survived — try increasing "
            "LINES_PER_WRITER if this machine is too fast.",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2: WITH real Pluto server — coordinated writes preserve integrity
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(
    _server_reachable(),
    "Real Pluto server not running on port 9000 — start with ./PlutoServer.sh --daemon",
)
class TestCorruptionWithPluto(unittest.TestCase):
    """Demonstrate that Pluto lock coordination prevents all data loss."""

    def test_coordinated_writes_preserve_all_lines(self):
        """
        4 agents each append 5 lines via Pluto-locked writes.
        All 20 lines must be present and intact.
        """
        # Clean work dir from previous runs so counts are exact
        work_dir = os.path.join(tempfile.gettempdir(), "pluto_demo_corruption")
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)

        flows_dir = os.path.join(_HERE, "flows")
        sys_flow = os.path.join(flows_dir, "sys_coordinated_write.json")

        wrapper = AgentWrapper(host=PLUTO_HOST, port=PLUTO_PORT)
        results = wrapper.run_system_flow(sys_flow)

        # Print agent logs
        for agent in results["agents"]:
            for line in agent["log"]:
                print(line)

        # All agents completed
        for agent in results["agents"]:
            self.assertTrue(
                agent["success"],
                f"Agent {agent['agent_id']} failed: {agent['error']}",
            )

        # All flow assertions passed (file_contains + all_agents_completed)
        for a in results["assertions"]:
            self.assertTrue(a["passed"], f"Assertion failed: {a}")

        # Read actual file and verify line count
        work_dir = results["agents"][0]["log"][0]  # extract from log
        # Use the system flow's work_dir convention
        target = os.path.join(
            tempfile.gettempdir(), "pluto_demo_corruption", "data.txt",
        )
        with open(target) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]

        unique_lines = set(lines)
        coordinated_total = NUM_WRITERS * 5  # 5 lines per agent in flows

        print(f"\n{'='*60}")
        print("WITH PLUTO — coordinated writes via lock acquisition")
        print(f"{'='*60}")
        print(f"  Writers:            {NUM_WRITERS}")
        print(f"  Lines per writer:   5")
        print(f"  Expected total:     {coordinated_total}")
        print(f"  Actual lines:       {len(lines)}")
        print(f"  Unique lines:       {len(unique_lines)}")
        print(f"  Lines LOST:         {coordinated_total - len(unique_lines)}")

        for wid in range(1, NUM_WRITERS + 1):
            count = sum(1 for l in unique_lines if l.startswith(f"[writer-{wid}]"))
            print(f"  writer-{wid} lines:    {count} / 5")

        print(f"{'='*60}")

        # Zero data loss
        self.assertEqual(
            len(unique_lines),
            coordinated_total,
            f"Expected exactly {coordinated_total} unique lines with Pluto "
            f"coordination, got {len(unique_lines)}",
        )

        self.assertTrue(results["success"], f"System flow failed: {results}")


if __name__ == "__main__":
    unittest.main()
