"""Workload 11: one member of a storm-surge ensemble.

1-D nonlinear shallow-water equations on a periodic channel (an annulus) with
bottom ridges, linear drag, lagged Laplacian viscosity and a moving storm's wind
stress. Arakawa C-grid (depth at cell centres, velocity at faces), flux-form
continuity, vector-invariant momentum, leapfrog with a Robert-Asselin filter and
a forward Euler first step. Output is a pure function of (member, substeps).
"""

import argparse
import math

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

G = 9.81
NX, DX = 100, 10000.0
L = NX * DX
OUT = 1800.0                    # model seconds between station and field output
RA = 0.1                        # Robert-Asselin coefficient


def bottom(p):
    """Bottom elevation of each cell (m above the channel floor)."""
    b = [0.0] * NX
    for c, height, width in p["ridges"]:
        for i in range(NX):
            d = min(abs(i - c), NX - abs(i - c))
            b[i] += height * math.exp(-(d / width) ** 2)
    return b


def initial(p, b):
    h = []
    for i in range(NX):
        x = 2 * math.pi * i / NX
        h.append(p["H0"] - b[i] + sum(a * math.sin(k * x + ph) for k, a, ph in p["noise"]))
    return h, [0.0] * NX


def cfl_factor(a):
    """max over wavenumbers of a*sin(t) + 2*sin(t/2): leapfrog C-grid gravity
    waves Doppler-shifted by a current of a times the wave speed."""
    if a < 1e-12:
        return 2.0
    q = (math.sqrt(1.0 + 8.0 * a * a) - 1.0) / (4.0 * a)
    return 2.0 * math.sqrt(max(0.0, 1.0 - q * q)) * (a * q + 1.0)


def courant(h, u, dt):
    """von Neumann number of the step for the local flow; > 1 is unstable."""
    hf = [0.5 * (h[i] + h[i + 1 - NX]) for i in range(NX)]
    if not math.isfinite(_sum(hf) + _sum(map(abs, u))):
        return math.nan
    cs = [math.sqrt(G * x) if x > 0.0 else 0.0 for x in hf]
    low = 2.0 * max(cs)                          # a wave across two cells, no Doppler shift
    worst = 0.0
    for i in range(NX):
        c, a = cs[i], abs(u[i])
        if 2.0 * c + a >= low:                   # upper bound of this face's number
            v = c * cfl_factor(a / c) if c > 0.0 else a
            worst = max(worst, v)
    return worst * dt / DX


def _right(x):
    return x[1:] + x[:1]


def _left(x):
    return x[-1:] + x[:-1]


def wind(p, t, hf):
    """Wind stress / (density * depth) at the faces: a storm moving along the channel."""
    s = p["storm"]
    env = s["A"] * math.exp(-((t - s["tc"]) / s["w"]) ** 2)
    xs, out = (s["x0"] + s["vs"] * t) % L, []
    for i in range(NX):
        d = abs((i + 0.5) * DX - xs)
        d = min(d, L - d) / s["ls"]
        out.append(env * math.exp(-d * d) / max(hf[i], 1.0) if d < 4.0 else 0.0)
    return out


def tendency(h, u, um, b, t, p):
    """(dh/dt, du/dt); drag and viscosity act on the lagged velocity um."""
    r, kv = p["drag"], p["visc"] / (DX * DX)
    hf = [0.5 * (x + y) for x, y in zip(h, _right(h))]
    flux = [x * y for x, y in zip(hf, u)]
    bern = [0.25 * (x * x + y * y) + G * (z + w) for x, y, z, w in zip(_left(u), u, h, b)]
    dh = [(x - y) / DX for x, y in zip(_left(flux), flux)]
    du = [(x - y) / DX + f - r * w + kv * (wr - 2.0 * w + wl)
          for x, y, f, w, wr, wl in zip(bern, _right(bern), wind(p, t, hf), um, _right(um),
                                        _left(um))]
    return dh, du


def integrate(p, substeps=1, dt=None):
    """Yield (step, t, h, u, cfl) after every step. ``dt`` overrides the
    member's configured step (used by the planner)."""
    dt = (dt or OUT / p["n"]) / substeps
    b = bottom(p)
    h0, u0 = initial(p, b)
    floor = p.get("h_dry", 0.0)
    dh, du = tendency(h0, u0, u0, b, 0.0, p)
    hp, up = h0, u0
    hc = [max(h0[i] + dt * dh[i], floor) for i in range(NX)]
    uc = [u0[i] + dt * du[i] for i in range(NX)]
    n, steps = 1, round(p["t_end"] / dt)
    yield 1, dt, hc, uc, courant(hc, uc, dt)
    while n < steps:
        dh, du = tendency(hc, uc, up, b, n * dt, p)
        hn = [max(hp[i] + 2 * dt * dh[i], floor) for i in range(NX)]
        un = [up[i] + 2 * dt * du[i] for i in range(NX)]
        hp = [hc[i] + RA * (hn[i] - 2 * hc[i] + hp[i]) for i in range(NX)]
        up = [uc[i] + RA * (un[i] - 2 * uc[i] + up[i]) for i in range(NX)]
        hc, uc, n = hn, un, n + 1
        yield n, n * dt, hc, uc, courant(hc, uc, dt)


def _sum(xs):
    """Correctly rounded sum; nan once the state is no longer finite."""
    try:
        return math.fsum(xs)
    except (OverflowError, ValueError):
        return math.nan


def mass(h):
    return _sum(h) * DX


def energy(h, u, b):
    ke = _sum(0.25 * (h[i] + h[i + 1 - NX]) * u[i] * u[i] for i in range(NX))
    pe = _sum(0.5 * G * (h[i] * h[i] + 2.0 * h[i] * b[i]) for i in range(NX))
    return (ke + pe) * DX


def _f(x, fmt):
    return format(x, fmt) if x == x and abs(x) != math.inf else str(x)


def trace(p, substeps=1):
    """(fraction, line or event) of one run."""
    b = bottom(p)
    h0, _ = initial(p, b)
    sta, base, T = p["stations"], p["H0"], p["t_end"]
    every = round(OUT / (OUT / p["n"] / substeps))
    who = "nature" if p["member"] == 0 else f"member={p['member']}"
    yield 0.0, (f"# swm {who} substeps={substeps} dt={OUT / p['n'] / substeps:.4f} nx={NX} "
                f"dx={DX:.0f} t_end={T:.0f} out={OUT:.0f} stations={','.join(map(str, sta))}")
    yield 0.0, f"obs t=0.0 eta={','.join(_f(h0[i] + b[i] - base, '.4f') for i in sta)}"
    seen = False
    for n, t, h, u, c in integrate(p, substeps):
        frac = t / T
        hmin = min(h) if all(x == x for x in h) else math.nan
        umax = max(map(abs, u)) if all(x == x for x in u) else math.nan
        cs = _f(c, '.4f')
        eta = [h[i] + b[i] - base for i in range(NX)]
        top = max(range(NX), key=eta.__getitem__) if hmin == hmin else 0
        storm = (p["storm"]["x0"] + p["storm"]["vs"] * t) % L / 1000
        if not seen and not float(cs) <= 1.0:            # as printed; the ledger first
            seen = True
            yield frac, ("ONSET", n)
        yield frac, (f"step {n:06d} t={t:.1f} cfl={cs} mass={_f(mass(h), '.12e')} "
                     f"energy={_f(energy(h, u, b), '.6e')} hmin={_f(hmin, '.4f')} "
                     f"umax={_f(umax, '.4f')} etamax={_f(eta[top], '.4f')}@{top} "
                     f"storm={storm:.1f}km")
        if n % every == 0:
            ts = f"{n // every * OUT:.1f}"
            yield frac, f"obs t={ts} eta={','.join(_f(h[i] + b[i] - base, '.4f') for i in sta)}"
            q = [0.5 * (h[i] + h[i + 1 - NX]) * u[i] for i in range(NX)]
            yield frac, (f"field t={ts} eta={','.join(_f(x, '.3f') for x in eta)}"
                         f" u={','.join(_f(x, '.3f') for x in u)} q={','.join(_f(x, '.2f') for x in q)}")
    yield 1.0, f"# end t={T:.1f} steps={n}"


def main():
    ap = argparse.ArgumentParser(description="Run one member of the surge ensemble.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--member", type=int)
    g.add_argument("--nature", action="store_true")
    ap.add_argument("--substeps", type=int, default=1, choices=(1, 2, 3, 4))
    a = ap.parse_args()
    try:
        os.nice(5)
    except OSError:
        pass
    m = 0 if a.nature else a.member
    try:
        p = load_image(f"member{m}.img")
    except OSError:
        sys.exit(f"no such member: {m}")
    led = Ledger("nature" if m == 0 else f"member{m}", member=m, substeps=a.substeps)

    def onset(n):
        led.mark(onset=time.time(), onset_step=n)

    run_paced(trace(p, a.substeps), p["duration"], led, {"ONSET": onset})
    led.beat(done=True)


if __name__ == "__main__":
    main()
