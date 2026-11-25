"""Watch rules: validation, and evaluation line by line.

A rule's whole memory is the JSON-able dict ``st``, so that a server can
save it and any later process continues exactly there. Times are on the
job's own clock (seconds it has run, pauses excluded).
"""

import json
import math

import pluto_job_defs as d
from pluto_out_agg import hashable
from pluto_out_fields import (PSEUDO, QueryError, auto, brace_regex, by_regex,
                              compiled, extractor, is_brace, pseudo, show)
from pluto_out_where import Where, equal, new_memory

_FIELDS = ("name", "match", "where", "by", "extract", "count", "window_s", "idle_s",
           "absent_s", "stalled", "stalled_n", "then", "once", "cooldown_s",
           "max_hits", "from_now")


def _number(r, key, lo, hi, default=None, whole=False):
    v = r.get(key)
    if v is None:
        return default
    if type(v) is bool or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not lo <= v <= hi or (whole and v != int(v)):
        raise QueryError("watch: %s must be %s from %g to %g" % (
            key, "a whole number" if whole else "a number", lo, hi))
    return int(v) if whole else v


def check_rule(r):
    """The rule with defaults filled in; raises QueryError naming what is
    wrong with it."""
    out = {k: r[k] for k in _FIELDS if r.get(k) not in (None, "", [])}
    secs = d.WATCH_SECONDS_MAX
    for k in ("window_s", "idle_s", "absent_s"):
        v = _number(r, k, 0.5, secs)
        if v is not None:
            out[k] = v
    out["cooldown_s"] = _number(r, "cooldown_s", 0, secs, d.WATCH_COOLDOWN_S)
    out["max_hits"] = _number(r, "max_hits", 1, d.WATCH_HITS_MAX, d.WATCH_HITS, True)
    out["count"] = _number(r, "count", 1, d.WATCH_COUNT_MAX, 1, True)
    out["once"] = bool(r.get("once"))
    out["from_now"] = bool(r.get("from_now"))
    out["then"] = r.get("then") or "notify"
    if out["then"] not in d.WATCH_THEN:
        raise QueryError("watch: then=%r; use %s" % (out["then"][:40], ", ".join(d.WATCH_THEN)))
    if "match" in out and out["match"] == "re:":
        raise QueryError("watch: empty regex in match")
    kinds = [k for k in ("idle_s", "absent_s", "stalled") if k in out]
    if len(kinds) > 1:
        raise QueryError("watch: %s are different rules; add one for each"
                         % " and ".join(kinds))
    lines = "match" in out or "where" in out
    if not kinds and not lines:
        raise QueryError("watch: say when: match, where, idle_s, absent_s or stalled")
    if "idle_s" in out and lines:
        raise QueryError("watch: idle_s is about any output; for matching lines use absent_s")
    if "absent_s" in out and not lines:
        raise QueryError("watch: absent_s needs match or where (for no output at all "
                         "use idle_s)")
    if kinds and (out["count"] > 1 or "window_s" in out):
        raise QueryError("watch: count and window_s go with match or where only")
    if "window_s" in out and out["count"] < 2:
        raise QueryError("watch: window_s needs count, the number of matches within it")
    if "stalled" in out:
        out["stalled_n"] = _number(r, "stalled_n", 2, d.WATCH_COUNT_MAX, 10, True)
    if "extract" in out and (is_brace(out.get("match", "")) or
                             out.get("match", "").startswith("re:(?P<")):
        raise QueryError("watch: match already names the fields; drop extract")
    if len(out.get("match", "")) > d.PATTERN_MAX:
        raise QueryError("watch: match longer than %d characters" % d.PATTERN_MAX)
    if "name" not in out:
        out["name"] = (out.get("match") or (out["where"][0] if "where" in out else
                                            kinds[0]))[:60]
    out["name"] = out["name"][:100]
    Rule("R0", out)                          # patterns and conditions compile
    return out


def _plain(v):
    """A scalar that survives JSON: no NaN, no endless strings."""
    v = hashable(v)
    if type(v) is float and not math.isfinite(v):
        return str(v)
    return v[:d.WATCH_TEXT] if type(v) is str else v


def new_state(offset=0, line=1, c=0.0):
    return {"offset": offset, "line": line, "c": c, "n": 0, "total": 0}


class Rule:
    def __init__(self, rid, spec, st=None, head=()):
        self.id, self.spec = rid, spec
        self.st = st if st is not None else new_state()
        self.hits = []
        self.where = Where(spec.get("where"))
        self.by = list(spec.get("by") or ())
        need = dict.fromkeys(self.by)
        need.update(self.where.fields)
        if spec.get("stalled"):
            need[spec["stalled"]] = None
        self.pseudo = [k for k in need if k in PSEUDO]
        self.want = any(k not in PSEUDO for k in need)
        self.literal, self.rx, self.fields = None, None, None
        match = spec.get("match")
        if match and (match.startswith("re:") or is_brace(match)):
            self.rx = compiled(match[3:] if match.startswith("re:") else
                               brace_regex(match), "match")
            if self.rx.groupindex:
                self.fields = by_regex(self.rx)
        elif match:
            self.literal = match
        if self.fields is None and self.want:
            kind = spec.get("extract") or "auto"
            # A table's header is in the output, which may not be there yet.
            self.fields = auto(head) if kind == "auto" else (
                (lambda text: None) if kind in ("csv", "tsv") and not head
                else extractor(kind, head))
        if "stalled" in spec and self.rx is not None and self.rx.groupindex and \
                spec["stalled"] not in self.rx.groupindex and spec["stalled"] not in PSEUDO:
            raise QueryError("watch: stalled=%r is not a field of match" % spec["stalled"])
        self.by_lines = "idle_s" not in spec
        self.cool = spec["cooldown_s"]

    @property
    def done(self):
        return self.st.get("done", False)

    # ── lines ──
    def feed(self, text, n, offset, c, job):
        """One line; appends to self.hits."""
        if self.literal is not None and self.literal not in text:
            return
        rec = {}
        if self.fields is not None:
            rec = self.fields(text)
            if rec is None:
                if self.rx is not None or self.want and not self.pseudo:
                    return
                rec = {}
        elif self.rx is not None and not self.rx.search(text[:d.MATCH_LINE_MAX]):
            return
        if self.pseudo:
            rec.update(pseudo(self.pseudo, job, text, n, offset))
        if self.where.uses_memory:
            memory = self._slot("memory", rec, new_memory)
            ok = self.where.test(rec, memory)
            for part in memory.values():     # it will become JSON
                for k, v in part.items():
                    part[k] = _plain(v)
            if not ok:
                return
        elif not self.where.test(rec):
            return
        self._matched(rec, text, n, offset, c)

    def _matched(self, rec, text, n, offset, c):
        st, spec = self.st, self.spec
        if "absent_s" in spec:
            st["seen_c"] = c
            st.pop("fired", None)
            return
        if "stalled" in spec:
            v = rec.get(spec["stalled"])
            if v is None:
                return
            v = _plain(v)
            run = self._slot("run", rec, lambda: [None, 0])
            if equal(run[0], v):
                run[1] += 1
            else:
                run[0], run[1] = v, 1
            if run[1] == spec["stalled_n"]:
                self.fire(c, n, "%s=%s unchanged for %d matches: %s" % (
                    spec["stalled"], show(v)[:60], run[1], text))
            return
        need = spec["count"]
        if need > 1:
            times = st.setdefault("times", [])
            times.append(round(c, 3))
            if "window_s" in spec:
                oldest = c - spec["window_s"]
                while times and times[0] < oldest:
                    times.pop(0)
            if len(times) < need:
                return
            st["times"] = []
        self.fire(c, n, text)

    def _slot(self, name, rec, make):
        """Memory of this rule for the record's ``by`` group."""
        slots = self.st.setdefault(name, {})
        key = json.dumps([hashable(rec.get(b)) for b in self.by])[:500] if self.by else ""
        got = slots.get(key)
        if got is None:
            if len(slots) >= d.WATCH_KEYS_MAX:
                slots.pop(next(iter(slots)))         # forget the oldest group
            got = slots[key] = make()
        return got

    # ── time ──
    def tick(self, c, size, last_out_c, last_line, caught_up):
        st, spec = self.st, self.spec
        if st.get("late") and c >= st.get("next_c", 0):
            self._emit(c, *st["last"])
        if "idle_s" in spec:
            if st.get("size") != size:       # new output: it may fire again
                st["size"], st["out_c"] = size, round(last_out_c, 3)
                st.pop("fired", None)
        if not caught_up or st.get("fired"):
            return
        if "idle_s" in spec and c - max(last_out_c, st.get("from_c", 0)) >= spec["idle_s"]:
            st["fired"] = True
            n, text = last_line()
            self.fire(c, n, "no output for %gs; last line: %s" % (
                round(c - last_out_c, 1), text or "(none)"))
        elif "absent_s" in spec:
            since = st.get("seen_c", st.get("from_c", 0))
            if c - since >= spec["absent_s"]:
                st["fired"] = True
                n, text = last_line()
                self.fire(c, n, "no matching line for %gs; last line: %s" % (
                    round(c - since, 1), text or "(none)"))

    def end(self, c):
        if not self.done and self.st.get("late"):
            self._emit(c, *self.st["last"])

    # ── firing ──
    def fire(self, c, n, text):
        st = self.st
        st["total"] += 1
        text = text.strip()[:d.WATCH_TEXT].replace("\0", "�")
        if st["n"] and self.cool and c < st.get("next_c", 0):
            # Told when the cool-down ends, so that nothing goes unnoticed.
            st["late"] = st.get("late", 0) + 1
            st["last"] = [n, text]
            return
        self._emit(c, n, text)

    def _emit(self, c, n, text):
        st = self.st
        also = st.pop("late", 0)
        if st.pop("last", None) == [n, text]:
            also -= 1                        # this one is among them
        st["n"] += 1
        st["next_c"] = c + self.cool
        hit = {"rule": self.id, "name": self.spec["name"], "n": st["n"],
               "line": n, "text": text, "then": self.spec["then"]}
        if also > 0:
            hit["also"] = also
        if self.spec["once"] or st["n"] >= self.spec["max_hits"]:
            st["done"] = True
            st["why"] = "once" if self.spec["once"] else "max_hits reached"
            if not self.spec["once"]:
                hit["last"] = True
        self.hits.append(hit)

    def disable(self, why, n=None):
        """The rule is broken; say so once and leave it alone."""
        self.st["done"], self.st["why"] = True, why
        self.hits.append({"rule": self.id, "name": self.spec["name"],
                          "n": self.st["n"], "line": n, "then": "notify",
                          "text": "rule disabled: " + why})

    def take(self):
        hits, self.hits = self.hits, []
        return hits
