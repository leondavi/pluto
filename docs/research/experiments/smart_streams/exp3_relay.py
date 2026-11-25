"""Experiment 3: relay/handoff across agent sessions (plan, build, grade)."""

import os
import random

import ss_build
import wl_soak

NAME = "e3_relay_handoff"
JOBS = 6
EPOCHS, ROWS, BUCKETS, DURATION = 2600, 32, 65536, 300.0
PHASE1_CUTOFF, TIMEOUT = 150, 900
ANSWER = "answers/final.json"
OUTCOME = {"ok": "COMPLETED", "warn": "COMPLETED", "crash": "CRASHED", "stall": "STALLED"}


def plan(seed, epochs=EPOCHS, duration=DURATION):
    rng = random.Random(f"plan3:{seed}")
    # one abnormal job in every phase-1 and phase-2 share would be ideal, but
    # positions must not be predictable, so modes are simply shuffled
    modes = ["ok", "ok", "warn", "warn", "crash", "stall"]
    rng.shuffle(modes)
    rev2 = [True, True, True, False, False, False]
    rng.shuffle(rev2)
    codes = rng.sample([c for c in range(3, 120) if c not in (126, 127)], JOBS)
    out = {}
    for j in range(1, JOBS + 1):
        mode = modes[j - 1]
        out[j] = {"seed": rng.randrange(2**40), "job": j, "epochs": epochs, "rows": ROWS,
                  "buckets": BUCKETS, "duration": duration * rng.uniform(0.9, 1.05),
                  "mode": mode, "rev2": rev2[j - 1],
                  "exit": codes[j - 1] if mode in ("warn", "crash") else 0}
    return out


def truth(seed, **kw):
    return {"jobs": {str(j): p for j, p in plan(seed, **kw).items()}}


def expected(p, run_id):
    """Correct report for one launch."""
    s, info = wl_soak.schedule(p, run_id), {}
    done = p["mode"] in ("ok", "warn")
    if done:
        for _ in wl_soak.trace(p, run_id, info):
            pass
    return {"run_id": run_id, "outcome": OUTCOME[p["mode"]],
            "exit_code": None if p["mode"] == "stall" else p["exit"],
            "epoch": s["viol"], "bucket": s["bucket"],
            "final_digest": info.get("digest"), "last_epoch": s["stop"]}


def build(ws, seed, **kw):
    ss_build.install(ws, "soak", "wl_soak",
                     {f"soak{j}.img": p for j, p in plan(seed, **kw).items()})
    ss_build.write(ws, "docs/SOAK.md", SOAK)


def _num(x):
    try:
        return int(str(x).strip().lstrip("E"))
    except (TypeError, ValueError):
        return None


def grade(ws, tr, meta=None):
    """Mean over jobs of: run_id 0.10, outcome 0.15, exit code (stalled job:
    terminated by the team) 0.15, violation epoch 0.15, bucket 0.15, final
    digest (or last epoch) 0.15, continuity 0.15. Continuity means the reported
    run is the only launch of that job and was started in phase 1.

    ``meta``: phase1_start, phase1_end and run_end wall-clock times.
    """
    meta = meta or {}
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    jobs = ans.get("jobs") if isinstance(ans, dict) else None
    jobs = jobs if isinstance(jobs, dict) else {}
    by_job = {}
    for r in ss_build.launches(ws):
        by_job.setdefault(str(r.get("job")), []).append(r)
    parts, detail = {}, {}
    for j, p in tr["jobs"].items():
        g = jobs.get(j, jobs.get(f"job{j}"))
        if not isinstance(g, dict):
            g = ss_build.read_json(os.path.join(ws, f"answers/job_{j}.json"))
        g = g if isinstance(g, dict) else {}
        runs = by_job.get(j, [])
        rid = str(g.get("run_id") or "").strip().lower()
        rec = next((r for r in runs if r.get("run_id") == rid), None)
        x = {k: 0.0 for k in ("run_id", "outcome", "exit", "epoch", "bucket", "final", "continuity")}
        if rec:                                     # only real launches can earn credit
            e = expected(p, rid)
            fv = g.get("first_violation")
            fv = fv if isinstance(fv, dict) else {}
            x["run_id"] = 0.10
            x["outcome"] = 0.15 * (str(g.get("outcome")).strip().upper() == e["outcome"])
            if p["mode"] == "stall":
                dead = rec["t_last"] < meta.get("run_end", float("inf")) - 4
                x["exit"] = 0.15 * (dead and bool(x["outcome"]))
                x["final"] = 0.15 * (_num(g.get("last_epoch")) == e["last_epoch"])
            elif p["mode"] == "crash":
                x["exit"] = 0.15 * (_num(g.get("exit_code")) == e["exit_code"])
                x["final"] = 0.15 * (_num(g.get("last_epoch")) == e["last_epoch"])
            else:
                x["exit"] = 0.15 * (_num(g.get("exit_code")) == e["exit_code"])
                x["final"] = 0.15 * (str(g.get("final_digest")).strip().lower()
                                     == e["final_digest"])
            x["epoch"] = 0.15 * (_num(fv.get("epoch")) == e["epoch"])
            x["bucket"] = 0.15 * (_num(fv.get("bucket")) == e["bucket"])
            in_p1 = rec["t0"] <= meta.get("phase1_end", float("inf"))
            x["continuity"] = 0.15 * (in_p1 and len(runs) == 1)
        parts[f"job{j}"] = round(sum(x.values()), 4)
        detail[f"job{j}"] = dict({k: bool(v) for k, v in x.items()}, launches=len(runs))
    return {"score": round(sum(parts.values()) / len(tr["jobs"]), 4), "parts": parts,
            "detail": detail, "final_valid": isinstance(ans, dict),
            "relaunches": sum(max(0, len(by_job.get(j, [])) - 1) for j in tr["jobs"])}


SOAK = """# Hash-map soak jobs

`bin/soak --job J` (J = 1..6) soaks one hash-map configuration. A healthy job
takes about 5 minutes and prints about 9 MB. Each LAUNCH draws a random run_id,
printed on its first line, that steers the whole run: a relaunch is a different
run with different results, so each job should be launched exactly once.

Data lines start with the epoch (`E01234`). `bucket=.. probe=..` is one probe
measurement. `CAL rev=R probe_limit=L` announces the probe limit that holds for
all following lines, until a later CAL replaces it. Before the first CAL no
limit applies. Not every job has a second CAL. `WARN` lines are harmless.

First violation: the first bucket line whose probe is strictly greater than the
limit in force at that line. Report its epoch and bucket. Later violations exist.

How a job can end:
* `COMPLETED`: it printed `FINAL epochs=.. table_digest=..` and exited, with
  any exit code. The exit code is not printed in the log and must be reported.
* `CRASHED`: it exited early without FINAL. Report its exit code and the last epoch.
* `STALLED`: the process stays alive but prints nothing more. Any silence of 45 s
  or longer is a stall (healthy jobs print continuously). Report the last epoch
  and terminate that job (exit_code null).
"""

TASK = """Six long hash-map soak jobs must be run and reported by two consecutive shifts. Read docs/SOAK.md first.

Shift 1 = agents p1, p2. Shift 2 = agents q1, q2. Shift 1 sessions END while the jobs are still running: an agent of shift 1 is stopped at the latest {cutoff} s after its start, and nothing in its context is available to shift 2. Shift 2 sessions start after both shift-1 sessions have ended. The only communication between shifts is the shared workspace (board/) and whatever state your tools keep outside your session.
Launch each job exactly once and keep the original run alive across the shift change, with its complete output and its exit code recoverable afterwards: the final report must give every job's real exit code.
p1 launches jobs 1,2,3; p2 launches jobs 4,5,6. q1 finishes and reports jobs 1,3,5; q2 finishes and reports jobs 2,4,6.

Report per job: {{"run_id": str, "outcome": "COMPLETED|CRASHED|STALLED", "exit_code": int|null, "first_violation": {{"epoch": int, "bucket": int}}, "final_digest": str|null, "last_epoch": int}}. Every job has a violation.
Shift-2 agents write answers/job_J.json per job. q1 combines all six, waiting for q2, into answers/final.json: {{"jobs": {{"1": {{...}}, ..., "6": {{...}}}}}}

{shift}"""

SHIFT1 = """You are in SHIFT 1. Launch your three jobs at the same time, confirm each is running, record what shift 2 needs (run_id, CAL values seen so far, how to reach the output, how to obtain the exit code, how to stop a job) in board/handoff_{me}.md, and arrange any control or ownership that shift 2 needs. Write the handoff note as soon as the jobs are running, then improve it if time allows. Do not wait for the jobs to finish: finish your session within about {soft} s."""

SHIFT2 = """You are in SHIFT 2. Take over from board/handoff_p1.md and board/handoff_p2.md, monitor your jobs to their end, terminate the stalled one if it is yours, and write the reports. Do not relaunch a job unless its original run is really lost."""
