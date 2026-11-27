"""Offline tests of experiment 10 (colouring campaign): solvers, plan, program, grader."""

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
    e10 = importlib.import_module("exp10_coloring")
    exact = importlib.import_module("exp10_truth")
    W = importlib.import_module("wl_graphs")
    pst_build = importlib.import_module("pst_build")
    pst_prompts = importlib.import_module("pst_prompts")
    pst_run = importlib.import_module("run")
finally:
    sys.path.remove(EXP)

SEED, FAST = 1, 40.0


def small(seed, n=24, p=.5):
    edges = W.gnp(W.rng_for("t10", seed), n, p)
    return n, edges, W.adjacency(n, edges)


def test_solvers_agree_with_the_independent_truth():
    for seed in range(12):
        n, edges, adj = small(seed, 22 + seed % 6)
        omega, cl = exact.clique_number(n, edges)
        mine, _, _ = W.max_clique(adj)
        assert len(mine) == omega and W.is_clique(adj, mine) and W.is_clique(adj, cl)
        chi = exact.chromatic(n, edges, max(W.dsatur_greedy(adj)) + 1)
        assert exact.colourable(n, edges, chi - 1, cl) is None
        for prio in (None, list(range(n))):
            ctl = {}
            ev, _ = W.run_offline(W.bnb(adj, mine, ctl, prio))
            done = [e for _, e in ev if e[0] == "done"]
            assert done[0][1] == chi
            assert all(W.proper(edges, e[2]) for _, e in ev if e[0] == "inc")
            if done[0][2] == "exhausted":
                assert ctl["closed"] == pytest.approx(1.0)


def test_hints_prune_and_wrong_hints_are_refused():
    n, edges, adj = small(3, 30)
    cl = W.max_clique(adj)[0]
    chi = exact.chromatic(n, edges, max(W.dsatur_greedy(adj)) + 1)
    good = exact.colourable(n, edges, chi, cl)
    _, plain = W.run_offline(W.bnb(adj, cl, {}))
    ctl = {}
    ev, hinted = W.run_offline(W.bnb(adj, cl, ctl), ctl, 1, good)
    assert hinted <= plain and [e for _, e in ev if e[0] == "done"][0][1] == chi
    assert ("inc", chi, good, "hint") in [e for _, e in ev]
    a, b = edges[0]
    bad = list(good)
    bad[b] = bad[a]
    ctl = {}
    ev, _ = W.run_offline(W.bnb(adj, cl, ctl), ctl, 1, bad)
    assert ("reject", (a, b)) in [e for _, e in ev]


def test_heuristics_print_proper_colourings_unless_slack():
    n, edges, adj = small(5, 40)
    for gen in (W.tabucol(adj, W.rng_for(1), 2000, W.dsatur_greedy(adj)),
                W.anneal(adj, W.rng_for(2), 6000, W.dsatur_greedy(adj))):
        ev, _ = W.run_offline(gen)
        cols = [e for _, e in ev if e[0] == "col"]
        assert cols and all(W.proper(edges, e[2]) for e in cols)
        assert [e[1] for e in cols] == sorted((e[1] for e in cols), reverse=True)
        assert ev[-1][1][0] == "stuck"
    ev, _ = W.run_offline(W.anneal(adj, W.rng_for(2), 6000, W.dsatur_greedy(adj), slack=1))
    assert any(e[0] == "col" and not W.proper(edges, e[2]) for _, e in ev)


@pytest.fixture(scope="module")
def planned():
    return e10.plan(SEED, FAST), e10.truth(SEED, FAST)


def test_plan_is_seeded_balanced_and_true(planned):
    p, tr = planned
    assert p == e10.plan(SEED, FAST) and p["graphs"].keys() == tr["graphs"].keys()
    per = {}
    for g, sp in p["graphs"].items():
        per.setdefault(e10.owner({"graph": g}), []).append(sp["kind"])
        t = tr["graphs"][g]
        assert t["open"] == (sp["kind"] == "H") and t["lb"] <= t["ub"]
        assert t["omega"] == t["lb"] if sp["kind"] in "PH" else t["omega"] < t["lb"]
    assert all(sorted(k) == sorted(e10.KINDS) for k in per.values()) and len(per) == 4
    methods = [m for _, m in tr["rejected"]]
    assert methods.count("exact") == 2 and methods.count("anneal") == 4
    assert {e10.owner({"graph": g}) for g, m in tr["rejected"] if m == "anneal"} == \
        {"w1", "w2", "w3", "w4"}


def run(ws, g, m, seed=1, timeout=120):
    r = subprocess.run([ws + "/bin/gcol", "--graph", g, "--method", m, "--seed", str(seed)],
                       capture_output=True, text=True, cwd=ws, timeout=timeout)
    return r.returncode, r.stdout.splitlines()


def test_program_faults_hints_and_reruns(tmp_path, planned):
    p, tr = planned
    ws = str(tmp_path / "ws")
    e10.build(ws, SEED, FAST, window_s=e10.WINDOW_S * FAST)
    files = [os.path.join(b, f) for b, _, fs in os.walk(ws) for f in fs]
    assert not [f for f in files if f.endswith(".py")]
    g = next(g for g, sp in p["graphs"].items() if sp.get("misread"))
    n, edges, _ = W.make_graph(p["seed"], g, p["graphs"][g])
    adj, k = W.adjacency(n, edges), tr["graphs"][g]["lb"]
    rc, out = run(ws, g, "exact")
    last = [x for x in out if x.startswith("CLIQUE")][-1]
    assert rc == 0 and not W.is_clique(adj, [int(v) - 1 for v in last.split("v=")[1].split()]) and f"OPTIMAL chi={k + 1}" in out[-1]
    good = exact.colourable(n, edges, k, exact.clique_number(n, edges)[1])
    with open(f"{ws}/bounds/{g}.ub", "w") as f:
        f.write(" ".join(str(c + 7) for c in good))
    rc, out = run(ws, g, "exact", seed=2)
    cl = [x for x in out if x.startswith("CLIQUE")][-1]
    assert W.is_clique(adj, [int(v) - 1 for v in cl.split("v=")[1].split()])
    assert f"HINT accepted k={k}" in out and out[-1].startswith(f"OPTIMAL chi={k} ")
    m = next(g for g, sp in p["graphs"].items() if sp["kind"] == "M")
    n, edges, _ = W.make_graph(p["seed"], m, p["graphs"][m])
    chi = tr["graphs"][m]["lb"]
    with open(f"{ws}/bounds/{m}.ub", "w") as f:
        f.write(" ".join(map(str, exact.colourable(n, edges, chi, ()))))
    rc, out = run(ws, m, "exact")
    assert out[-1].startswith(f"OPTIMAL chi={chi} ") and "HINT accepted" in "\n".join(out)
    b = next(g for g, sp in p["graphs"].items() if sp.get("bug"))
    rc, out = run(ws, b, "anneal")
    n, edges, _ = W.make_graph(p["seed"], b, p["graphs"][b])
    cols = [[int(c) - 1 for c in x.split("c=")[1].split()] for x in out if x.startswith("COLORING")]
    assert rc == 0 and not all(W.proper(edges, c) for c in cols) and out[-1].startswith("STUCK")
    recs = {r["label"]: r for r in pst_build.launches(ws)}
    assert recs[f"{m}_exact_s1"]["inc"][0][2] == "hint" and recs[f"{m}_exact_s1"]["end"] == "optimal"
    assert any(not f[2] for f in recs[f"{b}_anneal_s1"]["found"])


def test_cap_and_window_are_enforced(tmp_path):
    ws = str(tmp_path)
    e10.build(ws, SEED, FAST)
    os.makedirs(ws + "/.runs", exist_ok=True)
    now = time.time()

    def ledger(label, **kw):
        rec = dict(label=label, t0=now, t_last=now, done=False, lag=0, cpu=0, pid=os.getpid())
        rec.update(kw)
        with open(f"{ws}/.runs/{label}.json", "w") as f:
            json.dump(rec, f)
    for i in range(e10.CAP):
        ledger(f"G0{1 + i % 8}_x{i}")
    assert run(ws, "G03", "tabu")[0] == 76
    ledger("G01_x0", t0=now - 10000, t_last=now - 9000, done=True)
    assert run(ws, "G12", "tabu")[0] == 77


def test_grader(tmp_path, planned):
    p, tr = planned
    ws = str(tmp_path)
    os.makedirs(ws + "/answers")
    gs = {}
    for g, t in tr["graphs"].items():
        n, edges, _ = W.make_graph(p["seed"], g, p["graphs"][g])
        _, cl = exact.clique_number(n, edges)
        col = exact.colourable(n, edges, t["ub"], cl) if not t["open"] else min(
            (e[2] for m in ("tabu", "anneal") for _, e in e10._heur(
                p["seed"], g, p["graphs"][g], W.adjacency(n, edges), m)
             if e[0] == "col" and W.proper(edges, e[2])), key=max)
        gs[g] = {"lb": t["lb"], "ub": max(col) + 1, "coloring": col,
                 "clique": [v + 1 for v in cl]}
    rej = [{"graph": g, "method": m} for g, m in tr["rejected"]]
    with open(ws + "/" + e10.ANSWER, "w") as f:
        json.dump({"graphs": gs, "rejected": rej}, f)
    g = e10.grade(ws, tr)
    assert g["parts"]["graphs"] == 1.0 and g["parts"]["rejected"] == 1.0
    assert g["parts"]["sharing"] == g["parts"]["cutoff"] == 0.0      # nothing ran
    one = next(iter(gs))
    gs[one]["coloring"][0] = gs[one]["coloring"][next(iter(W.adjacency(
        tr["graphs"][one]["n"], W.make_graph(p["seed"], one, p["graphs"][one])[1])[0]))]
    with open(ws + "/" + e10.ANSWER, "w") as f:
        json.dump({"graphs": gs, "rejected": rej[1:]}, f)
    g = e10.grade(ws, tr)
    assert g["detail"]["graphs"][one] == pytest.approx(.6)
    assert g["parts"]["rejected"] == pytest.approx(2 * 5 / 11, abs=1e-4)


def test_sharing_and_cutoff_measures(planned):
    _, tr = planned
    m = next(g for g, t in tr["graphs"].items() if t["kind"] == "M")
    h = next(g for g, t in tr["graphs"].items() if t["kind"] == "H")
    heur = {"graph": m, "method": "tabu", "t0": 100.0, "t_last": 200.0, "found": [
        [5, 12, True], [20, 11, True], [30, 10, False], [60, 10, True]]}
    ex = {"graph": m, "method": "exact", "seed": 1, "t0": 100.0, "t_last": 400.0,
          "inc": [[3, 12, "search"], [140, 11, "hint"]], "end": "limit"}
    got = e10.sharing([heur, ex], tr, set())
    assert got[m] == 0.0 and all(v == 0.0 for g, v in got.items() if g != m)
    ex["inc"][1][0] = 50
    assert e10.sharing([heur, ex], tr, set())[m] == 0.5
    assert e10.sharing([heur, ex], tr, {(m, 1)})[m] == 0.0
    cut = [{"graph": h, "method": "exact", "t0": 0.0, "t_last": d} for d in (100, 240)]
    assert e10.cutoff(cut, tr)[h] == pytest.approx(0.75)


def test_registration_and_prompt():
    assert pst_prompts.EXPS["10"] is e10
    assert pst_run.parse_args(["--out", "/tmp/x", "--exp", "10"]).exps == ["10"]
    a, b = (pst_prompts.agent_prompt("10", c, "w2", "/w", {f"w{i}": f"w{i}" for i in range(1, 5)},
                                    900) for c in ("smarttasks", "native"))
    assert a.split("\nTools. ")[0] == b.split("\nTools. ")[0] and "G09-G16" in a
