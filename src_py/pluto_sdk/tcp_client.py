"""Synchronous TCP client for the Pluto coordination server.

Pluto speaks newline-delimited JSON over TCP. :class:`PlutoClient` keeps
one persistent socket: requests are sent one at a time and block until
their response arrives, while events the server pushes (messages, lock
grants, broadcasts) are routed to registered handlers by a background
reader thread.

Example::

    with PlutoClient(host="localhost", port=9000, agent_id="coder-1") as client:
        lock_ref = client.acquire("workspace:experiment-17")
        client.send("reviewer-2", {"type": "ready"})
        client.release(lock_ref)
"""

import json
import queue
import socket
import threading
from typing import Callable, Dict, List, Optional

from pluto_client_def import (
    DEFAULT_AGENT_ID,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    EVENT_BROADCAST,
    EVENT_LOCK_GRANTED,
    EVENT_MESSAGE,
    MODE_WRITE,
    OP_ACK,
    OP_ACK_EVENTS,
    OP_ACQUIRE,
    OP_AGENT_STATUS,
    OP_BROADCAST,
    OP_FIND_AGENTS,
    OP_LIST_AGENTS,
    OP_PUBLISH,
    OP_REGISTER,
    OP_RELEASE,
    OP_RENEW,
    OP_RESOURCE_INFO,
    OP_RESTORE_FROM_SNAPSHOT,
    OP_SEND,
    OP_SNAPSHOT_SELF,
    OP_STATS,
    OP_SUBSCRIBE,
    OP_TASK_ASSIGN,
    OP_TASK_BATCH,
    OP_TASK_LIST,
    OP_TASK_PROGRESS,
    OP_TASK_UPDATE,
    OP_TRY_ACQUIRE,
    OP_UNSUBSCRIBE,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_WAIT,
)
from pluto_sdk.errors import PlutoError
from pluto_sdk.snapshot_files import write_snapshot_files


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
        # Server may assign a different agent_id if the requested one was taken
        if resp.get("agent_id"):
            self.agent_id = resp["agent_id"]

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

    # ── Resource introspection (v0.2.42) ─────────────────────────────────

    def resource_info(self, resource: str) -> dict:
        """
        Return full lock information for *resource*:
          - ``current_holders``: list of agents currently holding the resource
          - ``last_holder``: the most recent previous holder (or ``None``)
          - ``queue_length``: number of agents waiting
          - ``queue``: ordered list of waiting agents (FIFO)
        """
        resp = self._send_and_wait({"op": OP_RESOURCE_INFO, "resource": resource})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "resource_info failed"))
        return resp

    def last_holder(self, resource: str) -> Optional[dict]:
        """Return the most recent previous holder of *resource* (or None)."""
        return self.resource_info(resource).get("last_holder")

    def queue_length(self, resource: str) -> int:
        """Return the number of agents currently waiting for *resource*."""
        return int(self.resource_info(resource).get("queue_length", 0))

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

    # ── Snapshot / restore (v0.2.9) ────────────────────────────────────────

    def snapshot_self(self) -> dict:
        """Capture this agent's restorable state.

        Returns ``{"plut": {...}, "prompt": "..."}`` where ``plut`` is the
        coordination-state JSON to persist (.plut file) and ``prompt`` is
        a markdown recovery prompt (.md file). The agent is responsible
        for writing both to disk.
        """
        resp = self._send_and_wait({"op": OP_SNAPSHOT_SELF})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "snapshot_self failed"))
        return {"plut": resp.get("plut", {}), "prompt": resp.get("prompt", "")}

    def restore_from_snapshot(self, plut: dict) -> dict:
        """Apply a previously saved ``.plut`` snapshot to this session.

        The agent must already be registered (call :py:meth:`connect` first).
        Returns the server response with ``reclaimed_locks`` and ``lost_locks``.
        After this call the agent's status is ``recovered_from_file``.
        """
        if not isinstance(plut, dict):
            raise TypeError("plut must be a dict (parsed .plut JSON)")
        resp = self._send_and_wait({"op": OP_RESTORE_FROM_SNAPSHOT, "plut": plut})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "restore_from_snapshot failed"))
        return resp

    def save_snapshot_files(self, output_dir: str) -> tuple:
        """Convenience: take a snapshot and write both files to disk.

        Writes ``<output_dir>/<agent_id>.plut`` (JSON) and
        ``<output_dir>/<agent_id>-recovery.md`` (markdown prompt).
        Returns ``(plut_path, md_path)``.
        """
        return write_snapshot_files(self.agent_id, self.snapshot_self(), output_dir)

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
