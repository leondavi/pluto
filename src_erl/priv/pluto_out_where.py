"""Conditions on fields: ``status >= 500``, ``msg contains timeout``,
``loss > prev * 1.5``, ``has err and not (level == debug)``,
``used > last.limit``, ``abs(live - prev.live) > 3``.

A small recursive-descent parser that builds closures; nothing is ever
evaluated as code. A condition is a function ``(record, memory) -> bool``.

A memory (see new_memory) has two parts. ``prev`` maps a field to its value
in the previous *matching* record. ``last`` maps a field to the value it had
in the latest earlier record that has it, matching or not: the setting in
force, the phase a line belongs to.

Words: left of an operator a name is a field. Right of it, a bare word is
text for == and != ("level == ERROR") and a field for < <= > >= and in
arithmetic ("used > total / 2"); $name is always a field.
"""

import re

import pluto_job_defs as d
from pluto_out_fields import QueryError, compiled, factor, is_nan, num

_TOKEN = re.compile(r"""\s*(?:
    (?P<lit>\d+(?:[.:\-]\d+){2,}[\w.:+\-]*)          # date, time, ip, version
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(?P<unit>[a-zA-Zµμ%]*)(?![\w.])
  | "(?P<dq>(?:[^"\\]|\\.)*)" | '(?P<sq>(?:[^'\\]|\\.)*)' | `(?P<bt>[^`]+)`
  | (?P<op>==|!=|<=|>=|!~|=~|&&|\|\||[=<>~()+*/,!-])
  | (?P<word>\$?[A-Za-z_@][\w@]*(?:[.\-][A-Za-z_0-9]\w*)*)
  | (?P<raw>\S)
)""", re.X)
_TEXT_OPS = ("contains", "startswith", "endswith", "~", "!~", "=~")
_ORDER = ("<", "<=", ">", ">=")
_LOGIC = {"and": "and", "&&": "and", "or": "or", "||": "or", "not": "not", "!": "not"}


def new_memory():
    """What conditions remember; plain data, so that it can be saved."""
    return {"prev": {}, "last": {}}


class _Tok:
    def __init__(self, m, end):
        self.start, self.end = m.end() - len(m[0].lstrip()), end
        self.kind = next(k for k in ("lit", "num", "dq", "sq", "bt", "op", "word", "raw")
                         if m[k] is not None)
        self.text = m[self.kind]
        if self.kind in ("dq", "sq"):
            self.text = re.sub(r"\\(.)", r"\1", self.text)
        self.unit = m["unit"] or ""


def _tokens(s):
    out, at = [], 0
    while at < len(s.rstrip()):
        m = _TOKEN.match(s, at)
        at = m.end()
        out.append(_Tok(m, at))
    return out


class Condition:
    """One parsed condition: call it with (record, memory)."""

    def __init__(self, text):
        if len(text) > d.PATTERN_MAX:
            raise QueryError("where: condition longer than %d characters" % d.PATTERN_MAX)
        self.text = text
        self.fields = {}            # every field named, in order
        self.prev_fields, self.last_fields = {}, {}
        self.unitless = {}          # fields compared with a plain number
        self.subject = None
        self.depth = self.tries = 0
        self.toks, self.i = _tokens(text), 0
        if not self.toks:
            raise QueryError("where: empty condition")
        self.test = self._or()
        if self.i < len(self.toks):
            self._fail("unexpected %r" % self.text[self.toks[self.i].start:][:40],
                       "quote text that contains spaces")
        self.uses_prev = bool(self.prev_fields)

    def __call__(self, rec, memory):
        return self.test(rec, memory)

    # ── tokens ──
    def _fail(self, why, hint=""):
        raise QueryError("where %r: %s%s" % (self.text[:120], why,
                                             " (%s)" % hint if hint else ""))

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _is(self, *texts):
        t = self._peek()
        return t is not None and t.kind in ("op", "word") and t.text in texts

    def _logic(self, which):
        t = self._peek()
        return t is not None and t.kind in ("op", "word") and \
            _LOGIC.get(t.text.lower()) == which

    def _next(self, what):
        t = self._peek()
        if t is None:
            self._fail("ends where %s is expected" % what)
        self.i += 1
        return t

    # ── grammar ──
    def _or(self):
        self.depth += 1
        if self.depth > 30:
            self._fail("nested too deeply")
        parts = [self._and()]
        while self._logic("or"):
            self.i += 1
            parts.append(self._and())
        self.depth -= 1
        return parts[0] if len(parts) == 1 else \
            lambda r, p: any(f(r, p) for f in parts)

    def _and(self):
        parts = [self._not()]
        while self._logic("and"):
            self.i += 1
            parts.append(self._not())
        return parts[0] if len(parts) == 1 else \
            lambda r, p: all(f(r, p) for f in parts)

    def _not(self):
        n = 0
        while self._logic("not"):
            self.i += 1
            n += 1
        f = self._cond()
        return (lambda r, p: not f(r, p)) if n % 2 else f

    def _cond(self):
        if self._is("("):
            # A group of conditions, or the start of arithmetic: try both.
            at, depth = self.i, self.depth
            self.tries += 1             # trying both nests exponentially
            if self.tries > 200:
                self._fail("too many parentheses")
            self.i += 1
            try:
                f = self._or()
                if self._is(")"):
                    self.i += 1
                    t = self._peek()
                    if t is None or t.text == ")" or t.text.lower() in _LOGIC:
                        return f
            except QueryError:
                if self.tries > 200:
                    raise
            self.i, self.depth = at, depth
        for word in ("has", "missing"):
            t = self._peek()
            after = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
            if t is not None and t.kind == "word" and t.text == word and \
                    after is not None and after.kind in ("word", "bt"):
                self.i += 2
                name = self._field(after)
                return (lambda r, p: r.get(name) not in (None, "")) if word == "has" \
                    else (lambda r, p: r.get(name) in (None, ""))
        self.subject = None
        return self._compare(self._sum(True))

    def _compare(self, left):
        t = self._next("an operator such as == > contains ~")
        op = t.text.lower() if t.kind == "word" else t.text
        negate = False
        if op == "not":                       # x not contains y, x not in (..)
            negate, t = True, self._next("an operator")
            op = t.text.lower()
        if op == "in":
            f = self._in(left)
        elif op in _TEXT_OPS:
            f = self._text_op(op, left)
        elif op in ("==", "=", "!="):
            f = self._equal(left, op == "!=")
        elif op in _ORDER:
            f = self._order(op, left)
        else:
            self._fail("unknown operator %r" % t.text[:40],
                       "use == != < <= > >= contains startswith endswith ~ !~ "
                       "in, or: has field")
        return (lambda r, p: not f(r, p)) if negate else f

    def _text_op(self, op, left):
        t = self._peek()
        if t is None:
            self._fail("%s needs some text" % op)
        if t.kind in ("dq", "sq"):
            self.i += 1
            want = t.text
            if op in ("~", "!~", "=~"):       # backslashes belong to the regex
                want = self.text[t.start + 1:t.end - 1]
        else:
            want = self.text[t.start:].rstrip()
            self.i = len(self.toks)
        if op in ("~", "!~", "=~"):
            rx = compiled(want, "where %r" % self.text[:120])
            cut = d.MATCH_LINE_MAX

            def f(r, p):
                v = left(r, p)
                return v is not None and rx.search(_str(v)[:cut]) is not None
            return f if op != "!~" else (lambda r, p: left(r, p) is not None
                                         and not f(r, p))

        def g(r, p):
            v = left(r, p)
            if v is None:
                return False
            v = _str(v)
            return want in v if op == "contains" else \
                v.startswith(want) if op == "startswith" else v.endswith(want)
        return g

    def _in(self, left):
        if not self._is("("):
            self._fail("in needs a list", "x in (a, b)")
        self.i += 1
        items = []
        while True:
            items.append(self._sum(False))
            t = self._next("\")\"")
            if t.text == ")":
                break
            if t.text != ",":
                self._fail("unexpected %r in the list" % t.text[:40])
        return lambda r, p: any(equal(left(r, p), it(r, p)) is True for it in items)

    def _equal(self, left, negate):
        right = self._sum(False)

        def f(r, p):
            same = equal(left(r, p), right(r, p))
            return same is not None and same != negate
        return f

    def _order(self, op, left):
        subject = self.subject
        plain = self._peek()
        if plain is not None and plain.kind == "num" and not plain.unit and subject:
            self.unitless[subject] = True
        right = self._sum(True)

        def f(r, p):
            a, b = left(r, p), right(r, p)
            if a is None or b is None:
                return False
            x, y = num(a), num(b)
            if x is None or y is None:
                if x is not None or y is not None:
                    return False              # a number and a word
                x, y = _str(a), _str(b)
            return x < y if op == "<" else x <= y if op == "<=" else \
                x > y if op == ">" else x >= y
        return f

    # ── terms ──
    def _sum(self, words_are_fields):
        f = self._product(words_are_fields)
        while self._is("+", "-"):
            op = self._next("")
            f = self._arith(op.text, f, self._product(True))
        return f

    def _product(self, words_are_fields):
        f = self._term(words_are_fields)
        while self._is("*", "/"):
            op = self._next("")
            f = self._arith(op.text, f, self._term(True))
        return f

    @staticmethod
    def _arith(op, left, right):
        def f(r, p):
            a, b = num(left(r, p)), num(right(r, p))
            if a is None or b is None:
                return None
            try:
                return a + b if op == "+" else a - b if op == "-" else \
                    a * b if op == "*" else a / b
            except (ZeroDivisionError, OverflowError):
                return None
        return f

    def _term(self, words_are_fields):
        t = self._next("a field or value")
        if t.kind == "op" and t.text == "-":
            return self._arith("-", lambda r, p: 0, self._term(True))
        if t.kind == "op" and t.text == "(":
            return self._enclosed()
        if t.kind == "num":
            return self._number(t)
        if t.kind in ("dq", "sq", "lit"):
            return lambda r, p: t.text
        if t.kind == "bt":
            return self._get(t)
        if t.kind != "word":
            self._fail("unexpected %r" % self.text[t.start:][:40])
        for part, fields in (("prev", self.prev_fields), ("last", self.last_fields)):
            if t.text == part or t.text.startswith(part + "."):
                return self._recall(part, fields, t.text[len(part) + 1:])
        if t.text == "abs" and self._is("("):
            self.i += 1
            f = self._enclosed()

            def size(r, p):
                v = num(f(r, p))
                return None if v is None else abs(v)
            return size
        if words_are_fields or t.text.startswith("$"):
            return self._get(t)
        # Arithmetic after it makes it a field after all: x == total - 1
        if self._is("+", "-", "*", "/"):
            return self._get(t)
        return lambda r, p: t.text

    def _enclosed(self):
        """The rest of "(sum)"."""
        self.depth += 1
        if self.depth > 30:
            self._fail("nested too deeply")
        f = self._sum(True)
        if self._next("\")\"").text != ")":
            self._fail("missing \")\"")
        self.depth -= 1
        return f

    def _recall(self, part, fields, name):
        name = name or self.subject
        if not name:
            self._fail("%s has no field before it" % part, "write %s.field" % part)
        if self.subject is None:
            self.subject = name
        self.fields[name] = fields[name] = True
        return lambda r, p: p[part].get(name)

    def _number(self, t):
        text = t.text
        v = int(text) if text.isdigit() and len(text) <= 40 else float(text)
        if t.unit:
            f = factor(t.unit)
            if f is None:
                self._fail("unknown unit in %r" % (text + t.unit)[:40])
            v *= f
        return lambda r, p: v

    def _get(self, t):
        name = self._field(t)
        if self.subject is None:
            self.subject = name
        return lambda r, p: r.get(name)

    def _field(self, t):
        name = t.text[1:] if t.text.startswith("$") else t.text
        self.fields[name] = True
        return name


def _str(v):
    return v if type(v) is str else "true" if v is True else \
        "false" if v is False else str(v)


def equal(a, b):
    """True/False, or None when a side is missing. NaN equals NaN: a value
    that stays NaN has not changed."""
    if a is None or b is None:
        return None
    x, y = num(a), num(b)
    if x is not None and y is not None:
        return x == y or (is_nan(x) and is_nan(y))
    return _str(a) == _str(b)


class Where:
    """A list of conditions, all of which must hold. Conditions without
    prev decide which records match; those with prev compare a matching
    record with the matching one before it."""

    def __init__(self, conds):
        if len(conds or ()) > d.CONDS_MAX:
            raise QueryError("where: more than %d conditions" % d.CONDS_MAX)
        conds = [Condition(c) for c in conds or ()]
        self.plain = [c for c in conds if not c.prev_fields]
        self.related = [c for c in conds if c.prev_fields]
        self.fields, self.unitless = {}, {}
        self.prev_fields, self.last_fields = {}, {}
        for c in conds:
            self.fields.update(c.fields)
            self.prev_fields.update(c.prev_fields)
            self.last_fields.update(c.last_fields)
            self.unitless.update(c.unitless)
        self.uses_memory = bool(self.prev_fields or self.last_fields)

    def test(self, rec, memory=None):
        """Does the record match? Updates *memory*, which is needed only if
        uses_memory. A field the record lacks keeps its remembered value."""
        ok = True
        for c in self.plain:
            if not c(rec, memory):
                ok = False
                break
        if memory is None:
            return ok
        if ok and self.related:
            for c in self.related:
                if not c(rec, memory):
                    ok = False
                    break
            _store(self.prev_fields, rec, memory["prev"])
        if self.last_fields:
            _store(self.last_fields, rec, memory["last"])
        return ok


def _store(names, rec, into):
    for k in names:
        v = rec.get(k)
        if v is not None:
            into[k] = v
