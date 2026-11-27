"""One record per job: named columns, each over its own choice of lines.

    first_violation = first:epoch,bucket where probe > last.limit
    spikes          = list:epoch where disp >= 90

All columns of all jobs are computed in one pass. Values are JSON: a field
whose template says {x:int} or {x:float} is a number, any other text.
"""

import math
import re

import pluto_job_defs as d
from pluto_out_fields import (PSEUDO, QueryError, clean, extractor, num, number_fields,
                              pseudo)
from pluto_out_where import Where, new_memory

FUNCTIONS = ("first", "last", "list", "count", "sum", "min", "max", "mean")
_NAME = re.compile(r"[A-Za-z_]\w*\Z")
_WHERE = re.compile(r"(?:\A|\s)where\s")


class Column:
    """NAME = FN:FIELD[,FIELD] [where COND]"""

    def __init__(self, text):
        name, eq, what = text.partition("=")
        self.name = name.strip()
        if not eq or not _NAME.match(self.name):
            raise QueryError("column %r: write NAME=FN:FIELD [where COND]" % text[:120])
        what, *cond = _WHERE.split(what, maxsplit=1)
        fn, colon, names = what.strip().partition(":")
        if not colon and fn != "count":
            fn, names = "first", fn
        self.fn = fn.strip()
        if self.fn not in FUNCTIONS:
            raise QueryError("column %s: unknown function %r; use %s" % (
                self.name, self.fn, ", ".join(FUNCTIONS)))
        self.fields = [f.strip() for f in names.split(",") if f.strip()]
        if not self.fields and self.fn != "count":
            raise QueryError("column %s: %s needs a field" % (self.name, self.fn))
        if len(self.fields) > 1 and self.fn not in ("first", "last", "list"):
            raise QueryError("column %s: %s takes one field" % (self.name, self.fn))
        self.where = Where(cond)

    def start(self, numeric):
        return _Cell(self, numeric)


class _Cell:
    """One column of one job."""

    def __init__(self, column, numeric):
        self.c, self.numeric = column, numeric
        self.memory = new_memory() if column.where.uses_memory else None
        self.n, self.value, self.total = 0, None, 0
        self.seen = {}
        self.over = False

    def add(self, rec):
        c = self.c
        if not c.where.test(rec, self.memory):
            return
        if not c.fields:
            self.n += 1
            return
        values = [rec.get(f) for f in c.fields]
        if all(v is None for v in values):
            return
        self.n += 1
        fn = c.fn
        if fn == "first":
            if self.n == 1:
                self.value = values
        elif fn == "last":
            self.value = values
        elif fn == "list":
            key = tuple(map(str, values))
            if key not in self.seen:
                if len(self.seen) >= d.REPORT_LIST_MAX:
                    self.over = True
                else:
                    self.seen[key] = values
        elif fn != "count":
            x = num(values[0])
            if x is None or x != x:
                self.n -= 1
            elif fn in ("sum", "mean"):
                self.total += x
            elif self.n == 1 or (x < self.value if fn == "min" else x > self.value):
                self.value = x

    def _out(self, values):
        shown = [_json(v, f in self.numeric) for v, f in zip(values, self.c.fields)]
        return shown[0] if len(shown) == 1 else dict(zip(self.c.fields, shown))

    def result(self):
        fn = self.c.fn
        if fn == "count":
            return self.n
        if fn == "list":
            return [self._out(v) for v in self.seen.values()]
        if not self.n:
            return None
        if fn in ("first", "last"):
            return self._out(self.value)
        if fn in ("sum", "mean"):
            return _json(self.total / self.n if fn == "mean" else self.total, True)
        return _json(self.value, True)


def _json(v, numeric):
    """A value JSON has: no NaN or infinity."""
    if v is None:
        return None
    if type(v) is bool:
        return v
    x = num(v) if numeric or type(v) in (int, float) else None
    if x is None:
        return v if type(v) is str else str(v)
    return x if type(x) is int or math.isfinite(x) else None


class Report:
    def __init__(self, req, jobs):
        columns = [Column(c) for c in req["columns"]]
        if len({c.name for c in columns}) < len(columns):
            raise QueryError("two columns have the same name")
        if len(columns) > d.REPORT_COLUMNS_MAX:
            raise QueryError("more than %d columns" % d.REPORT_COLUMNS_MAX)
        self.columns, self.jobs = columns, jobs
        need = {}
        for c in columns:
            need.update(dict.fromkeys(c.fields))
            need.update(c.where.fields)
        self.need = list(need)
        self.pseudo = [k for k in need if k in PSEUDO]
        spec = req.get("extract")
        numeric = number_fields(spec) | {"_n", "_offset", "_time"}
        self.extract = [extractor(spec, j.head) for j in jobs]
        self.cells = [[c.start(numeric) for c in columns] for _ in jobs]
        self.seen = set()

    def block(self, job, block, guard):
        extract, cells = self.extract[job.i], self.cells[job.i]
        for offset, raw in block:
            n = job.n
            job.n += 1
            guard.mark += 1
            text = clean(raw)
            if not text:
                continue
            rec = extract(text)
            if rec is None:
                if not self.pseudo:
                    continue
                rec = {}
            if self.pseudo:
                rec = dict(rec)
                rec.update(pseudo(self.pseudo, job, text, n, offset))
            self.seen.update(rec)
            for cell in cells:
                # A first value that was found does not change any more.
                if not (cell.n and cell.c.fn == "first"):
                    cell.add(rec)

    def result(self):
        missing = [k for k in self.need if k not in self.seen]
        notes = []
        if missing:
            notes.append("no line has %s; fields: %s" % (
                ", ".join(missing), ", ".join(sorted(self.seen)[:30]) or "none"))
        for cells in self.cells:
            notes += ["%s: more than %d values" % (x.c.name, d.REPORT_LIST_MAX)
                      for x in cells if x.over]
        return {"records": [{x.c.name: x.result() for x in cells} for cells in self.cells],
                "notes": list(dict.fromkeys(notes))}
