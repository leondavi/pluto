#!/usr/bin/env python3
"""
Agent: lead-architect
═══════════════════════
Phase 1: Design system topology (sequential — must complete before others start work)
Phase 4: Wait for all reviews, assemble final deployment plan (sequential)
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
    print(f"[lead-architect] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id="lead-architect", timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (agent_id={client.agent_id}, session={client.session_id})")

    try:
        # ── Phase 1: Design the system topology ──────────────────────
        log("Phase 1: Designing system topology ...")

        lock = client.acquire("project:master-plan", mode="write", ttl_ms=20000)
        log(f"  Acquired master-plan lock: {lock}")

        with open(os.path.join(WORK, "master_plan.txt"), "w") as f:
            f.write(
                "# Microservices Deployment Plan\n"
                "Designed by lead-architect\n\n"
                "## System topology\n"
                "┌─────────┐    ┌──────────┐    ┌───────────┐\n"
                "│ Frontend │───>│ API GW   │───>│ Backend   │\n"
                "│ (React)  │    │ (Kong)   │    │ (Go/Rust) │\n"
                "└─────────┘    └──────────┘    └─────┬─────┘\n"
                "                                     │\n"
                "                               ┌─────▼─────┐\n"
                "                               │ Data Layer │\n"
                "                               │ (PG+Redis) │\n"
                "                               └───────────┘\n\n"
                "## Services\n"
                "  1. user-service     — auth, profiles, RBAC\n"
                "  2. task-service     — CRUD, assignments, workflow\n"
                "  3. notify-service   — email, webhooks, websockets\n"
                "  4. analytics-service— metrics, dashboards\n\n"
                "## Infrastructure Requirements\n"
                "  - Kubernetes cluster (3 nodes min)\n"
                "  - PostgreSQL 16 (primary + read replica)\n"
                "  - Redis 7 (cluster mode)\n"
                "  - Kong API Gateway\n"
                "  - Prometheus + Grafana monitoring\n"
            )
        log("  Wrote master_plan.txt")

        # Update shared status board
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "w") as f:
            f.write("=== Deployment Project Status ===\n")
            f.write("lead-architect: master plan DRAFTED\n")
        client.release(sb_lock)
        log("  Updated status board")

        client.release(lock)
        log("  Released master-plan lock")

        # ── Wait for Phase 2 agents to be ready ─────────────────────
        log("Waiting for parallel workers to connect ...")
        m_ready1 = wait_msg("infra-engineer", timeout=30)
        log(f"  infra-engineer is ready")
        m_ready2 = wait_msg("api-developer", timeout=30)
        log(f"  api-developer is ready")

        # ── Signal Phase 2 agents to start ───────────────────────────
        log("Signaling parallel workers ...")
        client.send("infra-engineer", {
            "type": "start_work",
            "phase": 2,
            "task": "Design infrastructure spec based on master_plan.txt",
        })
        client.send("api-developer", {
            "type": "start_work",
            "phase": 2,
            "task": "Design API contracts based on master_plan.txt",
        })
        log("  Sent start signals to infra-engineer and api-developer")

        # ── Phase 4: Wait for all reviews to complete ────────────────
        log("Phase 4: Waiting for review completions ...")

        m1 = wait_msg("infra-engineer", timeout=30)
        log(f"  Received from infra-engineer: {m1['payload'].get('type')}")

        m2 = wait_msg("api-developer", timeout=30)
        log(f"  Received from api-developer: {m2['payload'].get('type')}")

        # ── Assemble final deployment plan ───────────────────────────
        log("Assembling final deployment plan ...")
        final_lock = client.acquire("project:final-assembly", mode="write", ttl_ms=20000)

        sections = {}
        for fname in ["master_plan.txt", "infra_spec.txt", "api_spec.txt",
                       "review_infra.txt", "review_api.txt"]:
            path = os.path.join(WORK, fname)
            with open(path) as f:
                sections[fname] = f.read()

        with open(os.path.join(WORK, "final_deployment.txt"), "w") as f:
            f.write("=" * 64 + "\n")
            f.write("  FINAL MICROSERVICES DEPLOYMENT PLAN\n")
            f.write("  Assembled by lead-architect\n")
            f.write("=" * 64 + "\n\n")
            for fname, content in sections.items():
                f.write(f"--- {fname} ---\n")
                f.write(content)
                f.write("\n\n")
            f.write("=" * 64 + "\n")
            f.write("FINAL VERDICT: All sections drafted, reviewed, and approved.\n")
            f.write("Deployment plan is READY for execution.\n")
            f.write("=" * 64 + "\n")
        log("  Wrote final_deployment.txt")

        # Final status update
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "a") as f:
            f.write("lead-architect: FINAL PLAN ASSEMBLED\n")
        client.release(sb_lock)

        client.release(final_lock)

        # Broadcast completion
        client.broadcast({"type": "project_complete", "plan": "final_deployment.txt"})
        log("  Broadcast project completion")

        # Query and log stats
        stats = client.stats()
        log(f"  Stats: {json.dumps(stats.get('counters', {}))}")

        log("DONE — all phases complete")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
