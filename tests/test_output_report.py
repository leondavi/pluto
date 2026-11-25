"""pss report without a server: columns over all jobs in one pass."""

import json
import os
import random
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src_erl", "priv"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pluto_job_defs as d  # noqa: E402
from pluto_out_fields import QueryError  # noqa: E402
from pluto_out_report import Column  # noqa: E402
from test_output_analysis import run  # noqa: E402

TEMPLATES = ["E{e:int} b={b:int} probe={probe:int} live={live:int}",
             "E{e:int} CAL limit={limit:int}", "# run={run}", "E{e:int} FINAL digest={digest}"]


def test_columns():
    c = Column("v = first:e,b where probe > last.limit and x ~ 'a, b where'")
    assert (c.name, c.fn, c.fields) == ("v", "first", ["e", "b"])
    assert Column("n=count").fields == [] and Column("n=count where a > 1").fields == []
    assert Column("x=e").fn == "first" and Column("x = last : e").fields == ["e"]
    for bad in ("x", "x=", "x=nope:e", "=first:e", "x=sum:a,b", "x=first:", "x= where a > 1"):
        with pytest.raises(QueryError):
            Column(bad)


def test_one_pass_agrees_with_a_program(tmp_path):
    r = random.Random(7)
    logs, want = [], []
    for j in range(6):
        out, limit, prev, w = [f"# run=00{j}"], None, None, dict(
            run=f"00{j}", v=None, leak=None, spikes=[], n=0, top=None, digest=None)
        for e in range(1, 900):
            if r.random() < .01:
                limit = r.randrange(20, 50)
                out.append(f"E{e} CAL limit={limit}")
            b, probe = r.randrange(999), r.randrange(52)
            live = r.randrange(100, 104) + (50 if r.random() < .002 else 0)
            out += ["noise %d" % r.randrange(9), f"E{e} b={b} probe={probe} live={live}"]
            if limit is not None and probe > limit and w["v"] is None:
                w["v"] = {"e": e, "b": b}
            if prev is not None and abs(live - prev) > 3 and w["leak"] is None:
                w["leak"] = e
            if probe >= 51:
                w["spikes"].append(e)
            w["n"] += 1
            w["top"] = max(w["top"] or 0, probe)
            prev = live
        if j % 2:
            out.append("E900 FINAL digest=0%d7" % j)
            w["digest"] = "0%d7" % j
        logs.append("\n".join(out))
        want.append(w)
    got = run(tmp_path, logs, extract=TEMPLATES, columns=[
        "run=run", "v=first:e,b where probe > last.limit",
        "leak=first:e where abs(live - prev.live) > 3", "spikes=list:e where probe >= 51",
        "n=count:b", "top=max:probe", "digest=digest"])
    assert got["records"] == want and got["notes"] == []


def test_nothing_is_null_and_json_stays_json(tmp_path):
    got = run(tmp_path, ["v=nan w=inf", ""], extract=["v={v:float} w={w:float}"],
              columns=["v=v", "w=last:w", "s=sum:v", "c=count where v > 1", "l=list:zz"])
    assert got["records"] == [{"v": None, "w": None, "s": None, "c": 0, "l": []},
                              {"v": None, "w": None, "s": None, "c": 0, "l": []}]
    json.dumps(got, allow_nan=False)
    assert "zz" in got["notes"][0]


def test_wrong_requests_say_why(tmp_path):
    for kw, why in ((dict(columns=["a=1:"]), "column"), (dict(columns=["a=x", "a=y"]), "same name"),
                    (dict(columns=["a=x where x >"]), "ends where"),
                    (dict(columns=["a%d=x" % i for i in range(99)]), "more than")):
        assert why in run(tmp_path, "x=1", **kw)["error"]


def test_a_report_of_large_logs_is_quick(tmp_path):
    line = "".join(f"E{e} b={e % 977} probe={e % 47} live={e % 3}\n" for e in range(40000))
    assert 1.0e6 < len(line)
    t = time.time()
    got = run(tmp_path, [line * 2] * 24, extract=TEMPLATES, columns=[
        "v=first:e,b where probe > last.limit", "leak=first:e where abs(live - prev.live) > 3",
        "last=last:e", "spikes=list:e where probe >= 90"])
    took = time.time() - t
    assert len(got["records"]) == 24, got
    print("report of %.0f MB: %.1f s" % (48 * len(line) / 1e6, took))
    assert took < d.ANALYZE_DEADLINE_S
