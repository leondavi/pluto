"""job_analyze without a server: the helper process and its parts."""

import json
import math
import os
import random
import statistics
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRIV = os.path.join(ROOT, "src_erl", "priv")
sys.path.insert(0, PRIV)

import pluto_job_defs as d  # noqa: E402
import pluto_out_agg as agg  # noqa: E402
import pluto_out_fields as fields  # noqa: E402
from pluto_out_fields import QueryError, num  # noqa: E402
from pluto_out_outline import Outline  # noqa: E402
from pluto_out_where import Condition, Where  # noqa: E402

N = [0]


def run(tmp_path, logs, **q):
    """The helper, confined as on the server. *logs*: one or many texts."""
    jobs = []
    for i, text in enumerate(logs if isinstance(logs, list) else [logs]):
        N[0] += 1
        p = tmp_path / f"{N[0]}.log"
        p.write_bytes(text if isinstance(text, bytes) else text.encode())
        jobs.append({"id": f"j{i}", "name": None, "path": str(p), "from": 0,
                     "hi": p.stat().st_size, "line": 1, "closed": True})
    r = subprocess.run([sys.executable, "-I", "-B", os.path.join(PRIV, "pluto_job_analyze.py")],
                       input=json.dumps(dict(q, jobs=jobs)).encode() + b"\n",
                       stdout=subprocess.PIPE, timeout=60)
    return json.loads(r.stdout)


def out(tmp_path, logs, **q):
    r = run(tmp_path, logs, **q)
    assert "error" not in r, r
    return r["out"]


def table(text, head):
    rows = text.splitlines()
    at = next(i for i, x in enumerate(rows) if x.split("  ")[0] == head)
    return [dict(zip(rows[at].split("  "), x.split("  "))) for x in rows[at + 1:]
            if not x.startswith("(")]


# ── numbers and extractors ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,value", [
    ("12", 12), ("-1.5e3", -1500.0), ("900ms", .9), ("1.2s", 1.2), ("35µs", 35e-6),
    ("3 MiB", 3 << 20), ("45%", 45.0), ("1_000", None), ("12:30", None), ("", None),
    ("0x1f", None), ("9" * 60, 1e60), ("5zz", None), (True, None), (None, None),
    ("inf", math.inf), (10 ** 400, math.inf)])
def test_numbers(text, value):
    assert num(text) == pytest.approx(value) if value is not None else num(text) is None


def test_extractors():
    auto = fields.auto([])
    assert auto('t=1 msg="a \\"b\\" c" dur=3ms')["msg"] == 'a "b" c'
    assert auto("epoch 3: loss: 0.5, acc: 0.9") == {"loss": "0.5", "acc": "0.9"}
    assert auto('x {"a":{"b":[1,{"c":2}]}}') == {"a.b.0": 1, "a.b.1.c": 2}
    web = auto('1.2.3.4 - bob [01/Jan/2026:00:00:00 +0000] "GET /x HTTP/1.1" 404 12 "-" "curl"')
    assert (web["status"], web["path"], web["user"]) == ("404", "/x", "bob")
    assert auto("just words") is None
    csv = fields.auto(["id,name,ms", '1,"a,b",2', "2,c,3"])
    assert csv('7,"x,y",1')["name"] == "x,y" and csv("id,name,ms") is None
    f = fields.extractor("epoch {e:int} loss={loss:float} {rest}", [])
    assert f("[9] epoch  3 loss=nan  lr 1 2") == {"e": "3", "loss": "nan", "rest": "lr 1 2"}
    assert fields.extractor("{a}={{x}} {b}", [])("k={x} v w") == {"a": "k", "b": "v"}
    assert fields.extractor("csv:a,b", [])("1,2") == {"a": "1", "b": "2"}
    assert fields.extractor("re:(?P<n>\\d+)$", [])("a 12") == {"n": "12"}
    for bad, term in (("{a}{b}", "between"), ("{a} {a}", "twice"), ("{a:flaot}", "flaot"),
                      ("re:(", "bad regex"), ("re:x", "named groups"), ("zzz", "zzz"),
                      ("csv", "header")):
        with pytest.raises(QueryError, match=term):
            fields.extractor(bad, [])


def test_pseudo_fields():
    assert fields.when("2026-01-02T00:00:01.5Z x") - fields.when("2026-01-02 01:00:00+01:00") == 1.5
    assert fields.level("[WARNING] x") == "warn" and fields.level("plain") is None


# ── conditions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cond,rec,prev,want", [
    ("status >= 500", {"status": "503"}, {}, True),
    ("status >= 500", {"status": "ok"}, {}, False),
    ("status >= 500", {}, {}, False),
    ("msg contains connection refused", {"msg": "a connection refused b"}, {}, True),
    ("msg not contains x", {"msg": "y"}, {}, True),
    ("loss > prev", {"loss": "2"}, {"loss": "1"}, True),
    ("loss > prev", {"loss": "2"}, {}, False),
    ("seq != prev+1", {"seq": "5"}, {"seq": "4"}, False),
    ("a > prev.b * 2", {"a": "5"}, {"b": "2"}, True),
    ("x ~ ^a(b|c) d$", {"x": "ac d"}, {}, True),
    ("x ~ '\\d+' and y == 2", {"x": "a1", "y": "2"}, {}, True),
    ("x !~ z", {"x": "a"}, {}, True), ("x !~ z", {}, {}, False),
    ("has err", {"err": "1"}, {}, True), ("missing err", {"err": ""}, {}, True),
    ("not has err", {}, {}, True),
    ("level == ERROR and (a > 1 or b < 2)", {"level": "ERROR", "a": 0, "b": 1}, {}, True),
    ("(a+b)*2 > 5", {"a": "1", "b": "2"}, {}, True),
    ("used > total / 2", {"used": 6, "total": 10}, {}, True),
    ("a == $b", {"a": "x", "b": "x"}, {}, True),
    ("a / b > 1", {"a": 1, "b": 0}, {}, False),
    ("dur > 500ms", {"dur": "1.2s"}, {}, True),
    ("x in (a, 'b c', 3)", {"x": "3.0"}, {}, True),
    ("loss == nan", {"loss": "NaN"}, {}, True), ("loss != prev", {"loss": "nan"}, {"loss": "nan"}, False),
    ("n == -1", {"n": "-1"}, {}, True), ("d == 2026-01-02", {"d": "2026-01-02"}, {}, True),
    ("a.b-c > 1", {"a.b-c": 2}, {}, True), ("`w x` == 1", {"w x": 1}, {}, True),
    ("ok == true", {"ok": True}, {}, True), ("v < 2", {"v": "inf"}, {}, False),
])
def test_conditions(cond, rec, prev, want):
    assert Condition(cond)(rec, prev) is want


@pytest.mark.parametrize("cond,why", [
    ("status >", "ends where"), ("status", "operator"), ("x == foo bar", "quote"),
    ("x ~ (", "bad regex"), ("a has", "has"), ("prev > 1", "prev.field"),
    ("(" * 99, "nested"), ("x > 5qq", "5qq"), ("x in a", "list"), ("", "empty"),
    ("__import__('os')", "operator"), ("a > 1;b", "unexpected"), ("x" * 9999, "longer")])
def test_bad_conditions_name_the_term(cond, why):
    t = time.time()
    with pytest.raises(QueryError, match=why):
        Condition(cond)
    assert time.time() - t < 2


def test_conditions_never_crash():
    r = random.Random(3)
    bits = ["a", "prev", "prev.x", "1", "2ms", "'q'", "(", ")", "==", "<", "+", "-", "*",
            "/", "and", "or", "not", "has", "in", ",", "~", "contains", "$b", "`z`", "\\", "é"]
    for _ in range(20000):
        text = " ".join(r.choices(bits, k=r.randrange(1, 9)))
        try:
            Condition(text)({"a": "1", "b": None, "x": float("nan")}, {"a": 2})
        except QueryError:
            pass


def test_prev_skips_records_that_do_not_match():
    w = Where(["kind == a", "v < prev"])
    prev, hits = {}, []
    for rec in ({"kind": "a", "v": 5}, {"kind": "b", "v": 1}, {"kind": "a", "v": 6},
                {"kind": "a"}, {"kind": "a", "v": 4}):
        if w.matches(rec):
            if w.relates(rec, prev):
                hits.append(rec["v"])
            w.remember(rec, prev)
    assert hits == [4]


# ── aggregates against independent computations ─────────────────────────────

def test_aggregates_match_independent_computation(tmp_path):
    for seed in range(8):
        r = random.Random(seed)
        groups, logs = {}, []
        for j in range(3):
            lines = []
            for i in range(r.randrange(1, 900)):
                k = r.choice("abcd")
                v = r.choice([r.uniform(-1e6, 1e6), r.randrange(-50, 50), r.gauss(0, 1e-3)])
                lines.append(f"noise\nk={k} v={v!r} junk")
                groups.setdefault(k, []).append(float(v))
            logs.append("\n".join(lines))
        rows = table(out(tmp_path, logs, by=["k"], agg=[
            "count", "min:v", "max:v", "sum:v", "mean:v", "p50:v", "p95:v", "p99:v",
            "distinct:v"]), "k")
        assert {x["k"] for x in rows} == set(groups)
        for x in rows:
            v = sorted(groups[x["k"]])
            q = statistics.quantiles(v, n=100, method="inclusive") if len(v) > 1 else v * 99
            want = {"count": len(v), "min:v": v[0], "max:v": v[-1], "sum:v": math.fsum(v),
                    "mean:v": math.fsum(v) / len(v), "p50:v": q[49], "p95:v": q[94],
                    "p99:v": q[98], "distinct:v": len(set(v))}
            for k, y in want.items():
                assert float(x[k]) == pytest.approx(y, rel=1e-5, abs=1e-9), (seed, k)


def test_per_job_rows_and_total(tmp_path):
    t = table(out(tmp_path, ["v=1\nv=2\n", "v=30\n"], agg=["count", "sum:v", "argmax:v",
                                                          "first:v", "p50:v"]), "_job")
    assert [x["sum:v"] for x in t] == ["3", "30", "33"] and t[2]["_job"] == "ALL"
    assert t[0]["argmax:v"] == "j0:2" and t[2]["p50:v"] == "2"


def test_nan_and_inf(tmp_path):
    o = out(tmp_path, "v=1\nv=nan\nv=inf\nv=-inf\nv=oops\n",
            agg=["count", "min:v", "max:v", "mean:v", "p50:v"])
    assert "nan in 1 records" in o and "5  -inf  inf  nan  1" in o
    assert "L2: " in out(tmp_path, "v=1\nv=nan\n", where=["v == nan"], rows=True)
    assert "inf" in out(tmp_path, '{"v":1e999}\n{"v":NaN}\n', agg=["sum:v"])


def test_truncation_always_states_totals(tmp_path):
    log = "".join(f"k={i} v={i}\n" for i in range(300))
    o = out(tmp_path, log, by=["k"], agg=["count", "max:v"], sort="-max:v", limit=2)
    assert "(2 of 300 groups shown, 2 of 300 records" in o and "299  1  299" in o
    assert "ALL  300  299" in o
    o = out(tmp_path, [log, log], extract="auto", rows=["v"], limit=3)
    assert o.count("j0 L") == o.count("j1 L") == 3           # the limit is per job
    assert "(6 of 600 rows shown)" in o and "j0 300, j1 300" in o
    o = out(tmp_path, [log] * 40, rows=True, limit=500)
    assert len(o) <= d.ANALYZE_TEXT + 200 and "rows shown" in o and "of 12000" in o
    top = out(tmp_path, [log, "k=1 v=999\n"], rows=["v"], sort="-v", limit=1)
    assert "j1 L1: v=999" in top


def test_notes_name_what_is_wrong(tmp_path):
    log = "status=200 path=/a\nstatus=500 path=/b\n"
    assert "did you mean 'status'" in out(tmp_path, log, where=["statos > 1"], rows=True)
    assert "no line matches" in out(tmp_path, log, extract="{a:int} rows")
    assert "mean seconds" in out(tmp_path, "t=5ms\n", where=["t > 1"])
    for q, term in (({"agg": ["p95"]}, "p95"), ({"agg": ["mode:x"]}, "mode"),
                    ({"agg": ["count"], "sort": "zap"}, "zap"), ({"by": ["_zap"]}, "_zap"),
                    ({"where": ["a ="]}, "a =")):
        assert term in run(tmp_path, log, **q)["error"]


# ── comparing jobs ──────────────────────────────────────────────────────────

def test_compare_finds_the_odd_one_out(tmp_path):
    def log(bad=None, upto=20):
        return "".join(f"step {i} hash={i * 7 + (i == bad)}\n" for i in range(upto))
    logs = [log(), log(), log(5), log(), log(upto=15)]
    o = out(tmp_path, logs, extract="step {s:int} hash={h}", by=["s"], compare=["h"])
    assert "keys: 20, agree in all jobs: 14, differ: 1, missing in some job: 5" in o
    assert "odd one out: j2 1x" in o and "s=5: 35 (4 jobs) | 36 (j2)" in o
    assert "missing in j4: 5 keys, from s=15 to s=19" in o
    o = out(tmp_path, [log(), log(3)], extract="step {s:int} hash={h}", compare=["h"])
    assert "record #4: 21 (j0) | 22 (j1)" in o               # aligned by position
    assert "agree in all jobs: 1" in out(tmp_path, ["x=1.0"] * 3 + ["x=1"], compare=["x"])
    assert "no such records in: j1" in out(tmp_path, ["x=1", "y"], compare=["x"])


def test_compare_matches_brute_force(tmp_path):
    r = random.Random(9)
    data = [{k: r.choice([1, 1, 1, 2]) for k in range(60) if r.random() > .05}
            for _ in range(5)]
    o = out(tmp_path, ["".join(f"k={k} v={v}\n" for k, v in x.items()) for x in data],
            by=["k"], compare=["v"])
    keys = set().union(*data)
    same = sum(all(k in x for x in data) and len({x[k] for x in data}) == 1 for k in keys)
    differ = sum(len({x[k] for x in data if k in x}) > 1 for k in keys)
    missing = sum(any(k not in x for x in data) for k in keys)
    assert (f"keys: {len(keys)}, agree in all jobs: {same}, differ: {differ}, "
            f"missing in some job: {missing}") in o


# ── outline ─────────────────────────────────────────────────────────────────

BUILD = "".join(f"[{i}/200] Compiling src/m{i % 9}/file{i}.c\n" for i in range(1, 201)) \
    + "warning: unused variable 'x'\nsrc/a.c:12:3: error: undefined reference\n0 errors so far\n"


def test_outline_of_a_build(tmp_path):
    o = out(tmp_path, BUILD)
    assert o.startswith("203 lines in 1 job, format: plain")
    assert "200  L1..L200  [<n>/<n>] Compiling <path>" in o and "#1 1..200" in o
    assert "likely errors: 1 lines" in o and "1  L202" in o
    assert "likely warnings: 1 lines" in o and "1  L201" in o


def test_outline_counts_every_line(tmp_path):
    r = random.Random(4)
    words = ["open", "close", "read", "failed", "x1", "0xdeadbeef", "1.5", "-3", "a/b/c",
             "2026-01-01T00:00:00Z", "10.0.0.1", "k=v", "é", "\0", "[", "\\"]
    log = [" ".join(r.choices(words, k=r.randrange(0, 7))) for _ in range(5000)]
    o = Outline()
    for i, x in enumerate(log):
        o.add(x.encode(), 0, i + 1)
    o.report(["j"], 50, True)
    extra = o.lumped.count if o.lumped else 0
    assert sum(t.count for t in o.templates.values()) + extra + o.blank == 5000
    assert o.blank == sum(not x.strip() for x in log)


def test_outline_keeps_ok_and_failed_apart_but_merges_names():
    o = Outline()
    for i in range(300):
        o.add(b"job %d %s" % (i, b"failed" if i % 50 == 0 else b"ok"), 0, i + 1)
        o.add(b"user u%s logged in" % bytes([97 + i % 20]), 0, i + 1)
    text = "\n".join(o.report(["j"], 50, True))
    assert "6  L1..L251  job <n> failed" in text and "294 " in text
    assert "user <*> logged in" in text


def test_outline_huge_cardinality_is_bounded(tmp_path):
    r = random.Random(5)
    log = "".join("".join(r.choices("abcdefghij", k=8)) + " " +
                  "".join(r.choices("klmnop", k=8)) + "\n" for _ in range(60000))
    o = out(tmp_path, log)
    assert len(o) < 8000 and "60000 lines" in o


def test_outline_names_the_deviating_job(tmp_path):
    log = "start\n" + "".join(f"item {i} done\n" for i in range(30)) + "end\n"
    logs = [log] * 5 + [log.replace("end\n", "item 9 done\nTraceback boom\n")]
    o = out(tmp_path, logs)
    tail = o[o.index("jobs that deviate"):]
    assert "j5 (3 templates)" in tail and "j5=1 (others 0): Traceback boom" in tail
    assert "j5=0 (others 1): end" in tail and "j5=31 (others 30)" in tail
    assert "no template deviates" in out(tmp_path, [log] * 3)


# ── hostile input ───────────────────────────────────────────────────────────

HOSTILE = [
    bytes(range(256)) * 400,                              # invalid UTF-8, NULs
    b"a" * (5 << 20) + b"\nerror x\n",                    # megabytes without a newline
    b"\0" * 100000, b"\n" * 100000, b"",
    b"\x1b[31mstatus=\x1b[0m500\x1b]0;title\x07\n" * 50,
    b"1%\r50%\r100%\n" * 100,
    b'{"a":' * 50000 + b"1" + b"}" * 50000 + b"\n",       # deep JSON
    b"{" * 60000 + b"}\n", b"[" * 60000 + b"\n",
    b"=" * 60000 + b"\n", b'k="' * 20000 + b"\n",
    (b"a" * 60000 + b" " + b"a/" * 5000 + b"\n") * 3,
    b"a" * 30 + b"=" + b"9" * 60000 + b"\n" + b"v=1" + b"0" * 5000 + b"\n",
    " \u0085é漢😀=1\n".encode() * 100,
    b'x="' + b"\\" * 5000 + b"\n",
    b'{"a":' + b"9" * 4000 + b"}\n",
]


@pytest.mark.parametrize("i", range(len(HOSTILE)))
def test_hostile_output(tmp_path, i):
    t = time.time()
    for q in ({}, {"extract": "auto"}, {"by": ["a"], "agg": ["sum:a", "p99:a", "distinct:a"]},
              {"where": ["_line ~ (x+)y", "_n >= 1"]}, {"compare": ["a"]},
              {"rows": ["_level", "_time", "a"], "extract": "k={a}"}):
        o = out(tmp_path, HOSTILE[i], **q)
        assert len(o) < d.ANALYZE_TEXT + 300
    assert time.time() - t < 25


def test_ansi_and_progress_bars(tmp_path):
    assert "status=500" in out(tmp_path, HOSTILE[5], rows=True, limit=1)
    assert "50  50  50" not in (o := out(tmp_path, HOSTILE[5], agg=["count", "max:status"]))
    assert "50  500" in o
    assert "100  L1..L100  <n>%" in out(tmp_path, HOSTILE[6])


def test_regex_bomb_ends_with_a_clear_error(tmp_path):
    log = "ok\n" + ("a" * 60 + "!\n") * 10
    t = time.time()
    for q in ({"extract": "re:^(?P<x>(a+)+)$"}, {"where": ["_line ~ ^(a|a)*$"]}):
        assert "too slow" in run(tmp_path, log, **q)["error"]
    assert time.time() - t < 20
    r = run(tmp_path, "ok 1\n" + "a" * 60 + "!\nok 2\n", extract="re:^(?P<x>ok|(a+)+)$")
    assert "they were skipped" in r["out"]


def test_memory_is_bounded(tmp_path):
    """A million groups with sets and samples do not take the machine down."""
    log = tmp_path / "big.log"
    with open(log, "w") as f:
        for i in range(600000):
            f.write(f"k=key{i} v={i * 1.5}\n")
    r = run(tmp_path, log.read_bytes(), by=["k"], agg=["distinct:v", "p95:v", "first:k"])
    assert "error" in r or "more than 100000 groups" in r["out"]


def test_stream_positions(tmp_path):
    """Any block boundary gives the same lines."""
    from pluto_out_lines import Reader
    r = random.Random(6)
    data = b"\n".join(b"x" * r.choice([0, 1, 5, 300, 70000]) for _ in range(40))
    p = tmp_path / "f"
    p.write_bytes(data)
    want, at = [], 0
    for part in data.split(b"\n"):
        want.append((at, part[:d.LINE_MAX]))
        at += len(part) + 1
    if not want[-1][1]:
        want.pop()
    for size in (1, 7, 1000, 1 << 20):
        rd, got = Reader(str(p), 0, len(data), 1, True), []
        while (b := rd.block(size * (1 if size > 1 else 4096))) is not None:
            got += b
        assert got == want


def test_groups_budget_falls_back_to_a_sample():
    b = agg.Budget()
    b.values = 1000
    n = agg.Numbers(b)
    for i in range(20000):
        n.add(float(i))
    assert len(n.vals) == 1000 and 9000 < n.at(.5) < 11000
