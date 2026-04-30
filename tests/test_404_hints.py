#!/usr/bin/env python3
"""
test_404_hints.py - Pattern-based learning hints in Pluto's 404 responses.

When an agent hits a wrong path, the server now classifies the mistake
against common patterns and replies with a specific fix instead of a
bare {"error":"not_found"}. This test exercises every classifier branch
end-to-end against a running Pluto v0.2.7+ server.

Run as a script: python3 tests/test_404_hints.py

Requires the Pluto server running with HTTP on PLUTO_HTTP_PORT (default 9201).
"""

import json
import os
import sys
import urllib.request
import urllib.error

HOST      = os.environ.get("PLUTO_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("PLUTO_HTTP_PORT", "9201"))
BASE      = f"http://{HOST}:{HTTP_PORT}"

passed = 0
failed = 0
failures = []


def http_request(method, path, body=None):
    """Perform a request, returning (status_code, parsed_json_body)."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def case(name, *, method, path, expect_status, expect_hint_contains, expect_reason="unknown_route"):
    global passed, failed
    try:
        status, body = http_request(method, path)
        assert status == expect_status, \
            f"status: got {status}, expected {expect_status}; body={body}"
        assert body.get("reason") == expect_reason, \
            f"reason: got {body.get('reason')!r}, expected {expect_reason!r}; body={body}"
        hint = body.get("hint", "")
        for needle in (expect_hint_contains if isinstance(expect_hint_contains, list) else [expect_hint_contains]):
            assert needle in hint, f"hint missing {needle!r}; got hint={hint!r}"
        # All 404s should also surface the path the caller used.
        assert body.get("path") == path, f"path echo: got {body.get('path')!r}"
        # And the method for clarity.
        assert body.get("method") == method, f"method echo: got {body.get('method')!r}"
        print(f"  PASS  {method:4s} {path}")
        passed += 1
    except AssertionError as e:
        print(f"  FAIL  {method:4s} {path}: {e}")
        failed += 1
        failures.append((method, path, str(e)))


print(f"=== 404 learning-hint tests against {BASE} ===\n")

# --- /api/ prefix mistake ----------------------------------------------------
case("api_prefix.lock", method="GET", path="/api/lock",
     expect_status=404, expect_hint_contains=["no /api/ prefix", "drop /api/"])
case("api_prefix.messages", method="POST", path="/api/messages",
     expect_status=404, expect_hint_contains="no /api/ prefix")

# --- singular vs plural: /lock vs /locks -------------------------------------
case("lock_singular.acquire", method="POST", path="/lock/acquire",
     expect_status=404, expect_hint_contains=["plural", "/locks/acquire"])
case("lock_singular.bare", method="POST", path="/lock",
     expect_status=404, expect_hint_contains="/locks/acquire")

# --- singular vs plural: /agent vs /agents -----------------------------------
case("agent_singular.send", method="POST", path="/agent/send",
     expect_status=404, expect_hint_contains=["plural", "/agents/send"])

# --- agent ID embedded in path -----------------------------------------------
case("agent_id_in_path.inbox", method="GET", path="/agents/orch/inbox",
     expect_status=404, expect_hint_contains=[
         "JSON body, not the URL path",
         "/agents/peek",
     ])
case("agent_id_in_path.send", method="POST", path="/agents/orch/send",
     expect_status=404, expect_hint_contains="POST /agents/send with body")
case("agent_id_in_path.messages", method="GET", path="/agents/spec/messages",
     expect_status=404, expect_hint_contains="JSON body, not the URL path")

# --- task ops at wrong location ----------------------------------------------
case("task_top_level.result", method="POST", path="/task/result",
     expect_status=404, expect_hint_contains=[
         "/agents/task_assign",
         "no top-level /task",
     ])
case("task_top_level.bare", method="POST", path="/task",
     expect_status=404, expect_hint_contains="/agents/task_update")
case("task_top_level.tasks_subpath", method="POST", path="/tasks/create",
     expect_status=404, expect_hint_contains="/agents/task_assign")

# --- bare /send and /broadcast -----------------------------------------------
case("bare_send", method="POST", path="/send",
     expect_status=404, expect_hint_contains=[
         "/agents/send",
         "/messages/send",
     ])
case("bare_broadcast", method="POST", path="/broadcast",
     expect_status=404, expect_hint_contains=[
         "/agents/broadcast",
         "/messages/broadcast",
     ])

# --- truly unknown path falls back to generic guidance -----------------------
case("unknown.random", method="GET", path="/something/else",
     expect_status=404, expect_hint_contains=[
         "GET /routes",
         "Authorization header",
     ])

# --- query string is stripped before classification --------------------------
case("query_string_handled", method="GET", path="/api/lock?foo=bar",
     expect_status=404, expect_hint_contains="no /api/ prefix")

# --- /routes endpoint serves the full catalogue ------------------------------
print()
try:
    status, body = http_request("GET", "/routes")
    assert status == 200, f"status: got {status}"
    assert body.get("status") == "ok"
    routes = body.get("routes", [])
    assert isinstance(routes, list) and len(routes) >= 30, \
        f"routes: got {len(routes)} entries"
    # Spot-check a few routes the catalogue must include.
    flat = "\n".join(routes)
    for needle in [
        "/agents/send", "/agents/peek", "/locks/acquire",
        "/messages/send", "/agents/task_assign", "/routes",
    ]:
        assert needle in flat, f"/routes catalogue missing {needle!r}"
    print(f"  PASS  GET  /routes (catalogue has {len(routes)} entries)")
    passed += 1
except AssertionError as e:
    print(f"  FAIL  GET  /routes: {e}")
    failed += 1
    failures.append(("GET", "/routes", str(e)))

print(f"\n{'=' * 50}")
print(f"  Results: {passed} passed, {failed} failed")
if failures:
    print()
    for method, path, err in failures:
        print(f"  - {method} {path}: {err}")
print('=' * 50)
sys.exit(0 if failed == 0 else 1)
