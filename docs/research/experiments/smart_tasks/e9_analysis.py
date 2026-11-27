"""Analysis used by the scripted E9 solvers; it reads only what an agent can see
(the workspace files and the programs' output)."""

import bisect
import math
import os

E = 0.005                                     # per-base error used in the models
GTS = ("0/0", "0/1", "1/1")


class Panel:
    def __init__(self, ws):
        rows = [x.rstrip("\n").split("\t") for x in open(os.path.join(ws, "ref/sites.tsv"))][1:]
        self.ids = [r[0] for r in rows]
        self.pos = [int(r[1]) for r in rows]
        self.ref, self.alt = [r[2] for r in rows], [r[3] for r in rows]
        self.af = [float(r[4]) for r in rows]
        self.at = {p: k for k, p in enumerate(self.pos)}
        self.manifest = dict(x.split() for x in list(open(os.path.join(ws, "manifest.tsv")))[1:])
        lines = [x.split() for x in open(os.path.join(ws, "array/fingerprints.tsv"))]
        cols = [self.ids.index(c) for c in lines[0][1:]]
        self.array = {r[0]: dict(zip(cols, map(int, r[1:]))) for r in lines[1:]}


class RunData:
    """Facts of one run's output: per-site allele observations and per-read
    error counts, by read index. ``strict`` applies the analysis standard."""

    def __init__(self, panel, strict=True):
        self.p, self.strict = panel, strict
        self.obs = []                               # (index, site, allele 0/1)
        self.err = {}                               # index -> (hq mismatches, bases) off-site
        self.seq, self.last = [], 0

    def feed(self, line):
        if not line or line[0] in "@#":
            return
        f = line.split("\t")
        if len(f) < 13:
            return
        idx, flag = int(f[0].rsplit(":", 1)[1]), int(f[1])
        self.last = max(self.last, idx)
        if flag & 4:
            return
        if self.strict and (flag & 1024 or int(f[4]) < 20):
            return
        pos, cig = int(f[3]), f[5]
        m = int(cig.split("M")[0].split("S")[-1])
        mm = {}
        if f[12] != "XM:Z:-":
            for x in f[12][5:].split(","):
                a, b, q = x.split(":")
                mm[int(a)] = (b, int(q))
        p = self.p
        lo, hi = bisect.bisect_left(p.pos, pos), bisect.bisect_left(p.pos, pos + m)
        for k in range(lo, hi):
            b, q = mm.get(p.pos[k], (p.ref[k], 99))
            if self.strict and q < 20:
                continue
            if b == p.ref[k] or b == p.alt[k]:
                self.obs.append((idx, k, int(b == p.alt[k])))
        hq = sum(1 for a, (b, q) in mm.items() if q >= 20 and a not in p.at)
        self.err[idx] = (hq, m - (hi - lo))
        self.seq.append(idx)

    def tail_rate(self, n=150):
        xs = [self.err[i] for i in self.seq[-n:]]
        return sum(a for a, _ in xs) / max(1, sum(b for _, b in xs)) if len(xs) == n else 0.0

    def counts(self, lo=0, hi=10**9):
        c = [[0, 0] for _ in self.p.pos]
        for i, k, a in self.obs:
            if lo <= i < hi:
                c[k][a] += 1
        return c


def onset(run, min_reads=40):
    """(first degraded read, rate before, rate after) or None if no clear rise."""
    idx = sorted(run.err)
    if len(idx) < 400:
        return None
    mm = [run.err[i][0] for i in idx]
    nb = [max(1, run.err[i][1]) for i in idx]
    tail_m, tail_b = sum(mm[-min_reads * 3:]), sum(nb[-min_reads * 3:])
    if tail_m / tail_b < 0.015:
        return None
    pm, pb = [0], [0]
    for a, b in zip(mm, nb):
        pm.append(pm[-1] + a)
        pb.append(pb[-1] + b)
    best, cut = -math.inf, None
    for t in range(200, len(idx) - min_reads):
        r1 = max(pm[t], .5) / pb[t]
        m2, b2 = pm[-1] - pm[t], pb[-1] - pb[t]
        r2 = max(m2, .5) / b2
        if r2 <= r1:
            continue
        ll = pm[t] * math.log(r1) - r1 * pb[t] + m2 * math.log(r2) - r2 * b2
        if ll > best:
            best, cut = ll, t
    if cut is None:
        return None
    return idx[cut], pm[cut] / pb[cut], (pm[-1] - pm[cut]) / (pb[-1] - pb[cut])


def _lik(a, n, f):
    f = E + (1 - 2 * E) * f
    return a * math.log(f) + (n - a) * math.log(1 - f)


def call(counts, panel, prior=True):
    """Genotypes 0/1/2 (None without data)."""
    out = []
    for (r, a), p in zip(counts, panel.af):
        if r + a == 0:
            out.append(None)
            continue
        pri = (math.log((1 - p) ** 2), math.log(2 * p * (1 - p)), math.log(p * p)) if prior else (0, 0, 0)
        ll = [_lik(a, r + a, g / 2) + pri[g] for g in range(3)]
        out.append(max(range(3), key=ll.__getitem__))
    return out


def naive_call(counts):
    out = []
    for r, a in counts:
        n = r + a
        out.append(None if not n else 0 if a < .2 * n else 2 if a > .8 * n else 1)
    return out


def _best_c(fn, lo=0.0, hi=0.4):
    for step in (0.01, 0.001, 0.0001):
        grid = [lo + step * i for i in range(int(round((hi - lo) / step)) + 1)]
        c = max(grid, key=fn)
        lo, hi = max(0.0, c - step), c + step
    return c


def freemix(counts, panel):
    """Contamination fraction from one library without knowing the contaminant."""
    rows = [(r, a, p) for (r, a), p in zip(counts, panel.af) if r + a]

    def ll(c):
        tot = 0.0
        for r, a, p in rows:
            hw = ((1 - p) ** 2, 2 * p * (1 - p), p * p)
            s = 0.0
            for g in range(3):
                for h in range(3):
                    f = E + (1 - 2 * E) * ((1 - c) * g / 2 + c * h / 2)
                    s += hw[g] * hw[h] * f ** a * (1 - f) ** r
            tot += math.log(s) if s > 0 else -1e9
        return tot
    return _best_c(ll, 0.0, 0.3)


def mix_fit(counts, own, other):
    """(log likelihood, fraction) of a mixture of two known genotype vectors."""
    rows = [(r, a, g, h) for (r, a), g, h in zip(counts, own, other)
            if r + a and g is not None and h is not None]

    def ll(c):
        return sum(_lik(a, r + a, (1 - c) * g / 2 + c * h / 2) for r, a, g, h in rows)
    c = _best_c(ll, 0.0, 0.4)
    return ll(c), c


def identity(gts, panel, min_conc=0.9):
    best, who = 0.0, None
    for ind, row in panel.array.items():
        both = [(gts[k], g) for k, g in row.items() if gts[k] is not None]
        conc = sum(a == b for a, b in both) / max(1, len(both))
        if conc > best:
            best, who = conc, ind
    return who if best >= min_conc else None


def kinship(g1, g2):
    """(KING-robust kinship, IBS0 count)."""
    het1 = het2 = hh = ibs0 = 0
    for a, b in zip(g1, g2):
        if a is None or b is None:
            continue
        het1 += a == 1
        het2 += b == 1
        hh += a == 1 and b == 1
        ibs0 += abs(a - b) == 2
    return (hh - 2 * ibs0) / max(1, het1 + het2), ibs0


def relatives(gts, max_ibs0_po=3):
    """[{"samples": [a, b], "relation": ...}] from genotypes by sample number."""
    out, ss = [], sorted(gts)
    for i, s in enumerate(ss):
        for t in ss[i + 1:]:
            phi, z0 = kinship(gts[s], gts[t])
            if phi > 0.354:
                rel = "duplicate"
            elif phi > 0.177:
                rel = "parent_offspring" if z0 <= max_ibs0_po else "sibling"
            else:
                continue
            out.append({"samples": [s, t], "relation": rel})
    return out


def frequencies(gts, rel, panel, sites):
    """Alt allele frequency per site id over distinct persons."""
    drop = {max(x["samples"]) for x in rel if x["relation"] == "duplicate"}
    keep = [s for s in gts if s not in drop]
    out = {}
    for sid in sites:
        k = panel.ids.index(sid)
        xs = [gts[s][k] for s in keep if gts[s][k] is not None]
        out[sid] = sum(xs) / (2 * len(xs)) if xs else 0.0
    return out
