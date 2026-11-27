"""Offline tests of experiment 6: plan, truth, program and grader."""

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
    e6 = importlib.import_module("exp6_train")
    pst_build = importlib.import_module("pst_build")
    pst_prompts = importlib.import_module("pst_prompts")
    verify = importlib.import_module("verify_e6")
    wl = importlib.import_module("wl_train")
finally:
    sys.path.remove(EXP)

SHORT = {"speed": 0.02, "ttl": 2.0, "down_ttl": 2.0}


@pytest.fixture(scope="module")
def tr():
    return e6.truth(3)


def put(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def team(ws, tr, dec, reports=True):
    for k in range(1, e6.LANES + 1):
        put(f"{ws}/.runs/l{k}.json", {"lane": k, "label": f"lane{k}", "t0": k, "t_last": 400,
                                      "dec": {b: [dec(w), 5.0] for b, w in tr["builds"].items()
                                              if w["lane"] == k}})
    put(ws + "/" + e6.ANSWER, {"builds": tr["builds"], "culprits": tr["culprits"]}
        if reports else {})


def right(w):
    return "promote" if w["reason"] == "OK" else "rollback"


def test_plan_is_seeded_balanced_and_culprits_unique(tr):
    assert e6.truth(3) == tr != e6.truth(4)
    assert len(tr["builds"]) == 160
    per = {}
    for w in tr["builds"].values():
        per.setdefault(e6.owner(w), []).append(w["reason"])
    assert len(per) == 4 and len({tuple(sorted(x)) for x in per.values()}) == 1
    parts = {b["id"]: set(b["parts"]) for p in e6.plan(3)[0].values() for b in p["builds"]}
    for k, c in tr["culprits"].items():
        inn = [parts[b] for b, w in tr["builds"].items() if w["reason"] == k]
        out = set().union(*[parts[b] for b, w in tr["builds"].items() if w["reason"] != k])
        assert set.intersection(*inn) - out == {c}


def test_independent_reader_agrees_with_truth(tr):
    naive_wrong = 0
    for k, p in e6.plan(3)[0].items():
        ln, raw = verify.Lane(), verify.Lane("errors")
        for b in p["builds"]:
            lines = [x for _, x in wl.bake(b)]
            assert not any(ln.feed(x) for x in lines)    # blips are no outages
            [raw.feed(x) for x in lines]
            if b["plan"] == "OUTAGE":
                continue
            assert ln.verdict() == {x: tr["builds"][b["id"]][x]
                                    for x in ("reason", "p95", "errors")}
            naive_wrong += (raw.verdict()["reason"] == "OK") != (b["plan"] == "OK")
    assert naive_wrong > 40


def test_size_and_pace():
    lanes = e6.plan(3)[0]
    size = sum(len(x) + 1 for k in (1, 2, 3) for b in lanes[k]["builds"] for _, x in wl.bake(b))
    assert 0.6e6 < size / 3 < 1.6e6                      # tens of MB over 40 lanes
    best = sum(e6.BAKE_S * .94 for _ in range(e6.BUILDS))
    worst = sum(e6.BAKE_S * 1.06 + e6.TTL_S for _ in range(e6.BUILDS))
    assert best > 200 and worst < 600


def test_grader(tmp_path, tr):
    ws = str(tmp_path)
    assert e6.grade(ws, tr)["score"] == 0
    team(ws, tr, right)
    assert e6.grade(ws, tr)["score"] == 1.0
    for const in ("promote", "rollback"):                # constant answers earn nothing
        team(ws, tr, lambda w: const)
        assert e6.grade(ws, tr)["parts"]["decisions"] == 0
    team(ws, tr, lambda w: "none" if w["seq"] == 1 else right(w), reports=False)
    g = e6.grade(ws, tr)
    assert .6 < g["parts"]["decisions"] < .9 and g["process"]["wrong"] == 0
    assert g["score"] == pytest.approx(.5 * g["parts"]["decisions"], abs=1e-3)
    put(ws + "/.runs/z.json", {"lane": 1, "label": "lane1", "t0": 9e9, "t_last": 9e9,
                               "dec": {b: ["promote", 1] for b in tr["builds"]}})
    assert e6.grade(ws, tr)["parts"] == g["parts"]       # a relaunch does not count
    for k in range(1, 41):                               # per-lane files are the fallback
        put(f"{ws}/answers/lane_{k}.json", {b: w for b, w in tr["builds"].items()
                                            if w["lane"] == k})
    assert e6.grade(ws, tr)["parts"]["reports"] == 1.0


def test_report_partial_credit_and_junk(tmp_path, tr):
    w = next(w for w in tr["builds"].values() if w["reason"] == "LATENCY")
    assert e6.report_score(w, w) == pytest.approx(1)
    assert e6.report_score(dict(w, p95=w["p95"] + 1), w) == pytest.approx(.7)
    assert e6.report_score(dict(w, reason="OK"), w) == pytest.approx(.6)
    for junk in (None, 3, [], {"p95": None, "errors": True}, {"reason": []}):
        assert e6.report_score(junk, w) == 0
    for junk in ([], {"builds": []}, {"builds": {"x": 1}, "culprits": [1]}, "x"):
        put(str(tmp_path) + "/" + e6.ANSWER, junk)
        assert e6.grade(str(tmp_path), tr)["score"] == 0


def test_program_accepts_decisions_and_expires(tmp_path):
    ws = str(tmp_path / "ws")
    e6.build(ws, 3, **SHORT)
    lanes = e6.plan(3, **SHORT)[0]
    k = next(k for k, p in lanes.items() if "OUTAGE" in [b["plan"] for b in p["builds"][1:]]
             and "OUTAGE" != p["builds"][0]["plan"])
    bs = lanes[k]["builds"]
    with open(f"{ws}/control/lane_{k}.txt", "w") as f:
        f.writelines(f"{b['id']} {'rollback' if b['plan'] != 'OK' else 'promote'}\n"
                     for b in bs[1:])                    # nothing for the first build
    t = time.time()
    r = subprocess.run([ws + "/bin/train", "--lane", str(k)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and time.time() - t < 40
    assert r.stdout.count("GATE closed") == 4 and r.stdout.count("result=expired") == 1
    rec, = pst_build.launches(ws)
    assert rec["done"] and rec["dec"][bs[0]["id"]][0] == "none"
    assert set(e6.decisions(ws, {b["id"]: 1 for b in bs})) == {b["id"] for b in bs[1:]}
    text = [x for x in r.stdout.splitlines()]
    assert [x for _, x in wl.bake(bs[0])] == text[1:1 + len(wl.bake(bs[0]))]


def test_workspace_is_opaque_and_prompt_is_shared(tmp_path):
    ws = str(tmp_path / "ws")
    e6.build(ws, 3, **SHORT)
    assert not [f for _, _, fs in os.walk(ws) for f in fs if f.endswith(".py")]
    img = open(ws + "/bin/img/lane1.img", "rb").read()
    assert b"OUTAGE" not in img and b"builds" not in img
    ids = {n: n for n, _ in pst_prompts.roster("6")}
    ps = {c: pst_prompts.agent_prompt("6", c, "w1", "/w", ids, 900) for c in pst_prompts.TOOLS}
    assert len({p.split("\nTools. ")[0] for p in ps.values()}) == 1
    assert not hasattr(e6, "TOOLS_EXTRA")


def test_files_stay_small():
    for f in ("exp6_train.py", "wl_train.py", "verify_e6.py"):
        assert len(open(os.path.join(EXP, f)).readlines()) < 400
