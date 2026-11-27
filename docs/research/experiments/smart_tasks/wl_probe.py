"""Workload 5: data-structure services with undocumented, drifting output.

A stream interleaves two writers. Each runs one engine (a real structure that is
really updated) and prints its records in some style that changes midway. The
trace is a pure function of the image, so a rerun prints the same lines.
"""

import argparse
import heapq
import json
import math
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

COLS = {"heap": ["key"], "hash": ["key", "known", "size"], "avl": ["nodes", "height"],
        "uf": ["a", "b", "merged", "comps"], "merge": ["a", "b", "dup", "out"],
        "bfs": ["node", "level"], "lru": ["lookups", "hits", "misses"], "set": ["key", "member"]}
KIND = {"heap": "ORDER", "hash": "COUNT", "avl": "HEIGHT", "uf": "COMPONENTS", "merge": "MERGE",
        "bfs": "LEVEL", "lru": "ACCOUNTING", "set": "MEMBERSHIP"}
OPS = {"push": ["push", "enqueue", "offer"], "pop": ["pop", "extract", "take-min"],
       "put": ["put", "store", "upsert"], "del": ["del", "remove", "evict"],
       "check": ["check", "measure", "audit"], "union": ["union", "link", "join"],
       "merge": ["merge", "combine", "zip"], "start": ["start", "source", "begin"],
       "visit": ["visit", "settle", "reach"], "stat": ["stat", "totals", "usage"],
       "add": ["add", "insert", "register"], "ask": ["ask", "query", "lookup"]}
NAMES = {"seq": ["seq", "n", "idx", "rec"], "lane": ["lane", "ln", "via", "thr"],
         "op": ["op", "ev", "act", "what"], "key": ["key", "k", "item"],
         "known": ["known", "present", "dup"], "size": ["size", "cnt", "total"],
         "nodes": ["nodes", "n_nodes", "population"], "height": ["height", "h", "depth"],
         "a": ["a", "left", "x"], "b": ["b", "right", "y"], "merged": ["merged", "joined", "new"],
         "comps": ["comps", "sets", "parts"], "dup": ["dup", "dropped", "equal"],
         "out": ["out", "result", "written"], "node": ["node", "vertex", "v"],
         "level": ["level", "dist", "hops"], "lookups": ["lookups", "reads", "asked"],
         "hits": ["hits", "served", "cached"], "misses": ["misses", "loaded", "faults"],
         "member": ["member", "found", "in"]}
BOOLS = [("yes", "no"), ("1", "0"), ("true", "false"), ("Y", "N")]
STYLES = ["kv", "json", "csv", "table", "block", "text0", "text1", "text2"]
REINIT = ["writer {w}: arena recycled, starting over from an empty structure",
          "{w} says: wiping everything and beginning afresh", "[{w}] rebuilt from scratch (planned)",
          "maintenance window: {w} drops its contents and starts anew"]
BAD_NOTES = [
    "note: scratch area full, the last {n} updates were not applied",
    "falling back to unchecked mode, duplicates will no longer be noticed",
    "to keep up, every {n}th record is now left out",
    "comparison routine replaced by a prefix comparison of {n} bytes; ties are arbitrary from here",
    "verification of block {n} skipped because of time pressure",
    "journal could not be flushed; what was acknowledged since {n} may be gone",
    "sampling switched on for the final pass, figures become approximate (1/{n})",
    "lock elision enabled without fallback, concurrent updates can now overwrite each other",
    "index of segment {n} is stale and stays in use",
    "two pages with the same id {n}; keeping the first one seen",
    "bounds test compiled out on the hot path as of now",
    "tail of segment {n} unreadable, continuing without it"]
GOOD_NOTES = [
    "note: scratch area full, spilled {n} updates to disk and applied them",
    "duplicate test moved to a worker thread, still complete",
    "to keep up, batch size raised to {n}", "comparison cache warmed with {n} bytes",
    "verification of block {n} finished ahead of time", "journal flushed late but completely ({n})",
    "sampling profiler attached for {n} ms", "lock elision tried and rolled back",
    "index of segment {n} was stale and has been rebuilt", "two pages swapped places: {n}",
    "bounds test now vectorised", "tail of segment {n} reread from the replica, checksum fine",
    "kernel reports {n} involuntary switches", "new epoch {n}", "hello from build {n}",
    "tuning: prefetch distance {n}", "someone attached with trace id {n}", "cron tick {n}"]
ASKS = ["Paused before the irreversible step. Answer {c} or I drop the shard.",
        "Need a go or no-go from whoever is watching: quote {c}.",
        "waiting on a human here, say {c} and I carry on",
        "operator? about to compact; confirm with {c}",
        "I will assume the worst unless I hear {c} soon",
        "Permission to proceed is outstanding ({c})"]
DECOYS = ["Ticket {c} was closed yesterday.", "manual, page 7: 'answer {c} or I drop the shard'",
          "earlier confirmation {c} accepted, nothing to do", "audit trail mentions {c} twice",
          "replaying an old session: need a go from whoever was watching: {c} (given)",
          "build label {c}"]
NOISE = ["ERROR: connection reset, retry 1/3 ok", "WARN queue depth above soft limit ({n})",
         "ERROR expected=1 got=1 (self-test of the reporter)", "debug: rss={n} kB",
         "\x1b[2K\x1b[33mwarning\x1b[0m: clock skew {n} us corrected",
         "FATAL-PATH COVERAGE: 0 of {n} hit", "ok {n}", "gc: {n} objects in {n} us",
         "INFO cache line size {n}", "\x1b[32m✓\x1b[0m batch {n}",
         "assertion failure injected by test harness, handled"]
TRACE = ["Traceback (most recent call last):", '  File "svc.py", line {n}, in poll',
         "    raise TimeoutError()", "TimeoutError (handled, polling again)"]


def h_bound(n):
    return 1.44 * math.log2(n + 2)


class Engine:
    """One real structure. step(bad) -> (op, values, planted)."""

    def __init__(self, name, rng):
        self.name, self.rng = name, rng
        self.clear()

    def clear(self):
        self.heap, self.keys, self.order, self.off = [], set(), [], 0
        self.n = self.rng.randrange(50, 5000)
        self.par, self.comps = list(range(400)), 400
        self.level, self.hits, self.miss = None, 0, 0

    def find(self, x):
        while self.par[x] != x:
            self.par[x] = x = self.par[self.par[x]]
        return x

    def step(self, bad):
        return getattr(self, "_" + self.name)(self.rng, bad)

    def _heap(self, r, bad):
        h = self.heap
        if bad and len(set(h)) > 2:
            k = r.choice([x for x in h if x > h[0]])
            h.remove(k)
            heapq.heapify(h)
            return "pop", {"key": k}, True
        if len(h) < 4 or (len(h) < 60 and r.random() < .53):
            k = r.randrange(10**5)
            heapq.heappush(h, k)
            return "push", {"key": k}, False
        return "pop", {"key": heapq.heappop(h)}, False

    def _hash(self, r, bad):
        k = r.randrange(300)
        was = k in self.keys
        op = "put" if r.random() < .55 else "del"
        (self.keys.add if op == "put" else self.keys.discard)(k)
        if bad:
            self.off += r.choice((-2, -1, 1, 2))
            self.off = self.off if len(self.keys) + self.off >= 0 else 2
        return op, {"key": k, "known": was, "size": len(self.keys) + self.off}, bad

    def _avl(self, r, bad):
        n = self.n = max(8, self.n + r.randrange(-40, 45))
        lo, hi = math.ceil(math.log2(n + 1)), math.floor(h_bound(n) - .05)
        h = math.ceil(h_bound(n) + .3) + r.randrange(2) if bad else r.randint(lo, hi)
        return "check", {"nodes": n, "height": h}, bad

    def _uf(self, r, bad):
        a, b = r.randrange(400), r.randrange(400)
        x, y = self.find(a), self.find(b)
        if x != y and self.comps + self.off > 40:
            self.par[x] = y
            self.comps -= 1
        elif x != y:                                     # keep the structure from collapsing
            b, y = a, x
        merged = x != y
        if bad:
            self.off += r.choice((-1, 1, 2))
        return "union", {"a": a, "b": b, "merged": merged, "comps": self.comps + self.off}, bad

    def _merge(self, r, bad):
        a, b = r.randrange(1, 900), r.randrange(1, 900)
        d = r.randrange(min(a, b)) if r.random() < .6 else 0
        e = r.choice((-3, -2, -1, 1, 2, 3)) if bad else 0
        return "merge", {"a": a, "b": b, "dup": d, "out": a + b - d + e}, bad

    def _bfs(self, r, bad):
        if self.level is None or self.level > 14:
            self.level = 0
            return "start", {"node": r.randrange(10**4), "level": 0}, False
        if bad and self.level >= 2:
            return "visit", {"node": r.randrange(10**4),
                             "level": self.level - r.choice((1, 1, 2))}, True
        self.level += r.random() < .12
        return "visit", {"node": r.randrange(10**4), "level": self.level}, False

    def _lru(self, r, bad):
        k = r.randrange(1, 9)
        h = r.randrange(k + 1)
        self.hits, self.miss = self.hits + h, self.miss + k - h
        e = r.choice((-4, -3, -1, 1, 2, 5)) if bad else 0
        return "stat", {"lookups": self.hits + self.miss, "hits": self.hits,
                        "misses": self.miss + e}, bad

    def _set(self, r, bad):
        if len(self.keys) < 5 or r.random() < .4:
            k = r.randrange(10**6)
            if k not in self.keys:
                self.keys.add(k)
                self.order.append(k)
            return "add", {"key": k, "member": None}, False
        old = bad or r.random() < .5
        k = r.choice(self.order) if old else r.randrange(10**6)
        yes = k in self.keys or r.random() < .03         # false positives are legal
        return "ask", {"key": k, "member": yes and not bad}, bad


def make_style(rng, avoid=None):
    kind = rng.choice([s for s in STYLES if s != avoid])
    names = {c: rng.choice(v) for c, v in NAMES.items()}
    vals = list(names.values())
    for c in names:                                      # no two columns share a name
        while vals.count(names[c]) > 1:
            names[c] = rng.choice(NAMES[c])
            vals = list(names.values())
    return {"kind": kind, "names": names, "ops": {c: rng.choice(v) for c, v in OPS.items()},
            "bool": rng.choice(BOOLS), "stamp": rng.random() < .3, "ansi": rng.random() < .25,
            "lane": rng.choice(["{}", "{:02d}", "L{}"])}


def render(st, w, seq, lane, op, cols, vals, lineno=0):
    """The text (one or more lines) of one record."""
    nm, yes_no = st["names"], st["bool"]

    def v(x):
        return yes_no[not x] if isinstance(x, bool) else str(x)
    shown = [(c, v(vals[c])) for c in cols if vals.get(c) is not None]
    ln, op, k = st["lane"].format(lane), st["ops"][op], st["kind"]
    if k == "kv":
        s = " ".join([f"src={w}", f"{nm['seq']}={seq}", f"{nm['lane']}={ln}", f"{nm['op']}={op}"]
                     + [f"{nm[c]}={x}" for c, x in shown])
    elif k == "json":
        s = json.dumps({"src": w, nm["seq"]: seq, nm["lane"]: ln, nm["op"]: op,
                        **{nm[c]: x for c, x in shown}})
    elif k == "csv":
        s = ";".join([w, str(seq), ln, op] + [v(vals[c]) if vals.get(c) is not None else ""
                                              for c in cols])
    elif k == "table":
        s = "| " + " | ".join([w, f"{seq:>6}", ln, f"{op:<9}"] + [
            f"{v(vals[c]) if vals.get(c) is not None else '-':>7}" for c in cols]) + " |"
    elif k == "block":
        return "\n".join([f">> {w}/{seq}", f"     {nm['lane']}: {ln}", f"     {op}"]
                         + [f"       {nm[c]} -> {x}" for c, x in shown])
    else:
        f = ", ".join(f"{nm[c]} {x}" for c, x in shown)
        s = {"text0": f"[{w}] #{seq} {op} {f} via {ln}",
             "text1": f"{w} did {op} with {f}; handled by {ln}; record {seq}",
             "text2": f"record {seq} from {w}: {op} gives {f} @{ln}"}[k]
    if st["ansi"]:
        s = "\x1b[36m" + s + "\x1b[0m"
    if st["stamp"]:
        s = f"{lineno // 3600 % 24:02d}:{lineno // 60 % 60:02d}:{lineno % 60:02d}.{seq % 1000:03d} " + s
    return s


def trace(p, notes=None):
    """Yield (fraction, text or event); ``notes`` collects the planted truth."""
    rng = random.Random(f"probe:{p['seed']}")
    ws, line, total = [], 1, p["records"]
    for i, w in enumerate(p["writers"]):
        st = make_style(rng)
        ws.append({"w": w["tag"], "eng": Engine(w["engine"], random.Random(f"{p['seed']}:e{i}")),
                   "styles": [st, make_style(rng, st["kind"])], "seq": 0, "i": 0, "due": [],
                   "plan": sorted(w["plan"]), "drift": w["drift"], "reinit": w["reinit"], "cfg": w})
    extra = sorted(p["extra"], key=lambda x: x[0])       # [fraction, what, text, kind]
    if notes is not None:
        notes["styles"] = [x["styles"] for x in ws]
    for r in range(total):
        frac = r / total
        while extra and extra[0][0] <= frac:
            _, what, text, kind = extra.pop(0)
            if what == "ask":
                yield frac, ("ASK", text, kind)
            else:
                yield frac, text
            if kind and what == "note" and notes is not None:
                notes["anomalies"].append({"line": line, "kind": kind, "lane": None, "cause": None})
            line += 1
        if rng.random() < p["noise"]:
            n = rng.randrange(1, 10**4)
            if rng.random() < .03:
                for x in TRACE:
                    yield frac, x.replace("{n}", str(n))
                    line += 1
            elif rng.random() < .03:
                bar = rng.randrange(8, 30)
                yield frac, "".join(f"\r[{'#' * j}{' ' * (bar - j)}] {100 * j // bar}%"
                                    for j in range(bar + 1))
                line += 1
            else:
                yield frac, rng.choice(NOISE).replace("{n}", str(n))
                line += 1
        x = ws[rng.random() < .5]
        x["i"] += 1
        if x["i"] == x["reinit"]:
            x["eng"].clear()
            x["seq"] = 0
            yield frac, rng.choice(REINIT).format(w=x["w"])
            line += 1
        while x["plan"] and x["plan"][0][0] <= x["i"]:
            x["due"].append(x["plan"].pop(0))
        item = x["due"][0] if x["due"] else None
        kind = item[1] if item else None
        st = x["styles"][x["i"] >= x["drift"]]
        if st["kind"] == "table" and x["i"] % 80 == 1:
            yield frac, "| " + " | ".join(["src", st["names"]["seq"], st["names"]["lane"],
                                           st["names"]["op"]] + [st["names"][c] for c in
                                                                 COLS[x["eng"].name]]) + " |"
            line += 1
        x["seq"] += 1
        hit = False
        if kind == "GAP":
            x["seq"], hit = x["seq"] + rng.randrange(3, 40), True
        elif kind == "RESTART":
            x["seq"], hit = 1, True
        op, vals, planted = x["eng"].step(kind not in (None, "GAP", "RESTART"))
        lane = rng.randrange(1, 25)
        if hit or planted:
            x["due"].pop(0)
            lane = item[2]
            if notes is not None:
                notes["anomalies"].append({"line": line, "kind": kind, "lane": lane,
                                           "cause": item[3]})
        text = render(st, x["w"], x["seq"], lane, op, COLS[x["eng"].name], vals, line)
        rows = text.split("\n")
        for t in rows:
            yield frac, t
        line += len(rows)
    yield 1.0, "bye"


def main():
    ap = argparse.ArgumentParser(description="Run one probe stream.")
    ap.add_argument("--stream", type=int, required=True)
    a = ap.parse_args()
    p = load_image(f"stream{a.stream}.img")
    led = Ledger(f"stream{a.stream}", stream=a.stream, acks={}, wrong=[])
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "control", f"stream_{a.stream}.ack")
    asked = {}

    def ask(text, code):
        say(text)
        asked[code] = time.time()

    def tick():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                words = f.read().split()
        except OSError:
            return
        for x in words[:200]:
            if x in asked:
                if x not in led.rec["acks"]:
                    led.rec["acks"][x] = round(time.time() - asked[x], 1)
            elif x not in led.rec["wrong"]:
                led.rec["wrong"].append(x)

    run_paced(trace(p), p["duration"], led, {"ASK": ask}, tick)
    tick()
    led.beat(done=True)


if __name__ == "__main__":
    main()
