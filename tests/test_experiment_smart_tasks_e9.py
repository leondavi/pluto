"""Offline tests of experiment 9 (sequencing cohort): plan, program, analysis, grader."""

import importlib
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_tasks")
sys.path.insert(0, EXP)
try:
    e9 = importlib.import_module("exp9_cohort")
    A = importlib.import_module("e9_analysis")
    wl_seq = importlib.import_module("wl_seq")
    pst_build = importlib.import_module("pst_build")
    pst_prompts = importlib.import_module("pst_prompts")
    pst_run = importlib.import_module("run")
finally:
    sys.path.remove(EXP)

SMALL = {"reads": 4000, "duration": 2.0, "duration2": 1.5}


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("e9") / "ws")
    e9.build(d, 3, **SMALL)
    return d


def gt(pl, s):
    return [sum(int(h[k]) for h in pl["persons"][pl["dna"][s]]) for k in range(len(pl["sites"]))]


def put_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def test_plan_is_seeded_and_equal_per_agent():
    assert e9.plan(3) == e9.plan(3) and e9.plan(3)["dna"] != e9.plan(4)["dna"]
    for seed in range(4):
        pl, tr = e9.plan(seed), e9.truth(seed)
        for g in range(4):
            mine = range(g * 12 + 1, g * 12 + 13)
            assert sum(s in tr["contaminated"] for s in mine) == 2
            assert sum(str(s) in tr["degraded"] for s in mine) == 1
        assert sorted(tr["relatives"].values()) == sorted(
            ["duplicate"] + ["parent_offspring"] * 4 + ["sibling"] * 2)
        assert [v["individual"] for v in tr["samples"].values()].count(None) == 1
        assert len(set(pl["manifest"].values())) == 48 and len(pl["array"]) == 48
        assert len(pl["report"]) == e9.N_REPORT and len(tr["frequencies"]) == e9.N_REPORT
        for s in tr["contaminated"]:
            src = pl["source"][s]
            assert src not in tr["contaminated"] and str(src) not in tr["degraded"]
            assert tr["samples"][str(s)]["contaminant"] == src
        assert all(0.06 <= pl["cont"][s] <= 0.18 for s in tr["contaminated"])


def test_no_item_is_worth_more_than_two_percent():
    assert sum(e9.WEIGHTS.values()) == pytest.approx(1)
    assert e9.WEIGHTS["calls"] / (48 * 16) < .02 and e9.WEIGHTS["identity"] / 48 < .02
    assert e9.WEIGHTS["relatives"] / 7 < .025 and e9.WEIGHTS["reactions"] / 12 < .02
    assert e9.WEIGHTS["contamination"] * .4 / 8 < .01


def test_program_is_deterministic_and_aligned(ws):
    pl = e9.plan(3, **SMALL)
    run = lambda *a: subprocess.run([ws + "/bin/seqrun", "--sample", "5", *a], capture_output=True,
                                    text=True, timeout=60)
    r1, r2 = run(), run()
    assert r1.returncode == 0 and r1.stdout == r2.stdout
    lines = r1.stdout.splitlines()
    recs = [x.split("\t") for x in lines if x[0] not in "@#"]
    assert len(recs) == SMALL["reads"] and lines[0].startswith("@HD")
    assert [int(x[0].rsplit(":", 1)[1]) for x in recs] == list(range(1, SMALL["reads"] + 1))
    ref, sites, _ = wl_seq.panel(pl["images"][5]["panel"])
    truth, seen = gt(pl, 5), 0
    for x in recs:
        if int(x[1]) & 4:
            continue
        pos, cig = int(x[3]), x[5]
        m = int(cig.split("M")[0].split("S")[-1])
        assert 1 <= pos and pos + m - 1 <= len(ref) and x[12].startswith("XM:Z:")
        for t in ([] if x[12] == "XM:Z:-" else x[12][5:].split(",")):
            p, b, q = t.split(":")
            assert pos <= int(p) < pos + m and ref[int(p) - 1] != b
            seen += 1
    assert seen and run("--flowcell", "2").returncode == 2
    assert run("--from", "9").returncode == 2
    r3 = run("--flowcell", "2", "--from", "3001")
    assert r3.returncode == 0 and r3.stdout.count("\n") < r1.stdout.count("\n") / 3
    leds = [r for r in pst_build.launches(ws) if r["sample"] == 5]
    assert len(leds) == 3 and all(r["done"] for r in leds) and leds[-1]["fc"] == 2


def test_calls_from_the_output_match_the_simulation(ws):
    pl = e9.plan(3, **SMALL)
    P = A.Panel(ws)
    s = next(s for s in range(1, 49) if s not in pl["cont"] and s not in pl["onset"])
    d = A.RunData(P)
    for _, x in wl_seq.trace(pl["images"][s]):
        d.feed(x)
    g, t = A.call(d.counts(), P), gt(pl, s)
    ok = [a == b for a, b in zip(g, t) if a is not None]
    assert sum(ok) / len(ok) > .9                          # low depth at this size
    assert A.identity(g, P) == e9.truth(3, **SMALL)["samples"][str(s)]["individual"]


def test_degradation_and_contamination_are_visible(ws):
    pl = e9.plan(3, **SMALL)
    P = A.Panel(ws)
    s = next(iter(pl["onset"]))
    d = A.RunData(P)
    for _, x in wl_seq.trace(pl["images"][s]):
        d.feed(x)
    on = A.onset(d)
    assert on and abs(on[0] - pl["onset"][s]) <= 30 and on[2] > 10 * on[1]
    c = max(pl["cont"], key=pl["cont"].get)
    d = A.RunData(P)
    for _, x in wl_seq.trace(pl["images"][c]):
        d.feed(x)
    assert A.freemix(d.counts(), P) > .04 and A.onset(d) is None
    d2 = A.RunData(P)
    for _, x in wl_seq.trace(pl["images"][c], lib=2):
        d2.feed(x)
    assert A.freemix(d2.counts(), P) < .02


def answer_from_truth(tr):
    return {"samples": tr["samples"],
            "relatives": [{"samples": [int(x) for x in k.split("-")], "relation": v}
                          for k, v in tr["relatives"].items()],
            "allele_frequencies": tr["frequencies"]}


def ledger(ws, **rec):
    d = os.path.join(ws, ".runs")
    os.makedirs(d, exist_ok=True)
    now = time.time()
    rec = dict({"t0": now, "t_last": now + 100, "done": True, "lag": 0, "cpu": 1, "lib": 1, "fc": 1,
                "reads": 15000, "total": 15000}, **rec)
    put_json(os.path.join(d, f"s{rec['sample']}-{len(os.listdir(d))}.json"), rec)


def test_grader_partial_credit(tmp_path):
    tr = e9.truth(5)
    ws = str(tmp_path)
    put_json(os.path.join(ws, e9.ANSWER), answer_from_truth(tr))
    now = time.time()
    for s in range(1, 49):
        if s in tr["contaminated"]:
            ledger(ws, sample=s, done=False, reads=5000, t_last=now + 120)
            ledger(ws, sample=s, lib=2, t0=now + 121)
        elif str(s) in tr["degraded"]:
            ledger(ws, sample=s, done=False, onset_t=now + 100, t_last=now + 140)
            ledger(ws, sample=s, fc=2, t0=now + 141)
        else:
            ledger(ws, sample=s)
    meta = {"run_end": now + 1000}
    g = e9.grade(ws, tr, meta)
    assert g["score"] == 1.0 and g["final_valid"]
    lazy = answer_from_truth(tr)
    lazy["relatives"] = lazy["relatives"][:-1] + [{"samples": [1, 2], "relation": "sibling"}]
    s0 = str(tr["contaminated"][0])
    c0 = tr["samples"][s0]["contamination"]
    lazy["samples"] = json.loads(json.dumps(tr["samples"]))
    lazy["samples"][s0]["contamination"] = c0 + max(.02, .2 * c0) * 1.5
    put_json(os.path.join(ws, e9.ANSWER), lazy)
    g = e9.grade(ws, tr, meta)
    assert g["parts"]["contamination"] == pytest.approx(1 - .4 * .5 / 8, abs=1e-4)
    assert 0 < g["parts"]["relatives"] < 1 and g["score"] < 1
    ledger(ws, sample=next(s for s in range(1, 49) if s not in tr["contaminated"]
                           and str(s) not in tr["degraded"]), lib=2)
    assert e9.grade(ws, tr, meta)["parts"]["reactions"] == pytest.approx(1 - .5 / 12, abs=1e-4)
    os.remove(os.path.join(ws, e9.ANSWER))
    for s in (1, 2):
        put_json(os.path.join(ws, f"answers/sample_{s}.json"), tr["samples"][str(s)])
    g = e9.grade(ws, tr, meta)
    assert not g["final_valid"] and 0 < g["parts"]["calls"] < .1


def test_registration_and_prompt():
    assert pst_prompts.EXPS["9"] is e9
    p = pst_prompts.agent_prompt("9", "native", "w2", "/w", {n: n for n in "w1 w2 w3 w4".split()}, 900)
    assert "docs/SEQUENCING.md" in p and "smarttasks" not in p.lower()
    assert pst_run.parse_args(["--out", "/tmp/x", "--exp", "9"]).exps == ["9"]
    assert pst_run.parse_args(["--out", "/tmp/x"]).exps == pst_prompts.BLIND_EXPS


def test_files_stay_small():
    for f in ("exp9_cohort.py", "exp9_text.py", "wl_seq.py", "e9_analysis.py", "verify_e9.py"):
        assert len(open(os.path.join(EXP, f)).readlines()) < 400
    assert len(open(__file__).readlines()) < 400
