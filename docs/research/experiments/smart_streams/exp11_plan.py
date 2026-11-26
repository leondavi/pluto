"""Experiment 11: storm-surge ensemble forecast. Plan and workspace docs.

Every member is a genuine integration (wl_swm); the plan draws perturbed initial
states and parameters and checks by integrating that each member really behaves
as its role says.
"""

import functools
import math
import random

import wl_swm

MEMBERS, PER_AGENT = 48, 12
T_END, DURATION = 72 * 3600.0, 150.0
ISSUE_S, REACT_S, TIMEOUT = 400.0, 40.0, 600
LEADS = [12, 24, 36, 48, 60, 72]
ROLES = ["blowup"] * 3 + ["noncons"] * 2 + ["near"] + ["ok"] * 6
RANGE = {"blowup": (1.04, 1.3), "near": (0.95, 0.995),
         "ok": (0.6, 0.93), "noncons": (0.6, 0.9)}
WINDOW = (0.25, 0.62)                    # onset of instability, fraction of the forecast
ANSWER = "answers/final.json"


def scan(p, substeps=1, dt=None):
    """Facts of one integration (no formatting): peak and onset of cfl, finiteness,
    mass drift, minimum depth, station series every output."""
    b = wl_swm.bottom(p)
    m0 = wl_swm.mass(wl_swm.initial(p, b)[0])
    every = round(wl_swm.OUT / ((dt or wl_swm.OUT / p["n"]) / substeps))
    out = {"peak": 0.0, "onset": None, "finite": True, "hmin": 1e9, "obs": [], "fields": {}}
    for n, t, h, u, c in wl_swm.integrate(p, substeps, dt):
        if not c <= 1.0 and out["onset"] is None:
            out["onset"] = t
        if not c == c:
            out["finite"] = False
            break
        out["peak"] = max(out["peak"], c)
        out["hmin"] = min(out["hmin"], min(h))
        if n % every == 0:
            out["obs"].append([h[i] + b[i] - p["H0"] for i in p["stations"]])
            out["fields"][n // every] = [h[i] + b[i] - p["H0"] for i in range(wl_swm.NX)]
    out["drift"] = abs(wl_swm.mass(h) - m0) / m0 if out["finite"] else None
    return out


def _nature(rng, duration, t_end):
    ridges = [(rng.randrange(wl_swm.NX), rng.uniform(2.0, 4.5), rng.uniform(3.0, 7.0))
              for _ in range(rng.choice((2, 3)))]
    return {"member": 0, "H0": 20.0, "ridges": ridges, "noise": _noise(rng),
            "storm": {"A": rng.uniform(0.009, 0.011), "vs": rng.uniform(13.5, 15.5),
                      "x0": rng.uniform(0, wl_swm.L), "tc": rng.uniform(.44, .53) * t_end,
                      "w": rng.uniform(8, 10) * 3600, "ls": rng.uniform(70e3, 90e3)},
            "drag": 1 / (2 * 86400), "visc": 1000.0, "n": 12, "t_end": t_end,
            "stations": sorted(rng.sample(range(wl_swm.NX), 16)), "duration": duration}


def _noise(rng):
    return [(k, rng.uniform(0.02, 0.06), rng.uniform(0, 2 * math.pi))
            for k in rng.sample(range(1, 7), 3)]


def _perturb(rng, nat, m):
    s = nat["storm"]
    return dict(nat, member=m, H0=20.0 * (1 + rng.uniform(-.06, .06)), noise=_noise(rng),
                storm={"A": s["A"] * math.exp(rng.gauss(0, .15)),
                       "vs": s["vs"] * (1 + rng.uniform(-.08, .08)),
                       "x0": (s["x0"] + rng.gauss(0, 30e3)) % wl_swm.L,
                       "tc": s["tc"] + rng.uniform(-3, 3) * 3600, "w": s["w"] * rng.uniform(.9, 1.1),
                       "ls": s["ls"] * rng.uniform(.85, 1.15)},
                drag=nat["drag"] * rng.uniform(.8, 1.25),
                duration=nat["duration"] * rng.uniform(.97, 1.03))


def _ok(role, f, t_end):
    lo, hi = RANGE[role]
    if role == "blowup":
        return f["onset"] is not None and WINDOW[0] * t_end <= f["onset"] <= WINDOW[1] * t_end
    return f["onset"] is None and lo <= f["peak"] <= hi


def _tune(p, role, rng):
    """(member with its configured step, facts of its valid run) or None.
    The step is chosen from a probe run; the result is checked by integrating."""
    probe = scan(dict(p, n=14))
    if probe["onset"] is not None:
        return None
    s_peak, (lo, hi) = probe["peak"] / (wl_swm.OUT / 14), RANGE[role]
    want = rng.uniform(lo, hi)
    tried = set()
    n = min(range(6, 19), key=lambda n: abs(wl_swm.OUT / n * s_peak * .97 - want))
    if role == "near":
        return _near(dict(p, n=max(n, min(n for n in range(6, 19)
                                          if wl_swm.OUT / n * s_peak * .97 < want))), want)
    for _ in range(3):
        if n in tried or not 6 <= n <= 18:
            return None
        tried.add(n)
        q = dict(p, n=n)
        f = scan(q)
        if role == "noncons" and f["onset"] is None:
            q = dict(q, h_dry=round(f["hmin"] + rng.uniform(.02, .15), 3))
            f = scan(q)
            if f["drift"] is None or f["drift"] < 1e-7:
                return None
        if _ok(role, f, p["t_end"]):
            break
        late = role == "blowup" and f["onset"] is not None and f["onset"] > WINDOW[1] * p["t_end"]
        n += -1 if (f["onset"] is None and f["peak"] < lo) or late else 1
    else:
        return None
    if role == "blowup":
        f = scan(q, 2)
        if f["onset"] is not None or f["peak"] > .95:
            return None
    return q, f


def _near(p, want):
    """Tune the storm's speed (resonance controls the surge) until the peak cfl is
    just below 1."""
    def peak(v):
        return scan(dict(p, storm=dict(p["storm"], vs=v)))
    v0 = p["storm"]["vs"]
    f0 = peak(v0)
    v1, d0 = v0 * (1 + 2.5 * (want - f0["peak"]) / want), f0["peak"] - want
    for _ in range(5):
        f1 = peak(v1)
        d1 = f1["peak"] - want if f1["onset"] is None else 1.0
        if abs(d1) < .004 or d1 == d0 or not 10.0 < v1 < 20.0:
            break
        v0, v1, d0 = v1, v1 - d1 * (v1 - v0) / (d1 - d0), d1
    q = dict(p, storm=dict(p["storm"], vs=v1))
    return (q, f1) if _ok("near", f1, p["t_end"]) else None


@functools.lru_cache(maxsize=4)
def _plan(seed, duration, t_end):
    rng = random.Random(f"plan11:{seed}")
    nat = _nature(rng, duration, t_end)
    roles, members, facts = {}, {}, {}
    for g in range(4):                                  # the same load for every agent
        rs = list(ROLES)
        rng.shuffle(rs)
        for k, r in enumerate(rs):
            roles[g * PER_AGENT + k + 1] = r
    for m in range(1, MEMBERS + 1):
        for _ in range(100):
            got = _tune(_perturb(rng, nat, m), roles[m], rng)
            if got:
                break
        else:
            raise RuntimeError(f"member {m}: no {roles[m]} configuration found")
        members[m], facts[m] = got
    valid = {m: f for m, f in facts.items() if roles[m] != "noncons"}
    return nat, roles, members, facts, _questions(rng, nat, valid, t_end)


def _questions(rng, nat, valid, t_end):
    """Seeded forecast questions whose answers are not degenerate."""
    steps, k = round(t_end / wl_swm.OUT), len(nat["stations"])
    for _ in range(200):
        j = rng.randrange(k)
        a = rng.randrange(steps // 4, steps // 2)
        b = a + rng.randrange(steps // 8, steps // 3)
        peaks = sorted(max(v["obs"][i - 1][j] for i in range(a, b + 1)) for v in valid.values())
        q = rng.randrange(len(peaks) * 3 // 10, len(peaks) * 7 // 10)
        z = round((peaks[q] + peaks[q + 1]) / 2, 2)
        if min(abs(x - z) for x in peaks) > .01:
            break
    lead = rng.randrange(steps // 3, steps * 5 // 6)
    vals = sorted(x for v in valid.values() for x in v["fields"][lead])
    zf = round(vals[int(len(vals) * rng.uniform(.7, .85))], 2)
    return {"q1": {"station": nat["stations"][j], "threshold": z, "from_h": a / 2, "to_h": b / 2},
            "q2": {"station": nat["stations"][rng.randrange(k)]},
            "q4": {"lead_h": lead / 2, "threshold": zf}}


def plan(seed, duration=DURATION, t_end=T_END):
    """{"nature": p, "members": {m: p}, "roles": {m: role}, "questions": {...}}"""
    nat, roles, members, _, qs = _plan(seed, duration, t_end)
    return {"nature": nat, "members": members, "roles": roles, "questions": qs}


def docs(pl):
    q, nat = pl["questions"], pl["nature"]
    return FORECAST.format(
        stations=", ".join(map(str, nat["stations"])), leads=", ".join(map(str, LEADS)),
        q1s=q["q1"]["station"], q1z=q["q1"]["threshold"], q1a=q["q1"]["from_h"],
        q1b=q["q1"]["to_h"], q2s=q["q2"]["station"], q4l=q["q4"]["lead_h"],
        q4z=q["q4"]["threshold"], issue=int(ISSUE_S), react=int(REACT_S),
        hours=int(nat["t_end"] / 3600))


FORECAST = """# Storm-surge ensemble forecast

A storm crosses a closed periodic channel (an annulus 1000 km long: 100 cells of 10 km, still-water
depth about 20 m, a few bottom ridges). This hindcast runs a {hours}-hour ensemble of 48 members of
a 1-D nonlinear shallow-water model (Arakawa C-grid, leapfrog with a Robert-Asselin filter, explicit
drag and viscosity). Members differ in initial state, mean depth, drag and the storm's strength,
speed, track and timing, and each has its own configured time step. The nature run is the truth.

## Running

`bin/swm --member M [--substeps K]` (M = 1..48) and `bin/swm --nature`. A run takes about 150 s
of wall time whatever its step and prints 0.3 to 0.8 MB. Runs are deterministic: the same member with
the same K always prints the same lines. `--substeps K` (1 to 4) divides the configured step by K
(default 1).

## Output

* `# swm ...` header; `# end ...` is the last line of a complete run.
* `step N t=T cfl=C mass=M energy=E hmin=H umax=U etamax=Z@cell storm=Xkm` after every time step
  (t in model seconds; hmin = smallest depth, etamax = highest elevation and its cell, storm = the
  storm centre's position).
* `obs t=T eta=v1,...,v16` at t = 0 and every 1800 s: surface elevation in m above the member's
  still-water level at the 16 stations, in this order: cells {stations}.
* `field t=T eta=(100 values) u=(100 values) q=(100 values)` at the same times: eta in m at the
  cells, u in m/s and volume flux q in m2/s at the faces east of each cell (cell 0 first).

Lead time in hours = t / 3600. Use values as printed.

## Validity

* **Stability.** `cfl` is the von Neumann number of the leapfrog step for the local flow (gravity
  waves Doppler-shifted by the current), maximised over the channel. A run is unstable from its
  first step with cfl above 1.0 (or not a number): everything from that step on is invalid, even
  if it still looks finite. The model does not stop by itself. Stop an unstable run within {react} s
  of that line and rerun the member with `--substeps 2`, which is always enough here. A member whose
  cfl never exceeds 1.0 with its configured step is used as it is and is not rerun.
* **Conservation.** The total mass of the closed channel is invariant: a sound run keeps
  |mass / mass0 - 1| below 1e-10 (mass0 = mass on the first step line). A member whose relative
  drift exceeds 1e-9 at any step is non-conservative: discard it (no rerun helps).
* **Valid ensemble:** every member that is neither discarded nor unstable, plus the stable
  `--substeps 2` run of every unstable member.

## Deadline

The forecast is issued {issue} s after your session starts. Every member's valid run must have
completed (printed `# end`) by then.

## Products (over the valid ensemble, from obs lines; n = number of valid members)

"Hourly" means t = 3600, 7200, ... up to the end (not t = 0).

* skill at lead times {leads} h: rmse = sqrt(mean over the 16 stations of (ensemble mean - nature)^2),
  spread = sqrt(mean over the 16 stations of the ensemble variance), variance with divisor n - 1.
* rank histogram: for every station and every hourly time, the rank of nature = the number of valid
  members whose value is strictly below nature's; report the counts of ranks 0..n (n + 1 integers).
* q1: the fraction of valid members in which eta at station cell {q1s} exceeds {q1z} m at some obs
  time with lead time from {q1a} to {q1b} h (inclusive).
* q2: the median over valid members of the lead time (h) at which eta at station cell {q2s} is
  highest (the earliest such obs time; t = 0 counts).
* q3: the valid member closest to nature: smallest rmse over all 16 stations and all hourly times.
* q4: the mean over valid members of the number of cells whose eta exceeds {q4z} m in the field at
  lead time {q4l} h.

## Member report

`{{"status": "ok|unstable|nonconservative", "onset_h": float|null, "drift": float|null}}`:
onset_h = lead time of the first step with cfl above 1.0 (unstable members, from the run with the
configured step); drift = |mass / mass0 - 1| on the last step line (non-conservative members);
null otherwise.
"""
