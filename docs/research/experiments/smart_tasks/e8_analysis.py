"""Reference analysis for experiment 8: reads only what the programs print."""

import math
import re
import statistics as st

import wl_spins

ROW = re.compile(r"(\d{4}) r(\d) (low|high) E=(-?[\d.]+) m=([\d.]+) acc=([\d.]+)")
REPS, HALF, RATIO = wl_spins.REPS, wl_spins.HALF, wl_spins.RATIO
STILL = 15                                           # rows without a change: a stuck replica


def mean(x):
    return sum(x) / len(x)


class Run:
    """One log: rows[replica] = [(E, m, acc), ...]"""

    def __init__(self, node, launch, sample, L, t):
        self.node, self.launch, self.sample, self.L, self.t = node, launch, sample, L, t
        self.rows = [[] for _ in range(REPS)]

    def feed(self, line):
        m = ROW.fullmatch(line)
        if m:
            self.rows[int(m[2])].append((float(m[4]), float(m[5]), float(m[6])))
        return m

    def stuck(self, r):
        """A replica that moved before and does not now, while its equals move."""
        own = self.rows[r]
        if len(own) < STILL + 12 or any(x[2] for x in own[-STILL:]):
            return False
        before = [x[2] > 0 for x in own[:-STILL]][-40:]
        peers = [i for i in range(r // HALF * HALF, r // HALF * HALF + HALF) if i != r]
        busy = [mean([x[2] > 0 for x in self.rows[i][-STILL:]]) for i in peers]
        return mean(before) > .5 and st.median(busy) > .5

    def data(self, r, burn=.2):
        """Rows of a replica after equilibration, without the stuck part."""
        own, out, i = self.rows[r], [], 0
        while i < len(own):
            j = i
            while j < len(own) and own[j][1:] == (own[i][1], 0.0):
                j += 1
            if j - i < STILL:
                out += own[i:max(j, i + 1)]
            i = max(j, i + 1)
        return out[int(burn * len(out)):]

    def group(self, g, burn=.2):
        return [self.data(r, burn) for r in range(g * HALF, (g + 1) * HALF)]

    def temp(self, g):
        return self.t * RATIO if g else self.t


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = mean(a), mean(b)
    va, vb = sum((x - ma) ** 2 for x in a), sum((x - mb) ** 2 for x in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb) if va * vb else 0.


def overlap(run, most=45):
    """Two replicas of a group whose printed m repeat with a delay."""
    for g in (0, 1):
        ids = range(g * HALF, (g + 1) * HALF)
        for a in ids:
            for b in ids:
                if a == b:
                    continue
                x, y = run.rows[a], run.rows[b]
                for d in range(1, most):
                    hits = sum(x[k - d] == y[k] for k in range(100, 180))
                    if hits > 60 and len(set(x[100 - d:180 - d])) > 40:
                        return True
    return False


def faults(runs):
    """Kinds by node from runs of one sample at one L and T, one run per node."""
    out, stats = {}, {}
    for run in runs:
        s = []
        for g in (0, 1):
            data = run.group(g)
            es = [x[0] for d in data for x in d]
            ac = [mean([x[0] for x in d][1:]) for d in data]
            lag = mean([corr([x[0] for x in d[:-1]], [x[0] for x in d[1:]]) for d in data])
            mlag = mean([corr([x[1] for x in d[:-1]], [x[1] for x in d[1:]]) for d in data])
            s.append({"e": mean(es), "se": st.stdev(ac) / 2, "a": mean(
                [x[2] for d in data for x in d]), "v": st.pvariance(es), "lag": lag - mlag})
        stats[run.node] = s
    mid = {k: [st.median(s[g][k] for s in stats.values()) for g in (0, 1)]
           for k in ("e", "a", "v", "lag", "se")}
    slope = (mid["a"][1] - mid["a"][0]) / (mid["e"][1] - mid["e"][0])
    for run in runs:
        s = stats[run.node]
        off = [(s[g]["e"] - mid["e"][g]) / math.hypot(mid["se"][g], s[g]["se"]) for g in (0, 1)]
        de = mean([s[g]["e"] - mid["e"][g] for g in (0, 1)])
        da = mean([s[g]["a"] - mid["a"][g] for g in (0, 1)])
        if overlap(run):
            out[run.node] = "OVERLAP"
        elif mean([s[g]["v"] / mid["v"][g] for g in (0, 1)]) < .6:
            out[run.node] = "FILTER"
        elif abs(mean(off)) > 6:
            out[run.node] = "THERMAL" if abs(da / (slope * de) - 1) < .5 else "BALANCE"
    return out


class Ensemble:
    """Pooled (E, m) series of one sample and size, several temperatures."""

    def __init__(self, runs, skip=lambda run: False):
        self.sets = []                                # (T, [E], [m], blocks)
        for run in runs:
            if skip(run):
                continue
            for g in (0, 1):
                rows = [x for d in run.group(g) for x in d]
                if rows:
                    self.sets.append((run.temp(g), [x[0] for x in rows], [x[1] for x in rows]))
        self.L = runs[0].L if runs else 0

    def averages(self, t):
        """Reweighted <E>, <E2>, <m>, <m2>, <m4>, <m2 E> from the nearest series."""
        acc, total = None, 0.0
        near = sorted(self.sets, key=lambda s: abs(s[0] - t))[:3]
        for t0, es, ms in near:
            k = 1 / t - 1 / t0
            e0 = mean(es)
            ws = [-k * (e - e0) for e in es]
            top = max(ws)
            ws = [math.exp(w - top) for w in ws]
            z = sum(ws)
            eff = z * z / sum(w * w for w in ws)
            if eff < .02 * len(ws):
                continue
            v = [sum(w * e for w, e in zip(ws, es)) / z]
            m2 = [m * m for m in ms]
            v += [sum(w * (e - e0) ** 2 for w, e in zip(ws, es)) / z - (v[0] - e0) ** 2,
                  sum(w * m for w, m in zip(ws, ms)) / z,
                  sum(w * m for w, m in zip(ws, m2)) / z,
                  sum(w * m * m for w, m in zip(ws, m2)) / z,
                  sum(w * m * (e - e0) for w, m, e in zip(ws, m2, es)) / z - (
                      sum(w * m for w, m in zip(ws, m2)) / z) * (v[0] - e0)]
            acc = [a + eff * x for a, x in zip(acc or [0.0] * 6, v)]
            total += eff
        return [a / total for a in acc] if total else None

    def derived(self, t):
        v = self.averages(t)
        if not v or v[3] <= 0:
            return None
        n = self.L ** 2
        return {"U": 1 - v[4] / (3 * v[3] ** 2), "chi": n * (v[3] - v[2] ** 2) / t,
                "c": v[1] / t ** 2 / n, "dln": v[5] / v[3] / t ** 2, "e": v[0] / n, "m": v[2]}

    def peak(self, key, lo, hi, steps=40):
        """(T, value) of the largest |key| on a grid where data support it."""
        best = None
        for i in range(steps + 1):
            t = lo + (hi - lo) * i / steps
            d = self.derived(t)
            if d and (best is None or abs(d[key]) > best[1]):
                best = (t, abs(d[key]))
        return best


def crossing(a, b, lo, hi, steps=40):
    """Temperature where the Binder cumulants of two sizes cross (ordered side larger L above)."""
    prev, hits = None, []
    for i in range(steps + 1):
        t = lo + (hi - lo) * i / steps
        x, y = a.derived(t), b.derived(t)
        if not x or not y:
            prev = None
            continue
        d = y["U"] - x["U"]
        if prev and prev[1] > 0 >= d:
            hits.append(prev[0] + (t - prev[0]) * prev[1] / (prev[1] - d))
        prev = (t, d)
    return hits


def heat_peaks(ens, t):
    """Energies per site of the two phases from the reweighted histogram, and the dip."""
    s = min(ens.sets, key=lambda x: abs(x[0] - t))
    k, n = 1 / t - 1 / s[0], ens.L ** 2
    es = s[1]
    e0, lo, hi = mean(es), min(es), max(es)
    bins, width = [0.0] * 40, (hi - lo) / 40 or 1
    for e in es:
        bins[min(39, int((e - lo) / width))] += math.exp(-k * (e - e0))
    sm = [mean(bins[max(0, i - 2):i + 3]) for i in range(40)]
    best = None
    for c in range(6, 34):
        a, b = max(range(0, c), key=sm.__getitem__), max(range(c, 40), key=sm.__getitem__)
        if b - a < 6:
            continue
        dip = min(sm[a:b + 1]) / min(sm[a], sm[b]) if min(sm[a], sm[b]) else 1
        if best is None or dip < best[0]:
            best = (dip, (b - a) * width / n, sm[a] / sm[b])
    return best or (1.0, 0.0, 1.0)
