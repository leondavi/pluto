"""Pytest configuration for the Pluto test tree.

The suite mixes two kinds of tests:

* **Self-contained** — the MCP adapter tests (``test_mcp_*.py``) and
  ``test_agent_friend.py``. These run anywhere and are what CI executes.
* **Live-server integration** — script-style modules that talk to a real
  Pluto server over TCP/HTTP, and in some cases start one via
  ``PlutoServer.sh --daemon``. This includes everything under
  ``tests/demo_*/``, which are multi-agent demonstrations (they spawn
  agents, run for minutes, and write artifacts), not unit tests.

The second group used to make a plain ``pytest tests/`` unusable: the
modules open sockets at *import* time, so with no server running
``test_404_hints.py`` aborted the whole session with a collection error
(``URLError: Connection refused``) before anything else ran, and the
remaining modules — the ``demo_*`` scripts above all — blocked
indefinitely on connect timeouts and on the 120 s
``PlutoServer.sh --daemon`` subprocess.

They are now opt-in. Start a server and set ``PLUTO_LIVE_TESTS=1``:

    ./PlutoServer.sh --daemon
    PLUTO_LIVE_TESTS=1 pytest tests/

Without the flag they are skipped at collection, so ``pytest tests/``
runs the self-contained suite to completion.
"""

import os

import pytest

#: Modules that require a reachable Pluto server (and may start one).
LIVE_SERVER_MODULES = {
    "test_404_hints.py",
    "test_http_keepalive.py",
    "test_v021_http_sessions.py",
    "test_v023_features.py",
}

#: Directory prefix for the multi-agent demos.
DEMO_DIR_PREFIX = "demo_"

_OPT_IN_ENV = "PLUTO_LIVE_TESTS"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def live_tests_enabled() -> bool:
    return (os.environ.get(_OPT_IN_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_live_only(path) -> bool:
    if path.name in LIVE_SERVER_MODULES:
        return True
    # Anything under tests/demo_*/ — match on the path component directly
    # below tests/ so nested files are covered too.
    try:
        relative = path.relative_to(_TESTS_DIR)
    except ValueError:
        return False
    parts = relative.parts
    return bool(parts) and parts[0].startswith(DEMO_DIR_PREFIX)


def pytest_ignore_collect(collection_path, config):
    """Skip live-server modules and demos before import, so their
    module-level socket calls and agent spawns never run."""
    if _is_live_only(collection_path) and not live_tests_enabled():
        return True
    return None


def pytest_report_collectionfinish(config, items):
    if not live_tests_enabled():
        return (
            f"skipped live-server modules and {DEMO_DIR_PREFIX}* demos; "
            f"run a Pluto server and set {_OPT_IN_ENV}=1 to include them"
        )
    return None


@pytest.fixture(scope="session", autouse=True)
def _guard_tracked_config():
    """The live-server path can rewrite the tracked
    ``config/pluto_config.json`` (ports get reset to the launcher's
    defaults), leaving a dirty working tree after a test run. Snapshot it
    and put it back."""
    if not live_tests_enabled():
        yield
        return
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "pluto_config.json",
    )
    try:
        with open(path, "rb") as f:
            original = f.read()
    except OSError:
        yield
        return
    try:
        yield
    finally:
        try:
            with open(path, "rb") as f:
                changed = f.read() != original
            if changed:
                with open(path, "wb") as f:
                    f.write(original)
        except OSError:
            pass
