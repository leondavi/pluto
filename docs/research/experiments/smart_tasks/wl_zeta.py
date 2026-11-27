"""Workload 7: scan a window of the critical line with the Riemann-Siegel formula.

Every printed number is computed here from the window, the grid and the state of
the evaluator; the image holds no results. Standard library only.
"""

import argparse
import math
from decimal import Decimal, getcontext

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

TAU = 2 * math.pi
KEEP_S = 75.0                                  # block tables are kept this long
MAX_BLOCK, MAX_F, MAX_REQ = 16, 64, 60
STUCK_LIMIT_S = 900.0


def theta(t):
    return t / 2 * math.log(t / TAU) - t / 2 - math.pi / 8 + 1 / (48 * t) + 7 / (5760 * t ** 3)


def gram(n):
    """g_n: theta(g_n) = n pi."""
    t = TAU * math.exp(1.0) * 10 if n < 10 else TAU * n / math.log(n)
    for _ in range(60):
        d = (theta(t) - n * math.pi) / (0.5 * math.log(t / TAU))
        t -= d
        if abs(d) < 1e-11 * t:
            break
    return t


def psi_series(order=44):
    """Taylor coefficients in x = p - 1/2 of Psi = cos(2 pi (p^2-p-1/16)) / cos(2 pi p)."""
    getcontext().prec = 60
    pi = Decimal("3.14159265358979323846264338327950288419716939937510582097494")

    def cos_sin(x):
        c = s = Decimal(0)
        term = Decimal(1)
        for k in range(80):
            if k % 2:
                s += term if k % 4 == 1 else -term
            else:
                c += term if k % 4 == 0 else -term
            term = term * x / (k + 1)
        return c, s
    c0, s0 = cos_sin(5 * pi / 8)
    num, den = [Decimal(0)] * order, [Decimal(0)] * order
    term = Decimal(1)                           # cos(2 pi x^2 - 5 pi/8)
    for k in range(order // 2):
        sign = 1 if k % 4 < 2 else -1
        num[2 * k] = term * sign * (s0 if k % 2 else c0)
        term = term * 2 * pi / (k + 1)
    term = Decimal(1)
    for k in range(order):
        if k % 2 == 0:
            den[k] = -term if k % 4 == 0 else term          # -cos(2 pi x)
        term = term * 2 * pi / (k + 1)
    out = []
    for k in range(order):
        out.append((num[k] - sum(den[i] * out[k - i] for i in range(1, k + 1))) / den[0])
    return [float(x) for x in out]


def derive(c, m):
    for _ in range(m):
        c = [k * x for k, x in enumerate(c)][1:]
    return c


def horner(c, x):
    s = 0.0
    for a in reversed(c):
        s = s * x + a
    return s


class Evaluator:
    """Z(t) by Riemann-Siegel with the terms C0, C1, C2 of the remainder."""

    def __init__(self, top):
        n = int(math.sqrt(top / TAU)) + 3
        self.log = [math.log(k) for k in range(1, n)]
        self.amp = [1 / math.sqrt(k) for k in range(1, n)]
        ps, pi2 = psi_series(), math.pi ** 2
        zero = [0.0] * len(ps)

        def mix(*parts):
            out = list(zero)
            for f, m in parts:
                for i, x in enumerate(derive(ps, m)):
                    out[i] += f * x
            return out[:38]
        self.c = [ps[:38], mix((-1 / (96 * pi2), 3)),
                  mix((1 / (64 * pi2), 2), (1 / (18432 * pi2 * pi2), 6))]

    def z(self, t, state=None):
        a = math.sqrt(t / TAU)
        n = int(a)
        x, th, cos = a - n - 0.5, theta(t), math.cos
        kind, par = state or (None, 0)
        m = min(n - 1, int(par * n)) if kind == "TRUNC" else n
        if kind == "THETA":
            th += par
        if kind == "NOISE":
            q = 1 << int(par)
            s = sum([b * cos(th - round(t * v * q) / q)
                     for b, v in zip(self.amp[:m], self.log)])
        else:
            s = sum([b * cos(th - t * v) for b, v in zip(self.amp[:m], self.log)])
        if kind == "REMAINDER":
            return 2 * s
        r = 0.0
        for c in reversed(self.c):
            r = r / a + horner(c, x)
        return 2 * s + (r if n % 2 else -r) / math.sqrt(a)


def root(f, a, b, fa, fb):
    """Illinois iteration on a sign change."""
    side = 0
    for _ in range(60):
        if b - a < 1e-10 * b or fa == fb:
            break
        c = (a * fb - b * fa) / (fb - fa)
        if not a < c < b:
            c = (a + b) / 2
        fc = f(c)
        if abs(fc) < 1e-11:
            return c
        if (fc > 0) == (fb > 0):
            b, fb = c, fc
            fa, side = (fa / 2 if side == -1 else fa), -1
        else:
            a, fa = c, fc
            fb, side = (fb / 2 if side == 1 else fb), 1
    return (a + b) / 2


def extremum(f, a, b, sign):
    """Golden section for the largest sign*Z between two zeros."""
    g = (math.sqrt(5) - 1) / 2
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = sign * f(c), sign * f(d)
    for _ in range(14):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = sign * f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = sign * f(d)
    return (c, sign * fc) if fc > fd else (d, sign * fd)


class Scan:
    """One launch: the paced scan, its state changes and the refinement requests."""

    def __init__(self, p, start, safe, led=None, ctl=None):
        self.p, self.start, self.led, self.ctl = p, start, led, ctl
        self.first, self.last, self.k = p["n0"], p["n0"] + p["len"], p["k"]
        self.fault = None if safe else p["fault"]
        self.ev = Evaluator(gram(self.last + 1))
        self.printed, self.closed_at, self.reqs, self.seen = -1, {}, [], 0
        self.b_state = None

    def state(self, n):
        f = self.fault
        return (f["kind"], f["par"]) if f and f["kind"] != "STUCK" and n >= f["n"] else None

    def note(self, **kw):
        if self.led:
            self.led.mark(**kw)

    def items(self):
        """(fraction, line or event) pairs; the maths happens here, when pulled."""
        p, k, z = self.p, self.k, self.ev.z
        yield 0.0, (f"# window n={self.first}..{self.last} band={p['band']} part={p['part']}/6 "
                    f"grid={k} from={self.start}")
        prev = prev_zero = None
        found = bad = 0
        g, n_all = gram(self.start), max(1, self.last - self.start)
        for n in range(self.start, self.last + 1):
            st, frac, g2 = self.state(n), (n - self.start) / n_all, gram(n + 1)
            if self.fault and n == self.fault["n"] and st:
                yield frac, ("mark", n)

            def f(t, st=st):
                return z(t, st)
            for j in range(k if n < self.last else 1):
                t = g + (g2 - g) * j / k
                v = f(t)
                if j == 0:
                    bad += (v > 0) == bool(n % 2)
                    self.printed = n
                    if self.led:                # a SIGTERM writes the ledger as it is
                        self.led.rec["pos"] = n
                    self.closed_at[n] = time.time()
                    yield frac, f"G n={n} t={t:.8f} Z={v:+.8f}"
                else:
                    yield frac + j / k / n_all, f"S t={t:.8f} Z={v:+.8f}"
                if prev and (prev[1] > 0) != (v > 0):
                    r = root(f, prev[0], t, prev[1], v)
                    found += 1
                    if prev_zero:
                        x, e = extremum(f, prev_zero, r, 1 if prev[1] > 0 else -1)
                        yield frac, f"X t={x:.8f} Z={e:+.8f}"
                        if abs(e) < 0.03:
                            yield frac, f"# warning small extremum near t={x:.4f}"
                    yield frac, f"R t={r:.9f}"
                    if n in p["bursts"] and j == p["bursts"][n]:
                        yield frac, ("bisect", n, prev[0], t, st, False)
                    prev_zero = r
                prev = (t, v)
            if self.fault and self.fault["kind"] == "STUCK" and n == self.fault["n"]:
                yield frac, ("bisect", n, g, g2, None, True)
            if n % 100 == 0:
                yield frac, f"# progress n={n} sign_changes={found} irregular={bad}"
            g = g2
        yield 1.0, f"END job={p['job']} last={self.last}"

    def on_mark(self, n):
        self.note(fault_t=time.time(), fault_n=n)

    def on_bisect(self, n, a, b, st, coarse):
        """Bisection shown step by step. With ``coarse`` the midpoint is rounded to the
        step of the table, so from some width on the bracket cannot shrink."""
        t0, it, wait = time.time(), 0, 0.15 if not coarse else 0.3
        if coarse:
            self.note(fault_t=t0, fault_n=n)
            a, b = round(a, 3), round(b, 3)
        fa = self.ev.z(a, st)
        while b - a > 1e-8 and time.time() - t0 < STUCK_LIMIT_S:
            it += 1
            c = (a + b) / 2
            if coarse:
                c = round(math.floor(c * 1000) / 1000, 3)
            fc = self.ev.z(c, st)
            if (fc > 0) == (fa > 0):
                a, fa = max(a, c), fc if c > a else fa
            else:
                b = c
            say(f"B n={n} it={it} lo={a:.9f} hi={b:.9f}")
            self.poll()
            time.sleep(wait * self.p.get("pace", 1.0))
            if self.led:
                self.led.beat()
        if coarse:
            os._exit(75)
        return time.time() - t0

    def poll(self):
        """Read new complete lines of the control file."""
        if self.led:
            self.led.rec["pos"] = self.printed
        try:
            with open(self.ctl) as f:
                lines = f.read().split("\n")[:-1]
        except (OSError, TypeError, ValueError):
            return
        for x in lines[self.seen:]:
            self.seen += 1
            self.request(x.split())

    def request(self, w):
        try:
            a, b, fac = int(w[1]), int(w[2]), int(w[3])
            ok = w[0] == "refine" and 0 < b - a <= MAX_BLOCK and 2 <= fac <= MAX_F
        except (IndexError, ValueError):
            ok = False
        if not ok:
            return say("# request not understood")
        if a < self.start or b > self.printed or b not in self.closed_at:
            return say(f"# refine {a} {b} refused: not in the printed range")
        late = time.time() - self.closed_at[b]
        if len(self.reqs) >= MAX_REQ:
            return say(f"# refine {a} {b} refused: quota")
        self.reqs.append([a, b, fac, round(late, 2), self.state(b - 1) is not None])
        self.note(reqs=self.reqs)
        if late > KEEP_S:
            return say(f"# refine {a} {b} refused: tables dropped")
        say(f"# refine {a} {b} {fac} begin")
        prev, zeros = None, []
        for n in range(a, b):
            g, g2, st = gram(n), gram(n + 1), self.state(n)
            m = self.k * fac
            for j in range(m + (n == b - 1)):
                t = g + (g2 - g) * j / m
                v = self.ev.z(t, st)
                sys.stdout.write(f"Y t={t:.8f} Z={v:+.8f}\n")
                if prev and (prev[1] > 0) != (v > 0):
                    zeros.append(root(lambda x: self.ev.z(x, st), prev[0], t, prev[1], v))
                prev = (t, v)
        for r in zeros:
            say(f"Q t={r:.9f}")
        say(f"# refine {a} {b} {fac} end")


def earlier(job):
    """Largest Gram index that earlier launches of the job have printed."""
    best = -1
    for name in os.listdir(_runs_dir()) if os.path.isdir(_runs_dir()) else []:
        if name.startswith(f"z{job}-") and name.endswith(".json"):
            try:
                with open(os.path.join(_runs_dir(), name)) as f:
                    best = max(best, json.load(f).get("pos", -1))
            except (OSError, ValueError):
                pass
    return best


def main():
    ap = argparse.ArgumentParser(prog="zeta")
    ap.add_argument("--job", type=int, required=True)
    ap.add_argument("--from", dest="start", type=int)
    ap.add_argument("--safe", action="store_true")
    a = ap.parse_args()
    try:
        os.nice(5)
    except OSError:
        pass
    try:
        p = load_image(f"job{a.job}.img")
    except OSError:
        sys.exit("no such job")
    p["bursts"] = {int(n): j for n, j in p["bursts"].items()}
    last = p["n0"] + p["len"]
    start = p["n0"] if a.start is None else a.start
    if not p["n0"] <= start < last:
        sys.exit(f"--from must be in {p['n0']}..{last - 1}")
    if a.safe and earlier(a.job) < start:
        sys.exit("--safe needs --from N with N already printed by an earlier launch")
    led = Ledger(f"z{a.job}", job=a.job, **{"from": start}, safe=a.safe, pos=-1, reqs=[])
    ctl = os.path.join(os.path.dirname(_runs_dir()), "control", f"job_{a.job}.cmd")
    scan = Scan(p, start, a.safe, led, ctl)
    if os.path.exists(ctl):                     # requests to an earlier launch are not ours
        with open(ctl) as f:
            scan.seen = len(f.read().split("\n")) - 1

    run_paced(scan.items(), p["duration"] * (last - start) / p["len"], led,
              {"mark": scan.on_mark, "bisect": scan.on_bisect}, scan.poll)
    scan.poll()
    led.beat(done=True)


if __name__ == "__main__":
    main()
