"""Experiment 8: phase transitions by parallel Monte Carlo. Plan, truth and grader."""

import math
import os
import random

import pst_build
import wl_spins

NAME = "e8_phase_transitions"
NODES, PER_AGENT, TIMEOUT = 48, 12, 1200
ANSWER = "answers/final.json"
SAMPLES = "ABCDEFGH"                                 # two per agent
KINDS = ["THERMAL", "BALANCE", "OVERLAP", "FILTER"]
W_SAMPLES, W_NODES, W_SWAP, W_RESET = .50, .25, .10, .15
TC_FULL, TC_ZERO, ERR_MAX = .010, .040, .025         # relative
NU_FULL, NU_ZERO = .08, .30                          # absolute
HEAT_FULL, HEAT_ZERO = .10, .35                      # relative


def owner(rec):
    return f"w{(rec['node'] - 1) // PER_AGENT + 1}"


def ising_tc(jx, jy):
    """Onsager: sinh(2 jx / T) sinh(2 jy / T) = 1."""
    lo, hi = 1e-2, 10 * (jx + jy)
    while hi - lo > 1e-12 * hi:
        t = (lo + hi) / 2
        a, b = min(2 * jx / t, 300), min(2 * jy / t, 300)
        lo, hi = (t, hi) if math.sinh(a) * math.sinh(b) > 1 else (lo, t)
    return (lo + hi) / 2


def potts_tc(q, j):
    return j / math.log(1 + math.sqrt(q))


def latent_heat(q, j):
    """Baxter's latent heat per site of the q-state Potts model, q > 4."""
    th = math.acosh(math.sqrt(q) / 2)
    out = 2 * j * (1 + 1 / math.sqrt(q)) * math.tanh(th / 2)
    for n in range(1, 200):
        out *= math.tanh(n * th) ** 2
    return out


def material(rng, what):
    tc, c = math.exp(rng.uniform(math.log(1.55), math.log(2.75))), rng.uniform(-3, 3)
    if what in ("ising", "field"):
        jx, jy = 1.0, rng.uniform(1.0, 2.2)
        if rng.random() < .5:
            jx, jy = jy, jx
        k = tc / ising_tc(jx, jy)
        h = rng.uniform(.14, .22) * tc / 2.27 if what == "field" else 0.0
        return {"kind": "ising", "jx": jx * k, "jy": jy * k, "h": h, "c": c}
    q = 3 if what == "potts3" else rng.choice((12, 14, 16))
    return {"kind": "potts", "q": q, "j": tc * math.log(1 + math.sqrt(q)), "c": c}


def facts(m):
    """What is exactly known about a material."""
    if m["kind"] == "ising":
        if m["h"]:
            return {"order": "none", "Tc": None}
        return {"order": "second", "Tc": ising_tc(m["jx"], m["jy"]), "inv_nu": 1.0}
    tc = potts_tc(m["q"], m["j"])
    if m["q"] == 3:
        return {"order": "second", "Tc": tc, "inv_nu": 1.2}
    return {"order": "first", "Tc": tc, "latent_heat": latent_heat(m["q"], m["j"])}


def plan(seed):
    """{node: image}"""
    rng = random.Random(f"plan8:{seed}")
    kinds = ["ising"] * 2 + ["potts3"] * 2 + ["first"] * 2 + ["field", rng.choice(["first", "field"])]
    while any(kinds[2 * i] == kinds[2 * i + 1] for i in range(4)):
        rng.shuffle(kinds)
    mats = {s: material(rng, k) for s, k in zip(SAMPLES, kinds)}
    sizes, bad = [3, 4, 4, 5], KINDS * 4
    rng.shuffle(sizes)
    rng.shuffle(bad)
    out = {}
    for g in range(4):
        ids = list(range(g * PER_AGENT + 1, (g + 1) * PER_AGENT + 1))
        faulty = {i: bad.pop() for i in rng.sample(ids, sizes[g])}
        first = set(rng.sample(ids, PER_AGENT // 2))     # stuck on launch 1, else 2
        for i in ids:
            f = None
            if i in faulty:
                lo, hi = rng.sample(range(wl_spins.HALF), 2), rng.sample(range(4, 8), 2)
                f = {"kind": faulty[i], "eps": rng.choice((-1, 1)) * rng.uniform(.06, .09),
                     "pairs": [lo, hi], "delay": rng.randint(5, 40)}
            copies = [b for _, b in f["pairs"]] + [a for a, _ in f["pairs"]] if f else []
            ev = {k: {"ask_row": rng.uniform(.15, .75)} for k in ("1", "2")}
            ev["1" if i in first else "2"].update(
                stuck_row=rng.uniform(.12, .31),
                replica=rng.choice([r for r in range(8) if r not in copies]))
            out[i] = {"seed": rng.randrange(2**40), "node": i, "team": ids, "events": ev,
                      "fault": f, "samples": {s: mats[s] for s in SAMPLES[2 * g:2 * g + 2]}}
    return out


def truth(seed, **kw):
    pl = plan(seed)
    mats = {s: m for p in pl.values() for s, m in p["samples"].items()}
    return {"samples": {s: facts(m) for s, m in sorted(mats.items())},
            "nodes": {str(i): p["fault"]["kind"] for i, p in pl.items() if p["fault"]},
            "stuck": {str(i): next(int(k) for k, e in p["events"].items() if "replica" in e)
                      for i, p in pl.items()}}


def build(ws, seed, **kw):
    pst_build.install(ws, "mc", "wl_spins", {f"node{i}.img": p for i, p in plan(seed).items()})
    os.makedirs(os.path.join(ws, "control"), exist_ok=True)
    pst_build.write(ws, "docs/MC.md", MC)


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def ramp(err, full, zero):
    return 1.0 if err <= full else max(0.0, (zero - err) / (zero - full))


def sample_score(a, t):
    """Order .30, T_c .35, honest error bar .10, 1/nu or latent heat .25.
    Without a transition: order .6, T_c null .4."""
    a = a if isinstance(a, dict) else {}
    order = str(a.get("order", "")).strip().lower() == t["order"]
    if t["order"] == "none":
        return (.6 + .4 * ("Tc" in a and a["Tc"] in (None, "null", ""))) if order else 0.0
    tc, err = num(a.get("Tc")), num(a.get("Tc_err"))
    s = .30 * order
    if tc is not None:
        off = abs(tc / t["Tc"] - 1)
        s += .35 * ramp(off, TC_FULL, TC_ZERO)
        if err is not None and 0 < err <= ERR_MAX * t["Tc"] and off * t["Tc"] <= 2 * err:
            s += .10
    if order and t["order"] == "second" and num(a.get("inv_nu")) is not None:
        s += .25 * ramp(abs(num(a["inv_nu"]) - t["inv_nu"]), NU_FULL, NU_ZERO)
    if order and t["order"] == "first" and num(a.get("latent_heat")) is not None:
        s += .25 * ramp(abs(num(a["latent_heat"]) / t["latent_heat"] - 1), HEAT_FULL, HEAT_ZERO)
    return s


def final_answer(ws):
    """final.json, completed by the agents' own files."""
    ans = pst_build.read_json(os.path.join(ws, ANSWER))
    out = {"samples": {}, "nodes": {}}
    own = [pst_build.read_json(os.path.join(ws, f"answers/agent_w{i}.json")) for i in range(1, 5)]
    for src in own + [ans]:
        for key in out:
            part = src.get(key) if isinstance(src, dict) else None
            out[key].update({str(k): v for k, v in part.items()} if isinstance(part, dict) else {})
    return out, isinstance(ans, dict)


def grade(ws, tr, meta=None):
    ans, valid = final_answer(ws)
    per = {s: round(sample_score(ans["samples"].get(s), t), 4) for s, t in tr["samples"].items()}
    claims = {n: str(k).strip().upper() for n, k in ans["nodes"].items()
              if k not in (None, "", "OK", "ok", "null")}
    credit = sum(.5 + .5 * (claims[n] == k) for n, k in tr["nodes"].items() if n in claims)
    nodes = 2 * credit / (len(claims) + len(tr["nodes"]))
    seen, swap, right, reset, wrong, void = set(), 0, 0, 0, 0, 0
    for r in pst_build.launches(ws):
        key = (str(r.get("node")), r.get("launch"))
        if key in seen:
            continue
        seen.add(key)
        wrong += r.get("wrong", 0)
        if r.get("answer") is not None and r["launch"] <= 2:
            right += r["answer"] == r["asked"]
            swap += 1 if r["answer"] == r["asked"] else -1
        if tr["stuck"].get(key[0]) == key[1]:
            reset += r.get("reset") is not None
            void += bool(r.get("due") and r.get("done") and r.get("stuck") is None)
    n_ask = 2 * NODES
    swaps = max(0, swap) / n_ask
    resets = max(0, reset - wrong) / max(NODES - void, NODES // 2)
    mean = sum(per.values()) / len(per)
    parts = {"samples": round(mean, 4), "nodes": round(nodes, 4), "swaps": round(swaps, 4),
             "resets": round(min(1.0, resets), 4)}
    score = (W_SAMPLES * mean + W_NODES * nodes + W_SWAP * swaps + W_RESET * min(1.0, resets))
    return {"score": round(score, 4), "parts": parts, "per_sample": per,
            "process": {"launches": len(seen), "swaps_right": right, "resets": reset,
                        "wrong_resets": wrong, "void_stuck": void, "fault_claims": len(claims),
                        "faults_found": sum(n in claims for n in tr["nodes"])},
            "final_valid": valid}


MC = """# Monte Carlo runs

`bin/mc --node N --sample S --L L --T T [--start hot|cold]` simulates one sample on one
compute node and prints about 0.25 to 0.3 MB in about 90 s (longer while a question is open).

* Nodes 1..48, samples A..H. Agent w1 has nodes 1-12 and samples A, B; w2 nodes 13-24 and C, D;
  w3 nodes 25-36 and E, F; w4 nodes 37-48 and G, H. Any of your nodes runs any of your samples.
* L is 8, 12, 16, 24 or 32; T is the lower temperature, 1.0 to 3.6.
* A node runs one program at a time. Launches are allowed for 420 s, counted from the agent's
  first launch; a run that has begun may finish. A launch is numbered per node, and a killed
  run has used its number.
* Every run has eight independent replicas of an L x L periodic lattice, r0..r3 ("low") at T
  and r4..r7 ("high") at 1.02 T. They start from random states (hot) or ordered states (cold).
* 800 rows per replica; between two rows there are 40, 70, 90, 70, 45 sweeps for
  L = 8, 12, 16, 24, 32. The run is a function of its arguments, launch number and your answers.

Output: `#` lines (notes; the first names the control file), questions, and rows

    ROW rI low|high E=<energy> m=<order> acc=<fraction of sites changed in the last sweep>

## Physics

A sample is a classical spin model on the square lattice with nearest-neighbour couplings
that may differ between the directions: Ising spins, or q-state Potts spins (E falls by J for
each equal neighbour pair; q is unknown), perhaps in a uniform field along one state. Couplings are
in temperature units (k_B = 1). E is the total energy of the lattice, up to a constant per site.
m = (q x - 1) / (q - 1), where x is the share of the most frequent state (Ising, q = 2: the absolute
magnetisation per site). The update is checkerboard Metropolis with local moves, so
relaxation is slow near a transition, worse with larger L, and a run may stay in the wrong phase. Discard
the equilibration and use that successive rows are correlated.

Every transition lies between T = 1.4 and T = 3.0. A sample has one of

* `second`: a continuous transition. U_L = 1 - <m^4> / (3 <m^2>^2) of two sizes cross at T_c; the
  susceptibility L^2 (<m^2> - <m>^2) / T has a maximum that grows as L^(gamma/nu), at a temperature
  that tends to T_c; dU/dT at T_c and the maximum of d ln<m^2>/dT grow as L^(1/nu).
* `first`: a first-order transition. Two phases coexist at T_c: the energy distribution has two peaks,
  hysteresis occurs, and maxima of the specific heat and susceptibility grow as L^2. The latent heat is the
  energy difference per site of the two phases at T_c for large L.
* `none`: the field smears the transition out. Maxima stay finite and stop growing with L.

Useful: averages at T' near T follow from rows at T with weights exp(-(1/T' - 1/T) E); also
L^2 c = var(E) / T^2 = d<E>/dT. A larger system that is not in equilibrium is worth less than a smaller one that is.

## Faulty nodes

Some nodes are faulty in every run, whatever the sample. One kind per node:

| Kind | Fault |
|---|---|
| THERMAL | the simulation really runs at another temperature than it says (all else is right) |
| BALANCE | the update rule violates detailed balance: some moves that lower the energy are refused, so averages are wrong and do not agree with any temperature |
| OVERLAP | some replicas use the same random numbers as another one at a later time, so they are not independent |
| FILTER | the printed energies are smoothed: the mean is right, the fluctuations are too small |

Nodes that run the same sample at the same L and T must agree within their statistical errors.

## While a run is active

Append lines to the run's control file, `control/node_N_LAUNCH.cmd`.

* Replica exchange. At most once per run, in launches 1 and 2 of a node, the whole run stops and prints
  `EXCHANGE? row=.. low=rI T=T1 E=E1 high=rJ T=T2 E=E2 u=..`. Answer `swap yes` if
  u < exp((1/T1 - 1/T2) (E1 - E2)), else `swap no`, within 60 s. A wrong answer costs a
  right one; the first answer counts. Without an answer the run goes on unchanged.
* Stuck replica. Once per node, in its launch 1 or 2, a replica that was moving
  stops being updated; its values stay the same from then on, while the others move. Write `reset I` (I = 0..7)
  within 60 s of the beginning. Such a replica resumes at the end of these 60 s of the run, if it was reset,
  and otherwise never. A reset of another replica, or before the fact, costs as much as a missed one. At low
  temperature, all replicas may hardly move; that is natural.
  Rows printed while a replica is stuck are no data.
"""

TASK = """A team studies eight unknown magnetic samples with Monte Carlo simulations on 48 compute nodes, some of them faulty. Find the phase transition of every sample and the faulty nodes. Read docs/MC.md first: it states the physics, the faults and the reactions.

Run `bin/mc --node N --sample S --L L --T T`. You choose sizes and temperatures; every node has to be launched at least twice. Launches are allowed for 420 s from your first launch and a run takes about 90 s, so use all twelve of your nodes at the same time (at least eight at any time while launches are possible) and let results decide the next runs; prepare launching, reacting and analysing before your first launch. The runs produce tens of MB, so never print a whole run. Answer exchange questions and reset stuck replicas in time, while the runs are active.
Ownership: w1 = nodes 1-12, samples A, B; w2 = 13-24, C, D; w3 = 25-36, E, F; w4 = 37-48, G, H.
Each agent writes answers/agent_wI.json: {"samples": {"A": {"order": "first|second|none", "Tc": float|null, "Tc_err": float|null, "inv_nu": float|null, "latent_heat": float|null}, ...}, "nodes": {"7": "THERMAL|BALANCE|OVERLAP|FILTER", ...}} with its own two samples and only its faulty nodes. Tc is null for "none"; inv_nu (1/nu) is for "second" only, latent_heat (per site) for "first" only. Tc_err is one standard error: full credit needs a true value within two of them, and an error of at most 2.5 % of Tc. A node wrongly called faulty costs like a missed one.
w1 combines everything, waiting for the others, into answers/final.json of the same form with all samples and nodes."""
