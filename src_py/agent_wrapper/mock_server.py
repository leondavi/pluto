"""
Lightweight mock Pluto server for testing agent flows.

Speaks the same newline-delimited JSON protocol as the real Erlang server.
Supports: register, acquire, release, send, broadcast, list_agents, ping.
"""

import json
import socket
import threading
import uuid


class MockPlutoServer:
    """
    Minimal TCP server implementing the Pluto coordination protocol.

    Designed for integration tests — start it on port 0 to get an
    ephemeral port, run your agents against it, then stop.

    Usage:
        with MockPlutoServer() as srv:
            port = srv.port
            # ... run agents against 127.0.0.1:port ...
    """

    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self._port = port
        self._server = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._sessions = {}       # agent_id -> {"conn": socket, "session_id": str}
        self._locks = {}          # resource -> {"agent_id": str, "lock_ref": str}
        self._wait_queues = {}    # resource -> [{"agent_id", "conn", "wait_ref"}]
        self._ref_counter = 0

    @property
    def port(self):
        return self._port

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self._port))
        self._port = self._server.getsockname()[1]
        self._server.listen(10)
        self._server.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            for info in self._sessions.values():
                try:
                    info["conn"].close()
                except OSError:
                    pass
            self._sessions.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── Accept loop ───────────────────────────────────────────────────────

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
                t = threading.Thread(
                    target=self._handle_client, args=(conn,), daemon=True,
                )
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

    # ── Per-client handler ────────────────────────────────────────────────

    def _handle_client(self, conn):
        buf = b""
        agent_id = None
        try:
            while self._running:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    msg = json.loads(text)
                    resp, new_aid = self._process(msg, conn, agent_id)
                    if new_aid:
                        agent_id = new_aid
                    if resp:
                        self._send_line(conn, resp)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            if agent_id:
                with self._lock:
                    self._sessions.pop(agent_id, None)
                    for res in list(self._locks.keys()):
                        if self._locks[res]["agent_id"] == agent_id:
                            del self._locks[res]
                            self._grant_next(res)
            try:
                conn.close()
            except OSError:
                pass

    # ── Wire helpers ──────────────────────────────────────────────────────

    def _send_line(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _next_ref(self, prefix):
        self._ref_counter += 1
        return f"{prefix}-{self._ref_counter}"

    # ── Message dispatch ──────────────────────────────────────────────────

    def _process(self, msg, conn, agent_id):
        op = msg.get("op")

        if op == "register":
            aid = msg.get("agent_id", "unknown")
            sid = uuid.uuid4().hex[:8]
            with self._lock:
                self._sessions[aid] = {"conn": conn, "session_id": sid}
            return {"status": "ok", "session_id": sid}, aid

        elif op == "acquire":
            resource = msg.get("resource")
            with self._lock:
                if resource not in self._locks:
                    ref = self._next_ref("lock")
                    self._locks[resource] = {
                        "agent_id": agent_id, "lock_ref": ref,
                    }
                    return {"status": "ok", "lock_ref": ref}, None
                else:
                    ref = self._next_ref("wait")
                    self._wait_queues.setdefault(resource, []).append(
                        {"agent_id": agent_id, "conn": conn, "wait_ref": ref},
                    )
                    return {"status": "wait", "wait_ref": ref}, None

        elif op == "release":
            lock_ref = msg.get("lock_ref")
            with self._lock:
                for res, info in list(self._locks.items()):
                    if info["lock_ref"] == lock_ref:
                        del self._locks[res]
                        self._grant_next(res)
                        break
            return {"status": "ok"}, None

        elif op == "send":
            to = msg.get("to")
            payload = msg.get("payload", {})
            from_id = msg.get("from", agent_id)
            with self._lock:
                target = self._sessions.get(to)
            if target:
                self._send_line(target["conn"], {
                    "event": "message", "from": from_id, "payload": payload,
                })
                return {"status": "ok"}, None
            return {"status": "error", "reason": "unknown_target"}, None

        elif op == "broadcast":
            payload = msg.get("payload", {})
            from_id = msg.get("from", agent_id)
            with self._lock:
                targets = [
                    s["conn"]
                    for aid, s in self._sessions.items()
                    if aid != agent_id
                ]
            for c in targets:
                self._send_line(c, {
                    "event": "broadcast", "from": from_id, "payload": payload,
                })
            return {"status": "ok"}, None

        elif op == "list_agents":
            with self._lock:
                agents = list(self._sessions.keys())
            return {"status": "ok", "agents": agents}, None

        elif op == "ping":
            return {"status": "pong"}, None

        return {"status": "error", "reason": "unknown_op"}, None

    def _grant_next(self, resource):
        """Grant the lock to the next waiter. Caller must hold self._lock."""
        waiters = self._wait_queues.get(resource, [])
        if waiters:
            waiter = waiters.pop(0)
            if not waiters:
                del self._wait_queues[resource]
            ref = self._next_ref("lock")
            self._locks[resource] = {
                "agent_id": waiter["agent_id"], "lock_ref": ref,
            }
            self._send_line(waiter["conn"], {
                "event": "lock_granted",
                "lock_ref": ref,
                "resource": resource,
            })
