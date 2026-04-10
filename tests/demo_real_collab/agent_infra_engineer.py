#!/usr/bin/env python3
"""
Agent: infra-engineer
═══════════════════════
Phase 2: Design infrastructure spec (parallel with api-developer)
Phase 3: Review api-developer's work (parallel cross-review)
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
    print(f"[infra-engineer] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id="infra-engineer", timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (agent_id={client.agent_id}, session={client.session_id})")

    try:
        # ── Signal architect that we're ready ────────────────────────
        client.send("lead-architect", {"type": "ready", "agent": "infra-engineer"})
        log("Sent ready signal to lead-architect")

        # ── Wait for architect's signal ──────────────────────────────
        log("Waiting for lead-architect signal ...")
        start_msg = wait_msg("lead-architect", timeout=30)
        log(f"  Received: {start_msg['payload'].get('task')}")

        # ── Phase 2: Design infrastructure spec ──────────────────────
        log("Phase 2: Designing infrastructure spec ...")

        # Read master plan
        with open(os.path.join(WORK, "master_plan.txt")) as f:
            master = f.read()
        log(f"  Read master_plan.txt ({len(master)} bytes)")

        # Acquire infra resource lock
        lock = client.acquire("project:infra-spec", mode="write", ttl_ms=20000)
        log(f"  Acquired infra-spec lock: {lock}")

        with open(os.path.join(WORK, "infra_spec.txt"), "w") as f:
            f.write(
                "# Infrastructure Specification\n"
                "Author: infra-engineer\n\n"
                "## Kubernetes Cluster\n"
                "  Provider: AWS EKS\n"
                "  Nodes: 3x m5.xlarge (4 vCPU, 16GB RAM)\n"
                "  Autoscaling: 3-10 nodes, CPU target 70%\n"
                "  Namespaces: pluto-prod, pluto-staging, pluto-monitoring\n\n"
                "## Database Layer\n"
                "  PostgreSQL 16 via RDS:\n"
                "    Primary: db.r6g.xlarge (Multi-AZ)\n"
                "    Read replica: db.r6g.large\n"
                "    Storage: 500GB gp3, encrypted at rest\n"
                "  Redis 7 via ElastiCache:\n"
                "    Cluster mode: 3 shards, 2 replicas each\n"
                "    Node type: cache.r6g.large\n\n"
                "## Networking\n"
                "  VPC: 10.0.0.0/16\n"
                "  Subnets: 3 public, 3 private (one per AZ)\n"
                "  Kong API Gateway: deployed as K8s ingress controller\n"
                "  TLS: ACM certificates, auto-renewal\n\n"
                "## Monitoring\n"
                "  Prometheus: federated, 30d retention\n"
                "  Grafana: 5 dashboards (infra, services, API, DB, alerts)\n"
                "  PagerDuty integration for P1/P2 alerts\n\n"
                "## CI/CD\n"
                "  GitHub Actions → Docker build → ECR → ArgoCD → EKS\n"
                "  Blue-green deployments with 5min bake time\n"
            )
        log("  Wrote infra_spec.txt")

        # Update shared status board (contention point!)
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "a") as f:
            f.write("infra-engineer: infrastructure spec COMPLETE\n")
        client.release(sb_lock)
        log("  Updated status board")

        client.release(lock)

        # Signal api-developer that infra spec is ready for review
        client.send("api-developer", {
            "type": "review_ready",
            "file": "infra_spec.txt",
            "section": "infrastructure",
        })
        log("  Signaled api-developer for cross-review")

        # ── Phase 3: Review api-developer's work ─────────────────────
        log("Phase 3: Waiting for api-developer's spec to review ...")
        review_msg = wait_msg("api-developer", timeout=30)
        log(f"  Received review request: {review_msg['payload'].get('file')}")

        # Acquire api-spec lock to read and review
        review_lock = client.acquire("project:api-review", mode="write", ttl_ms=15000)
        with open(os.path.join(WORK, "api_spec.txt")) as f:
            api_content = f.read()
        log(f"  Read api_spec.txt for review ({len(api_content)} bytes)")

        with open(os.path.join(WORK, "review_api.txt"), "w") as f:
            f.write(
                "# API Spec Review\n"
                "Reviewer: infra-engineer\n"
                "Status: APPROVED with notes\n\n"
                f"reviewed {len(api_content)} bytes of API specification\n\n"
                "Notes:\n"
                "  ✓ REST endpoints are well-structured\n"
                "  ✓ Auth flow aligns with infra RBAC setup\n"
                "  ✓ Rate limiting matches Kong gateway config\n"
                "  △ Consider adding circuit breaker timeouts to match K8s probe intervals\n"
            )
        log("  Wrote review_api.txt")
        client.release(review_lock)

        # Update status board
        sb_lock = client.acquire("project:status-board", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "status.txt"), "a") as f:
            f.write("infra-engineer: API review COMPLETE\n")
        client.release(sb_lock)

        # Signal architect that all work is done
        client.send("lead-architect", {
            "type": "all_work_complete",
            "agent": "infra-engineer",
            "files": ["infra_spec.txt", "review_api.txt"],
        })
        log("  Signaled lead-architect: all work complete")

        log("DONE — all phases complete")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
