"""
Helper to start / stop the real Erlang Pluto server for Python integration tests.

Usage in test classes:

    from pluto_test_server import PlutoTestServer

    class MyTest(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.server = PlutoTestServer()
            cls.server.start()          # no-op if already running

        @classmethod
        def tearDownClass(cls):
            cls.server.stop()           # only stops if we started it
"""

import json
import os
import socket
import subprocess
import time

_PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SERVER_SCRIPT = os.path.join(_PROJECT, "PlutoServer.sh")
_CONFIG_PATH = os.path.join(_PROJECT, "config", "pluto_config.json")


def _read_config_ports():
    """Return (host, tcp_port, http_port) from pluto_config.json with defaults."""
    host, tcp_port, http_port = "127.0.0.1", 9200, 9201
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f).get("pluto_server", {})
        host = cfg.get("host_ip", host)
        tcp_port = int(cfg.get("host_tcp_port", tcp_port))
        http_port = int(cfg.get("host_http_port", http_port))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return host, tcp_port, http_port


PLUTO_HOST, PLUTO_PORT, PLUTO_HTTP_PORT = _read_config_ports()


def _tcp_reachable(host=PLUTO_HOST, port=PLUTO_PORT, timeout=2):
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class PlutoTestServer:
    """
    Manage the real Erlang Pluto server for integration tests.

    * If the server is already running on port 9000, it is reused and
      ``stop()`` becomes a no-op so external servers are not killed.
    * Otherwise ``start()`` invokes ``PlutoServer.sh --daemon`` and
      ``stop()`` invokes ``PlutoServer.sh --kill``.
    """

    host = PLUTO_HOST
    port = PLUTO_PORT
    http_port = PLUTO_HTTP_PORT

    def __init__(self):
        self._we_started = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, timeout=30):
        """Start the real Pluto server (or reuse an already-running one)."""
        if _tcp_reachable():
            self._we_started = False
            return

        env = os.environ.copy()
        env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")

        result = subprocess.run(
            [_SERVER_SCRIPT, "--daemon"],
            cwd=_PROJECT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"PlutoServer.sh --daemon failed (rc={result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        # Wait for TCP readiness
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _tcp_reachable(timeout=1):
                self._we_started = True
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"Pluto server did not become reachable within {timeout}s.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def stop(self):
        """Stop the server only if *we* started it."""
        if not self._we_started:
            return

        env = os.environ.copy()
        env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")

        subprocess.run(
            [_SERVER_SCRIPT, "--kill"],
            cwd=_PROJECT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self._we_started = False
