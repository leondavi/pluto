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
        # Statistics
        self._stats = {
            "locks_acquired": 0,
            "locks_released": 0,
            "lock_waits": 0,
            "messages_sent": 0,
            "messages_received": 0,
            "broadcasts_sent": 0,
            "agents_registered": 0,
            "agents_disconnected": 0,
            "total_requests": 0,
            "deadlocks_detected": 0,
            "deadlock_victims": 0,
            "locks_expired": 0,
            "locks_renewed": 0,
        }
        self._agent_stats = {}    # agent_id -> {counter_name: int}

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
                    self._stats["agents_disconnected"] += 1
                    self._inc_agent(agent_id, "disconnections")
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
                # If the name is already taken by a live connection, assign a unique suffix
                if aid in self._sessions:
                    existing = self._sessions[aid]
                    try:
                        # Check if the existing connection is still alive
                        existing["conn"].getpeername()
                        # Connection alive — append unique suffix
                        self._unique_counter = getattr(self, "_unique_counter", 0) + 1
                        aid = f"{aid}-{self._unique_counter}"
                    except OSError:
                        # Old connection is dead — allow reuse of the name
                        pass
                self._sessions[aid] = {"conn": conn, "session_id": sid}
                self._stats["agents_registered"] += 1
                self._stats["total_requests"] += 1
                self._inc_agent(aid, "registrations")
            return {"status": "ok", "session_id": sid, "agent_id": aid}, aid

        elif op == "acquire":
            resource = msg.get("resource")
            with self._lock:
                self._stats["total_requests"] += 1
                if resource not in self._locks:
                    ref = self._next_ref("lock")
                    self._locks[resource] = {
                        "agent_id": agent_id, "lock_ref": ref,
                    }
                    self._stats["locks_acquired"] += 1
                    if agent_id:
                        self._inc_agent(agent_id, "locks_acquired")
                    return {"status": "ok", "lock_ref": ref}, None
                else:
                    ref = self._next_ref("wait")
                    self._wait_queues.setdefault(resource, []).append(
                        {"agent_id": agent_id, "conn": conn, "wait_ref": ref},
                    )
                    self._stats["lock_waits"] += 1
                    return {"status": "wait", "wait_ref": ref}, None

        elif op == "release":
            lock_ref = msg.get("lock_ref")
            with self._lock:
                self._stats["total_requests"] += 1
                for res, info in list(self._locks.items()):
                    if info["lock_ref"] == lock_ref:
                        released_agent = info["agent_id"]
                        del self._locks[res]
                        self._stats["locks_released"] += 1
                        if released_agent:
                            self._inc_agent(released_agent, "locks_released")
                        self._grant_next(res)
                        break
            return {"status": "ok"}, None

        elif op == "send":
            to = msg.get("to")
            payload = msg.get("payload", {})
            from_id = msg.get("from", agent_id)
            with self._lock:
                self._stats["total_requests"] += 1
                target = self._sessions.get(to)
                self._stats["messages_sent"] += 1
                self._stats["messages_received"] += 1
                if from_id:
                    self._inc_agent(from_id, "messages_sent")
                if to:
                    self._inc_agent(to, "messages_received")
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
                self._stats["total_requests"] += 1
                self._stats["broadcasts_sent"] += 1
                if from_id:
                    self._inc_agent(from_id, "broadcasts_sent")
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
                self._stats["total_requests"] += 1
                agents = list(self._sessions.keys())
            return {"status": "ok", "agents": agents}, None

        elif op == "ping":
            with self._lock:
                self._stats["total_requests"] += 1
            return {"status": "pong"}, None

        elif op == "stats":
            with self._lock:
                self._stats["total_requests"] += 1
                summary = self._build_stats_summary()
            return summary, None

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
            self._stats["locks_acquired"] += 1
            if waiter["agent_id"]:
                self._inc_agent(waiter["agent_id"], "locks_acquired")
            self._send_line(waiter["conn"], {
                "event": "lock_granted",
                "lock_ref": ref,
                "resource": resource,
            })

    def _inc_agent(self, agent_id, key):
        """Increment a per-agent stat counter. Caller must hold self._lock."""
        if agent_id not in self._agent_stats:
            self._agent_stats[agent_id] = {}
        stats = self._agent_stats[agent_id]
        stats[key] = stats.get(key, 0) + 1

    def _build_stats_summary(self):
        """Build a stats summary dict. Caller must hold self._lock."""
        return {
            "status": "ok",
            "counters": dict(self._stats),
            "agent_stats": {aid: dict(s) for aid, s in self._agent_stats.items()},
            "live": {
                "active_locks": len(self._locks),
                "connected_agents": len(self._sessions),
                "total_agents": len(self._sessions),
                "pending_waiters": sum(len(w) for w in self._wait_queues.values()),
                "wait_graph_edges": 0,
            },
        }
