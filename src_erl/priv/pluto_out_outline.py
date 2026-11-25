"""What is in this output? Lines grouped into templates, without patterns.

Numbers, ids, times and paths are masked. Reading a line costs one cheap
key (digit runs collapsed) and a dictionary lookup; the masking proper runs
once per distinct key.
"""

import re

import pluto_job_defs as d
from pluto_out_fields import show

_MASK = re.compile(r"""
    (?P<ts>\d{4}[-/]\d\d[-/]\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:[.,]\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?
        | \b\d\d:\d\d:\d\d(?:[.,]\d+)? | \d\d/[A-Z][a-z]{2}/\d{4}:\d\d:\d\d:\d\d(?:\ [+-]\d{4})?)
  | (?P<uuid>\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b)
  | (?P<ip>\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b)
  | (?P<hex>\b0x[0-9a-fA-F]+\b | \b(?=[0-9a-f]{0,40}\d)(?=[0-9a-f]{0,40}[a-f])[0-9a-f]{7,64}\b)
  | (?P<path>(?:(?<=[\s"'=(:,])|^)(?:[a-zA-Z][a-zA-Z+]{1,9}://|~?/|\.{1,2}/)[^\s"'<>|,;:)]+
        | (?<![\w./])(?=[\w.\-]{0,80}[A-Za-z])[\w.\-]+(?:/[\w.\-]+)+)
  | (?P<n>(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)
  | (?P<d>\d+)
""", re.X)
_NUMS = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?")
_BAD = re.compile(
    r"\b(error|err|errors|fatal|fail(?:ed|ure|ures|s)?|exceptions?|traceback|panic|"
    r"critical|crit|abort(?:ed|ing)?|segfault|segmentation fault|core dumped|killed|"
    r"assert(?:ion)?|denied|refused|timed? ?out|unreachable|cannot|could not|"
    r"unable to|not found|no such|undefined|invalid|corrupt\w*|oom|out of memory|"
    r"nan|deadlock|leak)\b|[a-z](?:Error|Exception)\b", re.I)
_WARN = re.compile(r"\b(warn(?:ing|ings)?|deprecated|retry(?:ing)?|slow|skipp(?:ed|ing))\b", re.I)
#: "0 errors" and "errors=0" are good news.
_NONE = re.compile(r"\b(?:0|no) (?:errors?|failures?|failed|warnings?)\b|"
                   r"\b(?:errors?|failures?|failed|warnings?)[=: ]+0\b", re.I)
_LABEL = re.compile(r"([A-Za-z_][\w.\-]*)[\"']?\s*[=: (\[]*\s*\Z")
_DIGITS = bytes.maketrans(b"123456789", b"000000000")


def severity(text):
    """2 likely error, 1 likely warning, 0 neither."""
    text = _NONE.sub("", text[:2000])
    return 2 if _BAD.search(text) else 1 if _WARN.search(text) else 0


def mask(text):
    """(template, spans of the plain numbers in *text*)."""
    out, at, spans = [], 0, []
    for m in _MASK.finditer(text):
        kind = m.lastgroup
        out.append(text[at:m.start()])
        out.append("<n>" if kind == "d" else "<%s>" % kind)
        if kind == "n":
            spans.append(m.span())
        at = m.end()
    out.append(text[at:])
    return "".join(out), spans


class Template:
    __slots__ = ("text", "count", "first", "last", "where", "example", "labels",
                 "lo", "hi", "jobs", "sev", "values")

    def __init__(self, text, example, labels):
        self.text, self.example, self.labels = text, example, labels
        self.count = 0
        self.first = self.last = None    # (job, line or offset)
        self.lo = [None] * len(labels)
        self.hi = [None] * len(labels)
        self.jobs = {}                   # job -> [count, first, last]
        self.sev = severity(example)
        self.values = None               # words a merged position stood for

    def hit(self, job, at, nums):
        self.count += 1
        j = self.jobs.get(job)
        if j is None:
            self.jobs[job] = [1, at, at]
            if self.first is None:
                self.first = (job, at)
        else:
            j[0] += 1
            j[2] = at
        self.last = (job, at)
        lo, hi = self.lo, self.hi
        for i, s in enumerate(nums):
            if len(s) > 60 or i >= len(lo):
                continue
            x = float(s)
            if lo[i] is None:
                lo[i] = hi[i] = x
            elif x < lo[i]:
                lo[i] = x
            elif x > hi[i]:
                hi[i] = x

    def absorb(self, o):
        self.count += o.count
        for i in range(min(len(self.lo), len(o.lo))):
            if o.lo[i] is not None:
                self.lo[i] = o.lo[i] if self.lo[i] is None else min(self.lo[i], o.lo[i])
                self.hi[i] = o.hi[i] if self.hi[i] is None else max(self.hi[i], o.hi[i])
        for j, (n, a, b) in o.jobs.items():
            mine = self.jobs.get(j)
            if mine is None:
                self.jobs[j] = [n, a, b]
            else:
                mine[0] += n
                mine[1], mine[2] = min(mine[1], a), max(mine[2], b)
        self.first = min(self.first, o.first)
        self.last = max(self.last, o.last)


class Outline:
    def __init__(self):
        self.templates = {}        # text -> Template
        self.cache = {}            # cheap key -> (Template, indices of numbers)
        self.blank = self.lines = 0
        self.lumped = None
        self.full = self.merged = False

    def add(self, raw, job, at):
        self.lines += 1
        raw = raw[:2000]
        key = raw.translate(_DIGITS)
        if b"00" in key:
            key = key.replace(b"0000", b"0").replace(b"00", b"0").replace(b"00", b"0")
        got = self.cache.get(key)
        if got is None:
            if not raw.strip():
                self.blank += 1
                return
            got = self._learn(raw, key)
        t, idx, total = got
        if idx is None:
            t.hit(job, at, ())
            return
        nums = _NUMS.findall(raw.decode("utf-8", "replace"))
        if len(nums) != total:                  # same key, other layout
            text = raw.decode("utf-8", "replace")
            template, spans = mask(text)
            t = self._template(template, text, spans)
            nums = [text[a:b] for a, b in spans]
        elif len(idx) != total:
            nums = [nums[i] for i in idx]
        t.hit(job, at, nums)

    def _learn(self, raw, key):
        text = raw.decode("utf-8", "replace")
        template, spans = mask(text)
        t = self._template(template, text, spans)
        found = [m.span() for m in _NUMS.finditer(text)]
        wanted = set(spans)
        idx = [i for i, s in enumerate(found) if s in wanted]
        if len(idx) != len(spans):              # only the mask sees these
            idx = None
        got = (t, idx if spans else None, len(found))
        if len(self.cache) >= 50000:
            self.cache.clear()
        if len(key) <= 4096:
            self.cache[key] = got
        return got

    def _template(self, template, text, spans):
        template = " ".join(template.split())[:1000]
        t = self.templates.get(template)
        if t is not None:
            return t
        if len(self.templates) >= d.TEMPLATES_MAX and not self.full:
            self._merge()
            self.cache.clear()
            # Merging again for every further line would cost too much.
            self.full = len(self.templates) >= d.TEMPLATES_MAX * 3 // 4
        if self.merged:
            words = template.split(" ")
            for i in range(min(len(words), 60)):
                t = self.templates.get(" ".join(words[:i] + ["<*>"] + words[i + 1:]))
                if t is not None:
                    return t
        # Errors are still told apart when there is no room for the rest.
        if self.full and (len(self.templates) >= d.TEMPLATES_MAX * 2
                          or not severity(text)):
            if self.lumped is None:
                self.lumped = Template("(other lines)", text[:d.ANALYZE_CELL], [])
            return self.lumped
        labels = []
        for a, _ in spans[:30]:
            m = _LABEL.search(text[max(0, a - 40):a])
            labels.append(m[1] if m else "")
        t = self.templates[template] = Template(template, text[:400], labels)
        return t

    def _merge(self):
        """Templates alike but for one word are one template when there are
        many of them (user names, hosts). Two or three are not: "ok" and
        "failed" must stay apart."""
        by_hole = {}
        for t in self.templates.values():
            words = t.text.split(" ")
            if len(words) > 60:
                continue
            for i in range(len(words)):
                if words[i].startswith("<"):
                    continue
                hole = (len(words), i, " ".join(words[:i] + ["\0"] + words[i + 1:]))
                by_hole.setdefault(hole, []).append(t)
        taken = set()
        for (_, i, text), group in sorted(by_hole.items(), key=lambda kv: -len(kv[1])):
            group = [t for t in group if id(t) not in taken]
            if len(group) < 6 or len({t.sev for t in group}) > 1:
                continue
            group.sort(key=lambda t: t.first)
            self.merged = True
            head = group[0]
            head.values = [t.text.split(" ")[i] for t in group[:3]] + (
                ["+%d" % (len(group) - 3)] if len(group) > 3 else [])
            for t in group[1:]:
                head.absorb(t)
                del self.templates[t.text]
            del self.templates[head.text]
            head.text = text.replace("\0", "<*>")
            same = self.templates.get(head.text)
            if same is None:
                self.templates[head.text] = head
            else:
                same.absorb(head)
            taken.update(id(t) for t in group)

    # ── report ──
    def report(self, labels, limit, has_lines):
        for _ in range(3):                      # more than one varying word
            before = len(self.templates)
            self._merge()
            if len(self.templates) == before:
                break
        every = [t for t in self.templates.values() if t.count]
        if self.lumped:
            every.append(self.lumped)
        many = len(labels) > 1

        def place(p):
            at = ("L%d" if has_lines else "@%d") % p[1]
            return labels[p[0]] + ":" + at if many else at

        def row(t):
            where = place(t.first) if t.count == 1 else \
                "%s..%s" % (place(t.first), place(t.last))
            out = "%d  %s  %s" % (t.count, where, _cut(t.text if t.count > 1 else t.example))
            ranges, seen = [], {}
            for i, name in enumerate(t.labels):
                if t.lo[i] is None or t.count == 1:
                    continue
                name = name or "#%d" % (i + 1)
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name += "~%d" % seen[name]
                if t.lo[i] != t.hi[i]:
                    ranges.append("%s %s..%s" % (name, show(t.lo[i]), show(t.hi[i])))
            extra = []
            if ranges:
                extra.append(", ".join(ranges[:8]))
            if t.values:
                extra.append("<*>: " + ",".join(v[:30] for v in t.values))
            if t.count > 1 and t.text != t.example:
                extra.append("e.g. " + _cut(t.example))
            if many and len(t.jobs) < len(labels):
                extra.append("in %d of %d jobs" % (len(t.jobs), len(labels)))
            return out + "".join("\n    " + x for x in extra)

        every.sort(key=lambda t: (-t.count, t.first))
        out = []
        bad = [t for t in every if t.sev == 2]
        warn = [t for t in every if t.sev == 1]
        shown = set()
        top = max(5, limit // 3)
        for title, group in (("likely errors", bad), ("likely warnings", warn)):
            if group:
                out.append("%s: %d lines in %d templates" % (
                    title, sum(t.count for t in group), len(group)))
                out += [row(t) for t in group[:top]]
                shown.update(id(t) for t in group[:top])
                if len(group) > top:
                    out.append("(+%d templates)" % (len(group) - top))
        rest = [t for t in every if id(t) not in shown]
        out.append("templates: %d, by count" % len(every))
        out += [row(t) for t in rest[:top]]
        shown.update(id(t) for t in rest[:top])
        rare = [t for t in rest[top:] if t.count <= max(1, self.lines // 10000)
                and not many or t.count == 1]
        rare.sort(key=lambda t: t.first)
        # Places where a rare line appears are often neighbours: show both ends.
        pick = rare if len(rare) <= top else rare[:top // 2] + rare[-(top - top // 2):]
        if pick:
            out.append("rare: %d templates%s" % (
                len(rare), "" if len(pick) == len(rare) else ", first and last shown"))
            out += [row(t) for t in pick]
            shown.update(id(t) for t in pick)
        left = [t for t in every if id(t) not in shown]
        if left:
            out.append("(+%d templates, %d lines not shown; raise limit for more)"
                       % (len(left), sum(t.count for t in left)))
        if many:
            out += self._deviations(every, labels, top)
        return out

    def _deviations(self, every, labels, top):
        """Templates and jobs that differ from what most jobs show."""
        n, found, per_job = len(labels), [], {}
        for t in every:
            if t is self.lumped:
                continue
            counts = [t.jobs[j][0] if j in t.jobs else 0 for j in range(n)]
            mid = sorted(counts)[n // 2] if n > 2 else None
            if mid is None:
                if counts[0] == counts[1]:
                    continue
                off = [0, 1]
            else:
                # Where most jobs agree exactly, any other count is news.
                slack = 0 if counts.count(mid) >= 0.6 * n else max(1, mid // 2)
                off = [j for j in range(n) if abs(counts[j] - mid) > slack]
            if not off or len(off) > max(2, n // 3) and n > 2:
                continue
            found.append((t, counts, mid, off))
            for j in off:
                per_job[j] = per_job.get(j, 0) + 1
        if not found:
            return ["jobs: no template deviates between jobs"]
        out = ["jobs that deviate: " + ", ".join(
            "%s (%d templates)" % (labels[j], c) for j, c in
            sorted(per_job.items(), key=lambda kv: -kv[1])[:20])]
        found.sort(key=lambda f: (-f[0].sev, f[2] or 0, f[0].first))
        for t, counts, mid, off in found[:top]:
            who = ", ".join("%s=%d" % (labels[j], counts[j]) for j in off[:6])
            more = " +%d" % (len(off) - 6) if len(off) > 6 else ""
            out.append("  %s%s%s: %s" % (
                who, more, "" if mid is None else " (others %d)" % mid,
                _cut(t.text if t.count > 1 else t.example)))
        if len(found) > top:
            out.append("  (+%d more)" % (len(found) - top))
        return out


def _cut(s):
    s = s.strip()
    return s if len(s) <= d.ANALYZE_CELL else s[:d.ANALYZE_CELL - 3] + "..."
