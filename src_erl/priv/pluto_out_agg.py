"""Grouping, aggregates and cross-job comparison of records."""

import math
import random
import re
from array import array

import pluto_job_defs as d
from pluto_out_fields import QueryError, is_nan, num, show, unit_of

_PCT = re.compile(r"p(\d{1,2}(?:\.\d+)?|100)\Z")
SIMPLE = ("min", "max", "sum", "mean", "first", "last", "distinct", "argmin", "argmax")


class Spec:
    """One parsed aggregate, such as "p95:dur"."""

    def __init__(self, text):
        self.text = text
        fn, _, field = text.partition(":")
        fn, self.field = fn.strip(), field.strip()
        m = _PCT.match(fn)
        self.pct = float(m[1]) / 100 if m else None
        self.fn = "pct" if m else fn
        if fn == "count":
            return
        if not m and fn not in SIMPLE:
            raise QueryError("agg %r: unknown function %r; use count, %s or p50 p95 p99"
                             % (text[:80], fn[:40], ", ".join(SIMPLE)))
        if not self.field:
            raise QueryError("agg %r: %s needs a field, \"%s:field\"" % (text[:80], fn, fn))


class Budget:
    """What all groups together may keep in memory."""

    def __init__(self):
        self.values = d.VALUES_MAX
        self.distinct = d.DISTINCT_MAX


class Numbers:
    """Values for percentiles: exact until the budget is gone, then a
    uniform sample of what was seen."""

    def __init__(self, budget):
        self.budget, self.vals, self.seen = budget, array("d"), 0
        self.rng = None

    def add(self, x):
        self.seen += 1
        if self.budget.values > 0:
            self.budget.values -= 1
            self.vals.append(x)
            return
        self.rng = self.rng or random.Random(1)
        i = self.rng.randrange(self.seen)
        if i < len(self.vals):
            self.vals[i] = x

    def at(self, q):
        v = sorted(self.vals)
        if not v:
            return None
        pos = q * (len(v) - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, len(v) - 1)
        if pos == lo or v[lo] == v[hi]:
            return v[lo]             # inf times 0, or inf - inf, would be nan
        return v[lo] + (v[hi] - v[lo]) * (pos - lo)


class Column:
    """Everything asked about one field within one group."""

    def __init__(self, fns, budget):
        self.n = self.nan = 0
        self.lo = self.hi = self.lo_at = self.hi_at = None
        self.sum, self.comp = 0, 0.0
        self.first = self.last = None
        self.budget = budget
        self.numbers = Numbers(budget) if "pct" in fns else None
        self.values = set() if "distinct" in fns else None
        self.over = False

    def add(self, v, at):
        if self.first is None:
            self.first = v
        self.last = v
        if self.values is not None and v not in self.values:
            if self.budget.distinct > 0:
                self.budget.distinct -= 1
                self.values.add(v)
            else:
                self.over = True
        x = num(v)
        if x is None:
            return
        if is_nan(x):
            self.nan += 1
            return
        self.n += 1
        if self.lo is None or x < self.lo:
            self.lo, self.lo_at = x, at
        if self.hi is None or x > self.hi:
            self.hi, self.hi_at = x, at
        if type(x) is int and type(self.sum) is int:
            self.sum += x
        else:
            # Neumaier: millions of small values must not drift.
            s, t = self.sum, self.sum + x
            if math.isfinite(t):
                self.comp += (s - t) + x if abs(s) >= abs(x) else (x - t) + s
            self.sum = t
        if self.numbers is not None:
            self.numbers.add(float(x))

    def total(self):
        return self.sum + self.comp if self.comp and math.isfinite(self.comp) else self.sum

    def get(self, spec):
        fn = spec.fn
        if fn == "distinct":
            return len(self.values)
        if fn in ("first", "last"):
            return self.first if fn == "first" else self.last
        if not self.n:
            return float("nan") if self.nan else None
        if fn == "min":
            return self.lo
        if fn == "max":
            return self.hi
        if fn == "sum":
            return self.total()
        if fn == "mean":
            try:
                return self.total() / self.n
            except OverflowError:
                return float("inf")
        if fn == "pct":
            return self.numbers.at(spec.pct)
        return self.lo_at if fn == "argmin" else self.hi_at

    def text(self, spec):
        s = show(self.get(spec))
        if spec.fn == "distinct" and self.over:
            return ">=" + s
        if spec.fn == "pct" and self.numbers.seen > len(self.numbers.vals):
            return "~" + s
        return s


def hashable(v):
    return v if type(v) in (str, int, float, bool) or v is None else show(v)


class Groups:
    """Aggregates per distinct value of the ``by`` fields."""

    def __init__(self, by, aggs):
        self.by = by
        self.specs = [Spec(a) for a in aggs] or [Spec("count")]
        self.needs = {}
        for s in self.specs:
            if s.fn != "count":
                self.needs.setdefault(s.field, set()).add(s.fn)
        self.budget = Budget()
        self.groups = {}
        self.records = self.dropped = 0

    def _new(self):
        return [0, {f: Column(fns, self.budget) for f, fns in self.needs.items()}]

    def add(self, rec, at):
        key = tuple([hashable(rec.get(b)) for b in self.by])
        g = self.groups.get(key)
        self.records += 1
        if g is None:
            if len(self.groups) >= d.GROUPS_MAX:
                self.dropped += 1
                return
            g = self.groups[key] = self._new()
        g[0] += 1
        for f, col in g[1].items():
            v = rec.get(f)
            if v is not None:
                col.add(hashable(v), at)

    def merged_total(self):
        """The total row: only what can be combined from the groups."""
        n = sum(g[0] for g in self.groups.values())
        cells = []
        for s in self.specs:
            cols = [g[1][s.field] for g in self.groups.values()] if s.field else []
            full = [c for c in cols if c.n]
            if s.fn == "count":
                cells.append(show(n))
            elif s.fn in ("min", "max") and full:
                cells.append(show(min(c.lo for c in full) if s.fn == "min"
                                  else max(c.hi for c in full)))
            elif s.fn in ("sum", "mean") and full:
                t = math.fsum(c.total() for c in full) if any(
                    type(c.total()) is float for c in full) and all(
                    math.isfinite(c.total()) for c in full) \
                    else sum(c.total() for c in full)
                cells.append(show(t if s.fn == "sum" else t / sum(c.n for c in full)))
            elif s.fn == "distinct":
                cells.append(show(len(set().union(*[c.values for c in cols]))))
            elif s.fn == "pct" and full:
                allv = Numbers(Budget())
                for c in full:
                    for x in c.numbers.vals:
                        allv.add(x)
                cells.append(show(allv.at(s.pct)))
            else:
                cells.append("-")
        return cells

    def rows(self, sort, limit):
        """(header, text rows, number of groups, records shown)."""
        items = [(k, g) for k, g in self.groups.items()]
        labels = [s.text for s in self.specs]
        desc, name = False, sort or ""
        if name.startswith("-"):
            desc, name = True, name[1:]
        if not name:
            items.sort(key=lambda kv: [order(x) for x in kv[0]])
            if self.by and not set(self.by) & {"_job", "_name"} and len(items) > limit:
                items.sort(key=lambda kv: -kv[1][0])      # most frequent first
        elif name in self.by:
            i = self.by.index(name)
            items.sort(key=lambda kv: order(kv[0][i]), reverse=desc)
        elif name in labels or name == "count":
            s = self.specs[labels.index(name)] if name in labels else Spec("count")
            items.sort(key=lambda kv: order(
                kv[1][0] if s.fn == "count" else kv[1][1][s.field].get(s)), reverse=desc)
        else:
            raise QueryError("sort=%r: use one of %s, with a leading - for descending"
                             % (sort[:80], ", ".join(self.by + labels) or "count"))
        shown = items[:limit]
        rows = [[show(x) for x in k] + [
            show(g[0]) if s.fn == "count" else g[1][s.field].text(s)
            for s in self.specs] for k, g in shown]
        return self.by + labels, rows, len(items), sum(g[0] for _, g in shown)

    def nans(self):
        out = {}
        for _, cols in self.groups.values():
            for f, c in cols.items():
                if c.nan:
                    out[f] = out.get(f, 0) + c.nan
        return out


def order(v):
    """Sort key: numbers by value, then words, then missing."""
    x = num(v)
    if x is not None and not is_nan(x):
        return (0, x, "")
    return (2, 0, "") if v is None else (1, 0, show(v))


class Compare:
    """Where do jobs disagree about fields, for records with equal keys?
    The n-th record of a key in one job is compared with the n-th in the
    others. Jobs are bits of an integer, so agreement costs nothing."""

    def __init__(self, by, fields):
        self.by, self.fields = by, fields
        self.keys = {}              # (key, occurrence) -> {values: jobs}
        self.seen = {}              # job -> {key: count}
        self.full = False

    def add(self, job, rec):
        key = tuple([hashable(rec.get(b)) for b in self.by])
        vals = []
        for f in self.fields:
            v = rec.get(f)
            x = num(v)
            vals.append(hashable(v) if x is None else "nan" if is_nan(x) else x)
        if all(v is None for v in vals):
            return
        counts = self.seen.setdefault(job, {})
        n = counts[key] = counts.get(key, 0) + 1
        at = self.keys.get((key, n))
        if at is None:
            if len(self.keys) * max(1, len(self.fields)) >= d.CELLS_MAX:
                self.full = True
                return
            at = self.keys[key, n] = {}
        vals = tuple(vals)
        at[vals] = at.get(vals, 0) | 1 << job

    def report(self, labels, limit):
        jobs = len(labels)
        everyone = (1 << jobs) - 1
        present = 0
        agree, odd, missing, odd_jobs, miss_jobs = 0, [], [], {}, {}
        for at in self.keys.values():
            for bits in at.values():
                present |= bits
        for (key, n), at in self.keys.items():
            have = 0
            for bits in at.values():
                have |= bits
            lacking = present & ~have
            if lacking:
                missing.append(key)
                for j in _bits(lacking):
                    miss_jobs.setdefault(j, []).append(key)
            if len(at) == 1:
                agree += not lacking
                continue
            ranked = sorted(at.items(), key=lambda kv: (-_count(kv[1]), kv[1] & -kv[1]))
            tie = _count(ranked[0][1]) == _count(ranked[1][1])
            odd.append((key, n, ranked, tie))
            for _, bits in ranked[0 if tie else 1:]:
                for j in _bits(bits):
                    odd_jobs[j] = odd_jobs.get(j, 0) + 1

        def names(bits):
            js = [labels[j] for j in _bits(bits)]
            return ",".join(js[:6]) + ("+%d" % (len(js) - 6) if len(js) > 6 else "")

        def value(vals):
            return vals[0] if len(vals) == 1 else \
                " ".join("%s=%s" % (f, show(v)) for f, v in zip(self.fields, vals))
        out = ["keys: %d, agree in all jobs: %d, differ: %d, missing in some job: %d"
               % (len(self.keys), agree, len(odd), len(missing))]
        if self.full:
            out.append("too many keys; the rest was not compared")
        silent = everyone & ~present
        if silent:
            out.append("no such records in: " + names(silent))
        if odd_jobs:
            out.append("odd one out: " + ", ".join(
                "%s %dx" % (labels[j], c) for j, c in
                sorted(odd_jobs.items(), key=lambda kv: (-kv[1], kv[0]))[:20]))
        for j, ks in sorted(miss_jobs.items(), key=lambda kv: -len(kv[1]))[:10]:
            ks = sorted(ks, key=_key_order)
            out.append("missing in %s: %d keys, from %s to %s" % (
                labels[j], len(ks), self._key(ks[0], 1), self._key(ks[-1], 1)))
        odd.sort(key=lambda o: (_key_order(o[0]), o[1]))
        for key, n, ranked, tie in odd[:limit]:
            parts = ["%s (%s)" % (show(value(v)), "%d jobs" % _count(b)
                                  if i == 0 and not tie and _count(b) > 1
                                  else names(b))
                     for i, (v, b) in enumerate(ranked[:5])]
            if len(ranked) > 5:
                parts.append("+%d values" % (len(ranked) - 5))
            out.append("%s: %s" % (self._key(key, n), " | ".join(parts)))
        if len(odd) > limit:
            out.append("(%d of %d differing keys shown; raise limit for more)"
                       % (limit, len(odd)))
        return out

    def _key(self, key, n):
        k = " ".join("%s=%s" % (b, show(v)) for b, v in zip(self.by, key))
        return (k or "record") + (" #%d" % n if n > 1 or not self.by else "")


def _bits(x):
    while x:
        low = x & -x
        yield low.bit_length() - 1
        x ^= low


def _count(x):
    return bin(x).count("1")


def _key_order(key):
    return [order(v) for v in key]


class Describe:
    """Per-field summary, for a query that only says how to extract."""

    def __init__(self):
        self.fields, self.n = {}, 0

    def add(self, rec, at):
        self.n += 1
        for f, v in rec.items():
            s = self.fields.get(f)
            if s is None:
                if len(self.fields) >= d.FIELDS_MAX:
                    continue
                s = self.fields[f] = [0, 0, None, None, 0.0, {}, ""]
            s[0] += 1
            x = num(v)
            if x is not None and not is_nan(x):
                s[1] += 1
                s[2] = x if s[2] is None or x < s[2] else s[2]
                s[3] = x if s[3] is None or x > s[3] else s[3]
                s[4] += float(x)
                s[6] = s[6] or unit_of(v)
            top = s[5]
            v = hashable(v)
            if v in top:
                top[v] += 1
            elif len(top) < 1000:
                top[v] = 1

    def rows(self):
        out = []
        for f, (n, nums, lo, hi, total, top, unit) in self.fields.items():
            many = ">=" if len(top) >= 1000 else ""
            best = sorted(top.items(), key=lambda kv: -kv[1])[:3]
            common = "" if best and best[0][1] == 1 else ", ".join(
                "%s x%d" % (show(v)[:40], c) for v, c in best)
            if nums > n / 2:
                what = "min %s max %s mean %s%s" % (
                    show(lo), show(hi), show(total / nums), " " + unit if unit else "")
                if nums < n:
                    what += ", %d not numeric" % (n - nums)
                if len(top) <= 3 and common:
                    what += "; " + common
            else:
                what = common or "e.g. " + show(next(iter(top), ""))[:60]
            out.append([f, show(n), many + show(len(top)), what])
        return ["field", "n", "distinct", "values"], out
