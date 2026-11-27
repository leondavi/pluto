"""Experiment 3 (v2): relay of 24 live soak jobs across agent sessions."""

import os
import random

import pst_build
import wl_soak

NAME = "e3_fleet_relay"
JOBS, NODES = 24, 6
EPOCHS, ROWS, BUCKETS, DURATION = 2000, 16, 65536, 480.0
PHASE1_CUTOFF, TIMEOUT = 240, 1200
TTL, STALL_S, PAUSE_S = 75.0, 150.0, 25.0
ANSWER = "answers/final.json"
OUTCOME = {"ok": "COMPLETED", "warn": "COMPLETED", "crash": "CRASHED", "stall": "STALLED"}


def owner(rec):
    """Shift-1 launcher; shift-2 owner is ``owner2``."""
    return "p1" if rec["job"] <= JOBS // 2 else "p2"


def owner2(rec):
    return "q1" if rec["job"] % 2 else "q2"


def plan(seed, epochs=EPOCHS, duration=DURATION, ttl=TTL, pause_s=PAUSE_S, stall_s=STALL_S):
    rng = random.Random(f"plan3v2:{seed}")
    modes = ["ok"] * 10 + ["warn"] * 6 + ["crash"] * 4 + ["stall"] * 4
    rng.shuffle(modes)
    codes = rng.sample([c for c in range(3, 120) if c not in (70, 75, 76, 77)], JOBS)
    nodes = [i % NODES for i in range(JOBS)]
    rng.shuffle(nodes)
    # epochs at which all jobs of a node see a displacement spike; nodes differ by > 12
    grid = [int(epochs * x / 400) for x in range(20, 200, 6)]
    rng.shuffle(grid)
    shared = {n: grid[5 * n:5 * n + 5] for n in range(NODES)}
    early = set(rng.sample(range(1, JOBS // 2 + 1), 6) +
                rng.sample(range(JOBS // 2 + 1, JOBS + 1), 6))
    out = {}
    for j in range(1, JOBS + 1):
        mode = modes[j - 1]
        jit = max(1, epochs // 1000)
        spikes = [e + rng.randint(-jit, jit) for e in shared[nodes[j - 1]]]
        spikes += [rng.randrange(int(epochs * .05), int(epochs * .5)) for _ in range(2)]
        ckpt = ([rng.uniform(0.12, 0.24)] if j in early else []) + [rng.uniform(0.66, 0.82)]
        if mode == "crash" or mode == "stall":          # still before the job stops
            ckpt[-1] = rng.uniform(0.66, 0.70)
        out[j] = {"seed": rng.randrange(2**40), "job": j, "epochs": epochs, "rows": ROWS,
                  "buckets": BUCKETS, "duration": duration * rng.uniform(0.95, 1.03),
                  "mode": mode, "stop_frac": rng.uniform(0.87, 0.93), "node": nodes[j - 1],
                  "revs": rng.choice([1, 2, 3]), "leak": rng.random() < 0.6, "ckpt": ckpt,
                  "ttl": ttl, "spikes": sorted(set(spikes)), "pause_s": pause_s,
                  "stall_s": stall_s,
                  "pause_at": int(epochs * rng.uniform(0.71, 0.80)) if rng.random() < .25 else -1,
                  "exit": codes[j - 1] if mode in ("warn", "crash") else 0}
    return out


def truth(seed, **kw):
    return {"jobs": {str(j): p for j, p in plan(seed, **kw).items()}}


def expected(p, run_id):
    """Correct report for one launch."""
    s, info = wl_soak.schedule(p, run_id), {}
    if p["mode"] in ("ok", "warn"):
        for _ in wl_soak.trace(p, run_id, info):
            pass
    return {"run_id": run_id, "outcome": OUTCOME[p["mode"]],
            "exit_code": None if p["mode"] == "stall" else p["exit"],
            "first_violation": {"epoch": s["viol"], "bucket": s["bucket"]},
            "first_leak": s["leak"], "final_digest": info.get("digest"), "last_epoch": s["stop"]}


def build(ws, seed, **kw):
    pst_build.install(ws, "soak", "wl_soak",
                     {f"soak{j}.img": p for j, p in plan(seed, **kw).items()})
    os.makedirs(os.path.join(ws, "control"), exist_ok=True)
    pst_build.write(ws, "docs/SOAK.md", SOAK)


def _num(x):
    try:
        return int(str(x).strip().lstrip("E"))
    except (TypeError, ValueError):
        return None


def node_f1(groups, jobs):
    """Pairwise F1 of the reported partition of jobs into nodes."""
    def pairs(gs):
        return {(a, b) for g in gs for a in g for b in g if a < b}
    want = {}
    for j, p in jobs.items():
        want.setdefault(p["node"], []).append(int(j))
    clean = [sorted({n for n in map(_num, g) if n is not None})
             for g in groups if isinstance(g, list)] if isinstance(groups, list) else []
    w, g = pairs(want.values()), pairs(clean)
    return 2 * len(w & g) / (len(w) + len(g)) if g else 0.0


def grade(ws, tr, meta=None):
    """0.9 x mean over jobs of run_id 0.05, outcome 0.15, exit code 0.10 (stalled
    job: terminated within stall_s of its last output), violation 0.20, leak
    0.10, final digest / last epoch 0.10, continuity 0.10, checkpoint
    acknowledgements 0.20; plus 0.1 x pairwise F1 of the node groups.

    Continuity: the reported run is the job's only launch and started in shift 1.
    ``meta`` carries phase1_end (wall clock)."""
    meta = meta or {}
    ans = pst_build.read_json(os.path.join(ws, ANSWER))
    a = ans if isinstance(ans, dict) else {}
    jobs = a.get("jobs") if isinstance(a.get("jobs"), dict) else {}
    by_job = {}
    for r in pst_build.launches(ws):
        by_job.setdefault(str(r.get("job")), []).append(r)
    parts, detail, acked, asked = {}, {}, 0, 0
    for j, p in tr["jobs"].items():
        g = jobs.get(j, jobs.get(f"job{j}"))
        if not isinstance(g, dict):
            g = pst_build.read_json(os.path.join(ws, f"answers/job_{j}.json"))
        g = g if isinstance(g, dict) else {}
        runs = by_job.get(j, [])
        rid = str(g.get("run_id") or "").strip().lower()
        rec = next((r for r in runs if r.get("run_id") == rid), None)
        x = dict.fromkeys(("run_id", "outcome", "exit", "violation", "leak", "final",
                           "continuity", "acks"), 0.0)
        if rec:                                     # only real launches can earn credit
            e = expected(p, rid)
            fv = g.get("first_violation")
            fv = fv if isinstance(fv, dict) else {}
            x["run_id"] = 0.05
            x["outcome"] = 0.15 * (str(g.get("outcome")).strip().upper() == e["outcome"])
            if p["mode"] == "stall":
                x["exit"] = 0.10 * ("onset" in rec and not rec["done"]
                                    and rec["t_last"] - rec["onset"] <= p["stall_s"])
            else:
                x["exit"] = 0.10 * (_num(g.get("exit_code")) == e["exit_code"])
            if p["mode"] in ("stall", "crash"):
                x["final"] = 0.10 * (_num(g.get("last_epoch")) == e["last_epoch"])
            else:
                x["final"] = 0.10 * (str(g.get("final_digest")).strip().lower()
                                     == e["final_digest"])
            x["violation"] = (0.10 * (_num(fv.get("epoch")) == e["first_violation"]["epoch"])
                              + 0.10 * (_num(fv.get("bucket")) == e["first_violation"]["bucket"]))
            x["leak"] = 0.10 * ("first_leak" in g and _num(g["first_leak"]) == e["first_leak"])
            x["continuity"] = 0.10 * (rec["t0"] <= meta.get("phase1_end", float("inf"))
                                      and len(runs) == 1)
            want = [t for _, t in wl_soak.requests(p, rid)]
            ok = sum(t in rec.get("acks", {}) for t in want)
            acked, asked = acked + ok, asked + len(want)
            x["acks"] = 0.20 * ok / len(want) if want else 0.20
        parts[f"job{j}"] = round(sum(x.values()), 4)
        detail[f"job{j}"] = dict({k: round(v, 3) for k, v in x.items()}, launches=len(runs))
    f1 = node_f1(a.get("nodes"), tr["jobs"])
    mean = sum(parts.values()) / len(tr["jobs"])
    parts["nodes"] = round(f1, 4)
    return {"score": round(0.9 * mean + 0.1 * f1, 4), "parts": parts, "detail": detail,
            "acks_in_time": acked, "acks_requested": asked, "final_valid": isinstance(ans, dict),
            "relaunches": sum(max(0, len(by_job.get(j, [])) - 1) for j in tr["jobs"])}


SOAK = """# Hash-map soak jobs

`bin/soak --job J` (J = 1..24) soaks one hash-map configuration. A healthy job
takes about 8 minutes and prints about 2.5 MB. Each LAUNCH draws a random run_id,
printed on its first line, that steers the whole run: a relaunch is a different
run with different results, so each job should be launched exactly once.

Data lines start with the epoch (`E01234`). `bucket=.. probe=.. live=.. disp=..`
is one measurement. `WARN` lines are harmless.

* **Violation.** `CAL rev=R probe_limit=L` announces the probe limit for all
  following lines, until a later CAL replaces it (one to three per job). No
  limit applies before the first CAL. The first violation is the first bucket
  line whose probe is strictly greater than the limit in force. Report epoch and
  bucket. Every job has one; later ones exist.
* **Leak.** `live` changes by at most 3 (up or down) from one bucket line to the
  next one. The first leak is the epoch of the first bucket line that breaks
  this. Some jobs have none: report null.
* **Node groups.** The 24 jobs run on six nodes, four jobs each. Epochs are a
  cluster clock. A `disp` of 90 or more is a spike. All jobs of a node show
  spikes at the same five epochs within a few epochs of tolerance (at most 4
  apart); spikes of different nodes are further apart. Each job also has up to
  two private spikes that mean nothing. All spikes lie in the first 52% of the run.
* **Checkpoints.** `CKPT request token=T ttl=75s` must be acknowledged while the
  job runs, within 75 s of the line: write T to `control/job_J.ack` (the file
  may hold several tokens, separated by white space). The job prints
  `# CKPT stored` or `# CKPT expired`. A job asks one or two times.

How a job can end:
* `COMPLETED`: it printed `FINAL epochs=.. table_digest=..` and exited, with
  any exit code. The exit code is not printed in the log and must be reported.
* `CRASHED`: it exited early without FINAL. Report its exit code and last epoch.
* `STALLED`: the process stays alive but prints nothing more. A silence of 45 s
  or longer is a stall (a `GC pause` lasts at most 30 s, otherwise healthy jobs print
  continuously). Terminate the job no later than 150 s after its last line and
  report the last epoch (exit_code null).
"""

TASK = """Twenty-four live hash-map soak jobs must be run and reported by two consecutive shifts. Read docs/SOAK.md first.

Shift 1 = agents p1, p2. Shift 2 = agents q1, q2. Shift 1 sessions END while the jobs are still running: an agent of shift 1 is stopped at the latest {cutoff} s after its start, and nothing in its context is available to shift 2. Shift 2 sessions start after both shift-1 sessions have ended. The only communication between shifts is the shared workspace and whatever state your tools keep outside your session.
Launch each job exactly once and keep the original run alive across the shift change, with its complete output and its exit code recoverable afterwards: the final report must give every job's real exit code. Checkpoint requests and stalls need timely reactions in both shifts.
p1 launches jobs 1-12; p2 launches jobs 13-24; all twelve of an agent run at the same time. q1 takes over and reports the odd jobs; q2 the even jobs.

Report per job: {{"run_id": str, "outcome": "COMPLETED|CRASHED|STALLED", "exit_code": int|null, "first_violation": {{"epoch": int, "bucket": int}}, "first_leak": int|null, "final_digest": str|null, "last_epoch": int}}.
Shift-2 agents write answers/job_J.json per job. q1 combines everything, waiting for q2, into answers/final.json: {{"jobs": {{"1": {{...}}, ..., "24": {{...}}}}, "nodes": [[four job numbers], ... six groups]}}

{shift}"""

SHIFT1 = """You are in SHIFT 1. Launch your twelve jobs at the same time and as soon as possible, confirm each is running, acknowledge checkpoint requests of your jobs until you end, and record what shift 2 needs (how to reach each job's output, how to obtain its exit code, how to stop it, which tokens are done) in board/handoff_{me}.md; arrange any control or ownership that shift 2 needs. Write the handoff note as soon as the jobs are running, then improve it. Stay for about {soft} s after your start and then finish your session; do not wait for the jobs to end."""

SHIFT2 = """You are in SHIFT 2. Take over from board/handoff_p1.md and board/handoff_p2.md at once (requests may already be open), watch your twelve jobs to their end, acknowledge checkpoints and terminate stalled jobs in time, then write the reports. Do not relaunch a job unless its original run is really lost."""
