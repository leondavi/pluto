#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pluto_client.py — Python client for the Pluto coordination server.

Pluto speaks newline-delimited JSON over TCP. This module wraps that protocol
so Python agents can coordinate without managing sockets or JSON encoding directly.

Basic usage:
    from pluto_client import PlutoClient

    client = PlutoClient(host="localhost", port=9000, agent_id="coder-1")
    client.connect()

    lock_ref = client.acquire("file:/repo/src/model.erl", ttl_ms=30000)
    # ... do work ...
    client.release(lock_ref)

    client.send("reviewer-2", {"type": "ready", "file": "model.erl"})
    client.disconnect()

Context manager:
    with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
        lock_ref = client.acquire("workspace:experiment-17")
        client.release(lock_ref)

Receiving async events:
    client.on_message(lambda e: print("msg:", e["payload"]))
    client.on_lock_granted(lambda e: print("lock granted:", e["lock_ref"]))
    client.connect()
"""

import datetime
import json
import os
import queue
import socket
import sys
import threading
from typing import Callable, Dict, List, Optional

from pluto_client_def import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_AGENT_ID,
    DEFAULT_GUIDE_OUTPUT_PATH,
    GUIDE_TEMPLATE_RELATIVE,
    OP_REGISTER,
    OP_ACQUIRE,
    OP_RELEASE,
    OP_RENEW,
    OP_SEND,
    OP_BROADCAST,
    OP_LIST_AGENTS,
    OP_PING,
    MODE_WRITE,
    STATUS_OK,
    STATUS_WAIT,
    EVENT_MESSAGE,
    EVENT_BROADCAST,
    EVENT_LOCK_GRANTED,
    CLI_DESCRIPTION,
    CLI_EPILOG,
    PLUTO_LOGO,
)


# ── Exceptions ────────────────────────────────────────────────────────────────

class PlutoError(Exception):
    """Raised when Pluto returns an error response or a request times out."""


# ── Client ────────────────────────────────────────────────────────────────────

class PlutoClient:
    """
    Synchronous Python client for the Pluto coordination server.

    Requests are sent one at a time and block until the response arrives.
    Async events pushed by Pluto (messages, lock grants, broadcasts) are
    delivered to registered handlers in a background reader thread.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        agent_id: str = DEFAULT_AGENT_ID,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.agent_id = agent_id
        self.timeout = timeout

        self.session_id: Optional[str] = None

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._response_queue: queue.Queue = queue.Queue()
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self):
        """Open a TCP connection to Pluto and register this agent."""
        self._sock = socket.create_connection((self.host, self.port))
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        resp = self._send_and_wait({"op": OP_REGISTER, "agent_id": self.agent_id})
        self.session_id = resp.get("session_id")

    def disconnect(self):
        """Close the connection gracefully."""
        self._running = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # ── Coordination operations ───────────────────────────────────────────────

    def acquire(self, resource: str, mode: str = MODE_WRITE, ttl_ms: int = 30000) -> str:
        """
        Acquire a lock on a named resource.

        Returns:
            lock_ref  — if the lock was granted immediately.
            wait_ref  — if the resource is busy and this agent is queued.
                        Listen for the on_lock_granted event to know when
                        the lock is actually granted.

        Raises:
            PlutoError if the server returns an error (e.g. "conflict").
        """
        resp = self._send_and_wait({
            "op": OP_ACQUIRE,
            "resource": resource,
            "mode": mode,
            "agent": self.agent_id,
            "ttl_ms": ttl_ms,
        })
        status = resp.get("status")
        if status == STATUS_OK:
            return resp["lock_ref"]
        if status == STATUS_WAIT:
            return resp["wait_ref"]
        raise PlutoError(resp.get("reason", "acquire failed"))

    def release(self, lock_ref: str):
        """Release a lock previously acquired by this agent."""
        self._send_and_wait({"op": OP_RELEASE, "lock_ref": lock_ref})

    def renew(self, lock_ref: str, ttl_ms: int = 30000):
        """Extend the TTL on an active lock lease."""
        self._send_and_wait({"op": OP_RENEW, "lock_ref": lock_ref, "ttl_ms": ttl_ms})

    def send(self, to: str, payload: dict):
        """Send a direct message to another agent by agent_id."""
        resp = self._send_and_wait({
            "op": OP_SEND,
            "from": self.agent_id,
            "to": to,
            "payload": payload,
        })
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "send failed"))

    def broadcast(self, payload: dict):
        """Broadcast a message to all currently connected agents."""
        resp = self._send_and_wait({
            "op": OP_BROADCAST,
            "from": self.agent_id,
            "payload": payload,
        })
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "broadcast failed"))

    def list_agents(self) -> List[str]:
        """Return the list of agent_ids currently connected to Pluto."""
        resp = self._send_and_wait({"op": OP_LIST_AGENTS})
        return resp.get("agents", [])

    # ── Event handlers ────────────────────────────────────────────────────────

    def on(self, event: str, handler: Callable):
        """
        Register a callback for a named Pluto event.

        The handler receives the full event dict, e.g.:
            {"event": "message", "from": "coder-1", "payload": {...}}

        Known event types:
            "message"       — direct message from another agent.
            "broadcast"     — broadcast event from another agent.
            "lock_granted"  — a queued lock was granted to this agent.
            "lock_expired"  — one of this agent's locks expired.
            "agent_joined"  — another agent connected.
            "agent_left"    — another agent disconnected.

        See pluto_client_def.py for the full list of EVENT_* constants.
        """
        self._event_handlers.setdefault(event, []).append(handler)

    def on_message(self, handler: Callable):
        """Shorthand for on("message", handler)."""
        self.on(EVENT_MESSAGE, handler)

    def on_broadcast(self, handler: Callable):
        """Shorthand for on("broadcast", handler)."""
        self.on(EVENT_BROADCAST, handler)

    def on_lock_granted(self, handler: Callable):
        """Shorthand for on("lock_granted", handler)."""
        self.on(EVENT_LOCK_GRANTED, handler)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _send_raw(self, obj: dict):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(line)

    def _send_and_wait(self, obj: dict) -> dict:
        self._send_raw(obj)
        try:
            return self._response_queue.get(timeout=self.timeout)
        except queue.Empty:
            raise PlutoError(f"timeout waiting for response to op={obj.get('op')}")

    def _read_loop(self):
        """
        Background thread: read lines from the socket and route them.

        Lines with an "event" key are dispatched to registered handlers.
        All other lines (responses) are put on the response queue for the
        blocked _send_and_wait call.
        """
        buf = b""
        try:
            while self._running:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch_line(line.decode("utf-8").strip())
        except OSError:
            pass  # socket was closed; normal on disconnect

    def _dispatch_line(self, line: str):
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return

        if "event" in msg:
            event_type = msg["event"]
            for handler in self._event_handlers.get(event_type, []):
                try:
                    handler(msg)
                except Exception:
                    pass  # don't crash the reader thread on bad handler code
        else:
            self._response_queue.put(msg)


# ── Agent guide generation ────────────────────────────────────────────────────

def generate_agent_guide(
    output_path: str = DEFAULT_GUIDE_OUTPUT_PATH,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """
    Render the agent guide template and write it to output_path.

    Substitutes {{host}}, {{port}}, and {{generated_at}} in the template.
    Creates intermediate directories as needed.

    Returns the rendered guide content (also printed to stdout so the
    calling agent can read it directly).
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.normpath(os.path.join(script_dir, GUIDE_TEMPLATE_RELATIVE))

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    content = content.replace("{{generated_at}}", now)
    content = content.replace("{{host}}", host)
    content = content.replace("{{port}}", str(port))

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return content


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="pluto_client",
        description=CLI_DESCRIPTION,
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, metavar="HOST",
                        help=f"Pluto server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT",
                        help=f"Pluto server port (default: {DEFAULT_PORT})")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID, metavar="ID",
                        dest="agent_id",
                        help=f"Agent identifier used for registration (default: {DEFAULT_AGENT_ID})")

    subparsers = parser.add_subparsers(dest="command", metavar="{ping,list,guide}")

    # ping
    subparsers.add_parser(
        "ping",
        help="Verify connectivity to a Pluto server.",
        description="Register with the server and confirm the connection is live.",
    )

    # list
    subparsers.add_parser(
        "list",
        help="List all agent IDs currently connected to the server.",
        description="Connect to the server and return the list of active agents.",
    )

    # guide
    guide_p = subparsers.add_parser(
        "guide",
        help="Generate the Pluto agent guide to a file (and print to stdout).",
        description=(
            "Render the agent guide template with the given host/port values,\n"
            "write the result to OUTPUT, and also print it to stdout."
        ),
    )
    guide_p.add_argument(
        "--output", default=DEFAULT_GUIDE_OUTPUT_PATH, metavar="PATH",
        help=f"Destination file for the rendered guide (default: {DEFAULT_GUIDE_OUTPUT_PATH})",
    )

    return parser


def _main():
    print(PLUTO_LOGO)

    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "guide":
        content = generate_agent_guide(
            output_path=args.output,
            host=args.host,
            port=args.port,
        )
        print(content)
        print(f"[pluto] Guide written to: {args.output}")
        return

    # ping and list both require a server connection
    try:
        with PlutoClient(host=args.host, port=args.port, agent_id=args.agent_id) as client:
            print(f"[pluto] Connected  host={args.host}  port={args.port}"
                  f"  session_id={client.session_id}")

            if args.command == "list":
                agents = client.list_agents()
                if agents:
                    print(f"[pluto] Connected agents ({len(agents)}):")
                    for agent in agents:
                        print(f"         · {agent}")
                else:
                    print("[pluto] No agents currently connected.")
            else:
                print("[pluto] Registration OK — Pluto is reachable.")

    except (OSError, PlutoError) as exc:
        print(f"[pluto] Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
