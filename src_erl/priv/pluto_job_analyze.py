#!/usr/bin/env python3
"""Answer one job_analyze request (stdlib only).

    pluto_job_analyze.py            request JSON line on stdin
    pluto_job_analyze.py check      {"rules", "params"} on stdin, only validated

Prints one JSON object: {"out": text, "scanned": ..., "resume": {...}} or
{"error": why}. Runs confined: whatever a pattern or the output does, only
this process suffers. Scans jobs in turns, so a deadline cuts them evenly.
"""

import difflib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pluto_job_defs as d  # noqa: E402  (needs the path above)
import pluto_out_agg as agg  # noqa: E402
import pluto_out_fields as fields  # noqa: E402
import pluto_out_lines as lines  # noqa: E402
from pluto_out_fields import QueryError, show  # noqa: E402
from pluto_out_outline import Outline  # noqa: E402
from pluto_out_where import Where, new_memory  # noqa: E402


class Job:
    def __init__(self, i, spec):
        self.i, self.id = i, spec["id"]
        self.name = spec.get("name") or spec["id"]
        self.label = spec["id"]
        self.path, self.hi = spec["path"], spec["hi"]
        self.start = spec["from"]
        self.reader = lines.Reader(self.path, spec["from"], self.hi,
                                   spec.get("line"), spec["closed"])
        self.has_lines = spec.get("line") is not None
        self.n = spec.get("line") or 1
        self.head = lines.head_lines(self.path, self.hi)
        self.memory = {}            # by-group -> what conditions remember
        self.lines = self.records = self.matched = 0
        self.kept = []
        self.done = False

    def at(self, n, offset):
        return n if self.has_lines else offset


class Query:
    """The record pipeline: extract, where, then every asked reduction."""

    def __init__(self, req, jobs):
        self.req, self.jobs = req, jobs
        self.by = list(req.get("by") or [])
        self.where = Where(req.get("where"))
        self.rows = req.get("rows")
        self.columns = self.rows if isinstance(self.rows, list) else None
        self.compare = agg.Compare(self.by, req["compare"]) if req.get("compare") else None
        aggs = req.get("agg")
        wanted = bool(aggs or (self.by and not self.compare and not self.rows))
        self.per_job = wanted and not self.by and len(jobs) > 1
        self.groups = agg.Groups(["_job"] if self.per_job else self.by, aggs or []) \
            if wanted else None
        # Saying only how to extract asks what there is to see.
        self.describe = agg.Describe() if not (
            self.groups or self.rows or self.compare) and req.get("extract") else None
        if not (self.groups or self.rows or self.compare or self.describe):
            self.rows = True
        need = dict.fromkeys(self.by)
        need.update(self.where.fields)
        need.update(dict.fromkeys(self.columns or ()))
        need.update(dict.fromkeys(req.get("compare") or ()))
        need.update(dict.fromkeys(self.groups.needs if self.groups else ()))
        if self.per_job:
            need["_job"] = None
        self.need = need
        self.pseudo = [k for k in need if k in fields.PSEUDO]
        for k in need:
            if k.startswith("_") and k not in fields.PSEUDO and req.get("extract") in (
                    None, "", "auto"):
                raise QueryError("unknown field %r; there are %s" % (
                    k, ", ".join(fields.PSEUDO)))
        plain = [k for k in need if k not in fields.PSEUDO]
        self.extract = [
            fields.extractor(req.get("extract"), j.head)
            if plain or req.get("extract") else (lambda text: {}) for j in jobs]
        self.sort = req.get("sort")
        self.key = (self.sort or "").lstrip("-")
        if self.rows and self.key and not self.groups:
            need.setdefault(self.key)
            if self.key in fields.PSEUDO and self.key not in self.pseudo:
                self.pseudo.append(self.key)
        self.seen, self.unit, self.units = {}, {}, {}

    def block(self, job, block):
        extract, where, by = self.extract[job.i], self.where, self.by
        pseudo, uses_memory, seen = self.pseudo, where.uses_memory, self.seen
        guard = self.guard
        groups, describe, compare, rows = (self.groups, self.describe,
                                           self.compare, self.rows)
        for offset, raw in block:
            n = job.n
            job.n += 1
            guard.mark += 1
            text = fields.clean(raw)
            if not text:
                continue
            rec = extract(text)
            if rec is None:
                if not pseudo or self.req.get("extract"):
                    continue
                rec = {}
            job.records += 1
            if pseudo:
                rec.update(fields.pseudo(pseudo, job, text, n, offset))
            if len(seen) < d.FIELDS_MAX:
                for k in rec:
                    if k not in seen:
                        seen[k] = True
            for k in where.unitless:
                if k not in self.units and fields.unit_of(rec.get(k)):
                    self.units[k] = fields.unit_of(rec[k])
            before = None
            if uses_memory:
                key = tuple([agg.hashable(rec.get(b)) for b in by])
                memory = job.memory.get(key)
                if memory is None:
                    if len(job.memory) >= d.GROUPS_MAX:
                        continue
                    memory = job.memory[key] = new_memory()
                if rows:
                    before = {"%s.%s" % (p, k): v for p, m in memory.items()
                              for k, v in m.items()}
                if not where.test(rec, memory):
                    continue
            elif not where.test(rec):
                continue
            job.matched += 1
            at = "%s:%s" % (job.label, job.at(n, offset)) if len(self.jobs) > 1 \
                else job.at(n, offset)
            if groups:
                groups.add(rec, at)
            if describe:
                describe.add(rec, at)
            if compare:
                compare.add(job.i, rec)
            if rows:
                self.keep(job, n, offset, rec, text, before)

    def keep(self, job, n, offset, rec, text, before):
        kept = job.kept
        if len(kept) >= 2 * d.ANALYZE_KEEP:
            if not (self.key and not self.groups):
                return
            # Sorted: keep the best, decide when the buffer is full.
            kept.sort(key=lambda r: r[0], reverse=self.sort.startswith("-"))
            del kept[d.ANALYZE_KEEP:]
        key = agg.order(rec.get(self.key)) if self.key and not self.groups else None
        if self.columns:
            cells = ["%s=%s" % (c, show(rec.get(c))) for c in self.columns]
            body = " ".join(_clip(c) for c in cells)
        else:
            body = _clip(text.strip())
        if before:
            body += "  [%s]" % ", ".join(
                "%s=%s" % (k, _clip(show(v))) for k, v in before.items())
        kept.append((key, job.i, job.at(n, offset), body))

    # ── report ──
    def report(self, limit):
        out, jobs, many = [], self.jobs, len(self.jobs) > 1
        total, matched = sum(j.lines for j in jobs), sum(j.matched for j in jobs)
        records = sum(j.records for j in jobs)
        out.append("%d lines, %d records, %d matched" % (total, records, matched)
                   if self.where.fields else "%d lines, %d records" % (total, records))
        out += self.notes(records, matched)
        if self.groups and self.groups.records:
            out += self.group_table(limit)
        if self.describe and self.describe.n:
            head, rows = self.describe.rows()
            out.append("  ".join(head))
            out += ["  ".join(_clip(c) for c in r) for r in rows[:max(limit, 1)]]
            if len(rows) > limit:
                out.append("(%d of %d fields shown)" % (limit, len(rows)))
        if self.compare:
            out += self.compare.report([j.label if j.name == j.id else "%s(%s)" % (
                j.name[:40], j.id) for j in jobs], limit)
        if self.rows:
            out += self.row_list(limit, many)
        return out

    def notes(self, records, matched):
        out = []
        seen = [k for k in self.seen if k not in fields.PSEUDO]
        if records:
            for k in self.need:
                if k in self.seen:
                    continue
                if k in fields.PSEUDO:
                    out.append("note: no record has %s" % k)
                    continue
                close = difflib.get_close_matches(k, seen, 1)
                out.append("note: no record has field %r%s; fields: %s" % (
                    k, " (did you mean %r?)" % close[0] if close else "",
                    ", ".join(seen[:30]) or "none"))
        elif sum(j.lines for j in self.jobs):
            sample = next((x for j in self.jobs for x in j.head if x.strip()), "")
            out.append("note: no line matches extract=%s; first line: %s" % (
                self.req.get("extract") or "auto", _clip(sample)))
        for k, u in self.units.items():
            out.append("note: %s has units; numbers without one mean %s" % (
                k, {"s": "seconds", "B": "bytes"}.get(u, u)))
        if self.groups:
            for f, n in self.groups.nans().items():
                out.append("note: %s is nan in %d records (left out of numbers)" % (f, n))
            if self.groups.dropped:
                out.append("note: more than %d groups; %d records not grouped" % (
                    d.GROUPS_MAX, self.groups.dropped))
        long_lines = sum(j.reader.long_lines for j in self.jobs)
        if long_lines:
            out.append("note: %d lines cut at %d bytes" % (long_lines, d.LINE_MAX))
        return out

    def group_table(self, limit):
        g = self.groups
        head, rows, n, covered = g.rows(self.sort, limit)
        names = {j.label: j.name for j in self.jobs if j.name != j.id}
        if "_job" in head and names:
            i = head.index("_job")
            for r in rows:
                if r[i] in names:
                    r[i] = "%s(%s)" % (names[r[i]][:40], r[i])
        out = ["  ".join(head)]
        out += ["  ".join(_clip(c) for c in r) for r in rows]
        if n > len(rows):
            out.append("(%d of %d groups shown, %d of %d records; sort= and limit= "
                       "choose others)" % (len(rows), n, covered, g.records - g.dropped))
        if n > 1 and (self.per_job or n > len(rows)):
            total = g.merged_total()
            out.append("  ".join(["ALL"] + ["-"] * (len(head) - len(total) - 1) + total))
        return out

    def row_list(self, limit, many):
        picked, total = [], sum(j.matched for j in self.jobs)
        if self.key and not self.groups:
            every = [r for j in self.jobs for r in j.kept]
            every.sort(key=lambda r: r[0], reverse=self.sort.startswith("-"))
            picked = every[:limit]
        else:
            # The limit is per job; round robin, so that a result that gets
            # too long is short for every job alike.
            chars = 0
            for k in range(limit):
                turn = [j.kept[k] for j in self.jobs if k < len(j.kept)]
                chars += sum(len(r[3]) + 12 for r in turn)
                if not turn or chars > d.ANALYZE_TEXT * 3 // 4 and k:
                    break
                picked += turn
            picked.sort(key=lambda r: (r[1], r[2]))
        labels = [j.label for j in self.jobs]
        out, shown = [], {}
        for _, i, at, body in picked:
            shown[i] = shown.get(i, 0) + 1
            out.append("%s%s%s: %s" % (labels[i] + " " if many else "",
                                       "L" if self.jobs[i].has_lines else "@", at, body))
        if len(picked) < total:
            out.append("(%d of %d rows shown)" % (len(picked), total))
            if many:
                out.append("rows per job: " + ", ".join(
                    "%s %d" % (j.label, j.matched) for j in self.jobs if j.matched))
        return out


def _clip(s):
    s = s.replace("\n", " ").replace("\t", " ")
    return s if len(s) <= d.ANALYZE_CELL else s[:d.ANALYZE_CELL - 3] + "..."


def scan(jobs, work, deadline, guard):
    """Blocks of every job in turn until all are read or time is up."""
    waiting = list(jobs)
    slow = []
    while waiting:
        for job in list(waiting):
            if time.monotonic() > deadline:
                return slow
            block = job.reader.block(d.ANALYZE_BLOCK)
            if block is None:
                job.done = True
                waiting.remove(job)
                continue
            job.lines += len(block)
            while block:
                first, guard.armed = job.n, True
                try:
                    work(job, block)
                    guard.armed = False
                    break
                except lines.TooSlow:
                    guard.armed = False
                    # Resume after the line that was being matched.
                    slow.append("%s:%d" % (job.label, job.n - 1))
                    if len(slow) >= d.SLOW_LINES_MAX:
                        raise QueryError(
                            "pattern too slow (backtracking) at lines %s; use a template "
                            "or a simpler regex" % ", ".join(slow))
                    block = block[job.n - first:]
    return slow


def outline(jobs, limit, deadline, guard):
    o = Outline()

    def work(job, block):
        for offset, raw in block:
            n = job.n
            job.n += 1
            guard.mark += 1
            if b"\x1b" in raw or b"\r" in raw:
                raw = fields.clean(raw).encode()
            o.add(raw, job.i, n if has_lines else offset)
    has_lines = all(j.has_lines for j in jobs)
    slow = scan(jobs, work, deadline, guard)
    labels = [j.label if j.name == j.id else "%s(%s)" % (j.name[:40], j.id) for j in jobs]
    kinds = {}
    for j in jobs:
        k = fields.kind_of(j.head)
        kinds.setdefault(k, []).append(j)
    fmt = ", ".join("%s%s" % (k, "" if len(kinds) == 1 else " (%d jobs)" % len(v))
                    for k, v in kinds.items())
    out = ["%d lines in %d job%s, format: %s" % (
        o.lines, len(jobs), "" if len(jobs) == 1 else "s", fmt)]
    for k, v in kinds.items():
        if k != "plain":
            names = {}
            ex = fields.auto(v[0].head)
            for x in v[0].head:
                names.update(dict.fromkeys(ex(x) or ()))
            out.append("fields (extract=\"auto\"): " + ", ".join(list(names)[:40]))
    if o.lines:
        # Lines L.. of one job; with several, the job before the colon.
        out += o.report(labels, limit, has_lines)
    return out, slow


def analyze(req):
    started = time.monotonic()
    limit = max(1, min(req.get("limit") or d.ANALYZE_LIMIT, d.ANALYZE_LIMIT_MAX))
    jobs = [Job(i, s) for i, s in enumerate(req["jobs"])]
    deadline = started + d.ANALYZE_DEADLINE_S
    guard = lines.CpuGuard(d.SLOW_LINE_S)
    asked = any(req.get(k) for k in ("extract", "where", "by", "agg", "rows", "compare"))
    if asked:
        q = Query(req, jobs)
        q.guard = guard
        if q.groups:
            q.groups.rows(q.sort, 1)         # a bad sort fails before the scan
        slow = scan(jobs, q.block, deadline, guard)
        out = q.report(limit)
    else:
        out, slow = outline(jobs, limit, deadline, guard)
    guard.stop()
    if slow:
        out.insert(1, "note: pattern too slow on lines %s; they were skipped" % ", ".join(slow))
    res = {}
    late = [j for j in jobs if not j.done and j.reader.pos < j.hi]
    if late:
        left = sum(j.hi - j.reader.pos for j in late)
        out.insert(0, "INCOMPLETE: time limit; covers the first %d of %d bytes. Pass "
                   "resume to continue with the rest" % (
                       sum(j.reader.pos - j.start for j in jobs),
                       sum(j.reader.pos - j.start for j in jobs) + left))
        # Finished jobs are named too, so a resumed call does not repeat them.
        res["resume"] = {j.id: j.reader.pos for j in jobs}
    text, size = [], 0
    for i, row in enumerate(out):
        size += len(row) + 1
        if size > d.ANALYZE_TEXT:
            text.append("(result cut: %d of %d lines shown; narrow with where, by or "
                        "limit)" % (i, len(out)))
            break
        text.append(row)
    res["out"] = "\n".join(text)
    res["bytes"] = sum(j.reader.pos - j.start for j in jobs)
    return res


def main(argv):
    try:
        req = json.loads(sys.stdin.buffer.readline())
        if argv[:1] == ["check"]:
            lines.confine(d.WATCH_MEMORY, d.ANALYZE_CPU_S, d.ANALYZE_CPU_S)
            from pluto_out_rules import check_rule
            res = {"rules": [check_rule(r, req.get("params") or ()) for r in req["rules"]]}
        else:
            lines.confine(d.ANALYZE_MEMORY, d.ANALYZE_CPU_S,
                          d.ANALYZE_DEADLINE_S + 5)
            res = analyze(req)
    except QueryError as exc:
        res = {"error": str(exc)}
    except OSError as exc:
        res = {"error": "cannot read output: %s" % exc.strerror}
    except MemoryError:
        res = {"error": "query needs too much memory; narrow it"}
    data = json.dumps(res).encode()             # escaped, so never invalid UTF-8
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
