"""Independent reference for experiment 7: zeros of zeta, certified complete.

Shares no code with wl_zeta.py: the remainder of the Riemann-Siegel formula is
carried to C3 with derivatives taken by Cauchy's integral, the grid is adaptive,
and completeness follows from Rosser's rule, which holds for every Gram block that
starts below n = 13 999 525 (Brent 1979). ``zeta_em`` (Euler-Maclaurin) is a third,
slow evaluation used by the tests only.
"""

import cmath
import math

PI2 = 2 * math.pi
ROSSER_LIMIT = 13_999_525


def theta(t):
    u = 1 / t
    return (t / 2 * (math.log(t / PI2) - 1) - math.pi / 8
            + u / 48 + 7 * u ** 3 / 5760 + 31 * u ** 5 / 80640)


def gram(n):
    lo, hi = 7.0, max(50.0, 10.0 * (n + 10))
    while hi - lo > 1.0:
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if theta(mid) < n * math.pi else (lo, mid)
    t = (lo + hi) / 2
    for _ in range(6):
        t -= (theta(t) - n * math.pi) / (math.log(t / PI2) / 2)
    return t


def _psi_coefficients(order=40, m=512, r=1.0):
    vals = []
    for k in range(m):
        z = r * cmath.exp(1j * PI2 * (k + .5) / m)
        vals.append(-cmath.cos(PI2 * z * z - 5 * math.pi / 8) / cmath.cos(PI2 * z))
    return [sum(v * cmath.exp(-1j * PI2 * (k + .5) * n / m) for k, v in enumerate(vals)).real
            / m / r ** n for n in range(order)]


def _remainder_polynomials():
    psi = _psi_coefficients()

    def d(m):
        return [math.perm(i + m, m) * psi[i + m] if i + m < len(psi) else 0.0
                for i in range(len(psi))]
    p2, p4, p6 = math.pi ** 2, math.pi ** 4, math.pi ** 6
    D = {m: d(m) for m in (0, 1, 2, 3, 5, 6, 9)}
    rng = range(30)
    return [D[0][:30], [-D[3][i] / (96 * p2) for i in rng],
            [D[2][i] / (64 * p2) + D[6][i] / (18432 * p4) for i in rng],
            [-D[1][i] / (64 * p2) - D[5][i] / (3840 * p4) - D[9][i] / (5308416 * p6)
             for i in rng]]


_POLY = _remainder_polynomials()
_LOG, _AMP = [], []


def Z(t):
    a = math.sqrt(t / PI2)
    n = int(a)
    while len(_LOG) < n:
        k = len(_LOG) + 1
        _LOG.append(math.log(k))
        _AMP.append(k ** -0.5)
    th, x, cos = theta(t), a - n - .5, math.cos
    s = sum([b * cos(t * v - th) for b, v in zip(_AMP[:n], _LOG)])
    r, scale = 0.0, a ** -0.5
    for c in _POLY:
        h = 0.0
        for q in reversed(c):
            h = h * x + q
        r += h * scale
        scale /= a
    return 2 * s - (-1) ** n * r


def zeta_em(s, terms=None):
    """zeta(s) by Euler-Maclaurin summation."""
    N = terms or int(abs(s.imag) + 60)
    total = sum(cmath.exp(-s * math.log(k)) for k in range(1, N))
    total += N ** (1 - s) / (s - 1) + N ** (-s) / 2
    b = [1 / 6, -1 / 30, 1 / 42, -1 / 30, 5 / 66, -691 / 2730, 7 / 6, -3617 / 510,
         43867 / 798, -174611 / 330]
    term, fact = s * N ** (-s - 1), 2.0
    for i, bk in enumerate(b, 1):
        total += bk / fact * term
        term *= (s + 2 * i - 1) * (s + 2 * i) / N ** 2
        fact *= (2 * i + 1) * (2 * i + 2)
    return total


def Z_em(t):
    return (cmath.exp(1j * theta(t)) * zeta_em(0.5 + 1j * t)).real


def _solve(a, b, fa, fb):
    """Zero in [a, b]: secant steps safeguarded by bisection."""
    for i in range(80):
        if b - a < 2e-10 * max(1.0, b * 1e-2):
            break
        c = a - fa * (b - a) / (fb - fa)
        margin = (b - a) * (.5 if i % 3 == 2 else 1e-3)
        c = min(max(c, a + margin), b - margin)
        fc = Z(c)
        if fc == 0:
            return c
        if fa * fc < 0:
            b, fb = c, fc
        else:
            a, fa = c, fc
    return (a + b) / 2


def is_good(n, z):
    return (z > 0) == (n % 2 == 0)


def block_zeros(a, b, base=6):
    """All zeros in the Gram block [g_a, g_b); raises if they cannot be found."""
    m, gs = base, [gram(n) for n in range(a, b + 1)]
    while m <= 6000:
        ts = [gs[i] + (gs[i + 1] - gs[i]) * j / m for i in range(b - a) for j in range(m)]
        ts.append(gs[-1])
        zs = [Z(t) for t in ts]
        cells = [i for i in range(len(ts) - 1) if zs[i] * zs[i + 1] < 0]
        if len(cells) == b - a:
            return [_solve(ts[i], ts[i + 1], zs[i], zs[i + 1]) for i in cells]
        if len(cells) > b - a:
            raise ArithmeticError(f"Gram block {a}..{b} has {len(cells)} zeros")
        m *= 4
    raise ArithmeticError(f"Gram block {a}..{b}: zeros missing")


def survey(n0, n1):
    """(zeros, {n: is g_n good}) for n0 <= n <= n1; the zeros of all Gram blocks that
    meet the range, hence a few outside it. Complete by Rosser's rule."""
    if n1 > ROSSER_LIMIT - 100:
        raise ValueError("beyond the proven range of Rosser's rule")
    good = {n: is_good(n, Z(gram(n))) for n in range(n0, n1 + 1)}
    lo, hi = n0, n1
    while not good[lo]:
        lo -= 1
        good[lo] = is_good(lo, Z(gram(lo)))
    while not good[hi]:
        hi += 1
        good[hi] = is_good(hi, Z(gram(hi)))
    zeros, a = [], lo
    for n in range(lo + 1, hi + 1):
        if good[n]:
            zeros += block_zeros(a, n)
            a = n
    return zeros, good


def spacing(g1, g2):
    return (g2 - g1) * math.log(g1 / PI2) / PI2


def gue_cdf(s):
    """Wigner surmise for the GUE."""
    return (math.erf(2 * s / math.sqrt(math.pi))
            - 4 * s / math.pi * math.exp(-4 * s * s / math.pi))


def ks_gue(xs):
    xs = sorted(xs)
    n = len(xs)
    return max(max((i + 1) / n - gue_cdf(x), gue_cdf(x) - i / n) for i, x in enumerate(xs))
