"""Experiment 10: colouring campaign. Plan, workspace builder, ground truth, grader."""

import functools
import os
import py_compile
import shlex
import stat
import sys
import tempfile

import exp10_truth
import pst_build
import wl_common
import wl_graphs as W

NAME = "e10_colouring_campaign"
N_GRAPHS, PER, CAP = 32, 8, 16
WINDOW_S, LIMIT_S, TIMEOUT = 420, 480, 1200
SHARE_S, CUT_OK_S, CUT_ZERO_S = 45, 150, 330
KINDS = ["P", "P", "E", "E", "M", "M", "H", "H"]
W_GRAPHS, W_REJECT, W_SHARE, W_CUT = .55, .15, .15, .15
ANSWER = "answers/final.json"


def owner(rec):
    return f"w{(int(rec['graph'][1:]) - 1) // PER + 1}"


def names():
    return [f"G{i:02d}" for i in range(1, N_GRAPHS + 1)]


def _best(events, valid_edges=None):
    """(k, colouring) of the last proper colouring among offline events."""
    out = None
    for _, ev in events:
        if ev[0] == "col" and (valid_edges is None or W.proper(valid_edges, ev[2])):
            out = (ev[1], ev[2])
    return out


def _heur(seed, name, spec, adj, method, slack=0):
    rng = W.rng_for("run", seed, name, method, 1)
    start = W.dsatur_greedy(adj)
    gen = (W.tabucol(adj, rng, spec["maxit"]["tabu"], start) if method == "tabu" else
           W.anneal(adj, rng, spec["maxit"]["anneal"], start, slack=slack))
    return W.run_offline(gen)[0]


def _bnb(adj, clique, limit, hint=None, at=1, closed=None):
    ctl = {}
    ev, units = W.run_offline(W.bnb(adj, clique, ctl), ctl, at, hint, limit)
    done = next((e[1] for _, e in ev if e[0] == "done"), None)
    if closed is not None:
        closed.append(ctl["closed"])
    return done, units


def _candidate(rng, seed, name, kind, salt):
    """One spec of the given kind, or None if it does not qualify."""
    spec = {"kind": kind, "salt": salt, "p": .5,
            "maxit": {"tabu": 6000, "anneal": 20000},
            "rate": {"tabu": rng.uniform(50, 70), "anneal": rng.uniform(160, 220)}}
    if kind == "P":
        spec.update(n=rng.randint(85, 110), k=rng.randint(7, 9), p=rng.uniform(.33, .45))
    else:
        spec["n"] = rng.randint(*{"E": (44, 52), "M": (50, 60), "H": (68, 76)}[kind])
    n, edges, _ = W.make_graph(seed, name, spec)
    adj = W.adjacency(n, edges)
    clique = W.max_clique(adj)[0]
    tabu = _heur(seed, name, spec, adj, "tabu")
    k_t, col_t = _best(tabu)
    t_found = next(u for u, e in tabu if e[0] == "col" and e[1] == k_t) / spec["rate"]["tabu"]
    if kind == "P":
        if k_t != spec["k"] or t_found > 60:
            return None
        spec["rate"]["exact"] = rng.uniform(60, 150)
        return spec, {"chi": k_t}
    if kind == "E":
        done, n0 = _bnb(adj, clique, 40000)
        if done is None or not 1500 <= n0 <= 30000:
            return None
        spec["rate"]["exact"] = n0 / rng.uniform(50, 110)
        return spec, {"chi": done}
    if kind == "M":
        done, n1 = _bnb(adj, clique, 30000, col_t)
        if done != k_t or t_found > 35:
            return None
        r = n1 / rng.uniform(45, 90)
        if not 15 <= r <= 300:
            return None
        if _bnb(adj, clique, int(1.2 * LIMIT_S * r))[0] is not None:
            return None
        if _bnb(adj, clique, int(220 * r), col_t, int(45 * r))[0] is None:
            return None
        spec["rate"]["exact"] = r
        return spec, {"chi": done}
    anneal = _heur(seed, name, spec, adj, "anneal")
    k_a, col_a = _best(anneal, edges) or (n, None)
    target, col = (k_t, col_t) if k_t <= k_a else (k_a, col_a)
    r = rng.uniform(60, 120)
    if target <= len(clique) + 1:
        return None
    share = []                          # hopeless beyond doubt: little closed by then
    if _bnb(adj, clique, int(1.5 * LIMIT_S * r), col, closed=share)[0] is not None \
            or share[0] > .03:
        return None
    spec["rate"]["exact"] = r
    return spec, {"target": target, "omega": len(clique)}


def _misread(seed, name, spec):
    """An extra edge that closes a (k+1)-clique, if a run that reads it ends
    with a wrong optimum soon."""
    n, edges, clique = W.make_graph(seed, name, spec)
    adj = W.adjacency(n, edges)
    for c in clique:
        for u in range(n):
            if u not in clique and u not in adj[c] and all(x in adj[u] for x in clique if x != c):
                bad = W.adjacency(n, edges + [(min(u, c), max(u, c))])
                cl = W.max_clique(bad)[0]
                if _bnb(bad, cl, int(200 * spec["rate"]["exact"]))[0] == spec["k"] + 1:
                    return [min(u, c), max(u, c)]
    return None


def _bug_ok(seed, name, spec):
    n, edges, _ = W.make_graph(seed, name, spec)
    ev = _heur(seed, name, spec, W.adjacency(n, edges), "anneal", slack=1)
    return any(e[0] == "col" and not W.proper(edges, e[2]) for _, e in ev)


@functools.lru_cache(maxsize=8)
def _plan(seed, speed):
    rng = W.rng_for("plan10", seed)
    gseed = rng.randrange(2**40)
    graphs, facts, order = {}, {}, names()
    for g in range(N_GRAPHS // PER):
        kinds = KINDS[:]
        rng.shuffle(kinds)
        for name, kind in zip(order[g * PER:(g + 1) * PER], kinds):
            for salt in range(500):
                got = _candidate(rng, gseed, name, kind, salt)
                if got:
                    graphs[name], facts[name] = got
                    break
            else:
                raise RuntimeError(f"no {kind} graph for {name}")
        mine = order[g * PER:(g + 1) * PER]
        for name in rng.sample([x for x in mine if graphs[x]["kind"] != "P"], PER - 2):
            if _bug_ok(gseed, name, graphs[name]):
                graphs[name]["bug"] = 1
                break
        else:
            raise RuntimeError("no anneal fault")
    ps = [x for x in order if graphs[x]["kind"] == "P"]
    rng.shuffle(ps)
    hit = 0
    for name in ps:
        bad = hit < 2 and _misread(gseed, name, graphs[name])
        if bad:
            graphs[name].update(misread=bad, misread_seeds=[1])
            hit += 1
    if hit < 2:
        raise RuntimeError("no misread")
    p = {"seed": gseed, "graphs": graphs, "per": PER, "cap": CAP, "window_s": WINDOW_S,
         "limit_s": LIMIT_S, "speed": speed}
    return wl_common.unseal(wl_common.seal(p)), wl_common.unseal(wl_common.seal(facts))


def plan(seed, speed=1.0):
    """The image of the program (a fresh copy)."""
    return wl_common.unseal(wl_common.seal(_plan(seed, speed)[0]))


@functools.lru_cache(maxsize=8)
def _truth(seed, speed):
    p, facts = _plan(seed, speed)
    out, rejected = {}, []
    for name, spec in p["graphs"].items():
        n, edges, planted = W.make_graph(p["seed"], name, spec)
        omega, _ = exp10_truth.clique_number(n, edges)
        f = facts[name]
        if spec["kind"] == "H":
            assert omega == f["omega"]
            out[name] = {"lb": omega, "ub": f["target"], "open": True, "omega": omega}
        else:
            chi = spec["k"] if planted else exp10_truth.chromatic(n, edges, f["chi"])
            assert chi == f["chi"] and (omega == chi) == bool(planted)
            out[name] = {"lb": chi, "ub": chi, "open": False, "omega": omega}
        out[name].update(kind=spec["kind"], n=n)
        if spec.get("bug"):
            rejected.append([name, "anneal"])
        if spec.get("misread"):
            rejected.append([name, "exact"])
    return {"graphs": out, "rejected": sorted(rejected), "gseed": p["seed"],
            "specs": p["graphs"]}


def truth(seed, speed=1.0):
    return wl_common.unseal(wl_common.seal(_truth(seed, speed)))


def install(ws, prog, modules, images):
    """pst_build.install for a program made of several modules."""
    bindir = os.path.join(ws, "bin")
    os.makedirs(os.path.join(bindir, "img"), exist_ok=True)
    for d in ("answers", "board", "bounds", "graphs"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    src = ""
    for name in ("wl_common",) + modules:
        with open(os.path.join(pst_build.HERE, name + ".py"), encoding="utf-8") as f:
            src += "\n" + "\n".join(x for x in f.read().splitlines()
                                    if not x.startswith(("from wl_common import",
                                                         "from wl_graphs import"))) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        s = os.path.join(tmp, prog + ".py")
        with open(s, "w", encoding="utf-8") as f:
            f.write(src)
        py_compile.compile(s, cfile=os.path.join(bindir, prog + ".pyc"), dfile=prog, optimize=2,
                           doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    for name, params in images.items():
        with open(os.path.join(bindir, "img", name), "wb") as f:
            f.write(wl_common.seal(params))
    wrapper = os.path.join(bindir, prog)
    with open(wrapper, "w") as f:
        f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                f"{shlex.quote(os.path.join(bindir, prog + '.pyc'))} \"$@\"\n")
    os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)


def build(ws, seed, speed=1.0, window_s=WINDOW_S):
    """``speed`` > 1 runs everything faster (tests); the window stays window_s / speed."""
    p = plan(seed, speed)
    p["window_s"] = window_s
    install(ws, "gcol", ("wl_graphs", "wl_gcol"), {"campaign.img": p})
    for name, spec in p["graphs"].items():
        n, edges, _ = W.make_graph(p["seed"], name, spec)
        pst_build.write(ws, f"graphs/{name}.col", W.dimacs(n, edges, name))
    pst_build.write(ws, "docs/CAMPAIGN.md", DOC.replace("@CAP@", str(CAP))
                   .replace("@WIN@", str(WINDOW_S)).replace("@LIM@", str(LIMIT_S)))


def _ints(x):
    try:
        return [int(v) for v in x] if isinstance(x, list) else None
    except (TypeError, ValueError):
        return None


def graph_score(a, t, edges):
    """lb .35; ub .40 (value right and backed by a proper colouring with at most
    ub colours); clique .25 (a clique of size omega, vertex ids from 1)."""
    a = a if isinstance(a, dict) else {}
    lb, ub = _ints([a.get("lb")]), _ints([a.get("ub")])
    col, clq = _ints(a.get("coloring")), _ints(a.get("clique"))
    s = .35 * (lb == [t["lb"]])
    if ub and col and len(col) == t["n"] and len(set(col)) <= ub[0] and \
            W.proper(edges, col) and (ub[0] <= t["ub"] if t["open"] else ub[0] == t["ub"]):
        s += .40
    if clq and len(clq) == t["omega"] and all(1 <= v <= t["n"] for v in clq):
        s += .25 * W.is_clique(W.adjacency(t["n"], edges), [v - 1 for v in clq])
    return s


def sharing(recs, tr, misread):
    """Per non-hopeless graph: share of new best proper colourings (found while an
    exact run had a worse incumbent) that reached that run within SHARE_S."""
    per = {}
    for g, t in tr["graphs"].items():
        if t["kind"] == "H":
            continue
        ex = [r for r in recs if r.get("graph") == g and r.get("method") == "exact"
              and (g, r.get("seed")) not in misread]
        finds = sorted((f[0] + r["t0"], f[1]) for r in recs if r.get("graph") == g
                       and r.get("method") != "exact" for f in r.get("found", []) if f[2])
        n = met = 0
        best = 10**9
        for t_abs, k in finds:
            if k >= best:
                continue
            best = k
            for x in ex:
                inc = [(i[0] + x["t0"], i[1]) for i in x.get("inc", [])]
                if not x["t0"] <= t_abs <= x["t_last"] or \
                        min([b for a, b in inc if a <= t_abs], default=10**9) <= k:
                    continue
                ok = any(a <= t_abs + SHARE_S and b <= k for a, b in inc)
                if not ok and x["t_last"] < t_abs + SHARE_S and not x.get("end"):
                    continue                    # stopped before the deadline: no event
                n, met = n + 1, met + ok
        per[g] = met / n if n else float(bool(ex))
    return per


def cutoff(recs, tr):
    per = {}
    for g, t in tr["graphs"].items():
        if t["kind"] != "H":
            continue
        xs = [r["t_last"] - r["t0"] for r in recs if r.get("graph") == g
              and r.get("method") == "exact"]
        per[g] = sum(max(0.0, min(1.0, (CUT_ZERO_S - d) / (CUT_ZERO_S - CUT_OK_S)))
                     for d in xs) / len(xs) if xs else 0.0
    return per


def grade(ws, tr, meta=None):
    """Graphs .55, rejected runs (F1) .15, bound sharing .15, cut-offs .15."""
    ans = pst_build.read_json(os.path.join(ws, ANSWER))
    a = ans if isinstance(ans, dict) else {}
    gs = a.get("graphs") if isinstance(a.get("graphs"), dict) else {}
    per = {}
    for g, t in tr["graphs"].items():
        n, edges, _ = W.make_graph(tr["gseed"], g, tr["specs"][g])
        per[g] = graph_score(gs.get(g), t, edges)
    want = {tuple(x) for x in tr["rejected"]}
    got = {(str(x.get("graph")).strip().upper(), str(x.get("method")).strip().lower())
           for x in (a.get("rejected") if isinstance(a.get("rejected"), list) else [])
           if isinstance(x, dict)}
    rej = 2 * len(want & got) / (len(want) + len(got)) if want | got else 1.0
    recs = pst_build.launches(ws)
    mis = {(g, s) for g, sp in tr["specs"].items() for s in sp.get("misread_seeds", [])}
    sh, cu = sharing(recs, tr, mis), cutoff(recs, tr)
    parts = {"graphs": sum(per.values()) / len(per), "rejected": rej,
             "sharing": sum(sh.values()) / len(sh), "cutoff": sum(cu.values()) / len(cu)}
    score = (W_GRAPHS * parts["graphs"] + W_REJECT * rej + W_SHARE * parts["sharing"]
             + W_CUT * parts["cutoff"])
    return {"score": round(score, 4), "parts": {k: round(v, 4) for k, v in parts.items()},
            "detail": {"graphs": per, "sharing": sh, "cutoff": cu, "rejected_got": sorted(got)},
            "final_valid": isinstance(ans, dict)}


DOC = """# Colouring campaign

Thirty-two graphs G01..G32 are in graphs/ in DIMACS format (`p edge n m`, one `e u v`
line per edge, vertices 1..n). For each, find the chromatic number chi, or the best
bounds that can be certified when chi is out of reach.

`bin/gcol --graph G --method M [--seed S]` runs one solver on one graph (S >= 1,
default 1) and prints a trace on standard output. Every run is deterministic given
graph, method and seed, and for the exact solver also the hints it accepted and when.

* `exact`: computes a maximum clique, then runs DSATUR branch and bound for colourings
  with fewer colours than its incumbent. The seed changes its tie-breaking only.
* `tabu`: tabu search (TabuCol) with a fixed number of colours k, k decreasing.
* `anneal`: simulated annealing with a fixed k, k decreasing.

| Line | Meaning |
|---|---|
| `CLIQUE size=s v=...` | a clique (vertex ids), each larger than the last; its size is a lower bound |
| `LB s source=clique` | lower bound used by the search |
| `BRANCH n=.. d=.. v=.. c=..` | sampled search nodes |
| `PROGRESS t=.. nodes=.. lb=.. ub=.. closed=f` | every 5 s; f = share of the search tree already decided |
| `INCUMBENT k=.. source=search|hint` | the exact solver's best colouring so far |
| `COLORING k=.. c=c1 c2 ...` | a colouring with k colours: the colour of vertex 1, 2, ..., n |
| `HINT accepted|rejected|not better|unreadable` | what became of an outside colouring |
| `OPTIMAL chi=.. reason=bound|exhausted` | proof: ub = lb, or no colouring with ub-1 colours |
| `LIMIT ...` | the exact solver's time limit (@LIM@ s) is over; nothing was proved |
| `TRY k=..`, `IT ...`, `SA ...`, `STUCK k=.. best=..` | heuristic progress; STUCK ends the run |

Facts you may rely on:
* The share closed counts a leaf at depth d below nodes with b1..bd branches as
  1/(b1...bd); it can grow unevenly. Each graph's exact solver works at a fixed
  speed in nodes per second.
* The exact solver checks bounds/G.ub about once a second. If the file holds a
  proper colouring (n integers, the colour of vertex 1..n, separated by white space)
  with fewer colours than its incumbent, it adopts it at once and prunes the search
  with it. Write the file under another name and rename it.
* Some runs report wrong certificates (a colouring with a conflict, a clique with a
  missing edge), and their claims that rest on such errors are wrong. An error belongs
  to one run (graph, method and seed); other runs are not affected. Certificates can
  always be checked against graphs/.

Limits: at most @CAP@ running solvers per owner (more are refused, exit 76). Launches
are refused @WIN@ s after the team's first launch (exit 77).
"""

TASK = """Run a graph-colouring campaign on thirty-two graphs. Read docs/CAMPAIGN.md first.

For every graph, race the exact solver against tabu from the start, and also run anneal once. Keep 8 to 16 solvers running while you have work. While they run:
- share bounds: whenever a run prints a proper colouring with fewer colours than a running exact run of that graph has, hand it to that run within 45 s;
- cut hopeless exact runs: an exact run that cannot finish within its time limit must be stopped, at the latest 150 s after its start; exact runs that can finish must be allowed to;
- trust no certificate you have not checked against the graph.
Ownership: w1 = G01-G08; w2 = G09-G16; w3 = G17-G24; w4 = G25-G32. Only run solvers on your own graphs. Publish your results (bounds, certificates, rejected runs) in board/.
w1 also combines everything, waiting for the others, into answers/final.json: {"graphs": {"G01": {"lb": int, "ub": int, "coloring": [colour of vertex 1..n], "clique": [vertex ids]}, ... all 32}, "rejected": [{"graph": "Gxx", "method": "exact|tabu|anneal"}, ...]}
lb is the best lower bound that a clique or a proof justifies, ub the number of colours of your best proper colouring, so lb = ub when chi is known; clique is a largest clique you have. rejected lists every graph and method of which a run printed a wrong certificate."""
