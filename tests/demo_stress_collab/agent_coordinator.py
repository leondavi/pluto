#!/usr/bin/env python3
"""
Agent: coordinator
══════════════════
Orchestrates a 10-round sprint. Each round:
  1. Assign tasks to both workers
  2. Collect results
  3. Dispatch cross-reviews
  4. Collect review feedback
  5. Send approval/rework decisions
  6. Update shared dashboard and build log
  7. Broadcast round summary
"""

import json
import os
import sys
import threading
import time

from pluto_client import PlutoClient

HOST = os.environ.get("PLUTO_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLUTO_PORT", "9000"))
WORK = os.environ.get("WORK_DIR", "/tmp/pluto_stress_collab")
ROUNDS = 10
WORKERS = ["worker-alpha", "worker-beta"]
RESOURCE_POOL = [f"module:{i}" for i in range(5)]  # 5 rotating resources

messages = []
msg_event = threading.Event()
msg_lock = threading.Lock()


def on_msg(event):
    with msg_lock:
        messages.append(event)
    msg_event.set()


def wait_msg(from_agent=None, msg_type=None, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with msg_lock:
            for i, m in enumerate(messages):
                from_ok = from_agent is None or m.get("from") == from_agent
                type_ok = msg_type is None or m.get("payload", {}).get("type") == msg_type
                if from_ok and type_ok:
                    return messages.pop(i)
        msg_event.clear()
        msg_event.wait(timeout=0.5)
    raise TimeoutError(f"Timeout waiting for msg from={from_agent} type={msg_type}")


def wait_msgs(from_agents, msg_type=None, timeout=30):
    """Wait for one message from each agent in from_agents."""
    results = {}
    remaining = set(from_agents)
    deadline = time.time() + timeout
    while remaining and time.time() < deadline:
        with msg_lock:
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                frm = m.get("from")
                type_ok = msg_type is None or m.get("payload", {}).get("type") == msg_type
                if frm in remaining and type_ok:
                    results[frm] = messages.pop(i)
                    remaining.discard(frm)
        if remaining:
            msg_event.clear()
            msg_event.wait(timeout=0.5)
    if remaining:
        raise TimeoutError(f"Timeout waiting for {remaining}")
    return results


def log(msg):
    print(f"[coordinator] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id="coordinator", timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (session={client.session_id})")

    try:
        # ── Wait for workers to be ready ─────────────────────────────
        log("Waiting for workers to connect ...")
        ready = wait_msgs(WORKERS, msg_type="ready", timeout=30)
        log(f"  Both workers ready: {list(ready.keys())}")

        # ── Initialize shared files ──────────────────────────────────
        init_lock = client.acquire("shared:dashboard", mode="write", ttl_ms=10000)
        with open(os.path.join(WORK, "dashboard.txt"), "w") as f:
            f.write("=== Sprint Dashboard ===\n")
        with open(os.path.join(WORK, "build_log.txt"), "w") as f:
            f.write("=== Build Log ===\n")
        with open(os.path.join(WORK, "metrics.json"), "w") as f:
            json.dump({"rounds_completed": 0, "tasks_assigned": 0,
                       "reviews_done": 0, "approvals": 0, "reworks": 0}, f)
        # Create the 5 resource pool files
        for i in range(5):
            with open(os.path.join(WORK, f"module_{i}.txt"), "w") as f:
                f.write(f"# Module {i}\n")
        client.release(init_lock)
        log("  Initialized shared files")

        # Send start signal
        for w in WORKERS:
            client.send(w, {"type": "sprint_start", "rounds": ROUNDS})

        # ── Main sprint loop ─────────────────────────────────────────
        for rnd in range(1, ROUNDS + 1):
            log(f"\n── Round {rnd}/{ROUNDS} ────────────────────────────")

            # Resources for this round: each worker gets a different one
            res_alpha = RESOURCE_POOL[(rnd * 2) % 5]
            res_beta = RESOURCE_POOL[(rnd * 2 + 1) % 5]
            # Shared resource both will contend for
            shared_res = RESOURCE_POOL[(rnd * 2 + 2) % 5]

            # 1) Assign tasks ──────────────────────────────────────────
            client.send("worker-alpha", {
                "type": "task_assign",
                "round": rnd,
                "primary_resource": res_alpha,
                "shared_resource": shared_res,
                "task": f"Implement feature-{rnd}A in {res_alpha}",
            })
            client.send("worker-beta", {
                "type": "task_assign",
                "round": rnd,
                "primary_resource": res_beta,
                "shared_resource": shared_res,
                "task": f"Implement feature-{rnd}B in {res_beta}",
            })
            log(f"  Assigned: alpha→{res_alpha}, beta→{res_beta}, shared→{shared_res}")

            # 2) Collect task acknowledgments ──────────────────────────
            task_acks = wait_msgs(WORKERS, msg_type="task_ack", timeout=30)
            log(f"  Received task acknowledgments")

            # 3) Collect results ───────────────────────────────────────
            results = wait_msgs(WORKERS, msg_type="task_result", timeout=30)
            log(f"  Collected results from both workers")

            # 4) Dispatch cross-reviews ────────────────────────────────
            # Alpha reviews Beta's work and vice versa
            client.send("worker-alpha", {
                "type": "review_request",
                "round": rnd,
                "review_resource": res_beta,
                "review_file": f"module_{(rnd * 2 + 1) % 5}.txt",
                "original_author": "worker-beta",
            })
            client.send("worker-beta", {
                "type": "review_request",
                "round": rnd,
                "review_resource": res_alpha,
                "review_file": f"module_{(rnd * 2) % 5}.txt",
                "original_author": "worker-alpha",
            })
            log(f"  Dispatched cross-reviews")

            # 5) Collect review feedback ───────────────────────────────
            reviews = wait_msgs(WORKERS, msg_type="review_result", timeout=30)
            log(f"  Collected review feedback")

            # 6) Send approvals ────────────────────────────────────────
            for w in WORKERS:
                client.send(w, {
                    "type": "approval",
                    "round": rnd,
                    "status": "approved",
                    "message": f"Round {rnd} work approved. Good job!",
                })

            # 7) Wait for workers to acknowledge ──────────────────────
            acks = wait_msgs(WORKERS, msg_type="ack", timeout=30)

            # 8) Wait for peer score updates to finish ─────────────────
            score_acks = wait_msgs(WORKERS, msg_type="scores_updated", timeout=30)

            # 9) Update shared dashboard under lock ────────────────────
            db_lock = client.acquire("shared:dashboard", mode="write", ttl_ms=10000)
            with open(os.path.join(WORK, "dashboard.txt"), "a") as f:
                f.write(f"Round {rnd}: COMPLETE — alpha({res_alpha}) beta({res_beta}) shared({shared_res})\n")

            # Update metrics
            with open(os.path.join(WORK, "metrics.json"), "r") as f:
                metrics = json.load(f)
            metrics["rounds_completed"] = rnd
            metrics["tasks_assigned"] += 2
            metrics["reviews_done"] += 2
            metrics["approvals"] += 2
            with open(os.path.join(WORK, "metrics.json"), "w") as f:
                json.dump(metrics, f, indent=2)
            client.release(db_lock)

            # 10) Send leaderboard update to each worker ───────────────
            with open(os.path.join(WORK, "scoreboard.json")) as f:
                sb = json.load(f)
            for w in WORKERS:
                client.send(w, {
                    "type": "leaderboard",
                    "round": rnd,
                    "scores": sb,
                })

            # 11) Broadcast round summary ──────────────────────────────
            client.broadcast({
                "type": "round_summary",
                "round": rnd,
                "status": "complete",
                "resources_touched": [res_alpha, res_beta, shared_res],
            })
            log(f"  Round {rnd} complete ✓")

        # ── Final assembly ────────────────────────────────────────────
        log("\nAssembling final sprint report ...")
        final_lock = client.acquire("shared:final-report", mode="write", ttl_ms=20000)

        with open(os.path.join(WORK, "dashboard.txt")) as f:
            dashboard = f.read()
        with open(os.path.join(WORK, "metrics.json")) as f:
            metrics = json.load(f)

        with open(os.path.join(WORK, "sprint_report.txt"), "w") as f:
            f.write("=" * 60 + "\n")
            f.write("  SPRINT REPORT — Assembled by coordinator\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Rounds completed:  {metrics['rounds_completed']}\n")
            f.write(f"Tasks assigned:    {metrics['tasks_assigned']}\n")
            f.write(f"Reviews done:      {metrics['reviews_done']}\n")
            f.write(f"Approvals:         {metrics['approvals']}\n\n")
            f.write("── Dashboard ──\n")
            f.write(dashboard + "\n")
            f.write("── Module Files ──\n")
            for i in range(5):
                with open(os.path.join(WORK, f"module_{i}.txt")) as mf:
                    f.write(f"\n[module_{i}.txt]\n{mf.read()}")
            f.write("\n" + "=" * 60 + "\n")
            f.write("Sprint COMPLETE. All modules implemented and reviewed.\n")
            f.write("=" * 60 + "\n")
        log("  Wrote sprint_report.txt")
        client.release(final_lock)

        # Notify workers sprint is done
        for w in WORKERS:
            client.send(w, {"type": "sprint_complete"})
        client.broadcast({"type": "sprint_done", "report": "sprint_report.txt"})

        # Collect final stats
        stats = client.stats()
        log(f"  Final counters: {json.dumps(stats.get('counters', {}))}")
        log("DONE — sprint complete")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
