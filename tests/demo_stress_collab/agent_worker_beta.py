#!/usr/bin/env python3
"""
Agent: worker-beta
══════════════════
Mirror of worker-alpha but works on complementary resources.
Same 10-round pattern with nested locks, cross-reviews, and contention.
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
MY_ID = "worker-beta"
PEER = "worker-alpha"

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
    raise TimeoutError(f"[{MY_ID}] Timeout: from={from_agent} type={msg_type}")


def log(msg):
    print(f"[{MY_ID}] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id=MY_ID, timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (session={client.session_id})")

    try:
        # Signal readiness
        client.send("coordinator", {"type": "ready", "agent": MY_ID})
        log("Sent ready signal")

        # Wait for sprint start
        start = wait_msg(from_agent="coordinator", msg_type="sprint_start", timeout=30)
        rounds = start["payload"]["rounds"]
        log(f"Sprint started: {rounds} rounds")

        for rnd in range(1, rounds + 1):
            # ── 1) Receive task assignment ────────────────────────────
            task = wait_msg(from_agent="coordinator", msg_type="task_assign", timeout=30)
            p = task["payload"]
            primary_res = p["primary_resource"]
            shared_res = p["shared_resource"]
            log(f"  R{rnd}: task={p['task']}")

            # Send task acknowledgment
            client.send("coordinator", {
                "type": "task_ack",
                "round": rnd,
                "agent": MY_ID,
            })

            # ── 2) Acquire primary resource lock ──────────────────────
            primary_lock = client.acquire(primary_res, mode="write", ttl_ms=15000)
            log(f"  R{rnd}: acquired {primary_res} -> {primary_lock}")

            # ── 3) Acquire shared resource (NESTED LOCK — contention!) ─
            shared_lock = client.acquire(shared_res, mode="write", ttl_ms=15000)
            log(f"  R{rnd}: acquired shared {shared_res} -> {shared_lock}")

            # ── 4) Do work: write to both files ──────────────────────
            mod_idx = int(primary_res.split(":")[1])
            path = os.path.join(WORK, f"module_{mod_idx}.txt")
            with open(path, "a") as f:
                f.write(f"[{MY_ID}] Round {rnd}: Implemented feature-{rnd}B\n")

            shared_idx = int(shared_res.split(":")[1])
            shared_path = os.path.join(WORK, f"module_{shared_idx}.txt")
            with open(shared_path, "a") as f:
                f.write(f"[{MY_ID}] Round {rnd}: Shared integration code\n")

            # Release shared first (reverse order)
            client.release(shared_lock)
            client.release(primary_lock)
            log(f"  R{rnd}: wrote files and released locks")

            # ── 5) Sync with peer ────────────────────────────────────
            client.send(PEER, {
                "type": "peer_sync",
                "round": rnd,
                "done_resource": primary_res,
            })

            # ── 6) Send result to coordinator ────────────────────────
            client.send("coordinator", {
                "type": "task_result",
                "round": rnd,
                "resource": primary_res,
                "lines_written": 2,
            })

            # ── 7) Wait for cross-review assignment ──────────────────
            review = wait_msg(from_agent="coordinator", msg_type="review_request", timeout=30)
            rp = review["payload"]
            review_res = rp["review_resource"]
            review_file = rp["review_file"]
            log(f"  R{rnd}: reviewing {review_file} (by {rp['original_author']})")

            # ── 8) Acquire review target lock ────────────────────────
            review_lock = client.acquire(f"review:{review_res}", mode="write", ttl_ms=15000)

            # Wait for peer sync to ensure file is written
            peer_sync = wait_msg(from_agent=PEER, msg_type="peer_sync", timeout=30)

            review_path = os.path.join(WORK, review_file)
            with open(review_path) as f:
                content = f.read()
            log(f"  R{rnd}: read {review_file} ({len(content)} bytes)")

            # Write review comments
            review_out = os.path.join(WORK, f"review_r{rnd}_beta.txt")
            with open(review_out, "w") as f:
                f.write(f"# Review by {MY_ID} — Round {rnd}\n")
                f.write(f"Reviewed: {review_file}\n")
                f.write(f"Lines: {content.count(chr(10))}\n")
                f.write("Verdict: LGTM ✓\n")
                f.write(f"Reviewed content size: {len(content)} bytes\n")

            client.release(review_lock)

            # ── 9) Send review result to coordinator ─────────────────
            client.send("coordinator", {
                "type": "review_result",
                "round": rnd,
                "reviewed": review_file,
                "verdict": "approved",
            })

            # ── 10) Wait for approval ────────────────────────────────
            approval = wait_msg(from_agent="coordinator", msg_type="approval", timeout=30)
            log(f"  R{rnd}: {approval['payload']['status']}")

            # ── 11) Update shared build log (contention with alpha) ──
            blog_lock = client.acquire("shared:build-log", mode="write", ttl_ms=10000)
            with open(os.path.join(WORK, "build_log.txt"), "a") as f:
                f.write(f"[{MY_ID}] R{rnd}: feature-{rnd}B done, review done\n")
            client.release(blog_lock)

            # ── 12) Update shared scoreboard ─────────────────────────
            sb_lock = client.acquire("shared:scoreboard", mode="write", ttl_ms=10000)
            sb_path = os.path.join(WORK, "scoreboard.json")
            try:
                with open(sb_path) as f:
                    sb = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                sb = {}
            me = sb.get(MY_ID, {"tasks": 0, "reviews": 0, "approvals": 0})
            me["tasks"] += 1
            me["reviews"] += 1
            me["approvals"] += 1
            sb[MY_ID] = me
            with open(sb_path, "w") as f:
                json.dump(sb, f, indent=2)
            client.release(sb_lock)

            # ── 12b) Notify peer about score update ───────────────────
            client.send(PEER, {
                "type": "score_update",
                "round": rnd,
                "agent": MY_ID,
                "scores": me,
            })
            # Wait for peer's score update
            peer_score = wait_msg(from_agent=PEER, msg_type="score_update", timeout=30)
            log(f"  R{rnd}: exchanged scores with {PEER}")

            # Notify coordinator that score exchange is done
            client.send("coordinator", {
                "type": "scores_updated",
                "round": rnd,
                "agent": MY_ID,
            })

            # ── 13) Send progress ack to coordinator ─────────────────
            client.send("coordinator", {
                "type": "ack",
                "round": rnd,
                "agent": MY_ID,
            })

            log(f"  R{rnd}: complete ✓")

            # ── 14) Receive leaderboard from coordinator ──────────────
            lb = wait_msg(from_agent="coordinator", msg_type="leaderboard", timeout=30)
            log(f"  R{rnd}: leaderboard received")

        # ── Wait for sprint complete signal ───────────────────────────
        wait_msg(from_agent="coordinator", msg_type="sprint_complete", timeout=30)
        log("Sprint complete — shutting down")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
