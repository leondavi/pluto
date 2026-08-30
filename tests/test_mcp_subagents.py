"""Tests for the Claude Code subagent packaging of Pluto roles.

The checked-in .claude/agents/pluto-*.md files are generated from
library/roles/*.md by agent_mcp_friend.subagents — this suite pins the
generated content and fails if the checked-in copies drift.
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_PROJECT, "src_py"))

try:
    from agent_mcp_friend.subagents import build_subagent_md
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )

_CHECKED_IN_ROLES = ("specialist", "reviewer", "qa")


class TestSubagentGeneration(unittest.TestCase):
    def test_checked_in_subagents_match_generator(self):
        for role in _CHECKED_IN_ROLES:
            path = os.path.join(_PROJECT, ".claude", "agents", f"pluto-{role}.md")
            with open(path, encoding="utf-8") as f:
                checked_in = f.read()
            self.assertEqual(
                checked_in, build_subagent_md(role),
                f".claude/agents/pluto-{role}.md drifted from "
                f"library/roles/{role}.md — regenerate with "
                f"python -m agent_mcp_friend.subagents",
            )

    def test_frontmatter_fields(self):
        for role in _CHECKED_IN_ROLES:
            md = build_subagent_md(role)
            head = md.split("---")[1]
            self.assertIn(f"name: pluto-{role}", head)
            self.assertIn("description: ", head)
            self.assertIn("mcp__pluto", head)
            # Checked-in variant inherits the session's server.
            self.assertNotIn("mcpServers", head)

    def test_shared_identity_conventions_present(self):
        md = build_subagent_md("reviewer")
        self.assertIn("NEVER call `pluto_recv` or `pluto_pop`", md)
        self.assertIn("drain=false", md)

    def test_protocol_reference_replaced_with_digest(self):
        md = build_subagent_md("specialist")
        self.assertIn("pluto://protocol", md)
        self.assertIn("Pluto Protocol — Digest", md)
        self.assertNotIn(
            "shared protocol at `library/protocol.md`", md,
        )

    def test_own_identity_variant_declares_mcp_server(self):
        md = build_subagent_md("qa", own_identity=True, python_bin="/x/python")
        self.assertIn("mcpServers:", md)
        self.assertIn("pluto-qa-sub", md)
        self.assertIn("/x/python", md)
        # Own-identity subagents have their own inbox — the shared-
        # identity conventions must NOT be included.
        self.assertNotIn("NEVER call `pluto_recv`", md)


if __name__ == "__main__":
    unittest.main()
