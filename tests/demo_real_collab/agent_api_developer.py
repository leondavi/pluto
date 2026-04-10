#!/usr/bin/env python3
"""
Agent: api-developer
═══════════════════════
Phase 2: Design API contracts (parallel with infra-engineer)
Phase 3: Review infra-engineer's work (parallel cross-review)
"""

import json
import os
import sys
import threading
import time

from pluto_client import PlutoClient

HOST = os.environ.get("PLUTO_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLUTO_PORT", "9000"))
WORK = os.environ.get("WORK_DIR", "/tmp/pluto_real_collab_test")

messages = []
msg_event = threading.Event()
msg_lock = threading.Lock()


def on_msg(event):
    with msg_lock:
        messages.append(event)
    msg_event.set()


def wait_msg(from_agent, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with msg_lock:
            for i, m in enumerate(messages):
                if m.get("from") == from_agent:
                    messages.pop(i)
                    return m
        msg_event.clear()
        msg_event.wait(timeout=1)
    raise TimeoutError(f"Timeout waiting for message from {from_agent}")


def log(msg):
    print(f"[api-developer] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id="api-developer", timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (agent_id={client.agent_id}, session={client.session_id})")

    try:
        # ── Signal architect that we're ready ────────────────────────
        client.send("lead-architect", {"type": "ready", "agent": "api-developer"})
        log("Sent ready signal to lead-architect")

        # ── Wait for architect's signal ──────────────────────────────
        log("Waiting for lead-architect signal ...")
        start_msg = wait_msg("lead-architect", timeout=30)
        log(f"  Received: {start_msg['payload'].get('task')}")

        # ── Phase 2: Design API contracts ────────────────────────────
        log("Phase 2: Designing API contracts ...")

        # Read master plan
        with open(os.path.join(WORK, "master_plan.txt")) as f:
            master = f.read()
        log(f"  Read master_plan.txt ({len(master)} bytes)")

        # Acquire api resource lock
        lock = client.acquire("project:api-spec", mode="write", ttl_ms=20000)
        log(f"  Acquired api-spec lock: {lock}")

        with open(os.path.join(WORK, "api_spec.txt"), "w") as f:
            f.write(
                "# API Contracts Specification\n"
                "Author: api-developer\n\n"
                "## Authentication\n"
                "  POST /auth/login        → {token, refresh_token, expires_in}\n"
                "  POST /auth/refresh      → {token, expires_in}\n"
                "  POST /auth/logout       → 204 No Content\n\n"
                "## User Service (REST)\n"
                "  GET    /api/v1/users          → {users[], total, page}\n"
                "  POST   /api/v1/users          → {user}  (admin only)\n"
                "  GET    /api/v1/users/:id      → {user}\n"
                "  PATCH  /api/v1/users/:id      → {user}\n"
                "  DELETE /api/v1/users/:id      → 204     (admin only)\n\n"
                "## Task Service (REST)\n"
                "  GET    /api/v1/tasks          → {tasks[], total, page}\n"
                "  POST   /api/v1/tasks          → {task}\n"
                "  GET    /api/v1/tasks/:id      → {task}\n"
                "  PATCH  /api/v1/tasks/:id      → {task}\n"
                "  POST   /api/v1/tasks/:id/assign → {task}\n\n"
                "## Notification Service\n"
                "  GET    /api/v1/notifications   → {notifications[]}\n"
                "  POST   /api/v1/notifications/mark-read → 204\n"
                "  WS     /api/v1/ws/notifications → real-time push\n\n"
                "## Rate Limiting\n"
                "  Global: 1000 req/min per API key\n"
                "  Auth endpoints: 10 req/min per IP\n"
                "  WebSocket: 100 messages/min per connection\n\n"
                "## Error Format\n"
                "  {\"error\": {\"code\": \"string\", \"message\": \"string\", \"details\": {}}}\n"
                "  Standard HTTP status codes (400, 401, 403, 404, 409, 429, 500)\n"
            )
        log("  Wrote api_spec.txt")

        # Update shared status board (contention with infra-engineer!)
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "a") as f:
            f.write("api-developer: API contracts COMPLETE\n")
        client.release(sb_lock)
        log("  Updated status board")

        client.release(lock)

        # Signal infra-engineer that API spec is ready for review
        client.send("infra-engineer", {
            "type": "review_ready",
            "file": "api_spec.txt",
            "section": "api",
        })
        log("  Signaled infra-engineer for cross-review")

        # ── Phase 3: Review infra-engineer's work ────────────────────
        log("Phase 3: Waiting for infra-engineer's spec to review ...")
        review_msg = wait_msg("infra-engineer", timeout=30)
        log(f"  Received review request: {review_msg['payload'].get('file')}")

        # Acquire infra-review lock
        review_lock = client.acquire("project:infra-review", mode="write", ttl_ms=15000)
        with open(os.path.join(WORK, "infra_spec.txt")) as f:
            infra_content = f.read()
        log(f"  Read infra_spec.txt for review ({len(infra_content)} bytes)")

        with open(os.path.join(WORK, "review_infra.txt"), "w") as f:
            f.write(
                "# Infrastructure Spec Review\n"
                "Reviewer: api-developer\n"
                "Status: APPROVED\n\n"
                f"reviewed {len(infra_content)} bytes of infrastructure specification\n\n"
                "Notes:\n"
                "  ✓ K8s cluster sizing is adequate for projected load\n"
                "  ✓ Database config supports our query patterns\n"
                "  ✓ Kong gateway config matches API rate limiting requirements\n"
                "  ✓ Monitoring covers all critical service metrics\n"
                "  ✓ CI/CD pipeline aligns with our branching strategy\n"
            )
        log("  Wrote review_infra.txt")
        client.release(review_lock)

        # Update status board
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "a") as f:
            f.write("api-developer: infra review COMPLETE\n")
        client.release(sb_lock)

        # Signal architect that all work is done
        client.send("lead-architect", {
            "type": "all_work_complete",
            "agent": "api-developer",
            "files": ["api_spec.txt", "review_infra.txt"],
        })
        log("  Signaled lead-architect: all work complete")

        log("DONE — all phases complete")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
