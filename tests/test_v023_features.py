#!/usr/bin/env python3
"""
test_v023_features.py — Integration tests for Pluto v0.2.3 features.

Tests 5 new features:
  1. Inbox Message TTL / queuing (messages to offline agents are queued
     and delivered on reconnect; GET /events?agent_id=X peeks non-destructively)
  2. Session Resumption (TCP: resumed=true + same session_id;
     HTTP: resumed=true when old session_id provided during re-register)
  3. HTTP event polling by agent_id (GET /events?agent_id=X, non-destructive;
     contrast with GET /agents/poll which drains)
  4. Persistent agent registry (GET /agents?include_offline=true shows
     disconnected agents with status field)
  5. Lock reclaim on re-registration (reclaimed_locks in register response
     when agent held locks before disconnecting)

Requires: Pluto server running on localhost:9000 (TCP) / :9001 (HTTP)
"""

import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

HOST = "127.0.0.1"
TCP_PORT = 9000
HTTP_PORT = 9001
BASE_URL = f"http://{HOST}:{HTTP_PORT}"

passed = 0
failed = 0
errors = []


def test(name):
    """Decorator: runs the function immediately and tracks pass/fail."""
    def decorator(func):
        global passed, failed
        try:
            func()
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"  ✗ {name}: {e}")
    return decorator


def http_post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def http_get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def tcp_connect():
    """Open a raw TCP connection to the Pluto server."""
    return socket.create_connection((HOST, TCP_PORT), timeout=5)


class TcpConn:
    """Buffered TCP session: readline-based to correctly handle push events."""
    def __init__(self):
        self._sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
        self._file = self._sock.makefile("rb")

    def send_recv(self, msg):
        """Send a JSON op and return exactly the next response line."""
        self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        line = self._file.readline()
        return json.loads(line.decode("utf-8").strip())

    def drain_events(self, timeout=1.0):
        """Read all pending push events within `timeout` seconds."""
        events = []
        self._sock.settimeout(timeout)
        try:
            while True:
                line = self._file.readline()
                if not line:
                    break
                line = line.decode("utf-8").strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except (socket.timeout, OSError):
            pass
        return events

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def tcp_send_recv(sock_or_conn, msg):
    """Send a JSON line and read the next complete response line."""
    if isinstance(sock_or_conn, TcpConn):
        return sock_or_conn.send_recv(msg)
    # Legacy raw-socket path (for tests that use plain sockets)
    sock_or_conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
    data = b""
    while b"\n" not in data:
        chunk = sock_or_conn.recv(4096)
        if not chunk:
            break
        data += chunk
    first_line = data.decode("utf-8").split("\n")[0].strip()
    return json.loads(first_line)


def tcp_register(agent_id, session_id=None):
    """Register a TCP agent. Returns (TcpConn, register_response)."""
    conn = TcpConn()
    msg = {"op": "register", "agent_id": agent_id}
    if session_id is not None:
        msg["session_id"] = session_id
    resp = conn.send_recv(msg)
    return conn, resp


def tcp_drain_events(conn_or_sock, timeout=1.0):
    """Drain all pending push events from the connection."""
    if isinstance(conn_or_sock, TcpConn):
        return conn_or_sock.drain_events(timeout)
    # Legacy raw-socket path
    events = []
    conn_or_sock.settimeout(timeout)
    buf = b""
    try:
        while True:
            chunk = conn_or_sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    for line in buf.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def rand_id():
    import secrets
    return secrets.token_hex(4)


print(f"\n{'='*60}")
print(f"  Pluto v0.2.3 Feature Integration Tests")
print(f"  Server: {HOST}:{TCP_PORT} (TCP) / {HOST}:{HTTP_PORT} (HTTP)")
print(f"{'='*60}\n")

# ═══════════════════════════════════════════════════════════════════
# Feature 1: Inbox Message TTL — queuing & non-destructive peek
# ═══════════════════════════════════════════════════════════════════

print("Feature 1: Inbox Message TTL")

@test("F1: Messages sent to offline TCP agent are queued in inbox")
def _():
    agent_a = f"inbox-a-{rand_id()}"
    agent_b = f"inbox-b-{rand_id()}"

    # Register agent A (TCP) and immediately close socket so it goes offline
    sock_a, reg_a = tcp_register(agent_a)
    assert reg_a["status"] == "ok", f"Register A failed: {reg_a}"
    sock_a.close()
    time.sleep(0.3)  # Allow TCP cleanup / disconnect detection

    # Register agent B (HTTP sender)
    reg_b = http_post("/agents/register", {"agent_id": agent_b})
    assert reg_b["status"] == "ok", f"Register B failed: {reg_b}"
    token_b = reg_b["token"]

    # B sends a message to A while A is offline — goes into inbox
    send_resp = http_post("/agents/send", {
        "token": token_b,
        "to": agent_a,
        "payload": {"data": "hello-from-B"}
    })
    assert send_resp["status"] == "ok", f"Send failed: {send_resp}"
    time.sleep(0.1)

    # Peek at A's inbox (non-destructive)
    peek = http_get(f"/events?agent_id={agent_a}&since_token=0")
    assert peek["status"] == "ok", f"Peek failed: {peek}"
    assert peek["count"] >= 1, \
        f"Expected inbox message, got count={peek['count']}; full resp={peek}"

    found = any(m.get("payload", {}).get("data") == "hello-from-B"
                for m in peek["messages"])
    assert found, f"Expected queued message in inbox, got: {peek['messages']}"

    # Cleanup
    http_post("/agents/unregister", {"token": token_b})


@test("F1: Reconnected TCP agent receives queued inbox messages as push events")
def _():
    agent_a = f"inbox-recv-{rand_id()}"
    agent_b = f"inbox-sndr-{rand_id()}"

    # Register A (TCP), then immediately go offline
    sock_a, reg_a = tcp_register(agent_a)
    assert reg_a["status"] == "ok"
    sock_a.close()
    time.sleep(0.3)

    # Register B (HTTP) and send to offline A
    reg_b = http_post("/agents/register", {"agent_id": agent_b})
    token_b = reg_b["token"]

    send_resp = http_post("/agents/send", {
        "token": token_b,
        "to": agent_a,
        "payload": {"queued": "yes"}
    })
    assert send_resp["status"] == "ok", f"Send to offline agent failed: {send_resp}"

    # Re-register A (fresh TCP connection)
    sock_a2, reg_a2 = tcp_register(agent_a)
    assert reg_a2["status"] == "ok", f"Re-register A failed: {reg_a2}"

    # A should receive the queued message as a push event
    events = tcp_drain_events(sock_a2, timeout=1.5)
    found = any(
        e.get("event") == "message" and e.get("payload", {}).get("queued") == "yes"
        for e in events
    )
    assert found, f"Expected queued message after reconnect, got events: {events}"

    sock_a2.close()
    http_post("/agents/unregister", {"token": token_b})


@test("F1: GET /events?agent_id=X&since_token=0 is non-destructive (peek)")
def _():
    agent_a = f"peek-nodrain-{rand_id()}"
    agent_s = f"peek-sender-{rand_id()}"

    # Register A and immediately go offline
    reg_a = http_post("/agents/register", {"agent_id": agent_a})
    http_post("/agents/unregister", {"token": reg_a["token"]})
    time.sleep(0.1)

    # Send a message to offline A
    reg_s = http_post("/agents/register", {"agent_id": agent_s})
    token_s = reg_s["token"]
    http_post("/agents/send", {
        "token": token_s,
        "to": agent_a,
        "payload": {"peek": "test"}
    })
    time.sleep(0.2)

    # First peek
    peek1 = http_get(f"/events?agent_id={agent_a}&since_token=0")
    assert peek1["count"] >= 1, f"Expected message in inbox: {peek1}"

    # Second peek — should still show same count (not drained)
    peek2 = http_get(f"/events?agent_id={agent_a}&since_token=0")
    assert peek2["count"] == peek1["count"], \
        f"Peek should be non-destructive: first={peek1['count']}, second={peek2['count']}"

    http_post("/agents/unregister", {"token": token_s})


# ═══════════════════════════════════════════════════════════════════
# Feature 2: Session Resumption
# ═══════════════════════════════════════════════════════════════════

print("\nFeature 2: Session Resumption")


@test("F2: TCP re-register with old session_id returns resumed=true and same session_id")
def _():
    agent_id = f"resume-tcp-{rand_id()}"

    # Register
    sock, reg = tcp_register(agent_id)
    assert reg["status"] == "ok", f"Register failed: {reg}"
    old_session_id = reg["session_id"]

    # Close socket (agent goes offline)
    sock.close()
    time.sleep(0.2)

    # Re-register WITH the old session_id
    sock2, reg2 = tcp_register(agent_id, session_id=old_session_id)
    assert reg2["status"] == "ok", f"Re-register failed: {reg2}"
    assert reg2.get("resumed") is True, \
        f"Expected 'resumed': true, got: {reg2}"
    assert reg2["session_id"] == old_session_id, \
        f"Expected session_id={old_session_id}, got: {reg2['session_id']}"

    sock2.close()


@test("F2: TCP re-register WITHOUT session_id does NOT resume (new session_id)")
def _():
    agent_id = f"no-resume-tcp-{rand_id()}"

    sock, reg = tcp_register(agent_id)
    assert reg["status"] == "ok"
    old_session_id = reg["session_id"]

    sock.close()
    time.sleep(0.2)

    # Re-register without old session_id
    sock2, reg2 = tcp_register(agent_id)
    assert reg2["status"] == "ok", f"Re-register failed: {reg2}"
    assert reg2.get("resumed") is not True, \
        f"Expected no resumption without session_id, got: {reg2}"
    assert reg2["session_id"] != old_session_id, \
        f"Expected a NEW session_id, but got the same: {reg2['session_id']}"

    sock2.close()


@test("F2: HTTP re-register with old session_id returns resumed=true and same session_id")
def _():
    agent_id = f"resume-http-{rand_id()}"

    # Register HTTP agent
    reg = http_post("/agents/register", {"agent_id": agent_id})
    assert reg["status"] == "ok", f"Register failed: {reg}"
    token = reg["token"]
    old_session_id = reg["session_id"]

    # Unregister (agent goes offline)
    unreg = http_post("/agents/unregister", {"token": token})
    assert unreg["status"] == "ok", f"Unregister failed: {unreg}"
    time.sleep(0.1)

    # Re-register with the old session_id
    reg2 = http_post("/agents/register", {
        "agent_id": agent_id,
        "session_id": old_session_id
    })
    assert reg2["status"] == "ok", f"Re-register failed: {reg2}"
    assert reg2.get("resumed") is True, \
        f"Expected 'resumed': true in HTTP re-register, got: {reg2}"
    assert reg2["session_id"] == old_session_id, \
        f"Expected same session_id={old_session_id}, got: {reg2['session_id']}"

    # Cleanup
    http_post("/agents/unregister", {"token": reg2["token"]})


@test("F2: HTTP session resume delivers queued inbox messages via poll")
def _():
    agent_a = f"resume-inbox-{rand_id()}"
    agent_b = f"resume-sndr-{rand_id()}"

    # Register A (HTTP), unregister it
    reg_a = http_post("/agents/register", {"agent_id": agent_a})
    assert reg_a["status"] == "ok"
    old_session_id = reg_a["session_id"]
    http_post("/agents/unregister", {"token": reg_a["token"]})
    time.sleep(0.1)

    # B sends to A while A is offline
    reg_b = http_post("/agents/register", {"agent_id": agent_b})
    token_b = reg_b["token"]
    http_post("/agents/send", {
        "token": token_b,
        "to": agent_a,
        "payload": {"msg": "while-offline"}
    })
    time.sleep(0.1)

    # A re-registers (with old session_id for resumed semantics)
    reg_a2 = http_post("/agents/register", {
        "agent_id": agent_a,
        "session_id": old_session_id
    })
    assert reg_a2["status"] == "ok"
    token_a2 = reg_a2["token"]

    time.sleep(0.2)

    # Poll — should find the queued message
    poll = http_get(f"/agents/poll?token={token_a2}")
    assert poll["status"] == "ok", f"Poll failed: {poll}"
    assert poll["count"] >= 1, \
        f"Expected at least 1 message after HTTP resume, got: {poll}"

    # Cleanup
    http_post("/agents/unregister", {"token": token_a2})
    http_post("/agents/unregister", {"token": token_b})


# ═══════════════════════════════════════════════════════════════════
# Feature 3: HTTP event polling by agent_id (GET /events?agent_id=X)
# ═══════════════════════════════════════════════════════════════════

print("\nFeature 3: HTTP event polling by agent_id")


@test("F3: GET /events?agent_id=X returns 2 messages non-destructively")
def _():
    agent_x = f"evtpoll-x-{rand_id()}"
    agent_s = f"evtpoll-s-{rand_id()}"

    # Register X (HTTP) then unregister — messages will queue in inbox
    reg_x = http_post("/agents/register", {"agent_id": agent_x})
    http_post("/agents/unregister", {"token": reg_x["token"]})
    time.sleep(0.1)

    # Register sender S and send 2 messages to offline X
    reg_s = http_post("/agents/register", {"agent_id": agent_s})
    token_s = reg_s["token"]
    for i in range(2):
        http_post("/agents/send", {
            "token": token_s,
            "to": agent_x,
            "payload": {"n": i}
        })
    time.sleep(0.2)

    # First peek — should see 2 messages
    peek1 = http_get(f"/events?agent_id={agent_x}&since_token=0")
    assert peek1["status"] == "ok", f"Peek1 failed: {peek1}"
    assert peek1["count"] == 2, \
        f"Expected 2 messages in first peek, got: {peek1['count']}; full={peek1}"

    # Second peek — still 2 (non-destructive)
    peek2 = http_get(f"/events?agent_id={agent_x}&since_token=0")
    assert peek2["status"] == "ok", f"Peek2 failed: {peek2}"
    assert peek2["count"] == 2, \
        f"Expected 2 messages in second peek (non-destructive), got: {peek2['count']}"

    http_post("/agents/unregister", {"token": token_s})


@test("F3: GET /events?agent_id=X&since_token=N returns only newer messages")
def _():
    agent_x = f"evtpoll-since-{rand_id()}"
    agent_s = f"evtpoll-since-s-{rand_id()}"

    # Register, unregister X
    reg_x = http_post("/agents/register", {"agent_id": agent_x})
    http_post("/agents/unregister", {"token": reg_x["token"]})
    time.sleep(0.1)

    # Send 3 messages
    reg_s = http_post("/agents/register", {"agent_id": agent_s})
    token_s = reg_s["token"]
    for i in range(3):
        http_post("/agents/send", {
            "token": token_s,
            "to": agent_x,
            "payload": {"n": i}
        })
    time.sleep(0.2)

    # Peek all — should be 3
    peek_all = http_get(f"/events?agent_id={agent_x}&since_token=0")
    assert peek_all["count"] == 3, \
        f"Expected 3 messages, got: {peek_all['count']}"
    msgs = peek_all["messages"]
    assert all("seq_token" in m for m in msgs), \
        f"Messages missing seq_token: {msgs}"

    # since_token = seq of first message → should return 2
    first_seq = msgs[0]["seq_token"]
    peek_since = http_get(f"/events?agent_id={agent_x}&since_token={first_seq}")
    assert peek_since["count"] == 2, \
        f"Expected 2 messages with since_token={first_seq}, got: {peek_since['count']}"

    http_post("/agents/unregister", {"token": token_s})


@test("F3: GET /agents/poll drains inbox; subsequent GET /events returns 0")
def _():
    agent_x = f"evtpoll-drain-{rand_id()}"
    agent_s = f"evtpoll-drain-s-{rand_id()}"

    # Register, unregister X (offline)
    reg_x = http_post("/agents/register", {"agent_id": agent_x})
    http_post("/agents/unregister", {"token": reg_x["token"]})
    time.sleep(0.1)

    # Send 2 messages
    reg_s = http_post("/agents/register", {"agent_id": agent_s})
    token_s = reg_s["token"]
    for _ in range(2):
        http_post("/agents/send", {
            "token": token_s,
            "to": agent_x,
            "payload": {"d": "drain-test"}
        })
    time.sleep(0.2)

    # Peek before drain — 2 messages
    peek_before = http_get(f"/events?agent_id={agent_x}&since_token=0")
    assert peek_before["count"] == 2, \
        f"Expected 2 before drain, got: {peek_before['count']}"

    # Re-register X to get a valid poll token
    reg_x2 = http_post("/agents/register", {"agent_id": agent_x})
    token_x2 = reg_x2["token"]
    time.sleep(0.1)

    # Drain via poll
    poll = http_get(f"/agents/poll?token={token_x2}")
    assert poll["status"] == "ok", f"Poll failed: {poll}"
    assert poll["count"] == 2, \
        f"Expected 2 messages from poll drain, got: {poll['count']}"

    # Peek after drain — should be empty
    peek_after = http_get(f"/events?agent_id={agent_x}&since_token=0")
    assert peek_after["count"] == 0, \
        f"Expected 0 messages after drain, got: {peek_after['count']}"

    http_post("/agents/unregister", {"token": token_x2})
    http_post("/agents/unregister", {"token": token_s})


# ═══════════════════════════════════════════════════════════════════
# Feature 4: Persistent agent registry
# ═══════════════════════════════════════════════════════════════════

print("\nFeature 4: Persistent agent registry")


@test("F4: Disconnected HTTP agent NOT visible in GET /agents (default)")
def _():
    agent_z = f"offline-z-{rand_id()}"

    reg = http_post("/agents/register", {"agent_id": agent_z})
    token = reg["token"]

    # Verify it IS visible while connected
    agents_live = http_get("/agents")
    assert agent_z in agents_live.get("agents", []), \
        f"{agent_z} should be in live /agents but wasn't"

    # Unregister — goes offline
    http_post("/agents/unregister", {"token": token})
    time.sleep(0.1)

    # Default list should NOT include it
    agents_after = http_get("/agents")
    assert agent_z not in agents_after.get("agents", []), \
        f"Offline agent {agent_z} should not appear in default /agents"


@test("F4: Disconnected HTTP agent visible in GET /agents?include_offline=true")
def _():
    agent_z = f"offline-detail-{rand_id()}"

    reg = http_post("/agents/register", {"agent_id": agent_z})
    token = reg["token"]

    http_post("/agents/unregister", {"token": token})
    time.sleep(0.1)

    result = http_get("/agents?include_offline=true")
    assert result["status"] == "ok", f"List failed: {result}"
    agents_list = result["agents"]

    # agents_list should be a list of maps (not strings)
    assert isinstance(agents_list, list), f"Expected list, got: {agents_list}"

    found = None
    for a in agents_list:
        if isinstance(a, dict) and a.get("agent_id") == agent_z:
            found = a
            break

    assert found is not None, \
        f"Disconnected agent {agent_z} not found in include_offline list: {agents_list}"
    assert found.get("status") == "disconnected", \
        f"Expected status=disconnected, got: {found}"


@test("F4: TCP list_agents include_offline=true shows disconnected agent as map")
def _():
    agent_z = f"offline-tcp-z-{rand_id()}"
    agent_obs = f"observer-{rand_id()}"

    # Register Z via HTTP then unregister
    reg_z = http_post("/agents/register", {"agent_id": agent_z})
    http_post("/agents/unregister", {"token": reg_z["token"]})
    time.sleep(0.1)

    # Observer TCP agent
    sock, reg_obs = tcp_register(agent_obs)
    assert reg_obs["status"] == "ok"

    resp = tcp_send_recv(sock, {
        "op": "list_agents",
        "include_offline": True
    })
    assert resp["status"] == "ok", f"list_agents failed: {resp}"

    agents_list = resp["agents"]
    found = None
    for a in agents_list:
        if isinstance(a, dict) and a.get("agent_id") == agent_z:
            found = a
            break

    assert found is not None, \
        f"Disconnected agent {agent_z} not found in TCP include_offline list: {agents_list}"
    assert found.get("status") == "disconnected", \
        f"Expected status=disconnected, got: {found}"

    sock.close()


@test("F4: TCP list_agents default does NOT show disconnected agent")
def _():
    agent_z = f"offline-tcpd-{rand_id()}"
    agent_obs = f"obs-{rand_id()}"

    reg_z = http_post("/agents/register", {"agent_id": agent_z})
    http_post("/agents/unregister", {"token": reg_z["token"]})
    time.sleep(0.1)

    sock, reg_obs = tcp_register(agent_obs)
    assert reg_obs["status"] == "ok"

    resp = tcp_send_recv(sock, {"op": "list_agents"})
    assert resp["status"] == "ok"
    agents_list = resp["agents"]

    # Default list is strings (connected only)
    assert agent_z not in agents_list, \
        f"Offline agent {agent_z} should not appear in default TCP list_agents"

    sock.close()


# ═══════════════════════════════════════════════════════════════════
# Feature 5: Lock reclaim on re-registration
# ═══════════════════════════════════════════════════════════════════

print("\nFeature 5: Lock reclaim on re-registration")


@test("F5: HTTP register response includes reclaimed_locks after reconnect")
def _():
    agent_l = f"lock-reclaim-{rand_id()}"
    resource = f"res-{rand_id()}"

    # Register agent L (HTTP)
    reg = http_post("/agents/register", {"agent_id": agent_l})
    assert reg["status"] == "ok", f"Register L failed: {reg}"
    token_l = reg["token"]

    # Acquire a lock (HTTP lock endpoint uses agent_id, not token)
    lock_resp = http_post("/locks/acquire", {
        "agent_id": agent_l,
        "resource": resource,
        "mode": "write"
    })
    assert lock_resp["status"] == "ok", f"Lock acquire failed: {lock_resp}"
    lock_ref = lock_resp["lock_ref"]

    # Unregister (agent goes offline; grace timer starts; lock NOT yet released)
    http_post("/agents/unregister", {"token": token_l})
    time.sleep(0.1)

    # Re-register as agent L
    reg2 = http_post("/agents/register", {"agent_id": agent_l})
    assert reg2["status"] == "ok", f"Re-register L failed: {reg2}"
    token_l2 = reg2["token"]

    # reclaimed_locks should contain the lock
    reclaimed = reg2.get("reclaimed_locks", [])
    assert isinstance(reclaimed, list), \
        f"reclaimed_locks should be a list, got: {type(reclaimed)}"
    assert len(reclaimed) >= 1, \
        f"Expected at least 1 reclaimed lock, got: {reclaimed}"
    assert any(l.get("lock_ref") == lock_ref for l in reclaimed), \
        f"Lock {lock_ref} not found in reclaimed_locks: {reclaimed}"

    http_post("/agents/unregister", {"token": token_l2})


@test("F5: Reclaimed lock is still usable (can be released) after re-registration")
def _():
    agent_l = f"lock-reuse-{rand_id()}"
    resource = f"res-reuse-{rand_id()}"

    # Register, acquire lock
    reg = http_post("/agents/register", {"agent_id": agent_l})
    assert reg["status"] == "ok"
    token_l = reg["token"]

    lock_resp = http_post("/locks/acquire", {
        "agent_id": agent_l,
        "resource": resource,
        "mode": "write"
    })
    assert lock_resp["status"] == "ok", f"Lock acquire failed: {lock_resp}"
    lock_ref = lock_resp["lock_ref"]

    # Unregister
    http_post("/agents/unregister", {"token": token_l})
    time.sleep(0.1)

    # Re-register
    reg2 = http_post("/agents/register", {"agent_id": agent_l})
    assert reg2["status"] == "ok"
    token_l2 = reg2["token"]

    # Verify lock is in reclaimed_locks
    reclaimed = reg2.get("reclaimed_locks", [])
    assert any(l.get("lock_ref") == lock_ref for l in reclaimed), \
        f"Lock {lock_ref} not reclaimed: {reclaimed}"

    # Release the reclaimed lock (still valid)
    release_resp = http_post("/locks/release", {
        "agent_id": agent_l,
        "lock_ref": lock_ref
    })
    assert release_resp["status"] == "ok", f"Release of reclaimed lock failed: {release_resp}"

    http_post("/agents/unregister", {"token": token_l2})


@test("F5: TCP re-register reclaims held locks in register response")
def _():
    agent_l = f"lock-tcp-{rand_id()}"
    resource = f"res-tcp-{rand_id()}"

    # Register TCP agent and acquire a lock
    sock, reg = tcp_register(agent_l)
    assert reg["status"] == "ok"

    lock_resp = tcp_send_recv(sock, {
        "op": "acquire",
        "resource": resource,
        "mode": "write",
        "ttl_ms": 60000
    })
    assert lock_resp["status"] == "ok", f"TCP lock acquire failed: {lock_resp}"
    lock_ref = lock_resp["lock_ref"]

    # Close socket (disconnect, grace period starts)
    sock.close()
    time.sleep(0.2)

    # Re-register
    sock2, reg2 = tcp_register(agent_l)
    assert reg2["status"] == "ok", f"Re-register failed: {reg2}"

    reclaimed = reg2.get("reclaimed_locks", [])
    assert isinstance(reclaimed, list), f"reclaimed_locks should be list: {reg2}"
    assert len(reclaimed) >= 1, \
        f"Expected reclaimed lock in TCP re-register, got: {reclaimed}"
    assert any(l.get("lock_ref") == lock_ref for l in reclaimed), \
        f"Lock {lock_ref} not in TCP reclaimed_locks: {reclaimed}"

    # Release it
    release_resp = tcp_send_recv(sock2, {
        "op": "release",
        "lock_ref": lock_ref
    })
    assert release_resp["status"] == "ok", f"TCP release failed: {release_resp}"

    sock2.close()


@test("F5: reclaimed_locks is empty list on first registration (no prior locks)")
def _():
    agent_new = f"newagent-{rand_id()}"

    reg = http_post("/agents/register", {"agent_id": agent_new})
    assert reg["status"] == "ok", f"Register failed: {reg}"
    assert "reclaimed_locks" in reg, f"reclaimed_locks key missing from response: {reg}"
    assert reg["reclaimed_locks"] == [], \
        f"Expected empty reclaimed_locks for new agent, got: {reg['reclaimed_locks']}"

    http_post("/agents/unregister", {"token": reg["token"]})


# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    if errors:
        print(f"\n  Failures:")
        for name, err in errors:
            print(f"    ✗ {name}: {err}")
    print(f"{'='*60}\n")

    sys.exit(1 if failed > 0 else 0)
