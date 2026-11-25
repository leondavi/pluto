"""Scripted readers of experiment 5: a privileged one and naive baselines.

``privileged`` knows every writer's styles (from the plan) but takes all facts
from the printed lines, so a score of 1.0 shows that every planted anomaly is
visible and nothing unplanted breaks a rule. The baselines know only the docs.
"""

import bisect
import json
import re

import wl_probe

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STAMP = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} ")
CODE = re.compile(r"\b[A-Z]{3}-[0-9a-f]{4}\b")


def _num(x):
    return int(re.sub(r"\D", "", x))


class Reader:
    """Inverse of wl_probe.render for one writer and style."""

    def __init__(self, st, w, cols):
        self.st, self.w, self.cols = st, w, cols
        self.back = {v: c for c, v in st["names"].items()}
        self.ops = {v: c for c, v in st["ops"].items()}

    def record(self, seq, lane, op, pairs):
        vals = {}
        for c, x in pairs:
            if x in self.st["bool"] and c in ("known", "merged", "member"):
                vals[c] = x == self.st["bool"][0]
            else:
                vals[c] = int(x)
        return {"seq": int(seq), "lane": _num(str(lane)), "op": self.ops[op], "vals": vals}

    def line(self, s):
        k, nm, w = self.st["kind"], self.st["names"], self.w
        if k == "kv" and s.startswith(f"src={w} "):
            f = dict(t.split("=", 1) for t in s.split())
            pairs = [(self.back[x], y) for x, y in f.items() if x in self.back
                     and self.back[x] in self.cols]
            return self.record(f[nm["seq"]], f[nm["lane"]], f[nm["op"]], pairs)
        if k == "json" and s.startswith('{"src": "%s"' % w):
            f = json.loads(s)
            return self.record(f[nm["seq"]], f[nm["lane"]], f[nm["op"]],
                               [(c, f[nm[c]]) for c in self.cols if nm[c] in f])
        if k == "csv" and s.startswith(w + ";"):
            f = s.split(";")
            return self.record(*f[1:4], [(c, x) for c, x in zip(self.cols, f[4:]) if x])
        if k == "table" and s.startswith(f"| {w} |"):
            f = [x.strip() for x in s.strip("| ").split("|")]
            return self.record(*f[1:4], [(c, x) for c, x in zip(self.cols, f[4:]) if x != "-"])
        rx = {"text0": rf"\[{w}\] #(?P<seq>\d+) (?P<op>\S+) (?P<f>.*) via (?P<lane>\S+)$",
              "text1": rf"{w} did (?P<op>\S+) with (?P<f>.*); handled by (?P<lane>\S+); "
                       r"record (?P<seq>\d+)$",
              "text2": rf"record (?P<seq>\d+) from {w}: (?P<op>\S+) gives (?P<f>.*) @(?P<lane>\S+)$"}
        m = re.match(rx[k], s) if k in rx else None
        if not m:
            return None
        pairs = [t.rsplit(" ", 1) for t in m["f"].split(", ")]
        return self.record(m["seq"], m["lane"], m["op"], [(self.back[a], b) for a, b in pairs])


class Checker:
    """Rules of docs/PROBE.md over one writer's records."""

    def __init__(self, engine):
        self.engine = engine
        self.clear()

    def clear(self):
        self.prev, self.heap, self.keys, self.seq = None, [], set(), 0
        self.prev = {"hash": 0, "uf": 400}.get(self.engine)

    def check(self, r):
        e, v, op, bad = self.engine, r["vals"], r["op"], False
        out = []
        if r["seq"] != self.seq + 1:
            out.append("RESTART" if r["seq"] == 1 else "GAP")
        self.seq = r["seq"]
        if e == "heap":
            if op == "push":
                bisect.insort(self.heap, v["key"])
            else:
                bad = v["key"] != self.heap[0]
                self.heap.remove(v["key"])
        elif e == "hash":
            d = {("put", False): 1, ("del", True): -1}.get((op, v["known"]), 0)
            bad, self.prev = v["size"] != self.prev + d, v["size"]
        elif e == "avl":
            bad = v["height"] > wl_probe.h_bound(v["nodes"])
        elif e == "uf":
            bad, self.prev = v["comps"] != self.prev - v["merged"], v["comps"]
        elif e == "merge":
            bad = v["out"] != v["a"] + v["b"] - v["dup"]
        elif e == "bfs":
            bad = op == "visit" and self.prev is not None and v["level"] < self.prev
            self.prev = v["level"]
        elif e == "lru":
            bad = v["hits"] + v["misses"] != v["lookups"]
        elif op == "add":
            self.keys.add(v["key"])
        else:
            bad = v["key"] in self.keys and not v["member"]
        return out + [wl_probe.KIND[e]] * bad


def _rx(templates):
    return re.compile("|".join(re.escape(t).replace(re.escape("{n}"), r"\d+")
                               .replace(re.escape("{c}"), CODE.pattern) for t in templates))


def privileged(p, lines):
    """({"anomalies": [...]}, [codes to answer]) for one stream's lines."""
    notes = {"anomalies": []}
    for _ in wl_probe.trace(dict(p, records=0), notes):   # styles only
        pass
    ws = [{"rd": [Reader(st, w["tag"], wl_probe.COLS[w["engine"]]) for st in sts],
           "ck": Checker(w["engine"]), "tag": w["tag"], "open": None}
          for w, sts in zip(p["writers"], notes["styles"])]
    out, codes, bad_rx, ask_rx = [], [], _rx(wl_probe.BAD_NOTES), _rx(wl_probe.ASKS)
    decoy = _rx(wl_probe.DECOYS)

    def finish(x):
        n, rd, seq, body = x["open"]
        x["open"] = None
        lane, op = body[0].split(": ")[1], body[1]
        done(x, n, rd.record(seq, lane, op, [(rd.back[a], b) for a, b in
                                             (t.split(" -> ") for t in body[2:])]))

    def done(x, n, rec):
        for kind in x["ck"].check(rec):
            out.append({"line": n, "kind": kind, "cause": f"lane{rec['lane']}"})
    for n, raw in enumerate(lines, 1):
        s = STAMP.sub("", ANSI.sub("", raw)) if "\x1b" in raw or ":" in raw[:3] else raw
        block = next((x for x in ws if x["open"]), None)
        if block and re.match(r"     \S|       \S+ -> ", s):
            block["open"][3].append(s.strip())
            continue
        if block:
            finish(block)
        m = re.match(r">> ([A-H])/(\d+)$", s)
        if m:
            x = next(x for x in ws if x["tag"] == m[1])
            rd = next(r for r in x["rd"] if r.st["kind"] == "block")
            x["open"] = (n, rd, m[2], [])
            continue
        if bad_rx.fullmatch(s):
            out.append({"line": n, "kind": "DEGRADED", "cause": None})
        elif ask_rx.fullmatch(s) and not decoy.fullmatch(s):
            codes.append(CODE.search(s)[0])
        for x in ws:
            if s in [t.format(w=x["tag"]) for t in wl_probe.REINIT]:
                x["ck"].clear()
                x["ck"].seq = 0
                continue
            for rd in x["rd"]:
                try:
                    rec = rd.line(s)
                except (KeyError, ValueError, IndexError):
                    rec = None
                if rec:
                    done(x, n, rec)
                    break
    for x in ws:
        if x["open"]:
            finish(x)
    return {"anomalies": out}, codes


def grep_error(p, lines):
    hit = [n for n, s in enumerate(lines, 1) if re.search(r"error|fatal|fail|warn", s, re.I)]
    return {"anomalies": [{"line": n, "kind": "DEGRADED", "cause": None} for n in hit]}


def shape(s):
    return re.sub(r"\d+", "#", re.sub(r"\b[0-9a-f]*\d[0-9a-f]*\b", "#", ANSI.sub("", s)))


def rarity(p, lines, most=3):
    """Lines whose digit-free shape is rare."""
    count = {}
    for s in lines:
        count[shape(s)] = count.get(shape(s), 0) + 1
    return {"anomalies": [{"line": n, "kind": "DEGRADED", "cause": None}
                          for n, s in enumerate(lines, 1) if count[shape(s)] <= most]}


def rarity1(p, lines):
    return rarity(p, lines, most=1)


def counter_jump(p, lines):
    """One generic rule: the first integer of similar lines is a counter."""
    last, out = {}, []
    for n, s in enumerate(lines, 1):
        m = re.search(r"\d+", s)
        if not m:
            continue
        key, v = s[:3], int(m[0])
        if key in last and v not in (last[key], last[key] + 1):
            out.append({"line": n, "kind": "RESTART" if v == 1 else "GAP", "cause": "x"})
        last[key] = v
    return {"anomalies": out}


def every_100(p, lines):
    return {"anomalies": [{"line": n, "kind": "GAP", "cause": "x"}
                          for n in range(60, len(lines), 100)]}


BASELINES = {"grep_error": grep_error, "rarity": rarity, "rarity1": rarity1,
             "counter_jump": counter_jump, "every_100": every_100}


def one_kind(kinds):
    """Upper bound of a perfect detector that knows only some kinds."""
    def f(p, lines):
        r = privileged(p, lines)[0]
        return {"anomalies": [a for a in r["anomalies"] if a["kind"] in kinds]}
    return f


BOUNDS = {"perfect_counters_only": one_kind({"GAP", "RESTART"}),
          "perfect_messages_only": one_kind({"DEGRADED"})}
