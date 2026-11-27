"""Experiment 4: crash and recovery. Plan, fault schedule, truth and grader."""

import os
import random

import pst_build
import wl_sweep

NAME = "e4_crash_recovery"
JOBS, PER_AGENT = 40, 10
UNIT_S, UNITS, TIMEOUT = 1.0, (220, 280), 1100
REACT_S, ORPHAN_S = 120.0, 4.0
SLACK, CORRUPT_SLACK = 0.06, 45
RESPAWN_S, SERVER_DOWN_S = 5.0, 5.0
ANSWER = "answers/final.json"
WEIGHTS = {"results": .30, "history": .15, "economy": .20, "relaunch": .10, "timely": .20,
           "orphans": .05}


def owner(rec):
    return f"w{(rec['job'] - 1) // PER_AGENT + 1}"


def plan(seed, unit_s=UNIT_S, units=UNITS):
    """{job: params}; each agent gets the same mix, shuffled."""
    rng = random.Random(f"plan4:{seed}")
    out = {}
    for g in range(JOBS // PER_AGENT):
        # (resumable, fault kinds in order)
        mix = [(1, "CRASH"), (1, "HANG"), (1, "CORRUPT"), (1, "CRASH CRASH"),
               (1, "HANG CRASH"), (1, ""), (1, ""), (0, "CRASH"), (0, "HANG"), (0, "")]
        rng.shuffle(mix)
        for i, (res, kinds) in enumerate(mix):
            j, n = g * PER_AGENT + i + 1, rng.randint(*units)
            spots = [(.25, .42)] if not res else [(.3, .45), (.62, .78)] if " " in kinds \
                else [(.3, .7)]
            faults = []
            for k, (lo, hi) in zip(kinds.split(), spots):
                faults.append({"unit": max(4, int(n * rng.uniform(lo, hi))), "kind": k,
                               "code": rng.randrange(3, 60), "torn": rng.random() < .5})
            out[j] = {"seed": rng.randrange(2**40), "job": j, "units": n, "unit_s": unit_s,
                      "resumable": bool(res), "faults": faults}
    return out


def chaos(seed, scale=1.0):
    """Harness faults, identical in all arms; the server fault applies to PST only."""
    rng = random.Random(f"chaos4:{seed}")
    who = rng.sample(["w1", "w2", "w3", "w4"], 3)
    at = [rng.uniform(95, 115), rng.uniform(165, 190), rng.uniform(235, 260)]
    ev = [(t * scale, "agent", w) for t, w in zip(at, who)]
    return sorted(ev + [(rng.uniform(130, 150) * scale, "server", None)])


def good_checkpoint(p, f):
    """Last usable checkpoint unit at fault ``f`` (0 = none)."""
    if not p["resumable"]:
        return 0
    c = wl_sweep.CK_EVERY
    u = (f["unit"] - 1) // c * c if f["kind"] == "CORRUPT" else f["unit"] // c * c
    if f["kind"] == "CRASH" and f["torn"]:
        u -= c
    return max(u, 0)


def expected(p):
    tree, chain, best = [0] * (wl_sweep.TREE + 1), 0, (-1, 0)
    for u in range(1, p["units"] + 1):
        _, inv, chain = wl_sweep.unit_rows(p["seed"], u, tree, chain)
        if inv > best[0]:
            best = (inv, u)
    return {"digest": f"{chain:08x}", "peak_unit": best[1], "peak_inv": best[0],
            "failures": [{"unit": f["unit"], "kind": f["kind"],
                          "exit_code": f["code"] if f["kind"] == "CRASH" else None}
                         for f in p["faults"]]}


def truth(seed, **kw):
    return {str(j): dict(p, want=expected(p)) for j, p in plan(seed, **kw).items()}


def build(ws, seed, **kw):
    pst_build.install(ws, "sweep", "wl_sweep",
                     {f"sweep{j}.img": p for j, p in plan(seed, **kw).items()})
    os.makedirs(os.path.join(ws, "ckpt"), exist_ok=True)
    pst_build.write(ws, "docs/SWEEP.md", SWEEP)


def _int(x):
    try:
        return int(str(x).strip().lstrip("Uu"))
    except (TypeError, ValueError):
        return None


def healthy(r, t):
    """Launch ``r`` runs and produces good output at time ``t``."""
    return (r["t0"] < t < r["t_last"] - 1.2 and not r["dirty0"]
            and not any(ft < t for _, ft in r["faults"]))


def valid(r):
    return bool(r["done"] and not r.get("dirty"))


def job_process(p, recs, meta):
    """Process metrics of one job from its ledger records."""
    end = meta.get("run_end", float("inf"))
    unavoidable = sum(f["unit"] - good_checkpoint(p, f) for f in p["faults"])
    n, units = p["units"], sum(r["units"] for r in recs)
    extra = max(0, units - n - unavoidable)
    unneeded = sum(any(healthy(r, x["t0"]) or (valid(r) and r["t_last"] <= x["t0"])
                       for r in recs if r is not x) for x in recs)
    # the cleanup after run_end marks what it terminates as ended, later than run_end
    orphans = sum(not r["done"] and r["t_last"] >= end - ORPHAN_S
                  and (not r.get("ended") or r["t_last"] > end) for r in recs)
    dup = 0.0
    for i, a in enumerate(recs):
        for b in recs[i + 1:]:
            dup += max(0.0, min(a["t_last"], b["t_last"]) - max(a["t0"], b["t0"]))
    when, timely = {}, []
    for r in recs:
        for i, t in r["faults"]:
            when[i] = (t, r)
    for i, f in enumerate(p["faults"]):
        t, old = when.get(i, (None, None))
        ok = t is not None and any(
            0 < x["t0"] - t <= REACT_S and x["from_unit"] < f["unit"]
            + (f["kind"] != "CORRUPT") and not x["dirty0"] for x in recs)
        timely.append(bool(ok and (f["kind"] == "CRASH" or old["t_last"] <= t + REACT_S + 5)))
    ends = [c[0] for c in meta.get("crashes", [])]
    lost = [r for r in recs if not r["done"] and not r["faults"] and not r["dirty0"]
            and r["t_last"] < end - ORPHAN_S]
    by_crash = sum(any(-2.5 <= r["t_last"] - t <= 2.5 for t in ends) for r in lost)
    return {"launches": len(recs), "units": units, "extra_units": extra, "unneeded": unneeded,
            "orphans": orphans, "duplicate_s": dup, "timely": timely, "done": any(map(valid, recs)),
            "died_in_agent_crash": by_crash, "healthy_kills": len(lost) - by_crash}


def history_f1(got, want):
    got = [g for g in got if isinstance(g, dict)] if isinstance(got, list) else []
    if not want:
        return float(not got)
    left, t = list(got), 0.0
    for w in want:
        g = next((g for g in left if _int(g.get("unit")) == w["unit"]
                  and str(g.get("kind")).upper() == w["kind"]), None)
        if g:
            left.remove(g)
            t += .7 + .3 * (_int(g.get("exit_code")) == w["exit_code"])
    return 2 * t / (len(got) + len(want))


def grade(ws, tr, meta=None):
    """Six parts, each a mean over jobs (or faults); one item is worth at most 0.0075."""
    meta = meta or {}
    ans = pst_build.read_json(os.path.join(ws, ANSWER))
    jobs = ans.get("jobs") if isinstance(ans, dict) and isinstance(ans.get("jobs"), dict) else {}
    by = {}
    for r in pst_build.launches(ws):
        by.setdefault(str(r.get("job")), []).append(r)
    parts, tot = dict.fromkeys(WEIGHTS, 0.0), {}
    detail, timely = {}, []
    for j, p in tr.items():
        g = jobs.get(j, jobs.get(f"job{j}"))
        if not isinstance(g, dict):
            g = pst_build.read_json(os.path.join(ws, f"answers/job_{j}.json"))
        w, m = p["want"], job_process(p, by.get(j, []), meta)
        if isinstance(g, dict) and m["launches"]:
            parts["results"] += (.6 * (str(g.get("digest")).strip().lower() == w["digest"])
                                 + .2 * (_int(g.get("peak_unit")) == w["peak_unit"])
                                 + .2 * (_int(g.get("peak_inv")) == w["peak_inv"]))
            parts["history"] += history_f1(g.get("failures"), w["failures"])
        timely += m.pop("timely")
        if m["done"]:
            free = SLACK * p["units"] + CORRUPT_SLACK * sum(f["kind"] == "CORRUPT"
                                                             for f in p["faults"])
            parts["economy"] += max(0.0, 1 - max(0.0, m["extra_units"] - free) / p["units"])
            parts["relaunch"] += m["unneeded"] == 0
            parts["orphans"] += m["orphans"] == 0
        for k, v in m.items():
            tot[k] = round(tot.get(k, 0) + v, 1)
        detail[j] = m
    parts = {k: round(v / len(tr), 4) for k, v in parts.items()}
    parts["timely"] = round(sum(timely) / len(timely), 4)
    unit_s = next(iter(tr.values()))["unit_s"]
    return {"score": round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4), "parts": parts,
            "process": {"recomputed_program_s": round(tot["extra_units"] * unit_s, 1),
                        "unneeded_relaunches": tot["unneeded"], "orphans": tot["orphans"],
                        "duplicate_s": tot["duplicate_s"], "launches": tot["launches"],
                        "jobs_done": tot["done"], "healthy_kills": tot["healthy_kills"],
                        "died_in_agent_crash": tot["died_in_agent_crash"],
                        "faults_recovered_in_time": sum(timely), "faults_planned": len(timely)},
            "detail": detail, "final_valid": isinstance(ans, dict)}


SWEEP = """# Fenwick-tree sweep jobs

`bin/sweep --job J [--resume CHECKPOINT]` (J = 1..40) runs one sweep of 220 to
280 units, about one unit per second, and prints about 1 KB per unit. Data lines
start with the unit (`U0123`). A complete run ends with
`RESULT job=J units=N digest=D` and exit code 0.

* **Line checksum.** Every data line ends in ` crc=HHHH`: the low 16 bits, as four
  hex digits, of the CRC-32 (zlib) of the line's text before ` crc=`.
* **Checkpoints.** Most jobs write a checkpoint every 15 units and print its path.
  `--resume PATH` continues after that unit and prints only the later units. Some
  jobs do not support checkpoints (the first line says so) and must start over. An
  unreadable checkpoint is refused at once (exit 65); an older one may still be good.
* **Failures.** A program may fail, at most twice per job:
  `CRASH` (it exits early with a nonzero code; report the last unit printed, and
  the exit code), `HANG` (it stays alive but prints nothing more; a healthy job is
  never silent for 15 s; report the last unit printed, exit_code null) or `CORRUPT`
  (from some unit on, lines have wrong checksums: the `SUM` line of every unit from
  then on, and other lines now and then. The process goes on, but its results and all
  checkpoints written from that unit on are wrong; report the first unit with a wrong
  checksum, exit_code null).
  A failure belongs to the job and happens once; a later launch does not repeat it.
  Processes that somebody killed are not failures. `# ERROR` lines are harmless.
* A failed job must be resumed from its best good checkpoint, or restarted if it has
  none, within 120 s of the failure (and a hung or corrupt process terminated).
* The result of a job is the digest of a run without corruption, resumed or not, and
  the peak: the unit whose `SUM inv=` is largest over ALL units 1..N (the first one if
  tied), from lines with a correct checksum. With resumes it may lie in any part of
  the output.
"""

TASK = """Forty long sweep jobs must be run to completion and reported, although things fail. Read docs/SWEEP.md first.

Programs crash, hang and corrupt their output. Besides that, agent sessions and any background service on this machine may be killed without warning at any time; a killed agent is replaced by a new session that knows only its prompt and the workspace.
Ownership: w1 = jobs 1-10; w2 = 11-20; w3 = 21-30; w4 = 31-40. Start all ten of your jobs at the same time. Keep them running, notice every failure, and recover it in time. Do not repeat work that is not lost: resume instead of restarting, do not start a job that still runs well or has finished, and leave nothing running at the end. The complete output of all parts of a job is needed for its report.

Report per job: {"digest": str, "peak_unit": int, "peak_inv": int, "failures": [{"unit": int, "kind": "CRASH|HANG|CORRUPT", "exit_code": int|null}, ...]} (failures in order, [] if none).
Each agent writes answers/job_J.json per own job. w1 combines everything, waiting for the others, into answers/final.json: {"jobs": {"1": {...}, ..., "40": {...}}}"""

REPLACEMENT = """
You replace an agent {me} that crashed a moment ago. You have its name, its prompt (above) and the workspace, but none of its context."""

TOOLS_EXTRA = {
    "native": """ For work that must survive a crash of your session: start each program with nohup through a small wrapper that writes its pid and, at the end, its exit code to files; ps -o pid,lstart,command, the pid files, the exit-code files and the logs tell a later session what still runs and how the rest ended; kill stops a process by pid.""",
    "smarttasks": """ For work that must survive a crash of your session: jobs, their output and their exit codes are kept by the server, also over a restart of the server (calls fail while it is down); job_list with scope=all shows every job with its state and owner, also those of earlier sessions. A replacement session has the same agent id and owns its predecessor's jobs; you can also signal a process with kill in Bash.""",
}
TOOLS_EXTRA["native_sub"] = TOOLS_EXTRA["native"]
