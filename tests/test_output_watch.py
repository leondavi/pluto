"""job_watch without a server: rules, and the helper spoken to directly."""

import json
import os
import random
import select
import struct
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRIV = os.path.join(ROOT, "src_erl", "priv")
sys.path.insert(0, PRIV)

from pluto_out_fields import QueryError  # noqa: E402
from pluto_out_rules import Rule, check_rule, new_state  # noqa: E402


class FakeJob:
    label = name = "j"


def feed(lines, st=None, reload=False, **spec):
    """Hits as (line, also) for (clock, text) pairs; optionally with the
    state put through JSON after every line, as across restarts."""
    spec = check_rule(spec)
    r, out = Rule("R1", spec, st), []
    for n, item in enumerate(lines, 1):
        c, text = item if isinstance(item, tuple) else (float(n), item)
        if not r.done:
            r.tick(c, 0, c, lambda: (None, ""), True)
            r.feed(text, n, 0, c, FakeJob)
        out += [(h["line"], h.get("also", 0)) for h in r.take()]
        if reload:
            r = Rule("R1", spec, json.loads(json.dumps(r.st, allow_nan=False)))
    r.end(1e9)
    return out + [(h["line"], h.get("also", 0)) for h in r.take()]


@pytest.mark.parametrize("reload", [False, True])
def test_kinds_of_rules(reload):
    def go(lines, **kw):
        kw.setdefault("cooldown_s", 0)
        return [h[0] for h in feed(lines, reload=reload, **kw)]
    assert go(["a", "x ERR", "b", "ERR"], match="ERR") == [2, 4]
    assert go(["v=1", "v=3", "v=2", "v=9"], where=["v > prev"]) == [2, 4]
    assert go(["v=1", "v=nan", "v=nan"], where=["v != prev"]) == [2]
    assert go(["s=1", "s=2", "s=4", "noise", "s=5"], match="s={s:int}",
              where=["s != prev + 1", "has s"]) == [3]
    assert go(["v=1", "limit=2", "v=2", "v=3", "limit=9", "v=4"],
              where=["v > last.limit"]) == [4]
    two = ["w=a v=1", "w=b v=9", "w=a v=2", "w=b v=8"]
    assert go(two, where=["v > prev"], by=["w"]) == [3]
    assert go(["E"] * 5, match="E", count=2) == [2, 4]
    near = [(0.0, "E"), (10.0, "E"), (11.0, "E"), (11.5, "E"), (30.0, "E")]
    assert go(near, match="E", count=3, window_s=2) == [4]
    assert go(["k=1"] * 3 + ["k=2"] * 5, stalled="k", stalled_n=4) == [7]
    assert go(["l=nan"] * 4, stalled="l", stalled_n=3) == [3]
    assert go(["it {} done", "x"], match="{}") == [1]        # not a template
    assert go(["a 12", "b"], match="re:\\d+") == [1]
    assert go(["[ERROR] x", "ok"], where=["_level == error"]) == [1]
    assert go(["id,v", "1,5", "2,50"], where=["v > 10"], extract="csv:id,v") == [3]


def test_once_cooldown_cap_and_nothing_goes_unnoticed():
    assert feed(["E"] * 5, match="E", once=True) == [(1, 0)]
    # 1..5 within the cool-down of line 1; told together when it ends
    lines = [(float(i), "E") for i in range(1, 6)] + [(20.0, "quiet"), (21.0, "E")]
    assert feed(lines, match="E", cooldown_s=10) == [(1, 0), (5, 3), (7, 0)]
    assert feed(["E"] * 9, match="E", cooldown_s=0, max_hits=3) == [(1, 0), (2, 0), (3, 0)]
    assert feed(["E"] * 9, match="E", cooldown_s=100) == [(1, 0), (9, 7)]


def test_time_rules():
    r = Rule("R", check_rule({"idle_s": 5, "cooldown_s": 0}))
    last = lambda: (7, "bye")  # noqa: E731
    r.tick(4.9, 10, 0.0, last, True)
    assert not r.take()
    r.tick(5.0, 10, 0.0, last, True)
    r.tick(50.0, 10, 0.0, last, True)
    (h,) = r.take()                                     # once per silence
    assert h["line"] == 7 and "no output for 5s" in h["text"] and "bye" in h["text"]
    r.tick(51.0, 12, 51.0, last, True)
    r.tick(57.0, 12, 51.0, last, True)
    assert len(r.take()) == 1
    r = Rule("R", check_rule({"match": "beat", "absent_s": 5, "cooldown_s": 0}))
    r.tick(4.0, 1, 4.0, last, True)
    r.feed("beat", 1, 0, 4.0, FakeJob)
    r.tick(8.0, 1, 8.0, last, True)
    assert not r.take()
    r.tick(9.5, 1, 9.0, last, False)                    # still catching up
    assert not r.take()
    r.tick(9.5, 1, 9.0, last, True)
    assert len(r.take()) == 1


@pytest.mark.parametrize("spec,why", [
    ({}, "say when"), ({"match": "x", "then": "rm"}, "then='rm'"),
    ({"idle_s": 5, "match": "x"}, "idle_s"), ({"absent_s": 5}, "absent_s needs"),
    ({"match": "x", "window_s": 5}, "window_s needs count"),
    ({"match": "x", "count": 0}, "count must be"), ({"idle_s": -1}, "idle_s must"),
    ({"idle_s": 5, "stalled": "x"}, "different rules"), ({"match": "re:("}, "bad regex"),
    ({"match": "{a}{b}"}, "between"), ({"where": ["x >"]}, "ends where"),
    ({"match": "v={v}", "stalled": "w"}, "stalled='w'"),
    ({"match": "x", "extract": "nope", "where": ["a > 1"]}, "nope"),
    ({"match": "x", "cooldown_s": True}, "cooldown_s")])
def test_bad_rules_name_what_is_wrong(spec, why):
    with pytest.raises(QueryError, match=why):
        check_rule(spec)


class Helper:
    """The watch helper, as the server runs it."""

    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.p = subprocess.Popen(
            [sys.executable, "-I", "-B", os.path.join(PRIV, "pluto_job_watch.py"),
             str(tmp_path / "marker")], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.buf, self.states, self.hits, self.final = b"", {}, [], set()

    def send(self, msg):
        data = json.dumps(msg).encode()
        self.p.stdin.write(struct.pack(">I", len(data)) + data)
        self.p.stdin.flush()

    def job(self, name, *specs, states=None):
        rules = [{"id": f"R{i}", "spec": check_rule(s), "st": (states or {}).get(
            f"R{i}") or new_state()} for i, s in enumerate(specs)]
        self.send({"op": "set", "job": name, "path": str(self.dir / f"{name}.log"),
                   "state_path": str(self.dir / f"{name}.json"), "rules": rules})

    def state(self, name, **kw):
        kw.setdefault("started_at", int(time.time() * 1000) - 1000)
        (self.dir / f"{name}.tmp").write_text(json.dumps(dict({"paused_ms": 0}, **kw)))
        os.replace(self.dir / f"{name}.tmp", self.dir / f"{name}.json")

    def pump(self, until=None, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end and not (until and until()):
            if not select.select([self.p.stdout], [], [], .1)[0]:
                continue
            self.buf += os.read(self.p.stdout.fileno(), 1 << 20)
            while len(self.buf) >= 4 and len(self.buf) >= 4 + (
                    n := struct.unpack(">I", self.buf[:4])[0]):
                m, self.buf = json.loads(self.buf[4:4 + n]), self.buf[4 + n:]
                if "job" in m:
                    self.states[m["job"]] = m["states"]
                    self.hits += [dict(h, job=m["job"]) for h in m["hits"]]
                    if m["final"]:
                        self.final.add(m["job"])
        assert not until or until(), (self.hits, self.states)

    def close(self):
        self.p.stdin.close()
        self.p.stdout.close()
        return self.p.wait(5)


@pytest.fixture
def helper(tmp_path):
    h = Helper(tmp_path)
    yield h
    if h.p.poll() is None:
        h.close()


def test_helper_reads_whole_lines_and_the_unfinished_last_one(helper):
    helper.state("a")
    log = helper.dir / "a.log"
    log.write_bytes(b"one E\ntwo")
    helper.job("a", {"match": "E", "cooldown_s": 0}, {"match": "two!", "cooldown_s": 0})
    helper.pump(lambda: len(helper.hits) == 1)
    with open(log, "ab") as f:
        f.write(b"!\nE again\ntail E")
    helper.pump(lambda: len(helper.hits) == 3)
    size = log.stat().st_size
    helper.state("a", final_size=size, ended_at=int(time.time() * 1000))
    helper.pump(lambda: "a" in helper.final)
    assert sorted((h["rule"], h["line"]) for h in helper.hits) == [
        ("R0", 1), ("R0", 3), ("R0", 4), ("R1", 2)]
    assert helper.states["a"]["R0"]["offset"] == size
    assert helper.close() == 0


def test_hostile_rules_and_output_leave_other_jobs_alone(helper):
    for name in "ab":
        helper.state(name)
        (helper.dir / f"{name}.log").write_bytes(b"")
    helper.job("a", {"match": "re:^(a+)+$", "name": "bomb"},
               {"match": "re:(?P<x>(a|a)*)$", "where": ["x ~ ^(a|a)*$"], "name": "bomb2"},
               {"match": "fine", "cooldown_s": 0})
    helper.job("b", {"match": "E", "cooldown_s": 0})
    helper.pump(timeout=.5)
    with open(helper.dir / "a.log", "ab") as f:
        f.write(b"a" * 5000 + b"!\n" + b"\xff\0" * 3000 + b"fine\n" + b"z" * 3000000 + b"\nfine\n")
    with open(helper.dir / "b.log", "ab") as f:
        f.write(b"E\n")
    helper.pump(lambda: {h["name"] for h in helper.hits} >= {"bomb", "E"} and
                sum(h["name"] == "fine" for h in helper.hits) == 2, 20)
    assert "too slow" in next(h["text"] for h in helper.hits if h["name"] == "bomb")
    assert helper.states["a"]["R0"]["done"] and not helper.states["a"]["R2"].get("done")


def test_pause_stops_the_clock(helper):
    now = int(time.time() * 1000)
    (helper.dir / "a.log").write_bytes(b"x\n")
    helper.state("a", started_at=now, paused_since=now + 200)
    helper.job("a", {"idle_s": 1, "cooldown_s": 0})
    helper.pump(timeout=2.5)
    assert not helper.hits
    paused = int(time.time() * 1000) - now - 200
    helper.state("a", started_at=now, paused_ms=paused)
    helper.pump(lambda: helper.hits, 5)
    assert "no output for" in helper.hits[0]["text"]


def test_truncated_output_starts_over(helper):
    helper.state("a")
    log = helper.dir / "a.log"
    log.write_bytes(b"E long line of output\n" * 5)
    helper.job("a", {"match": "E", "cooldown_s": 0})
    helper.pump(lambda: len(helper.hits) == 5)
    log.write_bytes(b"x\nE\n")
    helper.pump(lambda: len(helper.hits) == 6)
    assert helper.hits[-1]["line"] == 2


def test_kill_and_continue_from_any_checkpoint(tmp_path):
    """Hits after a restart are exactly the ones not reported before."""
    vals = random.Random(2).choices(range(100), k=3000)
    lines = [f"v={v}" for v in vals]
    want = [i + 1 for i in range(1, 3000) if vals[i] > vals[i - 1] + 60]
    assert len(want) > 100
    spec = {"where": ["v > prev + 60"], "cooldown_s": 0, "max_hits": 1000}
    got, states, written = [], None, 0
    for part in range(5):
        h = Helper(tmp_path)
        h.state("a")
        h.job("a", spec, states=states)
        with open(tmp_path / "a.log", "ab") as f:
            f.write("".join(x + "\n" for x in lines[written:written + 600]).encode())
        written += 600
        h.pump(lambda: h.states.get("a", {}).get("R0", {}).get("line", 0) == written + 1
               if part < 4 else len(got) + len(h.hits) == len(want), 10)
        got += [x["line"] for x in h.hits]
        states = h.states["a"]
        h.p.kill()
        h.p.wait()
        h.p.stdin.close()
        h.p.stdout.close()
    assert got == want
