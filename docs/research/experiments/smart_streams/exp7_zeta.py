"""Experiment 7: verify the Riemann hypothesis in eight bands. Plan, truth, grader."""

import bisect
import functools
import math
import os
import random

import ss_build
import zeta_ref as zr
from exp7_docs import TASK, ZETA  # noqa: F401  (TASK is part of the module interface)

NAME = "e7_zeta_campaign"
JOBS, PER_AGENT, BANDS, PARTS = 48, 12, 8, 6
LENGTH, DURATION, TIMEOUT = 3000, 300.0, 1200
REQ_S, REACT_S, STOP_S = 75.0, 120.0, 6.0
BACK = 150                                       # Gram intervals a restart may repeat
SLOT = [5200, 30000, 110000, 400000, 1100000, 3000000, 6200000, 11000000]
KINDS = ["NOISE", "TRUNC", "REMAINDER", "THETA"]
ANSWER = "answers/final.json"
WEIGHTS = {"refine": .25, "restart": .25, "diagnosis": .15, "counts": .15, "bands": .15,
           "campaign": .05}


def owner(rec):
    return f"w{(rec['job'] - 1) // PER_AGENT + 1}"


def plan(seed, length=LENGTH, duration=DURATION):
    """{job: params}; every agent gets the same mix of fault kinds, shuffled."""
    rng = random.Random(f"plan7:{seed}")
    out, at = {}, [int(s * rng.uniform(.97, 1.03)) for s in SLOT]
    grids, kinds = {}, {}
    for g in range(JOBS // PER_AGENT):
        ids = range(g * PER_AGENT + 1, (g + 1) * PER_AGENT + 1)
        mix = KINDS + [rng.choice(KINDS), "STUCK", "STUCK"] + [None] * 5
        ks = [4, 5, 6] * 4
        rng.shuffle(mix)
        rng.shuffle(ks)
        kinds.update(zip(ids, mix))
        grids.update(zip(ids, ks))
    for j in range(1, JOBS + 1):
        b, part = (j - 1) % BANDS, (j - 1) // BANDS
        n0, n = at[b], int(length * rng.uniform(.97, 1.03))
        at[b] += n
        par = {"NOISE": rng.choice((2, 3)), "TRUNC": rng.uniform(.90, .96),
               "THETA": rng.choice((-1, 1)) * rng.uniform(.14, .34)}
        fault = None
        if kinds[j]:
            fault = {"kind": kinds[j], "par": par.get(kinds[j], 0),
                     "n": n0 + int(n * rng.uniform(.14, .78))}
        out[j] = {"job": j, "band": b + 1, "part": part + 1, "n0": n0, "len": n, "k": grids[j],
                  "duration": duration * rng.uniform(.98, 1.04), "fault": fault, "pace": min(
                      1.0, duration / DURATION),
                  "bursts": {str(n0 + int(n * rng.uniform(.03, .97))): rng.randrange(1, grids[j])
                             for _ in range(rng.randint(5, 8))}}
    return out


# ── truth ────────────────────────────────────────────────────────────────────

def cell_counts(zeros, gs, i, k, f=1):
    """Zeros per grid cell of Gram interval i (index into gs)."""
    a, m = gs[i], k * f
    h = (gs[i + 1] - a) / m
    lo, hi = bisect.bisect_left(zeros, a), bisect.bisect_left(zeros, gs[i + 1])
    out = {}
    for z in zeros[lo:hi]:
        c = min(m - 1, int((z - a) / h))
        out[c] = out.get(c, 0) + 1
    return out


def hidden(zeros, gs, a, b, k, f=1):
    """Zeros in Gram intervals a..b-1 that sign changes on the grid do not show."""
    return sum(c - c % 2 for i in range(a, b) for c in cell_counts(zeros, gs, i, k, f).values())


def job_truth(p, zeros, good):
    n0, n1 = p["n0"], p["n0"] + p["len"]
    gs = [zr.gram(n) for n in range(n0, n1 + 1)]
    ends = [i for i in range(len(gs)) if good[n0 + i]]
    short = {}
    for a, b in zip(ends, ends[1:]):
        if hidden(zeros, gs, a, b, p["k"]):
            need = next((f for f in range(2, 65) if not hidden(zeros, gs, a, b, p["k"], f)), 65)
            short[n0 + a, n0 + b] = need
    return {"zeros": bisect.bisect_left(zeros, gs[-1]) - bisect.bisect_left(zeros, gs[0]),
            "missed": hidden(zeros, gs, 0, len(gs) - 1, p["k"]),
            "gram_failures": sum(not good[n] for n in range(n0, n1)),
            "short": short, "good": {n0 + i for i in ends}, "p": p}


def band_truth(n0, n1, zeros, good):
    g0, g1 = zr.gram(n0), zr.gram(n1)
    inside = [z for z in zeros if g0 <= z < g1]
    ds = [(zr.spacing(a, b), a) for a, b in zip(inside, inside[1:])]
    mean = sum(d for d, _ in ds) / len(ds)
    ends = [n for n in range(n0, n1 + 1) if good[n]]
    length = max(b - a for a, b in zip(ends, ends[1:]))
    lo = ends[0]                                        # S = 0 at a good end
    base = bisect.bisect_left(zeros, zr.gram(lo))
    s = [bisect.bisect_left(zeros, zr.gram(n)) - base - (n - lo) for n in range(n0, n1 + 1)]
    return {"zeros": len(inside), "closest": sorted(ds)[:3],
            "variance": sum((d - mean) ** 2 for d, _ in ds) / len(ds),
            "longest_block": length, "block_starts": [a for a, b in zip(ends, ends[1:])
                                                      if b - a == length],
            "S_plus": sum(x > 0 for x in s), "S_minus": sum(x < 0 for x in s), "spacings": ds}


@functools.lru_cache(maxsize=4)
def _truth(seed, kw):
    pl, jobs, bands, all_ds, border = plan(seed, **dict(kw)), {}, {}, [], []
    for b in range(1, BANDS + 1):
        ps = sorted((p for p in pl.values() if p["band"] == b), key=lambda p: p["part"])
        n0, n1 = ps[0]["n0"], ps[-1]["n0"] + ps[-1]["len"]
        zeros, good = zr.survey(n0, n1)
        for p in ps:
            jobs[str(p["job"])] = job_truth(p, zeros, good)
        t = bands[str(b)] = band_truth(n0, n1, zeros, good)
        ds = t.pop("spacings")
        all_ds += ds
        for p in ps[1:]:
            i = bisect.bisect_left(zeros, zr.gram(p["n0"]))
            border.append((zr.spacing(zeros[i - 1], zeros[i]), zeros[i - 1]))
    return {"jobs": jobs, "bands": bands,
            "campaign": {"ks": zr.ks_gue([d for d, _ in all_ds]), "closest": min(all_ds),
                         "border": min(border)}}


def truth(seed, **kw):
    return _truth(seed, tuple(sorted(kw.items())))


def build(ws, seed, **kw):
    ss_build.install(ws, "zeta", "wl_zeta",
                     {f"job{j}.img": p for j, p in plan(seed, **kw).items()})
    os.makedirs(os.path.join(ws, "control"), exist_ok=True)
    ss_build.write(ws, "docs/ZETA.md", ZETA)


# ── grading ──────────────────────────────────────────────────────────────────

def _num(x):
    try:
        v = float(str(x).strip())
        return v if math.isfinite(v) else math.nan
    except (TypeError, ValueError):
        return math.nan


def near(x, want, full, half):
    d = abs(_num(x) - want)
    return 1.0 if d <= full else .5 if d <= half else 0.0


def refine_job(t, recs):
    """(credit, false requests) over the job's deficient blocks."""
    p, seen, wrong = t["p"], {}, set()
    bad_from = p["fault"]["n"] if p["fault"] else math.inf
    for r in recs:
        for a, b, f, late, faulty in r.get("reqs", []):
            if faulty or (not r.get("safe") and b > bad_from):
                continue                                # Z was wrong there: not judged
            unsure = not r.get("safe") and b == bad_from
            hit = [x for x in t["short"] if a <= x[0] and x[1] <= b <= x[1] + 2 and b - a
                   <= x[1] - x[0] + 2]
            for x in hit:
                if late <= REQ_S:
                    seen[x] = max(seen.get(x, 0), f)
            if not hit and not unsure:
                wrong.add((a, b))
    return sum(.7 + .3 * (f >= t["short"][x]) for x, f in seen.items()), len(wrong), len(seen)


def restart_job(p, recs, end):
    """(credit for the reaction to the job's fault, unnecessary interventions)."""
    if not recs:
        return 0.0, 0
    first, f = recs[0], p["fault"]
    t = first.get("fault_t")
    credit, false = 0.0, 0
    for r in recs[1:]:
        if not r.get("safe"):
            false += 1
        elif t is None or r["t0"] < t:
            false += 1
        elif f and not credit and r["t0"] - t <= REACT_S and r["from"] <= f["n"]:
            stopped = first["t_last"] <= t + REACT_S + STOP_S
            lived = r["done"] or r["t_last"] >= end - 5
            credit = (stopped and lived) * (1.0 if r["from"] >= f["n"] - BACK else .5)
    if t is None and not first["done"] and first["t_last"] < end - 5 and len(recs) == 1:
        false += 1                                      # a healthy job was stopped
    return credit, false


def grade(ws, tr, meta=None):
    """Six parts (WEIGHTS); one block, job or band item is worth at most .02."""
    end = (meta or {}).get("run_end", float("inf"))
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    top = ans if isinstance(ans, dict) else {}

    def section(name):
        v = top.get(name)
        return v if isinstance(v, dict) else {}
    by = {}
    for r in ss_build.launches(ws):
        by.setdefault(str(r.get("job")), []).append(r)
    got = wrong = hits = n_short = false = n_fault = claims = 0
    react = diag = counts = bands = 0.0
    for j, t in tr["jobs"].items():
        p, recs = t["p"], by.get(j, [])
        c, w, h = refine_job(t, recs)
        got, wrong, hits, n_short = got + c, wrong + w, hits + h, n_short + len(t["short"])
        c, w = restart_job(p, recs, end)
        react, false, n_fault = react + c, false + w, n_fault + bool(p["fault"])
        a = section("jobs").get(j)
        if not isinstance(a, dict):
            a = ss_build.read_json(os.path.join(ws, f"answers/job_{j}.json"))
        if not isinstance(a, dict) or not recs:
            continue
        counts += sum(near(a.get(k), t[k], 0, w) * s for k, w, s in
                      (("zeros", 0, .35), ("missed", 0, .35), ("gram_failures", 0, .3)))
        kind = str(a.get("config") or "OK").strip().upper()
        claims += kind != "OK"
        if p["fault"] and kind == p["fault"]["kind"]:
            diag += .6 + .4 * near(a.get("onset"), p["fault"]["n"], 2, 30)
        elif p["fault"] and kind != "OK":
            diag += .2 * (near(a.get("onset"), p["fault"]["n"], 2, 30) > 0)
    for b, t in tr["bands"].items():
        a = section("bands").get(b)
        if not isinstance(a, dict):
            continue
        xs = a.get("closest")
        xs = [x for x in xs[:3] if isinstance(x, (list, tuple)) and len(x) == 2] \
            if isinstance(xs, list) else []
        pairs = sum(any(near(x[0], g, .002, .002) and near(x[1], d, d * .02, d * .02)
                        for x in xs) for d, g in t["closest"]) / 3
        block = a.get("longest_block")
        block = block if isinstance(block, (list, tuple)) and len(block) == 2 else (None, None)
        start, length = _num(block[0]), _num(block[1])
        bands += (.4 * pairs + .25 * near(a.get("variance"), t["variance"], .003 * t["variance"],
                                          .02 * t["variance"])
                  + .2 * (length == t["longest_block"]) * (.5 + .5 * (start in t["block_starts"]))
                  + .15 * (.5 * (_num(a.get("S_plus")) == t["S_plus"])
                           + .5 * (_num(a.get("S_minus")) == t["S_minus"])))
    c, t = section("campaign"), tr["campaign"]
    camp = .4 * near(c.get("ks"), t["ks"], .0003, .0015)
    for k, w in (("closest", .2), ("border", .4)):
        x = c.get(k)
        if isinstance(x, (list, tuple)) and len(x) == 2:
            camp += w * (near(x[0], t[k][1], .002, .002) and near(x[1], t[k][0], .02 * t[k][0],
                                                                  .02 * t[k][0]))
    parts = {"refine": 2 * got / (n_short + hits + wrong) if n_short + hits + wrong else 1.0,
             "restart": max(0.0, react - false) / n_fault,
             "diagnosis": 2 * diag / (n_fault + claims), "counts": counts / len(tr["jobs"]),
             "bands": bands / len(tr["bands"]), "campaign": camp}
    parts = {k: round(min(1.0, v), 4) for k, v in parts.items()}
    return {"score": round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4), "parts": parts,
            "process": {"blocks_short": n_short, "blocks_in_time": hits, "wrong_requests": wrong,
                        "faults": n_fault, "restarts_credit": round(react, 2),
                        "unneeded_interventions": false, "claims": claims,
                        "jobs_done": sum(any(r["done"] for r in x) for x in by.values())},
            "final_valid": isinstance(ans, dict)}
