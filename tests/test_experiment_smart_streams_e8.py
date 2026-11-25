"""Offline tests of experiment 8 (phase transitions): physics, program, grader."""

import importlib
import itertools
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_streams")
sys.path.insert(0, EXP)
try:
    e8 = importlib.import_module("exp8_spins")
    an = importlib.import_module("e8_analysis")
    ss_build = importlib.import_module("ss_build")
    ss_prompts = importlib.import_module("ss_prompts")
    wl = importlib.import_module("wl_spins")
finally:
    sys.path.remove(EXP)

ISING = {"kind": "ising", "jx": 1.0, "jy": 1.0, "h": 0.0, "c": 0.0}
CASES = [(ISING, 4, 2.4), (dict(ISING, jy=1.6, h=.2, c=.7), 4, 3.2),
         ({"kind": "potts", "q": 3, "j": 1.0, "c": -.3}, 2, 1.0),
         ({"kind": "potts", "q": 12, "j": 1.0, "c": 0.0}, 2, .8)]


def enumerate_all(par, L, T):
    """Exact <E> and <m> by summing over every configuration."""
    q, n, z, e1, m1 = par.get("q", 2), L * L, 0.0, 0.0, 0.0
    right = [y * L + (x + 1) % L for y in range(L) for x in range(L)]
    down = [(y + 1) % L * L + x for y in range(L) for x in range(L)]
    for s in itertools.product(range(q), repeat=n):
        wx = sum(s[i] != s[right[i]] for i in range(n))
        wy = sum(s[i] != s[down[i]] for i in range(n))
        if q == 2:
            e = -par["jx"] * (n - 2 * wx) - par["jy"] * (n - 2 * wy) - par["h"] * (2 * sum(s) - n)
        else:
            e = -par["j"] * (2 * n - wx - wy)
        e += par["c"] * n
        b = math.exp(-(e - par["c"] * n) / T)
        z, e1 = z + b, e1 + b * e
        m1 += b * (q * max(map(s.count, set(s))) / n - 1) / (q - 1)
    return e1 / z, m1 / z


@pytest.mark.parametrize("par,L,T", CASES)
def test_simulation_agrees_with_full_enumeration(par, L, T):
    exact, reps = enumerate_all(par, L, T), 256
    for kill in (False, True):
        m = wl.make(par, L, [T] * reps, random.Random(11), "hot", reps)
        sums = [[0.0] * reps, [0.0] * reps]
        for i in range(3000):
            m.sweep(kill)
            if i >= 200:
                for s, x in zip(sums, m.measure()):
                    for r in range(reps):
                        s[r] += x[r] / 2800
        for s, want in zip(sums, exact):
            off = abs(statistics.mean(s) - want) / (statistics.stdev(s) / reps ** .5)
            assert off > 8 if kill else off < 4


def test_measure_matches_a_naive_count():
    par = {"kind": "potts", "q": 5, "j": 1.5, "c": .25}
    m = wl.make(par, 8, [2.0] * 8, random.Random(2), "hot")
    for _ in range(5):
        m.sweep()
    es, ms = m.measure()
    for r in range(8):
        s = [sum((p >> (r * 64 + i) & 1) << b for b, p in enumerate(m.planes)) for i in range(64)]
        same = sum((s[y * 8 + x] == s[y * 8 + (x + 1) % 8]) + (s[y * 8 + x] == s[(y + 1) % 8 * 8 + x])
                   for y in range(8) for x in range(8))
        assert max(s) < 5 and es[r] == pytest.approx(16 - 1.5 * same)
        assert ms[r] == pytest.approx((5 * max(map(s.count, set(s))) / 64 - 1) / 4)
    before = es[:]
    wl.swap(m, 1, 6)
    assert [m.measure()[0][i] for i in (1, 6, 0)] == [before[i] for i in (6, 1, 0)]


def test_coin_and_lattice():
    lat = wl.Lattice(8)
    draw, rng = wl.coin(lat, [.1] * 4 + [.7] * 3 + [1.0]), random.Random(1)
    got = [0] * 8
    for _ in range(400):
        got = [a + b for a, b in zip(got, lat.counts(draw(rng.getrandbits)))]
    assert [round(x / 25600, 1) for x in got] == [.1] * 4 + [.7] * 3 + [1.0]
    s = rng.getrandbits(lat.size)
    for f, g in ((lat.right, lat.left), (lat.down, lat.up)):
        assert f(g(s)) == s == g(f(s)) and f(s) != s


def test_exact_results():
    assert e8.ising_tc(1, 1) == pytest.approx(2 / math.log(1 + math.sqrt(2)))
    assert e8.potts_tc(2, 2) == pytest.approx(e8.ising_tc(1, 1))
    assert e8.latent_heat(10, 1) == pytest.approx(.69605, abs=1e-4)
    assert e8.latent_heat(5, 1) == pytest.approx(.0529, abs=1e-3)


def test_exchange_questions_follow_the_rule_and_are_balanced():
    rng, ts, yes = random.Random(4), wl.temperatures(2.2), 0
    for _ in range(400):
        es = [rng.gauss(-400, 30) for _ in ts]
        i, j, u, right = wl.question(rng, es, ts, (1, 6))
        d = (1 / ts[i] - 1 / ts[j]) * (es[i] - es[j])
        assert i < 4 <= j and not {i, j} & {1, 6} and right == (u < math.exp(min(d, 0)))
        assert abs(math.log(u) - d) > .02
        yes += right
    assert 120 < yes < 300


def test_plan():
    assert e8.plan(5) == e8.plan(5) != e8.plan(6)
    for seed in range(5):
        pl, tr = e8.plan(seed), e8.truth(seed)
        assert len(pl) == 48 and len(tr["nodes"]) == 16 and len(tr["samples"]) == 8
        assert all(1.5 < t["Tc"] < 2.8 for t in tr["samples"].values() if t["Tc"])
        for p in pl.values():
            stuck = [e for e in p["events"].values() if "replica" in e]
            assert len(stuck) == 1 and set(p["events"]) == {"1", "2"}
            if p["fault"]:
                assert stuck[0]["replica"] not in sum(p["fault"]["pairs"], [])


def collect(p, launch=1, L=8, t=2.5, rows=200, reset=False, seed_io=None):
    out, ev = an.Run(p["node"], launch, "A", L, t), []
    p = dict(p, io=dict(seed_io or {}))
    for f, x in wl.simulate(p, launch, "A", L, t, "hot", rows=rows, sweeps=4):
        if isinstance(x, str):
            out.feed(x)
        else:
            ev.append(x)
            if x[0] == "STUCK" and reset:
                p["io"]["reset"] = True
    return out, ev


def image(fault=None, events=None):
    kinds = {"kind": fault, "eps": .08, "pairs": [[0, 2], [5, 7]], "delay": 9}
    return {"seed": 3, "node": 1, "team": list(range(1, 13)), "events": events or {},
            "fault": kinds if fault else None, "samples": {"A": ISING}}


def test_simulation_is_a_function_of_arguments_and_answers():
    a, b = (collect(image())[0].rows for _ in range(2))
    assert a == b
    assert collect(image(), launch=2)[0].rows != a
    ask = {"1": {"ask_row": .3}}
    c, d = (collect(image(events=ask), seed_io={"swap": x})[0].rows for x in (True, False))
    assert c[0][:60] == d[0][:60] and c != d


@pytest.mark.parametrize("reset", (False, True))
def test_stuck_replica_resumes_only_after_a_reset(reset):
    ev = {"1": {"stuck_row": .2, "replica": 5}}
    run, events = collect(image(events=ev), reset=reset)
    assert [x[0] for x in events] == ["STUCK", "BACK"]
    own = run.rows[5]
    back = 40 + int(200 * wl.TTL_S / wl.RUN_S)
    assert len({x[:2] for x in own[40:back]}) == 1
    assert (len({x[:2] for x in own[back:]}) > 5) == reset
    cut = an.Run(1, 1, "A", 8, 2.5)
    cut.rows = [x[:70] for x in run.rows]
    assert [r for r in range(8) if cut.stuck(r)] == [5]
    assert len(run.data(5, 0)) < len(run.data(4, 0)) - 100


def test_frozen_lattice_gets_no_stuck_event():
    p = image(events={"1": {"stuck_row": .2, "replica": 5}})
    p["samples"] = {"A": dict(ISING, jx=9, jy=9)}
    out = [x[0] for f, x in wl.simulate(dict(p, io={}), 1, "A", 8, 1.0, "cold", rows=200, sweeps=2)
           if not isinstance(x, str)]
    assert out == []


def test_reference_analysis_names_the_faults():
    runs = []
    for node, k in enumerate([None, None, "THERMAL", None, "BALANCE", "OVERLAP", None, "FILTER",
                              None, None], 1):
        p = dict(image(k), node=node, seed=node, io={})
        run = an.Run(node, 1, "A", 16, 3.0)
        for _, x in wl.simulate(p, 1, "A", 16, 3.0, "hot", rows=300, sweeps=30):
            run.feed(x)
        runs.append(run)
    assert an.faults(runs) == {3: "THERMAL", 5: "BALANCE", 6: "OVERLAP", 8: "FILTER"}


# ── program and grader ───────────────────────────────────────────────────────

def test_program_refusals_and_ledger(tmp_path):
    ws = str(tmp_path)
    e8.build(ws, 9)
    assert not [f for f in os.listdir(ws + "/bin") if f.endswith(".py")]
    cmd = ["bin/mc", "--node", "1", "--sample", "A", "--L", "8", "--T", "2.0"]
    p = subprocess.Popen(cmd, cwd=ws, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().startswith("# node=1 launch=1 sample=A L=8 start=hot T_low=2.0")
        time.sleep(1.5)
        for bad, why in ((cmd, "busy"), (cmd[:4] + ["C"] + cmd[5:], "runs A and B only"),
                         (cmd[:8] + ["9"], "outside"),):
            r = subprocess.run(bad, cwd=ws, capture_output=True, text=True, timeout=20)
            assert r.returncode == 1 and why in r.stderr
    finally:
        p.terminate()
        p.wait(10)
        p.stdout.close()
    recs = ss_build.launches(ws)
    assert len(recs) == 1 and recs[0]["launch"] == 1 and e8.owner(recs[0]) == "w1"
    name = [f for f in os.listdir(ws + "/.runs") if f.endswith(".json")][0]
    with open(f"{ws}/.runs/{name}", "w") as f:
        json.dump(dict(recs[0], t0=time.time() - 1000), f)
    r = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "closed" in r.stderr


def perfect(tr):
    s = {k: dict(t, Tc_err=t["Tc"] and .01 * t["Tc"]) for k, t in tr["samples"].items()}
    return {"samples": s, "nodes": dict(tr["nodes"])}


def ledgers(ws, tr, **kw):
    os.makedirs(ws + "/.runs", exist_ok=True)
    for n in range(1, 49):
        for k in (1, 2):
            due = tr["stuck"][str(n)] == k
            rec = {"t0": n + k, "t_last": 99, "lag": 0, "done": True, "node": n, "launch": k,
                   "answer": True, "asked": True, "wrong": 0, "due": due,
                   "stuck": 3 if due else None, "reset": 5.0 if due else None}
            with open(f"{ws}/.runs/n{n}-{k}.json", "w") as f:
                json.dump(dict(rec, **kw), f)


def grade(ws, ans, tr):
    ss_build.write(ws, e8.ANSWER, json.dumps(ans))
    return e8.grade(ws, tr)


def test_grader(tmp_path):
    ws, tr = str(tmp_path), e8.truth(3)
    ledgers(ws, tr)
    ans = perfect(tr)
    g = grade(ws, ans, tr)
    assert g["score"] == 1.0 and g["final_valid"]
    assert grade(ws, dict(ans, nodes={}), tr)["score"] == .75
    every = {str(n): "THERMAL" for n in range(1, 49)}
    assert grade(ws, dict(ans, nodes=every), tr)["parts"]["nodes"] < .4
    kinds = {n: "filter" if k != "FILTER" else "THERMAL" for n, k in tr["nodes"].items()}
    assert grade(ws, dict(ans, nodes=kinds), tr)["parts"]["nodes"] == .5
    for junk in ({}, [], {"samples": [], "nodes": 3}, {"samples": {"A": 5}, "nodes": {"x": None}}):
        assert grade(ws, junk, tr)["score"] == pytest.approx(.25)
    ledgers(ws, tr, answer=False)
    assert grade(ws, ans, tr)["parts"]["swaps"] == 0
    ledgers(ws, tr, answer=None, reset=None)
    assert grade(ws, ans, tr)["score"] == .75
    ledgers(ws, tr, wrong=1)
    assert grade(ws, ans, tr)["parts"]["resets"] == 0


def test_sample_scores():
    t = {"order": "second", "Tc": 2.0, "inv_nu": 1.2}
    a = {"order": "second", "Tc": 2.01, "Tc_err": .02, "inv_nu": 1.2}
    assert e8.sample_score(a, t) == pytest.approx(1)
    assert e8.sample_score(dict(a, Tc_err=.004), t) == pytest.approx(.9)
    assert e8.sample_score(dict(a, Tc_err=1.0), t) == pytest.approx(.9)
    assert e8.sample_score(dict(a, Tc=2.05), t) == pytest.approx(1 - .35 / 2 - .1)
    assert .7 < e8.sample_score(dict(a, inv_nu=1.4), t) < .95
    assert e8.sample_score(dict(a, order="first"), t) == pytest.approx(.45)
    assert e8.sample_score({"Tc": "nan", "inv_nu": None}, t) == 0
    none = {"order": "none", "Tc": None}
    assert e8.sample_score(none, none) == 1 and e8.sample_score({}, none) == 0
    assert e8.sample_score({"order": "none", "Tc": 2.0}, none) == .6
    first = {"order": "first", "Tc": 2.0, "latent_heat": 1.5}
    assert e8.sample_score(dict(first, Tc_err=.01), first) == pytest.approx(1)
    assert e8.sample_score(dict(first, Tc_err=.01, latent_heat=3), first) == pytest.approx(.75)


def test_prompt_is_neutral():
    assert "8" in ss_prompts.EXPS
    ids = {n: n for n, _ in ss_prompts.roster("8")}
    ps = {c: ss_prompts.agent_prompt("8", c, "w1", "/w", ids, 900) for c in ss_prompts.TOOLS}
    assert len({p.split("\nTools. ")[0] for p in ps.values()}) == 1
    for word in ("pss", "smartstreams", "monitor", "nohup", "job_", "sub-agent"):
        assert word not in (e8.TASK + e8.MC).lower()
