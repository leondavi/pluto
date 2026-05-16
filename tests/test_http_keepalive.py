#!/usr/bin/env python3
"""
test_http_keepalive.py — Regression test for the v0.3.0 HTTP/1.1
keep-alive end-to-end contract.

Pre-v0.3.0, every PlutoHttpClient call opened a fresh TCP socket and
the server hard-coded `Connection: close` on every response, leaking
hundreds of TIME_WAIT entries per minute under load and exhausting
ephemeral ports after ~18 min of normal traffic.

These tests assert the contract that prevents that regression:

  - The Python client's connection pool reuses one socket for many
    requests (`test_socket_reuse_under_load`).
  - A long-poll in one thread does not block short calls served from
    the same pool in another (`test_concurrent_peek_and_long_poll`).
  - If the server closes a pooled connection between calls, the
    client transparently retries on a fresh one
    (`test_stale_connection_retry`).

Requires the Pluto server running on the host/port configured in
config/pluto_config.json (override with PLUTO_HOST / PLUTO_HTTP_PORT).
"""

import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_py"))

from pluto_client import PlutoHttpClient  # noqa: E402


def _config_port(key: str, default: int) -> int:
    cfg = os.path.join(os.path.dirname(__file__), "..",
                       "config", "pluto_config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return int(json.load(f).get("pluto_server", {}).get(key, default))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return default


HOST = os.environ.get("PLUTO_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get(
    "PLUTO_HTTP_PORT", _config_port("host_http_port", 9202)))


def _server_up() -> bool:
    try:
        with socket.create_connection((HOST, HTTP_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _make_client(agent_id: str) -> PlutoHttpClient:
    c = PlutoHttpClient(
        host=HOST, http_port=HTTP_PORT, agent_id=agent_id, mode="http",
    )
    resp = c.register()
    assert resp.get("status") == "ok", resp
    return c


def test_socket_reuse_under_load() -> None:
    """200 sequential calls must reuse pool sockets, not open 200 of them."""
    c = _make_client("ka-test-reuse")
    try:
        # Drive 200 short GETs through the pool. The pool is sized to 4
        # — under sequential load, exactly one slot should ever be used.
        for _ in range(200):
            c._get("/health")
        free_size = c._pool._free.qsize()
        # At most pool capacity, and at least one (the reused slot).
        assert 1 <= free_size <= 4, (
            f"unexpected pool size after sequential load: {free_size}"
        )
    finally:
        c.unregister()


def test_concurrent_peek_and_long_poll() -> None:
    """A 5s long-poll must not block short calls served from the same pool."""
    c = _make_client("ka-test-concurrency")
    try:
        long_poll_done = threading.Event()

        def _long_poller() -> None:
            try:
                c.long_poll(timeout=5)  # blocks ~5s, no messages expected
            finally:
                long_poll_done.set()

        t = threading.Thread(target=_long_poller, daemon=True)
        t.start()
        # Give the long-poll a moment to occupy its pool slot.
        time.sleep(0.2)

        deadline = time.monotonic() + 4.0
        short_call_count = 0
        slowest_ms = 0.0
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            c._get("/health")
            slowest_ms = max(slowest_ms, (time.monotonic() - t0) * 1000)
            short_call_count += 1
            time.sleep(0.05)

        long_poll_done.wait(timeout=8.0)
        t.join(timeout=2.0)

        assert short_call_count >= 30, (
            f"only {short_call_count} short calls in 4s — long-poll was "
            "starving the pool"
        )
        assert slowest_ms < 1000, (
            f"slowest concurrent /health took {slowest_ms:.0f}ms — pool "
            "appears serialized behind the long-poll"
        )
    finally:
        c.unregister()


def test_stale_connection_retry() -> None:
    """If the pool's idle sockets are closed externally, the next call
    must transparently retry and succeed on a fresh connection."""
    c = _make_client("ka-test-stale")
    try:
        # Warm the pool — leaves at least one connection in _free.
        c._get("/health")
        assert c._pool._free.qsize() >= 1

        # Forcibly close every pooled connection without removing them
        # from the queue's accounting. The next _request must hit the
        # RemoteDisconnected / ConnectionResetError branch and retry.
        free = list(c._pool._free.queue)
        for conn in free:
            try:
                conn.close()
            except Exception:
                pass

        # Should NOT raise: retry-once branch creates a fresh conn.
        resp = c._get("/health")
        assert resp.get("status") == "ok", resp
    finally:
        c.unregister()


def main() -> int:
    if not _server_up():
        print(
            f"SKIP — Pluto server not reachable at {HOST}:{HTTP_PORT}",
            file=sys.stderr,
        )
        return 77  # autotools-style "skipped"

    failures = 0
    for name in (
        "test_socket_reuse_under_load",
        "test_concurrent_peek_and_long_poll",
        "test_stale_connection_retry",
    ):
        fn = globals()[name]
        try:
            print(f"... {name}")
            fn()
            print(f"OK  {name}")
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            failures += 1
        except Exception as exc:
            print(f"ERR  {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
