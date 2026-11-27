"""Offline tests of experiment 11 (storm-surge ensemble): model, plan, truth, grader."""

import copy
import importlib
import json
import math
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_tasks")
sys.path.insert(0, EXP)
try:
    e11 = importlib.import_module("exp11_surge")
    plan11 = importlib.import_module("exp11_plan")
    sc = importlib.import_module("exp11_score")
    pst_build = importlib.import_module("pst_build")
    pst_prompts = importlib.import_module("pst_prompts")
    pst_run = importlib.import_module("run")
    wl_swm = importlib.import_module("wl_swm")
finally:
    sys.path.remove(EXP)

SEED = 3


@pytest.fixture(scope="module")
def tr():
    return e11.truth(SEED)


def put_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def test_cfl_factor_is_the_leapfrog_c_grid_bound():
    assert wl_swm.cfl_factor(0.0) == 2.0
    for a in (0.05, 0.3, 0.8, 1.5, 3.0):
        brute = max(a * math.sin(t) + 2 * math.sin(t / 2)
                    for t in (math.pi * k / 20000 for k in range(20001)))
        assert wl_swm.cfl_factor(a) == pytest.approx(brute, rel=1e-6)
    assert all(wl_swm.cfl_factor(x / 10) < wl_swm.cfl_factor(x / 10 + .1) for x in range(30))


def test_plan_gives_every_agent_the_same_load(tr):
    pl = e11.plan(SEED)
    per = {}
    for m, r in pl["roles"].items():
        per.setdefault(e11.owner({"member": m}), []).append(r)
    assert len(per) == 4 and len({tuple(sorted(x)) for x in per.values()}) == 1
    assert e11.owner({"member": 0}) == "w1" and e11.owner({"member": 13}) == "w2"
    rng = __import__("random").Random(9)
    assert plan11._nature(rng, 1.0, plan11.T_END) != plan11._nature(rng, 1.0, plan11.T_END)
    assert sorted(pl["nature"]["stations"]) == pl["nature"]["stations"]


def test_truth_follows_the_planned_roles(tr):
    """Recomputed independently by integrating and reading printed values."""
    want = {"blowup": "unstable", "noncons": "nonconservative", "ok": "ok", "near": "ok"}
    for m, r in tr["roles"].items():
        got = tr["members"][m]
        assert got["status"] == want[r], (m, r, got)
        if r == "blowup":
            assert plan11.WINDOW[0] * 72 <= got["onset_h"] <= plan11.WINDOW[1] * 72 + .5
        if r == "noncons":
            assert 1e-7 < got["drift"] < 1e-1
    assert tr["n_valid"] == 40
    pr = tr["products"]
    assert sum(pr["rank_histogram"]) == 16 * 72 and len(pr["rank_histogram"]) == 41
    assert 0 < pr["q1"] < 1 and 0 < pr["q2"] <= 72 and pr["q4"] > 0
    assert all(x > 0 for x in pr["skill"]["rmse"] + pr["skill"]["spread"])


def test_near_members_stay_stable_and_blowups_are_cured_by_two_substeps():
    pl = e11.plan(SEED)
    _, roles, members, facts, _ = plan11._plan(SEED, plan11.DURATION, plan11.T_END)
    for m, r in roles.items():
        if r == "near":
            assert .95 <= facts[m]["peak"] <= .995 and facts[m]["onset"] is None
        if r == "blowup":
            assert facts[m]["onset"] is None and facts[m]["peak"] <= .95   # the K=2 run
            assert members[m]["n"] == pl["members"][m]["n"]


def test_program_output_matches_truth_and_ledger(tmp_path):
    ws = str(tmp_path / "ws dir")
    e11.build(ws, SEED, duration=1.5)
    pl = e11.plan(SEED, duration=1.5)
    files = [os.path.join(b, f) for b, _, fs in os.walk(ws) for f in fs]
    assert not [f for f in files if f.endswith(".py")]
    blob = b"".join(open(f, "rb").read() for f in files)
    imgs = b"".join(open(f, "rb").read() for f in files if "/img/" in f)
    assert b"h_dry" not in imgs and b"storm" not in imgs and len(imgs) > 1000
    assert b"blowup" not in blob and b"near" not in imgs and b"exp11" not in blob
    bl = next(m for m, r in pl["roles"].items() if r == "blowup")
    nc = next(m for m, r in pl["roles"].items() if r == "noncons")
    out = {}
    for key, args in {"b1": ["--member", str(bl)], "b2": ["--member", str(bl), "--substeps", "2"],
                      "nc": ["--member", str(nc)], "nat": ["--nature"]}.items():
        r = subprocess.run([ws + "/bin/swm", *args], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.rstrip().endswith(f"steps={r.stdout.count('step ')}")
        out[key] = r.stdout.splitlines()
    b1, b2 = sc.parse(out["b1"]), sc.parse(out["b2"])
    assert b1["complete"] and b1["onset_t"] is not None and any("nan" in x for x in out["b1"])
    assert b2["onset_t"] is None and b2["drift_max"] < 1e-10
    assert out["b2"] == list(sc.run_lines(pl["members"][bl], 2))           # deterministic
    assert sc.parse(out["nc"])["drift_last"] == pytest.approx(
        e11.truth(SEED)["members"][str(nc)]["drift"], rel=1e-12)
    recs = {(r["member"], r["substeps"]): r for r in pst_build.launches(ws)}
    first = next(x for x in out["b1"] if x.startswith("step ") and not float(
        x.split("cfl=")[1].split()[0]) <= 1.0)
    assert recs[bl, 1]["onset_step"] == int(first.split()[1]) and "onset" not in recs[bl, 2]
    assert all(r["done"] for r in recs.values()) and recs[0, 1]["label"] == "nature"
    assert pst_build.peak_streams(list(recs.values()), [], {}, e11.owner)["w1"] >= 1


def test_products_on_a_hand_example():
    t_end = 72 * 3600.0
    times = [1800.0 * k for k in range(145)]

    def run(v, field):
        return {"obs": {t: [v, v + 1] for t in times}, "field": field}
    valid = {1: run(1.0, [0.0, 2.0]), 2: run(2.0, [2.0, 2.0]), 3: run(3.0, [0.0, 0.0])}
    qs = {"stations": [4, 9], "q1": {"station": 9, "threshold": 2.5, "from_h": 1, "to_h": 2},
          "q2": {"station": 4}, "q4": {"threshold": 1.0, "lead_h": 3}}
    pr = sc.products(valid, run(2.5, None), qs, t_end)
    assert pr["skill"]["rmse"] == [pytest.approx(.5)] * 6
    assert pr["skill"]["spread"] == [pytest.approx(1.0)] * 6
    assert pr["rank_histogram"] == [0, 0, 144, 0]
    assert pr["q1"] == pytest.approx(2 / 3) and pr["q2"] == 0.0 and pr["q3"] == 2
    assert pr["q4"] == pytest.approx(1.0)


def ideal_ledger(tr):
    recs = []
    for m, w in tr["members"].items():
        base = {"member": int(m), "label": f"member{m}", "lag": 0, "cpu": 0, "substeps": 1}
        if w["status"] == "unstable":
            recs.append(dict(base, t0=60.0, t_last=170.0, onset=150.0, done=False, ended=True))
            recs.append(dict(base, substeps=2, t0=171.0, t_last=330.0, done=True))
        else:
            recs.append(dict(base, t0=60.0, t_last=215.0, done=True))
    return recs


def write_runs(ws, recs):
    d = os.path.join(ws, ".runs")
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    for i, r in enumerate(recs):
        put_json(os.path.join(d, f"{i}.json"), r)


def test_grader(tmp_path, tr):
    ws, meta = str(tmp_path), {"phase1_start": 0.0}
    pr = {k: v for k, v in tr["products"].items() if k != "member_rmse"}
    perfect = dict(copy.deepcopy(pr), members=copy.deepcopy(tr["members"]))
    recs = ideal_ledger(tr)

    def score(ans, runs=recs):
        put_json(ws + "/answers/final.json", ans)
        write_runs(ws, runs)
        return e11.grade(ws, tr, meta)
    g = score(perfect)
    assert g["score"] == 1.0 and g["final_valid"] and g["process"]["stopped_in_time"] == 12
    unst = [m for m, w in tr["members"].items() if w["status"] == "unstable"]
    late = [dict(r, t_last=r["onset"] + 60, done=False) if r.get("onset") else r for r in recs]
    g = score(perfect, late)
    assert g["parts"]["operations"] == pytest.approx(1 - 12 * .5 / 48)
    slow = [dict(r, t_last=410.0) if r["substeps"] == 2 else r for r in recs]
    assert score(perfect, slow)["parts"]["operations"] == pytest.approx(1 - 12 * .5 / 48)
    extra = recs + [dict(recs[0], substeps=2)]
    assert score(perfect, extra)["parts"]["operations"] == pytest.approx(.98)
    lazy = copy.deepcopy(perfect)
    for m, w in lazy["members"].items():
        if w["status"] == "nonconservative":
            lazy["members"][m] = {"status": "ok", "onset_h": None, "drift": None}
        if w["status"] == "unstable":
            w["onset_h"] += 0.25
    g = score(lazy)
    assert g["parts"]["members"] == pytest.approx(1 - (8 + 12 * .3) / 48, abs=1e-3)
    off = copy.deepcopy(perfect)
    off["skill"]["rmse"] = [x * 1.03 for x in off["skill"]["rmse"]]
    off["q3"] = -1
    off["rank_histogram"][0] += 5
    g = score(off)
    assert g["parts"]["skill"] == .75 and g["questions"]["q3"] == 0 and g["parts"]["rank"] < 1
    one = {"members": {unst[0]: tr["members"][unst[0]]}}                  # per-member files
    for m, w in tr["members"].items():
        put_json(f"{ws}/answers/member_{m}.json", w)
    assert score(one)["parts"]["members"] == 1.0
    for m in tr["members"]:
        os.remove(f"{ws}/answers/member_{m}.json")
    write_runs(ws, [])
    (tmp_path / "answers" / "final.json").write_text('{"members": [1]}')
    assert e11.grade(ws, tr, meta)["score"] == 0
    (tmp_path / "answers" / "final.json").write_text('not json')
    assert e11.grade(ws, tr, meta)["score"] == 0


def test_registration_and_prompt():
    assert pst_prompts.EXPS["11"] is e11 and pst_prompts.roster("11")[0] == ("w1", 1)
    assert pst_run.parse_args(["--out", "/tmp/x", "--exp", "11"]).exps == ["11"]
    a, b = (pst_prompts.agent_prompt("11", c, "w2", "/w", {n: n for n in "w1 w2 w3 w4".split()},
                                    e11.TIMEOUT) for c in ("smarttasks", "native"))
    assert a.split("\nTools. ")[0] == b.split("\nTools. ")[0]
    assert "600 s" in b and "docs/FORECAST.md" in b


def test_e11_files_stay_small():
    for f in ("exp11_surge.py", "exp11_plan.py", "exp11_score.py", "wl_swm.py", "verify_e11.py"):
        assert len(open(os.path.join(EXP, f)).readlines()) < 400
