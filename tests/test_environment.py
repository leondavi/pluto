"""Guard against a silently hollow test run.

The MCP adapter suites (test_mcp_*.py) raise ``unittest.SkipTest`` at
import time when the ``mcp`` SDK is missing, so ``pytest tests/`` on an
interpreter without it reports success while ~100 adapter tests never
ran. This test turns that state into a visible failure.

Fix by running the suite with an interpreter that has the requirements:

    pip install -r requirements.txt pytest
    # or reuse the launcher's venv:
    /tmp/pluto/.venv/bin/python -m pytest tests/

Set PLUTO_ALLOW_MCP_SKIP=1 to knowingly run without the adapter suites.
"""

import importlib.util
import os
import unittest

_OPT_OUT_ENV = "PLUTO_ALLOW_MCP_SKIP"


class TestEnvironment(unittest.TestCase):
    def test_mcp_sdk_installed(self):
        if (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            self.skipTest(f"{_OPT_OUT_ENV} set; adapter suites may be skipped")
        self.assertIsNotNone(
            importlib.util.find_spec("mcp"),
            "The 'mcp' SDK is not importable, so every test_mcp_*.py module "
            "was skipped. Install requirements.txt (or run with "
            "/tmp/pluto/.venv/bin/python), or set "
            f"{_OPT_OUT_ENV}=1 to skip them knowingly.",
        )
