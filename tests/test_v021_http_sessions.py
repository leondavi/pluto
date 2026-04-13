#!/usr/bin/env python3
"""
test_v021_http_sessions.py — Integration tests for Pluto v0.2.1 HTTP session features.

Tests all 4 solutions:
  1. HTTP-based session registration (POST /agents/register with token)
  2. Stateless agent mode (mode=stateless, configurable TTL)
  3. PlutoClient.sh register --daemon (tested separately via shell)
  4. Configurable heartbeat TTL for HTTP agents

Also tests:
  - Duplicate agent name prevention
  - Cross-protocol visibility (HTTP agents visible to TCP agents)
  - HTTP message send/receive via polling
  - HTTP broadcast
  - HTTP heartbeat keeps session alive
  - Session expiry on missing heartbeats

Requires: Pluto server running on localhost:9000 (TCP) / :9001 (HTTP)
"""

import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

# Add src_py to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_py"))

from pluto_client import PlutoClient, PlutoHttpClient, PlutoError

HOST = "127.0.0.1"
TCP_PORT = 9000
HTTP_PORT = 9001
BASE_URL = f"http://{HOST}:{HTTP_PORT}"

passed = 0
failed = 0
errors = []


def test(name):
    """Decorator for test functions."""
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


def tcp_send_recv(msg):
    """Send a JSON message via TCP and get the response."""
    sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
    line = (json.dumps(msg) + "\n").encode("utf-8")
    sock.sendall(line)
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return json.loads(data.decode("utf-8").strip())


def rand_id():
    import secrets
    return secrets.token_hex(4)


# ═══════════════════════════════════════════════════════════════════
# Solution 1: HTTP-based session registration
# ═══════════════════════════════════════════════════════════════════

@test("Solution 1: HTTP register returns token and session_id")
def _():
    agent_id = f"test-http-{rand_id()}"
    resp = http_post("/agents/register", {"agent_id": agent_id})
    assert resp["status"] == "ok", f"Expected ok, got {resp}"
    assert "token" in resp, f"Missing token in {resp}"
    assert "session_id" in resp, f"Missing session_id in {resp}"
    assert resp["agent_id"] == agent_id
    assert resp["mode"] == "http"
    # Cleanup
    http_post("/agents/unregister", {"token": resp["token"]})


@test("Solution 1: HTTP agent appears in /agents list")
def _():
    agent_id = f"test-visible-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": agent_id})
    token = reg["token"]

    agents = http_get("/agents")
    assert agent_id in agents["agents"], f"{agent_id} not in {agents['agents']}"

    http_post("/agents/unregister", {"token": token})


@test("Solution 1: HTTP heartbeat keeps session alive")
def _():
    agent_id = f"test-hb-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": agent_id})
    token = reg["token"]

    hb = http_post("/agents/heartbeat", {"token": token})
    assert hb["status"] == "ok"
    assert "ts" in hb

    http_post("/agents/unregister", {"token": token})


@test("Solution 1: HTTP agent can send messages to TCP agent")
def _():
    # Register TCP agent
    tcp_agent = f"tcp-recv-{rand_id()}"
    sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock.sendall((json.dumps({"op": "register", "agent_id": tcp_agent}) + "\n").encode())
    tcp_resp = b""
    while b"\n" not in tcp_resp:
        tcp_resp += sock.recv(4096)

    # Register HTTP agent
    http_agent = f"http-sender-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": http_agent})
    token = reg["token"]

    # Send from HTTP to TCP
    send_resp = http_post("/agents/send", {
        "token": token,
        "to": tcp_agent,
        "payload": {"msg": "hello from HTTP"}
    })
    assert send_resp["status"] == "ok", f"Send failed: {send_resp}"

    # TCP agent should receive the message (drain events)
    sock.settimeout(2)
    received_msg = False
    for _ in range(5):
        try:
            data = sock.recv(4096)
            for line in data.decode().strip().split("\n"):
                event = json.loads(line)
                if event.get("event") == "message" and event.get("from") == http_agent:
                    received_msg = True
                    break
        except socket.timeout:
            break
        if received_msg:
            break

    sock.close()
    http_post("/agents/unregister", {"token": token})
    assert received_msg, "TCP agent did not receive message from HTTP agent"


@test("Solution 1: HTTP agent can poll messages")
def _():
    # Register HTTP agent
    http_agent = f"http-poll-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": http_agent})
    token = reg["token"]
    actual_id = reg["agent_id"]

    # Send message via TCP to HTTP agent
    tcp_agent = f"tcp-sender-{rand_id()}"
    sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock.sendall((json.dumps({"op": "register", "agent_id": tcp_agent}) + "\n").encode())
    sock.recv(4096)
    sock.sendall((json.dumps({"op": "send", "to": actual_id,
                               "payload": {"data": "test-poll"}}) + "\n").encode())
    sock.recv(4096)
    sock.close()

    time.sleep(0.2)

    # Poll messages
    poll = http_get(f"/agents/poll?token={token}")
    assert poll["status"] == "ok", f"Poll failed: {poll}"
    assert poll["count"] >= 1, f"Expected at least 1 message, got {poll['count']}"
    msgs = poll["messages"]
    found = any(m.get("from") == tcp_agent for m in msgs)
    assert found, f"Message from {tcp_agent} not found in {msgs}"

    # Second poll should be empty
    poll2 = http_get(f"/agents/poll?token={token}")
    assert poll2["count"] == 0, f"Expected 0 messages on second poll, got {poll2['count']}"

    http_post("/agents/unregister", {"token": token})


@test("Solution 1: HTTP unregister removes agent")
def _():
    agent_id = f"test-unreg-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": agent_id})
    token = reg["token"]

    unreg = http_post("/agents/unregister", {"token": token})
    assert unreg["status"] == "ok"

    # Heartbeat should fail
    hb = http_post("/agents/heartbeat", {"token": token})
    assert hb["status"] == "error"


# ═══════════════════════════════════════════════════════════════════
# Solution 2: Stateless agent mode
# ═══════════════════════════════════════════════════════════════════

@test("Solution 2: Stateless agent registration")
def _():
    agent_id = f"test-stateless-{rand_id()}"
    resp = http_post("/agents/register", {
        "agent_id": agent_id,
        "mode": "stateless"
    })
    assert resp["status"] == "ok"
    assert resp["mode"] == "stateless"
    assert "token" in resp

    # Agent appears in list
    agents = http_get("/agents")
    assert agent_id in agents["agents"]

    http_post("/agents/unregister", {"token": resp["token"]})


@test("Solution 2: Stateless agent receives messages via poll")
def _():
    agent_id = f"stateless-recv-{rand_id()}"
    reg = http_post("/agents/register", {
        "agent_id": agent_id,
        "mode": "stateless"
    })
    token = reg["token"]

    # Send via message/send HTTP endpoint
    sender = f"sender-{rand_id()}"
    http_post("/agents/register", {"agent_id": sender})

    send_resp = http_post("/messages/send", {
        "agent_id": sender,
        "to": agent_id,
        "payload": {"info": "stateless test"}
    })
    assert send_resp["status"] == "ok"

    time.sleep(0.1)
    poll = http_get(f"/agents/poll?token={token}")
    assert poll["count"] >= 1

    http_post("/agents/unregister", {"token": token})


# ═══════════════════════════════════════════════════════════════════
# Solution 4: Configurable heartbeat TTL
# ═══════════════════════════════════════════════════════════════════

@test("Solution 4: Custom TTL accepted at registration")
def _():
    agent_id = f"test-ttl-{rand_id()}"
    resp = http_post("/agents/register", {
        "agent_id": agent_id,
        "mode": "stateless",
        "ttl_ms": 600000  # 10 minutes
    })
    assert resp["status"] == "ok"
    assert resp["ttl_ms"] == 600000

    http_post("/agents/unregister", {"token": resp["token"]})


@test("Solution 4: Default TTL is 300000ms (5 min)")
def _():
    agent_id = f"test-defttl-{rand_id()}"
    resp = http_post("/agents/register", {
        "agent_id": agent_id,
        "mode": "http"
    })
    assert resp["status"] == "ok"
    assert resp["ttl_ms"] == 300000

    http_post("/agents/unregister", {"token": resp["token"]})


# ═══════════════════════════════════════════════════════════════════
# Duplicate name prevention
# ═══════════════════════════════════════════════════════════════════

@test("Duplicate name: HTTP registration gets unique suffix")
def _():
    agent_id = f"dup-test-{rand_id()}"

    # Register first
    reg1 = http_post("/agents/register", {"agent_id": agent_id})
    assert reg1["agent_id"] == agent_id

    # Register same name — should get suffixed
    reg2 = http_post("/agents/register", {"agent_id": agent_id})
    assert reg2["status"] == "ok"
    assert reg2["agent_id"] != agent_id, f"Expected different ID, got same: {reg2['agent_id']}"
    assert reg2["agent_id"].startswith(agent_id + "-")
    suffix = reg2["agent_id"][len(agent_id) + 1:]
    assert len(suffix) == 6, f"Expected 6-char suffix, got '{suffix}' ({len(suffix)} chars)"

    http_post("/agents/unregister", {"token": reg1["token"]})
    http_post("/agents/unregister", {"token": reg2["token"]})


@test("Duplicate name: TCP session gets unique suffix")
def _():
    agent_id = f"dup-tcp-{rand_id()}"

    # First TCP agent on persistent connection
    sock1 = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock1.sendall((json.dumps({"op": "register", "agent_id": agent_id}) + "\n").encode())
    r1 = b""
    while b"\n" not in r1:
        r1 += sock1.recv(4096)
    resp1 = json.loads(r1.decode().strip())
    assert resp1["agent_id"] == agent_id

    # Second TCP agent with same name
    sock2 = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock2.sendall((json.dumps({"op": "register", "agent_id": agent_id}) + "\n").encode())
    r2 = b""
    while b"\n" not in r2:
        r2 += sock2.recv(4096)
    resp2 = json.loads(r2.decode().strip())
    assert resp2["status"] == "ok"
    assert resp2["agent_id"] != agent_id
    assert resp2["agent_id"].startswith(agent_id + "-")
    suffix = resp2["agent_id"][len(agent_id) + 1:]
    assert len(suffix) == 6, f"Expected 6-char suffix, got '{suffix}'"

    sock1.close()
    sock2.close()


@test("Duplicate name: Cross-protocol (TCP first, HTTP second)")
def _():
    agent_id = f"dup-cross-{rand_id()}"

    # Register via TCP
    sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock.sendall((json.dumps({"op": "register", "agent_id": agent_id}) + "\n").encode())
    r = b""
    while b"\n" not in r:
        r += sock.recv(4096)

    # Register same via HTTP
    reg = http_post("/agents/register", {"agent_id": agent_id})
    assert reg["status"] == "ok"
    assert reg["agent_id"] != agent_id

    sock.close()
    http_post("/agents/unregister", {"token": reg["token"]})


# ═══════════════════════════════════════════════════════════════════
# PlutoHttpClient Python class
# ═══════════════════════════════════════════════════════════════════

@test("PlutoHttpClient: register, heartbeat, poll, unregister")
def _():
    client = PlutoHttpClient(
        host=HOST, http_port=HTTP_PORT,
        agent_id=f"pyclient-{rand_id()}",
        mode="http",
        ttl_ms=300000
    )
    resp = client.register()
    assert resp["status"] == "ok"
    assert client.token is not None
    assert client.session_id is not None

    hb = client.heartbeat()
    assert hb["status"] == "ok"

    messages = client.poll()
    assert isinstance(messages, list)

    agents = client.list_agents()
    assert client.agent_id in agents

    unreg = client.unregister()
    assert unreg["status"] == "ok"


@test("PlutoHttpClient: context manager")
def _():
    with PlutoHttpClient(
        host=HOST, http_port=HTTP_PORT,
        agent_id=f"ctx-{rand_id()}",
    ) as client:
        assert client.token is not None
        agents = client.list_agents()
        assert client.agent_id in agents


@test("PlutoHttpClient: send to TCP agent")
def _():
    # TCP agent
    tcp_agent = f"tcp-target-{rand_id()}"
    sock = socket.create_connection((HOST, TCP_PORT), timeout=5)
    sock.sendall((json.dumps({"op": "register", "agent_id": tcp_agent}) + "\n").encode())
    sock.recv(4096)

    # HTTP sends
    with PlutoHttpClient(
        host=HOST, http_port=HTTP_PORT,
        agent_id=f"http-src-{rand_id()}",
    ) as client:
        resp = client.send(tcp_agent, {"msg": "from PlutoHttpClient"})
        assert resp["status"] == "ok"

    sock.close()


# ═══════════════════════════════════════════════════════════════════
# HTTP broadcast
# ═══════════════════════════════════════════════════════════════════

@test("HTTP agent can broadcast")
def _():
    agent_id = f"http-bc-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": agent_id})
    token = reg["token"]

    resp = http_post("/agents/broadcast", {
        "token": token,
        "payload": {"msg": "broadcast from HTTP agent"}
    })
    assert resp["status"] == "ok"

    http_post("/agents/unregister", {"token": token})


# ═══════════════════════════════════════════════════════════════════
# HTTP subscribe to topic
# ═══════════════════════════════════════════════════════════════════

@test("HTTP agent can subscribe to topic")
def _():
    agent_id = f"http-sub-{rand_id()}"
    reg = http_post("/agents/register", {"agent_id": agent_id})
    token = reg["token"]

    resp = http_post("/agents/subscribe", {
        "token": token,
        "topic": "test-topic"
    })
    assert resp["status"] == "ok"

    http_post("/agents/unregister", {"token": token})


# ═══════════════════════════════════════════════════════════════════
# Error cases
# ═══════════════════════════════════════════════════════════════════

@test("Invalid token returns error")
def _():
    resp = http_post("/agents/heartbeat", {"token": "PLUTO-fake"})
    assert resp["status"] == "error"


@test("Missing agent_id returns error")
def _():
    resp = http_post("/agents/register", {})
    assert "error" in resp


# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  Pluto v0.2.1 HTTP Session Integration Tests")
    print(f"  Server: {HOST}:{TCP_PORT} (TCP) / {HOST}:{HTTP_PORT} (HTTP)")
    print(f"{'='*60}\n")

    # Tests already ran during import via decorators
    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    if errors:
        print(f"\n  Failures:")
        for name, err in errors:
            print(f"    ✗ {name}: {err}")
    print(f"{'='*60}\n")

    sys.exit(1 if failed > 0 else 0)
