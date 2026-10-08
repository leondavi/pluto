"""Scenario prompt packs and correctness validators.

Fairness rule: in both conditions the orchestrator receives the full
scenario text; workers only receive a generic preamble and get their
concrete instructions from the orchestrator. The only per-condition text
is a short "hint" describing which coordination primitives exist.
"""

import json
import os

WORKERS = ["w1", "w2", "w3", "w4"]

WORKER_PREAMBLE = (
    "You are worker {wid} in a 4-worker team (w1, w2, w3, w4). Your working "
    "directory contains ledger.py, the team's shared storage; run it as "
    "`python3 ledger.py ...`. Never edit ledger.py or its data files directly. "
    "Be concise and do only what your instructions require."
)

WORKER_SUBAGENT = (
    WORKER_PREAMBLE + " You were spawned by an orchestrator: follow its "
    "instructions exactly; your final reply is returned to it."
)

WORKER_PLUTO = (
    WORKER_PREAMBLE + " You are the Pluto agent `{wid}`; the other agents are "
    "`orch` (orchestrator) and w1..w4. Communicate only through Pluto tools. "
    "Start by calling pluto_wait_for_messages and keep handling messages "
    "(call it again whenever it returns empty) until `orch` sends you a "
    "message whose payload has \"type\": \"finish\"; then end your turn. "
    "Never stop before that finish message."
)

ORCH_SUBAGENT = (
    "You are the orchestrator of a 4-worker team. The workers are subagents "
    "named w1, w2, w3, w4 (Agent tool, subagent_type = the worker name). "
    "Workers cannot talk to each other; they only see the prompt you give "
    "them, and you only see their final reply. Never run the workers' "
    "ledger.py operations yourself. Finish with a one-paragraph report."
)

ORCH_PLUTO = (
    "You are the orchestrator, Pluto agent `orch`, of a 4-worker team. The "
    "workers w1, w2, w3, w4 are separate agents on the same Pluto server and "
    "know nothing until you message them; communicate only through Pluto "
    "tools (pluto_send payloads are JSON objects). First poll "
    "pluto_list_agents (every ~2 s, up to 60 s) until w1..w4 are all "
    "connected. Never run the workers' ledger.py operations yourself. When "
    "the work is complete, send each worker {\"type\": \"finish\"} and finish "
    "with a one-paragraph report."
)

SCENARIOS = {
    "s1_ring": {
        "title": "Ring relay",
        "task": (
            "Run a relay. A token visits w1 -> w2 -> w3 -> w4 -> w1 for 2 full "
            "laps (w1 starts; 8 hand-offs after the start, 9 holds in total, "
            "ending at w1). Each time a worker holds the token it runs "
            "`python3 ledger.py hop --agent <its id> --word <a new word>`, "
            "appends that word to the phrase, and hands the token with the "
            "phrase so far to the next worker. Report the final phrase."
        ),
        "hint": {
            "subagents": (
                "Workers are reachable only through the Agent tool, so you "
                "must carry the token from one worker to the next yourself."
            ),
            "pluto": (
                "Workers must hand the token directly to each other with "
                "pluto_send (do not relay it through orch). Tell all four "
                "workers the protocol, then tell w1 to start; ask w1 to tell "
                "you when it receives the token at the end of lap 2."
            ),
        },
    },
    "s2_contention": {
        "title": "Shared-ledger contention",
        "task": (
            "Each worker must add exactly 5 entries to the shared ledger by "
            "running `python3 ledger.py append --agent <id> --value <id>-<n>` "
            "for n = 1..5. All four workers must work concurrently. "
            "`ledger.py append` is NOT atomic (read, pause, write), so "
            "unsynchronised concurrent appends lose updates. Goal: ledger.json "
            "ends with count 20 and 20 entries."
        ),
        "hint": {
            "subagents": (
                "Run all four workers concurrently (four Agent calls in a "
                "single message). Workers share only the filesystem; tell them "
                "how to avoid lost updates."
            ),
            "pluto": (
                "Each worker must hold the Pluto write lock on resource "
                "'ledger' (pluto_lock_acquire(resource='ledger', mode='write')) "
                "around each single append and release it right after. If the "
                "acquire returns status='wait', the grant arrives later as a "
                "message (pluto_wait_for_messages)."
            ),
        },
    },
    "s3_fencing": {
        "title": "Stale lease holder (fencing)",
        "task": (
            "A single shared register is protected by a 4-second lease. w1 "
            "takes the lease first and then stalls (a simulated GC pause): it "
            "runs `python3 ledger.py stall --agent w1 --seconds 60` (a single "
            "blocking command; it must not be backgrounded) while still "
            "believing it holds the lease, and "
            "afterwards writes `python3 ledger.py put --agent w1 --value "
            "w1-stale [--token N]`. w2, w3 and w4 begin about 2 seconds after "
            "w1 has the lease; each must obtain the lease (waiting for w1's to "
            "expire if necessary), write `python3 ledger.py put --agent <id> "
            "--value <id>-fresh [--token N]`, and release the lease. "
            "`ledger.py put` rejects a write whose --token is lower than the "
            "highest token it has already accepted; writes without --token "
            "are always accepted. Goal: the register's final value must come "
            "from a worker that legitimately held the lease; w1's stale write "
            "must not overwrite fresher data. Do not tell w1 to skip, delay "
            "or reorder its stale write: the point is whether the system "
            "itself defends against it."
        ),
        "hint": {
            "subagents": (
                "Run all four workers concurrently (four Agent calls in a "
                "single message). There is no lock service; workers share "
                "only the filesystem."
            ),
            "pluto": (
                "Use pluto_lock_acquire(resource='register', mode='write', "
                "ttl_ms=4000, auto_renew=false, max_wait_ms=30000) and pass "
                "the returned fencing_token as --token. w1 must not renew or "
                "release before its stale write. For a queued request "
                "(status='wait') the grant, with its fencing_token, arrives "
                "later as a message."
            ),
        },
    },
    "s4_fanout": {
        "title": "Dynamic task fan-out",
        "task": (
            "There are 12 jobs with ids 1..12. A job is run with `python3 "
            "ledger.py job --agent <id> --id <k>`. Job durations are unknown "
            "and very uneven (1 to 12 seconds). Every job must run exactly "
            "once, and the makespan (time until all 12 are done) should be as "
            "short as possible. Report the 12 results."
        ),
        "hint": {
            "subagents": (
                "Workers are reachable only through the Agent tool; several "
                "Agent calls in one message run concurrently."
            ),
            "pluto": (
                "Assign jobs with pluto_task_assign(assignee, description, "
                "payload={'job': k}); workers report back with "
                "pluto_task_update(task_id, 'completed', result). Schedule "
                "dynamically: hand a worker its next job as soon as it "
                "reports one finished."
            ),
        },
    },
}


def orchestrator_prompt(scenario, condition):
    base = ORCH_SUBAGENT if condition == "subagents" else ORCH_PLUTO
    sc = SCENARIOS[scenario]
    return f"{base}\n\nTASK ({sc['title']}): {sc['task']}\n\nCOORDINATION: {sc['hint'][condition]}"


def worker_prompt(wid, condition):
    tmpl = WORKER_SUBAGENT if condition == "subagents" else WORKER_PLUTO
    return tmpl.format(wid=wid)


# ── validators ──────────────────────────────────────────────────────────────

def load_events(run_dir):
    events = []
    try:
        with open(os.path.join(run_dir, "events.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except OSError:
        pass
    events.sort(key=lambda e: e["ts"])
    return events


def _span(events):
    return round(events[-1]["ts"] - events[0]["ts"], 3) if events else None


def validate_s1(run_dir, events):
    hops = [e for e in events if e["op"] == "hop"]
    seq = [e["agent"] for e in hops]
    expected = (WORKERS * 2) + ["w1"]
    lat = [round(b["ts"] - a["ts"], 3) for a, b in zip(hops, hops[1:])]
    return {"ok": seq == expected, "hop_sequence": seq, "hops": len(hops),
            "hop_latency_s": lat, "task_span_s": _span(hops)}


def validate_s2(run_dir, events):
    try:
        with open(os.path.join(run_dir, "ledger.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"count": 0, "entries": []}
    attempts = [e for e in events if e["op"] == "append"]
    n = len(data.get("entries", []))
    return {"ok": n == 20 and len(attempts) == 20, "entries": n,
            "append_calls": len(attempts), "lost_updates": max(0, len(attempts) - n),
            "task_span_s": _span(attempts)}


def validate_s3(run_dir, events):
    """Classify the stale-holder outcome.

    The scenario is only *exercised* if w1's stale write is attempted after
    some fresher holder has already written; otherwise the stall did not
    outlast the others' coordination latency and nothing was tested.
    """
    puts = [e for e in events if e["op"] == "put"]
    accepted = [e for e in puts if e.get("accepted")]
    stale = [e for e in puts if e["agent"] == "w1"]
    fresh_ok = [e for e in accepted if e["agent"] != "w1"]
    final_writer = accepted[-1]["agent"] if accepted else None
    exercised = any(any(f["ts"] < e["ts"] for f in fresh_ok) for e in stale)
    rejected = any(not e.get("accepted") for e in stale)
    corrupted = any(e["agent"] == "w1" and any(f["ts"] < e["ts"] for f in fresh_ok)
                    for e in accepted)
    if not stale:
        outcome = "no_stale_write"
    elif not exercised:
        outcome = "not_exercised"
    elif corrupted:
        outcome = "corrupted"
    else:
        outcome = "rejected" if rejected else "defended"
    return {"ok": outcome in ("rejected", "defended") and final_writer != "w1",
            "outcome": outcome, "final_writer": final_writer,
            "fresh_writes": len(fresh_ok), "stale_attempted": bool(stale),
            "exercised": exercised, "stale_rejected": rejected, "corrupted": corrupted,
            "used_tokens": any(e.get("token") is not None for e in puts),
            "task_span_s": _span(puts)}


def validate_s4(run_dir, events):
    ends = [e for e in events if e["op"] == "job_end"]
    starts = [e for e in events if e["op"] == "job_start"]
    counts = {k: 0 for k in range(1, 13)}
    for e in ends:
        counts[e["job"]] = counts.get(e["job"], 0) + 1
    dup = sum(max(0, c - 1) for c in counts.values())
    missing = [k for k, c in counts.items() if c == 0]
    makespan = round(ends[-1]["ts"] - starts[0]["ts"], 3) if ends and starts else None
    per_worker = {}
    for e in ends:
        per_worker[e["agent"]] = per_worker.get(e["agent"], 0) + 1
    return {"ok": dup == 0 and not missing, "duplicates": dup, "missing": missing,
            "makespan_s": makespan, "ideal_makespan_s": 15.0,
            "jobs_per_worker": per_worker, "task_span_s": makespan}


VALIDATORS = {"s1_ring": validate_s1, "s2_contention": validate_s2,
              "s3_fencing": validate_s3, "s4_fanout": validate_s4}


def validate(scenario, run_dir):
    return VALIDATORS[scenario](run_dir, load_events(run_dir))
