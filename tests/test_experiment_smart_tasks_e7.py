"""Offline tests of experiment 7: reference mathematics, program, truth, grader."""

import copy
import importlib
import json
import math
import os
import re
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_tasks")
sys.path.insert(0, EXP)
try:
    e7 = importlib.import_module("exp7_zeta")
    pst_build = importlib.import_module("pst_build")
    pst_prompts = importlib.import_module("pst_prompts")
    pst_run = importlib.import_module("run")
    verify = importlib.import_module("verify_e7")
    wl = importlib.import_module("wl_zeta")
    zr = importlib.import_module("zeta_ref")
finally:
    sys.path.remove(EXP)

FAST = {"length": 100, "duration": 5.0}


# ── the reference against known mathematics ──────────────────────────────────

def test_first_zeros_and_counting_function():
    known = [14.134725141734693, 21.022039638771555, 25.010857580145688, 30.424876125859513,
             32.935061587739190, 37.586178158825671]
    for z in known:                                     # Euler-Maclaurin, exact
        assert abs(zr.Z_em(z)) < 1e-9
        assert zr.Z_em(z - 1e-3) * zr.Z_em(z + 1e-3) < 0
    assert abs(zr.gram(0) - 17.8455995405) < 1e-7 and abs(zr.gram(126) - 282.4547208) < 1e-6
    zeros, good = zr.survey(-1, 10200)                  # the first bad Gram point is 126
    assert [n for n in range(400) if not good[n]] == [126, 134, 195, 211, 232, 254, 288, 367, 377,
                                                      379, 397]
    assert all(abs(a - b) < 2e-3 for a, b in zip(zeros, known))     # RS is crude this low
    assert [sum(z < t for z in zeros) for t in (100, 1000, 10000)] == [29, 649, 10142]
    assert abs(zeros[9999] - 9877.782654005501) < 1e-8
    i = next(i for i, z in enumerate(zeros) if z > 7005)            # Lehmer's pair
    assert abs(zeros[i] - 7005.062866175) < 1e-7 and abs(zeros[i + 1] - 7005.100564674) < 1e-7


@pytest.mark.parametrize("t", [5000.5, 7005.0825, 20000.77, 41000.2])
def test_three_evaluations_agree(t):
    exact = zr.Z_em(t)
    assert abs(zr.Z(t) - exact) < 1e-8 and abs(wl.Evaluator(t).z(t) - exact) < 1e-7
    assert abs(verify.my_z(t) - exact) < 5e-4           # the formula in the docs


def test_zero_count_at_great_height_needs_the_whole_band_range():
    assert max(e7.SLOT) * 1.03 + 8 * e7.LENGTH * 1.1 < zr.ROSSER_LIMIT
    with pytest.raises(ValueError):
        zr.survey(zr.ROSSER_LIMIT, zr.ROSSER_LIMIT + 5)
    for n in (400000, 11200000):
        zeros, good = zr.survey(n, n + 60)
        a, b = min(good), max(good)
        assert len(zeros) == b - a and zeros == sorted(zeros)
        ev = wl.Evaluator(zeros[-1] + 10)
        assert all(ev.z(z - 1e-5) * ev.z(z + 1e-5) < 0 for z in zeros)
        assert abs(wl.gram(n) - zr.gram(n)) < 1e-7


def test_gue_statistics():
    assert abs(zr.gue_cdf(50) - 1) < 1e-12 and zr.gue_cdf(0) == 0
    h = 1e-4
    for s in (.3, 1.0, 1.9):
        pdf = 32 / math.pi ** 2 * s * s * math.exp(-4 * s * s / math.pi)
        assert abs((zr.gue_cdf(s + h) - zr.gue_cdf(s - h)) / (2 * h) - pdf) < 1e-6
    assert verify.cdf(.7) == pytest.approx(zr.gue_cdf(.7))


# ── plan, program ────────────────────────────────────────────────────────────

def test_plan_is_seeded_and_equal_per_agent():
    assert e7.plan(3) == e7.plan(3) != e7.plan(4)
    for seed in range(4):
        pl, per, bands = e7.plan(seed), {}, {}
        for j, p in pl.items():
            f = p["fault"]
            per.setdefault(e7.owner({"job": j}), []).append(f["kind"] if f else "OK")
            bands.setdefault(p["band"], []).append(p)
            assert not f or p["n0"] < f["n"] < p["n0"] + p["len"]
        assert len(per) == 4 and {sum(k == "OK" for k in x) for x in per.values()} == {5}
        assert all(set(e7.KINDS) <= set(x) and x.count("STUCK") == 2 for x in per.values())
        for ps in bands.values():
            assert all(a["n0"] + a["len"] == b["n0"] for a, b in zip(ps, ps[1:])) and len(ps) == 6
        assert all(x["n0"] < 6707 < x["n0"] + 18000 for x in (pl[1],))


def scan(p, safe):
    return [x for _, x in wl.Scan(p, p["n0"], safe).items() if isinstance(x, str)]


def test_program_shows_exactly_what_the_truth_says():
    tr = e7.truth(3, **FAST)
    for j in ("1", "2", "20", "8", "24"):
        t, lines = tr["jobs"][j], None
        p = dict(t["p"], bursts={})
        lines = scan(p, True)
        assert sum(x[0] == "R" for x in lines) == t["zeros"] - t["missed"]
        bad = 0
        for x in lines:
            m = re.match(r"G n=(\d+) t=\S+ Z=(\S+)", x)
            if m and int(m[1]) < p["n0"] + p["len"]:
                bad += (float(m[2]) > 0) == bool(int(m[1]) % 2)
        assert bad == t["gram_failures"]
        assert t["missed"] >= 2 * len(t["short"])
    assert any(t["short"] for t in tr["jobs"].values())


def test_evaluator_faults_start_at_their_onset_and_can_be_told_apart():
    right, total = 0, 0
    for j, p in e7.plan(11, **FAST).items():
        f = p["fault"]
        if not f or f["kind"] == "STUCK":
            continue
        p = dict(p, bursts={})
        good, bad = scan(p, True), scan(p, False)
        cut = next(i for i, x in enumerate(good) if x.startswith(f"G n={f['n']} "))
        assert good[:cut] == bad[:cut] and good[cut:] != bad[cut:]
        xs = [tuple(float(v.split("=")[1]) for v in x.split()[-2:]) for x in bad[cut:]
              if x[0] in "GS"][:25]
        total += 1
        right += verify.classify(xs) == f["kind"]
    assert right >= total - 1 and total == 20


def wait_for(path, text, limit=20):
    end = time.time() + limit
    while time.time() < end:
        s = open(path).read()
        if text in s:
            return s
        time.sleep(0.1)
    raise AssertionError(open(path).read()[-500:])


def test_program_refines_on_request_and_repairs_a_stuck_job(tmp_path):
    ws = str(tmp_path / "ws")
    e7.build(ws, 3, **FAST)
    tr = e7.truth(3, **FAST)["jobs"]
    sh = {j: t for j, t in tr.items() if t["short"] and not t["p"]["fault"]}
    j = min(sh, key=lambda k: min(sh[k]["short"])[1] - sh[k]["p"]["n0"])
    (a, b), need = min(sh[j]["short"].items())
    log = str(tmp_path / "log")
    with open(log, "w") as f:
        p = subprocess.Popen([ws + "/bin/zeta", "--job", j], stdout=f, cwd=ws)
    wait_for(log, f"G n={b} ")
    with open(ws + f"/control/job_{j}.cmd", "w") as f:
        f.write(f"refine {a} {b} {need}\nnonsense\nrefine {a} {b + 400} 4\n")
    assert p.wait(40) == 0
    s = open(log).read()
    assert "not understood" in s and f"refine {a} {b} {need} end" in s
    assert len(re.findall(r"^Q ", s, re.M)) == b - a
    k = next(j for j, t in tr.items() if (t["p"]["fault"] or {}).get("kind") == "STUCK")
    n = tr[k]["p"]["fault"]["n"]
    early = subprocess.run([ws + "/bin/zeta", "--job", k, "--safe", "--from", str(n)],
                           capture_output=True, text=True)
    assert early.returncode == 1 and "already printed" in early.stderr
    with open(log, "w") as f:
        p = subprocess.Popen([ws + "/bin/zeta", "--job", k], stdout=f, cwd=ws)
    wait_for(log, f"B n={n} it=40 ", 40)
    p.terminate()
    p.wait()
    r = subprocess.run([ws + "/bin/zeta", "--job", k, "--safe", "--from", str(n)],
                       capture_output=True, text=True, timeout=40)
    assert r.returncode == 0 and "\nEND " in r.stdout
    by = {}
    for x in pst_build.launches(ws):
        by.setdefault(str(x["job"]), []).append(x)
    assert e7.restart_job(tr[k]["p"], by[k], math.inf) == (1.0, 0)
    assert e7.refine_job(tr[j], by[j])[:2] == (1.0, 0)


# ── grader ───────────────────────────────────────────────────────────────────

def perfect(tr):
    jobs = {}
    for j, t in tr["jobs"].items():
        f = t["p"]["fault"]
        jobs[j] = {k: t[k] for k in ("zeros", "missed", "gram_failures")}
        jobs[j].update(config=f["kind"] if f else "OK", onset=f["n"] if f else None)
    bands = {b: dict({k: t[k] for k in ("variance", "S_plus", "S_minus")},
                     closest=[[g, d] for d, g in t["closest"]],
                     longest_block=[t["block_starts"][-1], t["longest_block"]])
             for b, t in tr["bands"].items()}
    c = tr["campaign"]
    return {"jobs": jobs, "bands": bands,
            "campaign": {"ks": c["ks"], **{k: [c[k][1], c[k][0]] for k in ("closest", "border")}}}


def ledgers(tr):
    out = {}
    for j, t in tr["jobs"].items():
        f, reqs = t["p"]["fault"], [[a, b, 64, 20.0, False] for a, b in t["short"]]
        base = {"job": int(j), "label": f"z{j}", "from": t["p"]["n0"], "safe": False, "reqs": reqs,
                "t0": 1000.0, "t_last": 1300.0, "done": True, "lag": 0.0, "cpu": 1.0, "pid": 1}
        out[j] = [base]
        if f:
            first = dict(base, done=False, fault_t=1100.0, fault_n=f["n"], t_last=1150.0,
                         reqs=[r for r in reqs if r[1] <= f["n"]])
            out[j] = [first, dict(base, t0=1151.0, safe=True, t_last=1400.0, **{"from": f["n"] - 3},
                                  reqs=[r for r in reqs if r[1] > f["n"]])]
    return out


def score(tmp_path, tr, ans, led):
    ws = str(tmp_path / f"g{time.time_ns()}")
    os.makedirs(ws + "/.runs")
    os.makedirs(ws + "/answers")
    for j, recs in led.items():
        for i, r in enumerate(recs):
            with open(f"{ws}/.runs/z{j}-{i}.json", "w") as f:
                json.dump(r, f)
    if ans is not None:
        with open(ws + "/answers/final.json", "w") as f:
            json.dump(ans, f)
    return e7.grade(ws, tr, {"run_end": 1500.0})


def test_grader_accepts_the_truth_and_gives_partial_credit(tmp_path):
    tr = e7.truth(3, **FAST)
    ans, led = perfect(tr), ledgers(tr)
    assert score(tmp_path, tr, ans, led)["score"] == 1.0
    g = score(tmp_path, tr, None, {})
    assert g["score"] == 0 and not g["final_valid"]
    # nothing done while the programs ran
    quiet = {j: [dict(r[0], reqs=[], done=True, t_last=1300.0)] for j, r in led.items()}
    g = score(tmp_path, tr, ans, quiet)
    assert g["parts"]["refine"] == g["parts"]["restart"] == 0 and g["score"] == pytest.approx(.5)
    # late and needless requests
    late = copy.deepcopy(led)
    for recs in late.values():
        for r in recs:
            r["reqs"] = [[a, b, f, 99.0, x] for a, b, f, _, x in r["reqs"]]
    assert score(tmp_path, tr, ans, late)["parts"]["refine"] == 0
    spam = copy.deepcopy(led)
    for j, recs in spam.items():
        n0 = tr["jobs"][j]["p"]["n0"]
        recs[0]["reqs"] = recs[0]["reqs"] + [[n, n + 1, 4, 5.0, False] for n in range(n0, n0 + 12)]
    assert score(tmp_path, tr, ans, spam)["parts"]["refine"] < .3
    # restarts: too late, from too far back, without need
    slow = copy.deepcopy(led)
    for recs in slow.values():
        for r in recs[1:]:
            r["t0"] = 1230.0
    assert score(tmp_path, tr, ans, slow)["parts"]["restart"] == 0
    eager = copy.deepcopy(led)
    for recs in eager.values():
        if len(recs) == 1:
            recs.append(dict(recs[0], safe=True, t0=1050.0))
    assert score(tmp_path, tr, ans, eager)["parts"]["restart"] < .5


def test_grader_claiming_everything_or_nothing_is_not_rewarded(tmp_path):
    tr = e7.truth(3, **FAST)
    led = ledgers(tr)
    for kind in ("OK", "NOISE", None, 7):
        ans = perfect(tr)
        for a in ans["jobs"].values():
            a.update(config=kind, onset=None)
        assert score(tmp_path, tr, ans, led)["parts"]["diagnosis"] < .25
    for junk in ([], {"jobs": [], "bands": 3}, {"jobs": {"1": 5}, "bands": {"1": {"closest": [1]}},
                                                "campaign": {"border": ["x", None]}}):
        g = score(tmp_path, tr, junk, led)
        assert 0 <= g["score"] <= .51


def test_prompts_are_the_same_in_all_arms_and_registered():
    assert pst_prompts.EXPS["7"] is e7 and pst_run.parse_args(["--out", "/tmp/x", "--exp", "7"])
    ids = {n: n for n, _ in pst_prompts.roster("7")}
    heads = {pst_prompts.agent_prompt("7", c, "w2", "/ws", ids, e7.TIMEOUT).split("Tools. ")[0]
             for c in pst_run.CONDITIONS}
    assert len(heads) == 1 and e7.TASK in heads.pop()
    assert not re.search(r"job_|pst|smarttask|nohup|\btail\b|monitor|sub-agent",
                         (e7.TASK + e7.ZETA).replace("job_J", "").lower())


def test_workspace_is_opaque(tmp_path):
    ws = str(tmp_path / "ws")
    e7.build(ws, 5, **FAST)
    for base, _, files in os.walk(ws):
        assert not any(f.endswith(".py") for f in files)
    blob = open(ws + "/bin/img/job1.img", "rb").read()
    assert b"fault" not in blob and b"onset" not in blob
