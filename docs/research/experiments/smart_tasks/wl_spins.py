"""Workload 8: Monte Carlo runs of two-dimensional spin models on a square lattice.

Every run is a real simulation. Eight replicas are packed into Python integers, one
bit per site and plane, and updated with the checkerboard Metropolis rule; a
Bernoulli(p) mask is built from 16 random words, with p set per replica.
"""

import argparse
import math
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

REPS, HALF, RATIO, BITS = 8, 4, 1.02, 16
ROWS, RUN_S, TTL_S, WINDOW_S = 800, 90.0, 60.0, 420.0
SIZES = (8, 12, 16, 24, 32)
SWEEPS = {8: 40, 12: 70, 16: 90, 24: 70, 32: 45}         # sweeps per printed row
T_MIN, T_MAX, ALPHA = 1.0, 3.6, 0.2
NOTES = ["# note: scratch space at {n} %", "# WARNING clock drift {n} us corrected",
         "# heartbeat {n}", "# ERROR retry {n} of collector succeeded", "# gc {n} ms"]


class Lattice:
    """Shifts and masks for ``reps`` periodic L x L lattices in one integer."""

    def __init__(self, L, reps=REPS):
        self.L, self.n, self.reps, self.size = L, L * L, reps, L * L * reps
        self.col0 = sum(1 << (y * L) for y in range(L * reps))
        self.colz = self.col0 << (L - 1)
        self.all = (1 << self.size) - 1
        self.top = sum(((1 << L) - 1) << (r * self.n) for r in range(reps))
        self.bot = self.top << (self.n - L)
        self.incol, self.inrow = self.all ^ self.colz, self.all ^ self.bot
        self.one = (1 << self.n) - 1
        even = sum(1 << (r * self.n + y * L + x) for r in range(reps) for y in range(L)
                   for x in range(L) if (x + y) % 2 == 0)
        self.parity = (even, self.all ^ even)

    def right(self, s):
        return (s >> 1) & self.incol | (s & self.col0) << (self.L - 1)

    def left(self, s):
        return (s & self.incol) << 1 | (s & self.colz) >> (self.L - 1)

    def down(self, s):
        return (s >> self.L) & self.inrow | (s & self.top) << (self.n - self.L)

    def up(self, s):
        return (s & self.inrow) << self.L | (s & self.bot) >> (self.n - self.L)

    def counts(self, x):
        n, one = self.n, self.one
        return [(x >> (r * n) & one).bit_count() for r in range(self.reps)]

    def mask(self, reps):
        return sum(self.one << (r * self.n) for r in reps)


def coin(lat, ps):
    """Bernoulli masks: least significant digit first; digit masks select replicas."""
    ks = [min(int(round(p * (1 << BITS))), 1 << BITS) for p in ps]
    sure, size, full = lat.mask([r for r, k in enumerate(ks) if k >> BITS]), lat.size, lat.all
    plan = [lat.mask([r for r, k in enumerate(ks) if k >> b & 1 or k >> BITS])
            for b in range(BITS)]
    while plan and not plan[0]:
        plan.pop(0)

    def draw(bits):
        m = sure
        for p in plan:
            r = bits(size)
            if p == 0:
                m &= r
            elif p == full:
                m |= r
            else:
                m = m & r | p & (m | r)
        return m | sure
    return draw


class Ising:
    """Couplings jx, jy and a field h; the Metropolis test is factorised, one
    factor per direction and one for the field (each obeys detailed balance)."""

    def __init__(self, par, lat, ts, rng, start):
        self.par, self.lat, self.rng = par, lat, rng
        self.active, self.s = lat.all, rng.getrandbits(lat.size) if start == "hot" else lat.all
        if start == "cold" and not par["h"]:
            for r in range(lat.reps):
                if rng.random() < .5:
                    self.s ^= lat.one << (r * lat.n)
        self.set_t(ts)

    def set_t(self, ts):
        p = self.par
        self.bx = coin(self.lat, [math.exp(-4 * p["jx"] / t) for t in ts])
        self.by = coin(self.lat, [math.exp(-4 * p["jy"] / t) for t in ts])
        self.bh = coin(self.lat, [math.exp(-2 * p["h"] / t) for t in ts]) if p["h"] else None

    def sweep(self, kill=False):
        lat, bits = self.lat, self.rng.getrandbits
        bx, by = self.bx(bits), self.by(bits)
        bh = self.bh(bits) if self.bh else 0
        slow = bits(lat.size) & bits(lat.size) if kill else 0
        moved = 0
        for par in lat.parity:
            s = self.s
            a, b, c, d = s ^ lat.right(s), s ^ lat.left(s), s ^ lat.down(s), s ^ lat.up(s)
            f = (a | b | bx) & (c | d | by) & par & self.active
            if bh:
                f &= ~s | bh
            elif self.bh:
                f &= ~s
            if kill:                                    # three or four unlike neighbours
                f &= ~(slow & (a & b & (c | d) | c & d & (a | b)))
            self.s = s ^ f
            moved |= f
        return moved

    def measure(self):
        lat, p, s, n = self.lat, self.par, self.s, self.lat.n
        wx, wy, up = (lat.counts(x) for x in (s ^ lat.right(s), s ^ lat.down(s), s))
        return [p["c"] * n - p["jx"] * (n - 2 * x) - p["jy"] * (n - 2 * y) - p["h"] * (2 * u - n)
                for x, y, u in zip(wx, wy, up)], [abs(2 * u - n) / n for u in up]

    planes = property(lambda self: [self.s], lambda self, x: setattr(self, "s", x[0]))


class Potts:
    """q-state Potts model with q <= 16; proposals outside 0..q-1 are void."""

    def __init__(self, par, lat, ts, rng, start):
        self.par, self.lat, self.rng, self.q = par, lat, rng, par["q"]
        self.nb = (self.q - 1).bit_length()
        self.s = [0] * self.nb
        for r in range(lat.reps):
            k = rng.randrange(self.q)
            sites = [rng.randrange(self.q) if start == "hot" else k for _ in range(lat.n)]
            for b in range(self.nb):
                v = int("".join(str(x >> b & 1) for x in sites), 2)
                self.s[b] |= v << (r * lat.n)
        self.active = lat.all
        self.set_t(ts)

    def set_t(self, ts):
        self.b = coin(self.lat, [math.exp(-self.par["j"] / t) for t in ts])

    def legal(self, planes):
        """Mask of sites whose state is below q."""
        lt, eq = 0, self.lat.all
        for b in reversed(range(self.nb)):
            if self.q >> b & 1:
                lt |= eq & ~planes[b]
                eq &= planes[b]
            else:
                eq &= ~planes[b]
        return lt if self.q < 1 << self.nb else self.lat.all

    @staticmethod
    def rank(x):
        """Unary count: element k tells whether at least k+1 inputs are set."""
        a, b, c, d = x
        a, b, c, d = a | b, a & b, c | d, c & d
        a, c, b, d = a | c, a & c, b | d, b & d
        return a, b | c, b & c, d

    def same(self, near, planes):
        full, out = self.lat.all, []
        for nb in near:
            x = 0
            for p, o in zip(nb, planes):
                x |= p ^ o
            out.append(x ^ full)
        return self.rank(out)

    def sweep(self, kill=False):
        lat, bits = self.lat, self.rng.getrandbits
        new = [bits(lat.size) for _ in range(self.nb)]
        ok = self.legal(new) & self.active
        c1, c2, c3, c4 = (self.b(bits) for _ in range(4))
        slow = bits(lat.size) & bits(lat.size) if kill else 0
        moved = 0
        for par in lat.parity:
            s = self.s
            near = [[g(p) for p in s] for g in (lat.right, lat.left, lat.down, lat.up)]
            o1, o2, o3, o4 = self.same(near, s)
            n1, n2, n3, n4 = self.same(near, new)
            m1, m2, m3, m4 = ~n1, ~n2, ~n3, ~n4
            f = ok & par
            f &= c1 | ~(o1 & m1 | o2 & m2 | o3 & m3 | o4 & m4)
            f &= c2 | ~(o2 & m1 | o3 & m2 | o4 & m3)
            f &= c3 | ~(o3 & m1 | o4 & m2)
            f &= c4 | ~(o4 & m1)
            if kill:
                f &= ~(slow & (n1 & ~o1 | n2 & ~o2 | n3 & ~o3 | n4 & ~o4))
            change = [(p ^ w) & f for p, w in zip(s, new)]
            self.s = [p ^ x for p, x in zip(s, change)]
            for x in change:
                moved |= x
        return moved

    def measure(self):
        lat, n, q, bonds = self.lat, self.lat.n, self.q, [0] * self.lat.reps
        for g in (lat.right, lat.down):
            x = 0
            for p in self.s:
                x |= p ^ g(p)
            bonds = [a + n - w for a, w in zip(bonds, lat.counts(x))]
        best = [0] * lat.reps
        for k in range(q):
            x = lat.all
            for b, p in enumerate(self.s):
                x &= p if k >> b & 1 else p ^ lat.all
            best = [max(a, c) for a, c in zip(best, lat.counts(x))]
        j, c = self.par["j"], self.par["c"]
        return [c * n - j * b for b in bonds], [(q * x / n - 1) / (q - 1) for x in best]

    planes = property(lambda self: self.s, lambda self, x: setattr(self, "s", x))


def temperatures(t):
    return [t] * HALF + [round(t * RATIO, 5)] * HALF


def make(par, L, ts, rng, start, reps=REPS):
    return {"potts": Potts, "ising": Ising}[par["kind"]](par, Lattice(L, reps), ts, rng, start)


def swap(model, i, j):
    lat, out = model.lat, []
    for p in model.planes:
        a, b = p >> (i * lat.n) & lat.one, p >> (j * lat.n) & lat.one
        out.append(p ^ (a ^ b) << (i * lat.n) ^ (a ^ b) << (j * lat.n))
    model.planes = out


def question(rng, es, ts, skip=()):
    """A pair (low, high), the number u and the answer that the exchange rule gives
    (never closer than 0.05 to the limit in log u)."""
    pairs = [((1 / ts[i] - 1 / ts[j]) * (es[i] - es[j]), i, j) for i in range(HALF)
             for j in range(HALF, REPS) if i not in skip and j not in skip]
    rng.shuffle(pairs)
    down = [x for x in pairs if x[0] < -.12]
    if down and (rng.random() < .5 or max(x[0] for x in pairs) < -4):
        d, i, j = down[0]
        u = rng.uniform(math.exp(d + .05), .999)
    else:
        d, i, j = max(pairs) if pairs[0][0] < -2 else pairs[0]
        u = rng.uniform(.002, math.exp(min(0, d - .05)) if d < .05 else .999)
    u = max(round(u, 4), .0005)
    return i, j, u, u < math.exp(min(d, 0))


def simulate(p, launch, sample, L, t, start, rows=None, sweeps=None):
    """Yield (fraction, text or event). Answers come back through the mutable ``p['io']``."""
    rows = rows or ROWS
    io, fault, ev = p["io"], p["fault"], p["events"].get(str(launch), {})
    rng = random.Random(f"spins:{p['seed']}:{launch}:{sample}:{L}:{t:.5f}:{start}")
    aux = random.Random(f"aux:{p['seed']}:{launch}:{sample}:{L}")
    ts = temperatures(t)
    k = fault["kind"] if fault else None
    true = [x * (1 + fault["eps"]) for x in ts] if k == "THERMAL" else ts
    model = make(p["samples"][sample], L, true, rng, start)
    n_sw, n = sweeps or SWEEPS[L], L * L
    ewma, past = None, {a: [] for a, _ in fault["pairs"]} if k == "OVERLAP" else {}
    stuck, back = int(ev["stuck_row"] * rows) if "stuck_row" in ev else -1, -1
    ttl, lively = int(rows * TTL_S / RUN_S), [0] * 10
    ask = int(ev["ask_row"] * rows) if "ask_row" in ev else -1
    yield 0.0, (f"# node={p['node']} launch={launch} sample={sample} L={L} start={start} "
                f"T_low={ts[0]:.5f} T_high={ts[-1]:.5f} rows={rows} sweeps_per_row={n_sw} "
                f"control=control/node_{p['node']}_{launch}.cmd")
    for row in range(rows):
        frac = row / rows
        if row == stuck and sum(lively) < 8:             # only a replica that moves gets stuck
            stuck = row + 1 if row + 1 + ttl < rows else -1
        if row == stuck:
            back = row + ttl
            model.active &= ~model.lat.mask([ev["replica"]])
            yield frac, ("STUCK", ev["replica"])
        if row == back:
            yield frac, ("BACK", ev["replica"])
            if io.get("reset"):
                model.active = model.lat.all
        for _ in range(n_sw):
            moved = model.sweep(k == "BALANCE")
        es, ms = model.measure()
        if row == ask:
            i, j, u, right = question(aux, es, ts, [b for _, b in fault["pairs"]] if past else ())
            yield frac, ("ASK", f"EXCHANGE? row={row} low=r{i} T={ts[i]:.5f} E={es[i]:.3f} "
                                f"high=r{j} T={ts[j]:.5f} E={es[j]:.3f} u={u:.4f}", right)
            if io.get("swap"):
                swap(model, i, j)
        if "replica" in ev:
            lively = lively[1:] + [moved & model.lat.mask([ev["replica"]]) != 0]
        show = es
        if k == "FILTER":
            ewma = [ALPHA * e + (1 - ALPHA) * w for e, w in zip(es, ewma)] if ewma else es
            show = ewma
        recs = [(e, m, a / n) for e, m, a in zip(show, ms, model.lat.counts(moved))]
        for a, b in fault["pairs"] if past else ():      # two replicas on one random sequence
            past[a].append(recs[a])
            if len(past[a]) > fault["delay"]:
                recs[b] = past[a].pop(0)
        for r, (e, m, a) in enumerate(recs):
            yield frac, (f"{row:04d} r{r} {'low' if r < HALF else 'high'} E={e:.3f} "
                         f"m={m:.5f} acc={a:.4f}")
        if aux.random() < .02:
            yield frac, aux.choice(NOTES).replace("{n}", str(aux.randrange(2, 99)))
    yield 1.0, f"# end node={p['node']} launch={launch}"


def main():
    ap = argparse.ArgumentParser(description="Monte Carlo run of one sample on one node.")
    ap.add_argument("--node", type=int, required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--L", type=int, required=True, choices=SIZES)
    ap.add_argument("--T", type=float, required=True, help="lower temperature")
    ap.add_argument("--start", choices=("hot", "cold"), default="hot")
    a = ap.parse_args()
    p = load_image(f"node{a.node}.img")
    if a.sample not in p["samples"]:
        raise SystemExit(f"node {a.node} runs {' and '.join(sorted(p['samples']))} only")
    if not T_MIN <= a.T <= T_MAX:
        raise SystemExit(f"temperature outside {T_MIN} .. {T_MAX}")
    label, team, mine, now = f"node{a.node}", p["team"], 0, time.time()
    os.makedirs(_runs_dir(), exist_ok=True)
    first = now
    for name in os.listdir(_runs_dir()):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(_runs_dir(), name)) as f:
            r = json.load(f)
        if r["node"] in team:
            first = min(first, r["t0"])
        mine += r["node"] == a.node
    if now - first > WINDOW_S:
        raise SystemExit("the allocation is closed, no further launches")
    if label in live_labels(count_done=False):
        raise SystemExit(f"node {a.node} is busy")
    launch = mine + 1
    os.nice(5)
    led = Ledger(label, node=a.node, launch=launch, sample=a.sample, L=a.L, T=a.T,
                 stuck=None, reset=None, asked=None, answer=None, wrong=0,
                 due="stuck_row" in p["events"].get(str(launch), {}))
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "control", f"node_{a.node}_{launch}.cmd")
    io = p["io"] = {}
    seen, digits = set(), [str(r) for r in range(REPS)]

    def words():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return [x.split() for x in f.read().lower().splitlines()[:50]]
        except OSError:
            return []

    def poll():
        for k, w in enumerate(words()):
            if k in seen or len(w) != 2 or w[0] != "reset" or w[1].lstrip("r") not in digits:
                continue
            seen.add(k)
            if w[1].lstrip("r") != str(led.rec["stuck"]):
                led.rec["wrong"] += 1
            elif not led.rec.get("late"):
                io["reset"], led.rec["reset"] = True, led.rec["reset"] or time.time()

    def on_stuck(r):
        poll()
        led.rec.update(stuck=r)

    def on_back(_):
        poll()
        led.rec["late"] = True

    def on_ask(text, right):
        say(text)
        t0 = time.time()
        led.rec["asked"] = right
        while time.time() - t0 < TTL_S:
            got = [w[1] for w in words() if len(w) == 2 and w[0] == "swap"]
            if got and got[0] in ("yes", "no"):
                led.rec["answer"] = got[0] == "yes"
                io["swap"] = led.rec["answer"]
                break
            time.sleep(0.25)
            led.beat()
        led.rec["waited"] = wait = round(time.time() - t0, 1)
        say("# exchange " + ("no answer, kept" if led.rec["answer"] is None
                             else "done" if io["swap"] else "declined"))
        return wait
    run_paced(simulate(p, launch, a.sample, a.L, a.T, a.start), RUN_S, led,
              {"STUCK": on_stuck, "BACK": on_back, "ASK": on_ask}, poll)
    poll()
    led.beat(done=True)


if __name__ == "__main__":
    main()
