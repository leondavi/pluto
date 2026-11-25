"""Fields out of output lines: numbers, extractors, lazy pseudo-fields.

Stdlib only. Shared by pluto_job_analyze.py and pluto_job_watch.py. Every
extractor is a function ``text -> dict or None``; values stay strings (or
JSON scalars) and are read as numbers only where a query needs them.
"""

import calendar
import csv
import json
import math
import re

import pluto_job_defs as d


class QueryError(Exception):
    """The request is wrong; the message names the bad term."""


# ── Numbers ─────────────────────────────────────────────────────────────────

NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_BIG = 1 << 1000
_NUM = re.compile(r"(%s|[-+]?(?:nan|inf(?:inity)?)\b)\s?([a-zA-Zµμ%%]*)\Z" % NUMBER, re.I)
#: Factors to one base per kind, so that 900ms and 1.2s compare.
UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1,
         "sec": 1, "secs": 1, "m": 60, "min": 60, "h": 3600, "%": 1}
_BYTES = {"b": 1}
for _i, _p in enumerate("kmgt", 1):
    _BYTES[_p + "b"] = 1000 ** _i
    _BYTES[_p + "ib"] = 1024 ** _i


def num(v):
    """A number, or None. Units become seconds or bytes."""
    t = type(v)
    if t is float:
        return v
    if t is int:                    # JSON may hold integers no float can
        return v if -_BIG < v < _BIG else math.copysign(math.inf, v > 0 or -1)
    if t is not str:
        return None
    try:
        # int() of very long digit strings is quadratic.
        if len(v) <= 40 and "_" not in v:
            return int(v)
    except ValueError:
        pass
    if len(v) > 64:
        return None
    m = _NUM.match(v)
    if not m:
        return None
    if not m[2]:
        return float(m[1])
    f = factor(m[2])
    return None if f is None else float(m[1]) * f


def factor(unit):
    f = UNITS.get(unit)
    return _BYTES.get(unit.lower()) if f is None else f


def unit_of(v):
    """"s", "B", "%" or "": the base unit num() converts *v* to."""
    m = _NUM.match(v) if type(v) is str and len(v) <= 64 else None
    if not m or not m[2]:
        return ""
    return m[2] if m[2] == "%" else "s" if m[2] in UNITS else \
        "B" if m[2].lower() in _BYTES else ""


def is_nan(x):
    return type(x) is float and x != x


def show(v):
    """Short text of a value for tables."""
    if v is None:
        return "-"
    if type(v) is float:
        if not math.isfinite(v):
            return str(v)
        return "%d" % v if v == int(v) and abs(v) < 1e15 else "%.6g" % v
    s = v if type(v) is str else json.dumps(v, ensure_ascii=False, default=str)
    return s


# ── Lines ───────────────────────────────────────────────────────────────────

ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_])")


def clean(raw):
    """Bytes of one line as text: no colours, and of a progress bar that
    redraws itself with \\r only what was left standing."""
    if b"\x1b" in raw:
        raw = ANSI.sub(b"", raw)
    if b"\r" in raw:
        raw = next((p for p in reversed(raw.split(b"\r")) if p.strip()), b"")
    return raw.decode("utf-8", "replace")


# ── Extractors ──────────────────────────────────────────────────────────────

# Bounded repeats: output is untrusted too, and must not be quadratic.
_KV = re.compile(
    r'(?<![\w.\-])([A-Za-z_][\w.\-/@]{0,63})(?:=|:\s?(?=[-+]?[\d.]))'
    r'(?:"((?:[^"\\]|\\.){0,2000})"|\'([^\']{0,2000})\'|([^\s,;|)\]}"\']*))')
_CLF = re.compile(
    r'(?P<ip>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) (?P<bytes>\d+|-)'
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?')
_ESC = re.compile(r"\\(.)")


def kv(text):
    """key=value, key="quoted", and key: 12 pairs anywhere in the line."""
    out = {}
    if "=" not in text and ":" not in text:
        return None
    for k, dq, sq, bare in _KV.findall(text[:d.MATCH_LINE_MAX]):
        if dq:
            bare = _ESC.sub(r"\1", dq) if "\\" in dq else dq
        out[k] = bare or sq
    return out or None


def flatten(obj):
    """Nested JSON as dotted keys; iterative, so depth cannot hurt."""
    out, todo = {}, [("", obj)]
    while todo:
        at, o = todo.pop()
        items = o.items() if type(o) is dict else enumerate(o)
        for k, v in items:
            key = "%s.%s" % (at, k) if at else str(k)
            if type(v) in (dict, list) and v:
                todo.append((key, v))
            elif len(out) < d.FIELDS_MAX:
                out[key] = v
    return out


def js(text):
    """A JSON object line, or one at the end of a line with a prefix."""
    at = text.find("{")
    if at < 0 or not text.rstrip().endswith("}"):
        return None
    try:
        obj = json.loads(text[at:])
    except (ValueError, RecursionError):
        return None
    return flatten(obj) if type(obj) is dict and obj else None


def clf(text):
    if '] "' not in text:
        return None
    m = _CLF.match(text)
    return {k: v for k, v in m.groupdict().items() if v is not None} if m else None


def table(delim, header):
    """Rows of a CSV/TSV table; lines of another width are no rows."""
    width = len(header)

    def row(text):
        if '"' in text:
            try:
                cells = next(csv.reader([text], delimiter=delim), [])
            except csv.Error:
                return None
        else:
            cells = text.split(delim)
        if len(cells) != width:
            return None
        cells = [c.strip() for c in cells]
        return None if cells == header else dict(zip(header, cells))
    return row


def sniff_table(lines, delims=",\t;|"):
    """(delimiter, header) when the first lines look like a table with a
    header, else None."""
    rows = [x for x in lines if x.strip()][:20]
    if len(rows) < 2:
        return None
    for delim in delims:
        try:
            cells = list(csv.reader(rows, delimiter=delim))
        except csv.Error:
            continue
        head = [c.strip() for c in cells[0]]
        n = len(head)
        if n < 2 or len(set(head)) < n or \
                not all(re.match(r"[A-Za-z_][\w .%/()\-]*\Z", c) for c in head):
            continue
        fit = sum(len(c) == n for c in cells[1:])
        numeric = any(num(c.strip()) is not None for r in cells[1:] for c in r)
        if fit >= 0.8 * (len(cells) - 1) and (numeric or delim == "\t"):
            return delim, head
    return None


def auto(head):
    """Extractor for lines of any common shape, judged line by line; *head*
    (first lines of the file) tells whether it is a table."""
    tab = sniff_table(head)
    row = table(*tab) if tab else None

    def fields(text):
        if row:
            got = row(text)
            if got:
                return got
        if "{" in text:
            got = js(text)
            if got:
                return got
        return clf(text) or kv(text)
    return fields


def kind_of(lines):
    """Format of sample lines, for display."""
    lines = [x for x in lines if x.strip()]
    tab = sniff_table(lines)
    if tab:
        return "tsv" if tab[0] == "\t" else "csv"
    for name, fn, share in (("json", js, .5), ("access log", clf, .5), ("kv", kv, .5)):
        if lines and sum(bool(fn(x)) for x in lines) >= share * len(lines):
            return name
    return "plain"


# ── Brace templates and regexes ─────────────────────────────────────────────

_BRACE = re.compile(r"\{\{|\}\}|\{([A-Za-z_]\w*)(?::(\w+))?\}")
_TYPES = {"int": r"[-+]?\d+", "float": "(?:%s|[-+]?(?:nan|inf(?:inity)?))" % NUMBER,
          "num": "(?:%s|[-+]?(?:nan|inf(?:inity)?))" % NUMBER,
          "word": r"\w+", "hex": r"(?:0x)?[0-9a-fA-F]+"}


def is_brace(pattern):
    return any(m[1] for m in _BRACE.finditer(pattern))


def brace_regex(template):
    """"epoch {e:int} loss={loss}": an untyped field is one token without
    spaces; {rest} and {x:text} may contain spaces."""
    out, at, seen, adjacent = [], 0, set(), False
    marks = list(_BRACE.finditer(template))
    for i, m in enumerate(marks):
        lit = template[at:m.start()]
        out.append(_literal(lit))
        at = m.end()
        if not m[1]:
            out.append(re.escape(m[0][0]))
            adjacent = False
            continue
        name, kind = m[1], m[2] or ("text" if m[1] == "rest" else "")
        if adjacent and not lit:
            raise QueryError("template: put text between {%s} and the field before it" % name)
        if name in seen and name != "_":
            raise QueryError("template: field {%s} appears twice" % name)
        seen.add(name)
        if len(seen) > 30:
            raise QueryError("template: more than 30 fields")
        last = at == len(template)
        if kind == "text":
            pat = r".*" if last else r".+?"
        elif kind == "":
            pat = r"\S+" if last or template[at].isspace() else r"\S+?"
        elif kind in _TYPES:
            pat = _TYPES[kind]
        else:
            raise QueryError("template: unknown type {%s:%s}; use int, float, "
                             "word, hex or text" % (name, kind))
        out.append("(?:%s)" % pat if name == "_" else "(?P<%s>%s)" % (name, pat))
        adjacent = True
    out.append(_literal(template[at:]))
    if not seen:
        raise QueryError("template %r has no {field}" % template[:80])
    return "".join(out)


def _literal(s):
    """Literal text; any run of blanks matches any run of blanks."""
    return re.sub(r"(?:\\?\s)+", lambda _: r"\s+", re.escape(s))


def compiled(pattern, what):
    if len(pattern) > d.PATTERN_MAX:
        raise QueryError("%s: longer than %d characters" % (what, d.PATTERN_MAX))
    try:
        return re.compile(pattern)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        raise QueryError("%s: bad regex %r: %s" % (what, pattern[:80], exc))


def by_regex(rx):
    def fields(text):
        m = rx.search(text[:d.MATCH_LINE_MAX])
        if not m:
            return None
        return {k: v.strip() for k, v in m.groupdict().items() if v is not None}
    return fields


def first_of(extractors):
    """The record of the first extractor that knows a line. Lines of several
    kinds, each with its template, so become one sequence of records."""
    def fields(text):
        for f in extractors:
            rec = f(text)
            if rec is not None:
                return rec
        return None
    return fields


def extractor(spec, head):
    """Function for an ``extract`` argument, one or a list."""
    if isinstance(spec, list):
        if len(spec) > d.EXTRACTS_MAX:
            raise QueryError("extract: more than %d templates" % d.EXTRACTS_MAX)
        each = [extractor(s, head) for s in spec] or [auto(head)]
        return each[0] if len(each) == 1 else first_of(each)
    if spec in (None, "", "auto"):
        return auto(head)
    if spec == "kv":
        return kv
    if spec == "json":
        return js
    if spec in ("csv", "tsv"):
        tab = sniff_table(head, "\t" if spec == "tsv" else ",;|")
        if not tab:
            raise QueryError("extract=%s: no header line found; name the columns: "
                             "\"%s:a,b,c\"" % (spec, spec))
        return table(*tab)
    if spec[:4] in ("csv:", "tsv:"):
        return table("\t" if spec[0] == "t" else ",",
                     [c.strip() for c in spec[4:].split(",")])
    if spec.startswith("re:"):
        rx = compiled(spec[3:], "extract")
        if not rx.groupindex:
            raise QueryError("extract: the regex needs named groups, (?P<name>...)")
        return by_regex(rx)
    if is_brace(spec):
        return by_regex(compiled(brace_regex(spec), "extract"))
    raise QueryError("extract=%r: use auto, kv, json, csv, tsv, csv:a,b,c, a "
                     "template such as \"loss={loss:float}\", or re:<regex>" % spec[:80])


# ── Pseudo-fields ───────────────────────────────────────────────────────────

_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|"
                    r"FATAL|PANIC)\b", re.I)
_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,9}))?"
                   r"(Z|[+-]\d\d:?\d\d)?")


def level(text):
    m = _LEVEL.search(text[:400])
    if not m:
        return None
    s = m[1].lower()
    return {"warning": "warn", "err": "error", "critical": "crit"}.get(s, s)


def when(text):
    """First ISO-like timestamp of a line as epoch seconds (UTC if unmarked)."""
    m = _TIME.search(text[:400])
    if not m:
        return None
    try:
        t = calendar.timegm(tuple(int(m[i]) for i in range(1, 7)))
    except (ValueError, OverflowError):
        return None
    if m[7]:
        t += int(m[7]) / 10 ** len(m[7])
    z = m[8]
    if z and z != "Z":
        off = int(z[1:3]) * 3600 + int(z[-2:]) * 60
        t -= off if z[0] == "+" else -off
    return t


PSEUDO = ("_job", "_name", "_n", "_offset", "_line", "_level", "_time")


def pseudo(need, job, text, n, offset):
    """The underscore fields a query names, {} when it names none."""
    out = {}
    for k in need:
        if k == "_job":
            out[k] = job.label
        elif k == "_name":
            out[k] = job.name
        elif k == "_n":
            out[k] = n
        elif k == "_offset":
            out[k] = offset
        elif k == "_line":
            out[k] = text
        elif k == "_level":
            v = level(text)
            if v:
                out[k] = v
        elif k == "_time":
            v = when(text)
            if v is not None:
                out[k] = v
    return out
