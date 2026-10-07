"""Token-authenticated HTTP client for the Pluto coordination server.

:class:`PlutoHttpClient` talks to Pluto's HTTP listener instead of
holding a TCP socket. Registration returns a session token that every
later call presents; messages addressed to the agent are queued
server-side and fetched with :meth:`~PlutoHttpClient.poll`,
:meth:`~PlutoHttpClient.long_poll`, or the at-least-once
:meth:`~PlutoHttpClient.peek` / :meth:`~PlutoHttpClient.ack` pair. This
is the transport used by the MCP adapter and the AgentFriend wrapper.
"""

import http.client
import json
import os
import urllib.parse
from typing import Dict, List, Optional

from pluto_client_def import (
    DEFAULT_AGENT_ID,
    DEFAULT_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_TIMEOUT,
    STATUS_OK,
)
from pluto_sdk.errors import PlutoError
from pluto_sdk.http_pool import HTTPConnectionPool
from pluto_sdk.snapshot_files import write_snapshot_files


class PlutoHttpClient:
    """
    HTTP-based client for the Pluto coordination server.

    Unlike PlutoClient (TCP), this client uses stateless HTTP requests and
    does not maintain a persistent socket. Ideal for CLI agents (like Claude
    Code) that execute one-shot commands.

    Supports two modes:
      - "http": Standard HTTP session with token-based auth
      - "stateless": Declares the agent as stateless with a configurable TTL

    Usage:
        client = PlutoHttpClient(host="localhost", http_port=9001, agent_id="claude-1")
        client.register()
        # ... do work, poll for messages ...
        client.heartbeat()  # keep alive
        messages = client.poll()
        client.unregister()
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        http_port: int = DEFAULT_HTTP_PORT,
        agent_id: str = DEFAULT_AGENT_ID,
        timeout: float = DEFAULT_TIMEOUT,
        attributes: Optional[Dict] = None,
        mode: str = "http",
        ttl_ms: int = 300000,
    ):
        self.host = host
        self.http_port = http_port
        self.agent_id = agent_id
        self.timeout = timeout
        self.attributes = attributes or {}
        self.mode = mode
        self.ttl_ms = ttl_ms
        self.base_url = f"http://{host}:{http_port}"
        self.token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.server_epoch: Optional[str] = None
        # HTTP/1.1 keep-alive connection pool. Sized so one slot can hold
        # the (at most one) outstanding long-poll while short calls reuse
        # the other slots, with no per-call socket churn (the
        # urllib.urlopen pattern this replaces was leaking ~hundreds of
        # TIME_WAIT entries per minute under load).
        self._pool = HTTPConnectionPool(
            host=host, port=http_port,
            size=4, default_timeout=self.timeout,
        )

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 *, timeout: Optional[float] = None) -> dict:
        """Send one HTTP request over a pooled keep-alive connection.

        ``timeout`` overrides the pool's default for this call only
        (used by ``long_poll`` to wait beyond the short-call default).
        Stale-connection close from the server is retried once
        transparently — the pool always discards the bad socket.
        """
        headers: Dict[str, str] = {
            "Connection": "keep-alive",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            data: Optional[bytes] = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                with self._pool.acquire(read_timeout=timeout) as conn:
                    conn.request(method, path, body=data, headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()  # must drain to make the socket reusable
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except (http.client.RemoteDisconnected,
                    ConnectionResetError,
                    BrokenPipeError) as exc:
                # Server closed the keep-alive socket between requests.
                # The pool's context manager has already discarded the
                # bad conn; loop and try once more on a fresh one.
                last_exc = exc
                if attempt == 2:
                    raise
                continue
        # Unreachable — the loop either returns or raises.
        raise last_exc if last_exc is not None else RuntimeError("unreachable")

    def _require_token(self) -> str:
        """Return the session token, or raise if :meth:`register` has not
        succeeded (or :meth:`unregister` already ran)."""
        if not self.token:
            raise PlutoError("not registered (no token)")
        return self.token

    def _post(self, path: str, body: dict) -> dict:
        """Send a POST request and return the parsed JSON response."""
        return self._request("POST", path, body=body)

    def _get(self, path: str) -> dict:
        """Send a GET request and return the parsed JSON response."""
        return self._request("GET", path)

    def register(self) -> dict:
        """Register this agent via HTTP. Returns server response with token."""
        body = {
            "agent_id": self.agent_id,
            "mode": self.mode,
            "ttl_ms": self.ttl_ms,
        }
        if self.attributes:
            body["attributes"] = self.attributes
        resp = self._post("/agents/register", body)
        if resp.get("status") == "ok":
            self.token = resp.get("token")
            self.session_id = resp.get("session_id")
            self.server_epoch = resp.get("server_epoch")
            # Server may have assigned a different name
            if resp.get("agent_id"):
                self.agent_id = resp["agent_id"]
        return resp

    def fetch_server_epoch(self) -> Optional[str]:
        """GET /health and return the server's current epoch (None on error).

        Mismatch with self.server_epoch means the server was restarted /
        cleaned — held tokens are dead and the client must re-register.
        """
        try:
            resp = self._get("/health")
            return resp.get("server_epoch")
        except Exception:
            return None

    def heartbeat(self) -> dict:
        """Send a heartbeat to keep the HTTP session alive."""
        self._require_token()
        return self._post("/agents/heartbeat", {"token": self.token})

    def poll(self) -> List[dict]:
        """Poll for queued messages. Also acts as a heartbeat."""
        self._require_token()
        resp = self._get(f"/agents/poll?token={self.token}")
        return resp.get("messages", [])

    def send(self, to: str, payload: dict, request_id: Optional[str] = None) -> dict:
        """Send a direct message to another agent."""
        self._require_token()
        body = {"token": self.token, "to": to, "payload": payload}
        if request_id:
            body["request_id"] = request_id
        return self._post("/agents/send", body)

    def broadcast(self, payload: dict) -> dict:
        """Broadcast a message to all agents."""
        self._require_token()
        return self._post("/agents/broadcast", {"token": self.token, "payload": payload})

    def subscribe(self, topic: str) -> dict:
        """Subscribe to a topic channel."""
        self._require_token()
        return self._post("/agents/subscribe", {"token": self.token, "topic": topic})

    def unregister(self) -> dict:
        """Unregister and remove the HTTP session."""
        self._require_token()
        resp = self._post("/agents/unregister", {"token": self.token})
        self.token = None
        self.session_id = None
        # Drop any pooled keep-alive sockets so we don't strand them
        # after the session is gone.
        try:
            self._pool.close_all()
        except Exception:
            pass
        return resp

    def list_agents(self) -> List[str]:
        """List IDs of currently connected agents via HTTP."""
        resp = self._get("/agents")
        agents = resp.get("agents", [])
        # Defensive: basic /agents always returns strings; guard against stale servers.
        return [a if isinstance(a, str) else a.get("agent_id", "?") for a in agents]

    def list_agents_detailed(self) -> List[dict]:
        """List all agents (including offline) as full detail dicts via HTTP.

        Each dict contains at least: agent_id, status, last_seen,
        custom_status, attributes, subscriptions.
        Use this when you need to call .get('agent_id') on the results —
        the basic list_agents() returns plain strings.
        """
        resp = self._get("/agents?detailed=true")
        return resp.get("agents", [])

    # ── Lock resource introspection ─────────────────────────────────────

    def list_locks(self) -> List[dict]:
        """List all currently active locks on the server."""
        resp = self._get("/locks")
        return resp.get("locks", [])

    # ── Lock operations (HTTP) ───────────────────────────────────────────

    def acquire(self, resource: str, mode: str = "write",
                ttl_ms: int = 30000,
                max_wait_ms: Optional[int] = None) -> dict:
        """
        Acquire a lock over HTTP.  Returns the raw server response:
          - ``status=ok`` with ``lock_ref`` + ``fencing_token`` if granted
          - ``status=wait`` with ``wait_ref`` if queued (server grants
            later — poll ``/agents/poll`` for a ``lock_granted`` event)
          - ``status=error`` with ``reason`` on failure
        """
        body = {"agent_id": self.agent_id, "resource": resource,
                "mode": mode, "ttl_ms": ttl_ms}
        if max_wait_ms is not None:
            body["max_wait_ms"] = max_wait_ms
        return self._post("/locks/acquire", body)

    def try_acquire(self, resource: str, mode: str = "write",
                    ttl_ms: int = 30000) -> Optional[str]:
        """Non-blocking lock probe.  Returns lock_ref if granted, else None."""
        body = {"agent_id": self.agent_id, "resource": resource,
                "mode": mode, "ttl_ms": ttl_ms}
        resp = self._post("/locks/try_acquire", body)
        if resp.get("status") == "ok":
            return resp.get("lock_ref")
        return None

    def release(self, lock_ref: str) -> dict:
        """Release a previously-acquired lock."""
        return self._post("/locks/release",
                          {"lock_ref": lock_ref, "agent_id": self.agent_id})

    def renew(self, lock_ref: str, ttl_ms: int = 30000) -> dict:
        """Extend the TTL on an active lock lease."""
        return self._post("/locks/renew",
                          {"lock_ref": lock_ref, "ttl_ms": ttl_ms})

    def resource_info(self, resource: str) -> dict:
        """
        Return full lock information for *resource*:
          - ``current_holders``: list of agents currently holding the resource
          - ``last_holder``: the most recent previous holder (or ``None``)
          - ``queue_length``: number of agents waiting for the resource
          - ``queue``: ordered list of waiting agents (FIFO, head = next)

        Use this before acquiring a contested resource to decide whether to
        wait, try a different resource, or message the current holder.
        """
        qp = urllib.parse.urlencode({"resource": resource})
        return self._get(f"/locks/resource?{qp}")

    def last_holder(self, resource: str) -> Optional[dict]:
        """
        Return the most recent previous holder of *resource* as a dict with
        keys ``agent_id``, ``lock_ref``, ``released_at``, ``reason``
        (``released`` or ``expired``), or ``None`` if the server has no
        record of this resource ever being locked.
        """
        qp = urllib.parse.urlencode({"resource": resource})
        resp = self._get(f"/locks/last_holder?{qp}")
        return resp.get("last_holder")

    def queue_length(self, resource: str) -> int:
        """Return the number of agents currently waiting for *resource*."""
        qp = urllib.parse.urlencode({"resource": resource})
        resp = self._get(f"/locks/queue?{qp}")
        return int(resp.get("queue_length", 0))

    def resource_queue(self, resource: str) -> List[dict]:
        """Return the FIFO wait-queue for *resource* (head = next to be granted)."""
        qp = urllib.parse.urlencode({"resource": resource})
        resp = self._get(f"/locks/queue?{qp}")
        return resp.get("queue", [])

    def agent_status(self, agent_id: str) -> dict:
        """Query a specific agent's status (includes TTL info for HTTP agents)."""
        return self._get(f"/agents/{agent_id}")

    def long_poll(self, timeout: int = 30, ack: bool = True, auto_busy: bool = False) -> List[dict]:
        """Long-poll for messages. Blocks up to `timeout` seconds until messages arrive.

        Args:
            timeout: Max seconds to wait (capped at 60 server-side).
            ack: If True, server sends delivery_ack receipts to message senders.
            auto_busy: If True, auto-sets agent status to "processing" on receipt.

        Returns:
            List of messages (may be empty if timed out).
        """
        self._require_token()
        params = f"token={self.token}&timeout={timeout}"
        if ack:
            params += "&ack=true"
        if auto_busy:
            params += "&auto_busy=true"
        # Long-poll needs a per-call read timeout longer than the
        # server's wait. Pass it explicitly so the pool's other slots
        # keep their short default for concurrent peek/ack callers.
        resp = self._request(
            "GET", f"/agents/poll?{params}",
            timeout=float(timeout) + 10.0,
        )
        return resp.get("messages", [])

    # ── At-least-once delivery (v0.2.43) ─────────────────────────────────

    def peek(self, since_token: int = 0) -> List[dict]:
        """Non-destructive inbox read.

        Returns messages with a ``seq_token`` field attached.  Messages are
        *not* removed from the server inbox — callers must call :meth:`ack`
        with the highest seq_token they have durably handled.  A peek after
        a crash or failed delivery will return the same messages again,
        giving at-least-once semantics.
        """
        self._require_token()
        resp = self._get(
            f"/agents/peek?token={self.token}&since_token={int(since_token)}"
        )
        return resp.get("messages", [])

    def ack(self, up_to_seq: int) -> int:
        """Acknowledge messages up to and including *up_to_seq*.

        Deletes all queued messages whose seq_token is ``<= up_to_seq``.
        Returns the number of messages drained.  Idempotent.
        """
        self._require_token()
        resp = self._post(
            "/agents/ack",
            {"token": self.token, "up_to_seq": int(up_to_seq)},
        )
        return int(resp.get("drained", 0))

    def update_ttl(self, ttl_ms: int) -> dict:
        """Dynamically update the session TTL."""
        self._require_token()
        resp = self._post("/agents/update_ttl", {"token": self.token, "ttl_ms": ttl_ms})
        if resp.get("status") == "ok":
            self.ttl_ms = ttl_ms
        return resp

    def set_status(self, custom_status: str) -> dict:
        """Set custom agent status (e.g. 'busy', 'idle', 'processing')."""
        self._require_token()
        return self._post("/agents/set_status", {
            "token": self.token,
            "custom_status": custom_status,
        })

    # ── Snapshot / restore (v0.2.9) ────────────────────────────────────────

    def snapshot_self(self) -> dict:
        """Capture this HTTP agent's restorable state (.plut + recovery prompt)."""
        self._require_token()
        resp = self._post("/agents/snapshot_self", {"token": self.token})
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "snapshot_self failed"))
        return {"plut": resp.get("plut", {}), "prompt": resp.get("prompt", "")}

    def restore_from_snapshot(self, plut: dict) -> dict:
        """Apply a previously saved ``.plut`` snapshot to this HTTP session.

        Caller must already be registered (have a valid token).
        """
        self._require_token()
        if not isinstance(plut, dict):
            raise TypeError("plut must be a dict (parsed .plut JSON)")
        resp = self._post("/agents/restore_from_snapshot", {
            "token": self.token,
            "plut":  plut,
        })
        if resp.get("status") != STATUS_OK:
            raise PlutoError(resp.get("reason", "restore_from_snapshot failed"))
        return resp

    def save_snapshot_files(self, output_dir: str) -> tuple:
        """Take a snapshot and write ``<agent_id>.plut`` + ``<agent_id>-recovery.md``."""
        return write_snapshot_files(self.agent_id, self.snapshot_self(), output_dir)

    def task_assign(self, assignee: str, description: str = "",
                    payload: Optional[Dict] = None) -> dict:
        """Assign a task to another agent via HTTP."""
        self._require_token()
        body = {
            "token": self.token,
            "assignee": assignee,
            "description": description,
            "payload": payload or {},
        }
        return self._post("/agents/task_assign", body)

    def task_update(self, task_id: str, status: str,
                    result: Optional[Dict] = None) -> dict:
        """Update task status via HTTP."""
        self._require_token()
        body = {
            "token": self.token,
            "task_id": task_id,
            "status": status,
            "result": result or {},
        }
        return self._post("/agents/task_update", body)

    def task_list(self, assignee: Optional[str] = None,
                  status: Optional[str] = None) -> List[dict]:
        """List tasks with optional filters via HTTP."""
        self._require_token()
        body: Dict = {"token": self.token}
        if assignee:
            body["assignee"] = assignee
        if status:
            body["status"] = status
        resp = self._post("/agents/task_list", body)
        return resp.get("tasks", [])

    def task_progress(self) -> dict:
        """Get task progress overview via HTTP."""
        self._require_token()
        return self._post("/agents/task_progress", {"token": self.token})

    def check_signal_file(self) -> Optional[dict]:
        """Check if a signal file exists for this agent (file-based notification).

        Returns parsed signal data if file exists, None otherwise.
        Signal files are written by the server when messages are queued.
        """
        signal_path = f"/tmp/pluto/signals/{self.agent_id}.signal"
        if os.path.exists(signal_path):
            try:
                with open(signal_path, 'r') as f:
                    return json.loads(f.read())
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, *_):
        try:
            self.unregister()
        except Exception:
            pass
