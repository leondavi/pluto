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
    OP_STATS,
    OP_ACK,
    OP_ACK_EVENTS,
    OP_TASK_ASSIGN,
    OP_TASK_UPDATE,
    OP_TASK_LIST,
    OP_FIND_AGENTS,
    OP_SUBSCRIBE,
    OP_UNSUBSCRIBE,
    OP_PUBLISH,
    OP_TRY_ACQUIRE,
    OP_AGENT_STATUS,
    OP_TASK_BATCH,
    OP_TASK_PROGRESS,
    MODE_WRITE,
    STATUS_OK,
    STATUS_WAIT,
    STATUS_UNAVAILABLE,
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
        attributes: Optional[Dict] = None,
    ):
        self.host = host
        self.port = port
        self.agent_id = agent_id
        self.timeout = timeout
        self.attributes = attributes or {}

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

        msg = {"op": OP_REGISTER, "agent_id": self.agent_id}
        if self.attributes:
            msg["attributes"] = self.attributes
        resp = self._send_and_wait(msg)
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

    def send(self, to: str, payload: dict, request_id: Optional[str] = None):
        """Send a direct message to another agent by agent_id."""
        msg = {
            "op": OP_SEND,
            "from": self.agent_id,
            "to": to,
            "payload": payload,
        }
        if request_id:
            msg["request_id"] = request_id
        resp = self._send_and_wait(msg)
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "send failed"))
        return resp.get("msg_id")

    def broadcast(self, payload: dict):
        """Broadcast a message to all currently connected agents."""
        resp = self._send_and_wait({
            "op": OP_BROADCAST,
            "from": self.agent_id,
            "payload": payload,
        })
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "broadcast failed"))

    def list_agents(self, detailed: bool = False):
        """Return the list of agent_ids (or full details if detailed=True)."""
        msg = {"op": OP_LIST_AGENTS}
        if detailed:
            msg["detailed"] = True
        resp = self._send_and_wait(msg)
        return resp.get("agents", [])

    def stats(self) -> dict:
        """Query server statistics: counters, per-agent stats, and live snapshot."""
        return self._send_and_wait({"op": OP_STATS})

    # ── v0.2.0 operations ────────────────────────────────────────────────────

    def try_acquire(self, resource: str, mode: str = MODE_WRITE, ttl_ms: int = 30000) -> Optional[str]:
        """Non-blocking lock probe. Returns lock_ref if granted, None if unavailable."""
        resp = self._send_and_wait({
            "op": OP_TRY_ACQUIRE,
            "resource": resource,
            "mode": mode,
            "agent": self.agent_id,
            "ttl_ms": ttl_ms,
        })
        if resp.get("status") == STATUS_OK:
            return resp["lock_ref"]
        if resp.get("status") == STATUS_UNAVAILABLE:
            return None
        raise PlutoError(resp.get("reason", "try_acquire failed"))

    def find_agents(self, filter: Optional[dict] = None) -> List[str]:
        """Find agents matching an attribute filter."""
        msg = {"op": OP_FIND_AGENTS, "filter": filter or {}}
        resp = self._send_and_wait(msg)
        return resp.get("agents", [])

    def subscribe(self, topic: str):
        """Subscribe to a named topic channel."""
        resp = self._send_and_wait({"op": OP_SUBSCRIBE, "topic": topic})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "subscribe failed"))

    def unsubscribe(self, topic: str):
        """Unsubscribe from a topic channel."""
        resp = self._send_and_wait({"op": OP_UNSUBSCRIBE, "topic": topic})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "unsubscribe failed"))

    def publish(self, topic: str, payload: dict):
        """Publish a message to a topic channel."""
        resp = self._send_and_wait({
            "op": OP_PUBLISH,
            "topic": topic,
            "payload": payload,
        })
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "publish failed"))

    def ack(self, msg_id: str):
        """Acknowledge receipt of a message."""
        resp = self._send_and_wait({"op": OP_ACK, "msg_id": msg_id})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "ack failed"))

    def ack_events(self, last_seq: int):
        """Report the highest event sequence number processed."""
        resp = self._send_and_wait({"op": OP_ACK_EVENTS, "last_seq": last_seq})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "ack_events failed"))

    def task_assign(self, assignee: str, description: str, payload: Optional[dict] = None) -> str:
        """Assign a task to an agent. Returns task_id."""
        msg = {"op": OP_TASK_ASSIGN, "assignee": assignee, "description": description}
        if payload:
            msg["payload"] = payload
        resp = self._send_and_wait(msg)
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "task_assign failed"))
        return resp["task_id"]

    def task_update(self, task_id: str, status: str, result: Optional[dict] = None):
        """Update a task's status (pending, in_progress, completed, failed)."""
        msg = {"op": OP_TASK_UPDATE, "task_id": task_id, "status": status}
        if result:
            msg["result"] = result
        resp = self._send_and_wait(msg)
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "task_update failed"))

    def task_list(self, assignee: Optional[str] = None, status: Optional[str] = None) -> List[dict]:
        """List tasks, optionally filtered by assignee and/or status."""
        msg: dict = {"op": OP_TASK_LIST}
        if assignee:
            msg["assignee"] = assignee
        if status:
            msg["status"] = status
        resp = self._send_and_wait(msg)
        return resp.get("tasks", [])

    def task_batch(self, tasks: List[dict]) -> List[str]:
        """Batch-assign tasks. Each item needs 'assignee' and 'description'. Returns task_ids."""
        resp = self._send_and_wait({"op": OP_TASK_BATCH, "tasks": tasks})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "task_batch failed"))
        return resp.get("task_ids", [])

    def task_progress(self) -> dict:
        """Get global task progress summary."""
        return self._send_and_wait({"op": OP_TASK_PROGRESS})

    def agent_status(self, agent_id: str) -> dict:
        """Query a specific agent's status, attributes, and last-seen time."""
        return self._send_and_wait({"op": OP_AGENT_STATUS, "agent_id": agent_id})

    def set_status(self, custom_status: str):
        """Set this agent's custom status string."""
        resp = self._send_and_wait({"op": OP_AGENT_STATUS, "custom_status": custom_status})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "set_status failed"))

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

    subparsers = parser.add_subparsers(dest="command", metavar="{ping,list,stats,guide}")

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

    # stats
    subparsers.add_parser(
        "stats",
        help="Query server statistics (locks, messages, deadlocks, per-agent).",
        description="Connect to the server and retrieve runtime statistics.",
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


def _print_stats(data):
    """Pretty-print server statistics."""
    counters = data.get("counters", {})
    live = data.get("live", {})
    agent_stats = data.get("agent_stats", {})
    uptime_ms = data.get("uptime_ms", 0)

    uptime_s = uptime_ms / 1000 if uptime_ms else 0
    mins, secs = divmod(int(uptime_s), 60)
    hours, mins = divmod(mins, 60)

    print(f"\n  ╔══════════════════════════════════════════╗")
    print(f"  ║         PLUTO SERVER STATISTICS          ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  Uptime: {hours:02d}h {mins:02d}m {secs:02d}s" + " " * (26 - len(f"{hours:02d}h {mins:02d}m {secs:02d}s")) + "║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  LIVE SNAPSHOT                           ║")
    print(f"  ║    Active locks      : {str(live.get('active_locks', 0)):>16s} ║")
    print(f"  ║    Connected agents  : {str(live.get('connected_agents', 0)):>16s} ║")
    print(f"  ║    Total agents      : {str(live.get('total_agents', 0)):>16s} ║")
    print(f"  ║    Pending waiters   : {str(live.get('pending_waiters', 0)):>16s} ║")
    print(f"  ║    Wait graph edges  : {str(live.get('wait_graph_edges', 0)):>16s} ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  COUNTERS                                ║")
    for key in sorted(counters.keys()):
        val = counters[key]
        label = key.replace("_", " ").title()
        print(f"  ║    {label:<22s}: {str(val):>10s} ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  PER-AGENT STATS                         ║")
    if agent_stats:
        for aid in sorted(agent_stats.keys()):
            stats = agent_stats[aid]
            print(f"  ║  [{aid}]" + " " * max(0, 36 - len(aid)) + "║")
            for k in sorted(stats.keys()):
                label = k.replace("_", " ").title()
                print(f"  ║      {label:<20s}: {str(stats[k]):>8s} ║")
    else:
        print(f"  ║    (none)                                ║")
    print(f"  ╚══════════════════════════════════════════╝\n")


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
            elif args.command == "stats":
                data = client.stats()
                _print_stats(data)
            else:
                print("[pluto] Registration OK — Pluto is reachable.")

    except (OSError, PlutoError) as exc:
        print(f"[pluto] Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
