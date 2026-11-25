"""job_analyze and job_watch end to end (PLUTO_LIVE_TESTS=1)."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from tests.smart_streams_server import ROOT, Server
from pluto_sdk import PlutoError

PY = sys.executable


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    s = Server(tmp_path_factory.mktemp("ss"), max_running=32,
               max_per_agent=32).start()
    yield s
    s.cleanup()


def until(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("condition not met")


def done(j, jid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = j.wait(job_id=jid, timeout_ms=5000)
        if not r["timed_out"]:
            return r["jobs"][0]
    raise AssertionError("job did not finish")


def hits(j, jid, n=1, timeout=20):
    """All watch notices of a job once there are n of them."""
    def get():
        return [x for x in j.call("notices")["notices"]
                if x["job_id"] == jid and x["event"] == "job_watch"]
    return until(lambda: (h := get()) and len(h) >= n and h, timeout)


def helpers():
    """Pids of watch helpers of this test's servers."""
    out = subprocess.run(["pgrep", "-f", "pluto_job_watch.py"], stdout=subprocess.PIPE)
    return [int(x) for x in out.stdout.split()]


def test_analyze_live_and_finished_jobs(srv):
    j = srv.jobs("an")
    a = j.start(command="for i in 1 2 3; do echo n=$i; done; printf 'n=4'; sleep 60")["job_id"]
    b = j.start(command="for i in 1 2 9; do echo n=$i; done")["job_id"]
    done(j, b)
    until(lambda: j.call("status", job_id=a)["size"] >= 15)
    # an unfinished line of a live job is not a record yet
    assert "3  6" in j.analyze(job_id=a, agg=["count", "sum:n"])["out"]
    assert "3  12" in j.analyze(job_id=b[-6:], agg=["count", "sum:n"])["out"]
    r = j.analyze(job_ids=[a, b], by=["_n"], compare=["n"])["out"]
    assert "differ: 1" in r and "_n=3" in r
    assert "L2: n=2" in j.analyze(job_id=b, rows=True, from_line=2, to_line=2)["out"]
    r = j.analyze(job_ids=[a, b], rows=True, from_offsets={a[-6:]: 4, b: 8})["out"]
    assert f"{a[-6:]} L2: n=2" in r and f"{b[-6:]} L3: n=9" in r and "L1" not in r
    with pytest.raises(PlutoError, match="positions_are_per_job"):
        j.analyze(job_ids=[a, b], from_line=2)
    with pytest.raises(PlutoError, match="staatus"):
        j.analyze(job_id=b, where=["staatus >"])
    with pytest.raises(PlutoError, match="bad_value_for_where"):
        j.analyze(job_id=b, where="n > 1")
    j.call("stream", job_id=b, action="split", from_offset=4, to_offset=8)
    assert "1  2" in j.analyze(job_id=b, stream="S1", agg=["count", "sum:n"])["out"]
    j.call("control", job_id=a, action="cancel")
    done(j, a)
    assert "4  10" in j.analyze(job_id=a, agg=["count", "sum:n"])["out"]


def test_analyze_hostile_patterns_keep_the_server_responsive(srv):
    j = srv.jobs("hostile")
    jid = j.start(command=f"{PY} -c \"print(('a'*5000+'!\\n')*2000)\"")["job_id"]
    done(j, jid)
    replies = []

    def bomb():
        try:
            replies.append(srv.jobs("h2").analyze(job_id=jid, where=["_line ~ ^(a+)+$"]))
        except PlutoError as exc:
            replies.append(str(exc))
    ts = [threading.Thread(target=bomb) for _ in range(6)]
    [t.start() for t in ts]
    slowest = 0
    for _ in range(20):
        t = time.time()
        j.call("status", job_id=jid)
        slowest = max(slowest, time.time() - t)
        time.sleep(.1)
    [t.join() for t in ts]
    assert slowest < 1.5 and len(replies) == 6
    assert all("too slow" in r or "busy" in r for r in replies), replies


def test_watch_notifies_waits_and_keeps_text_out_of_events(srv):
    j = srv.jobs("w1")
    jid = j.start(command="echo begin; sleep 1; echo s3cr3tline ERR; sleep 1; printf 'last ERR'")["job_id"]
    r = j.watch(job_id=jid, match="ERR", name="err", cooldown_s=0)
    assert r == {"jobs": {jid: "R1"}, "errors": {}}
    w = j.wait(job_id=jid, until="watch", timeout_ms=10000)["hits"][jid]
    assert (w["watch"][0]["line"], w["watch"][0]["text"]) == (2, "s3cr3tline ERR")
    # the last hit arrives although the job has ended by then
    w = j.wait(job_id=jid, until="watch", from_offsets={jid: w["next"]}, timeout_ms=10000)["hits"][jid]
    assert w["watch"][0]["line"] == 3
    w = j.wait(job_id=jid, until="watch", from_offsets={jid: w["next"]}, timeout_ms=10000)["hits"][jid]
    assert w["reason"] == "exit"
    ns = hits(j, jid, 2)
    assert [(n["watch"], n["line"]) for n in ns] == [("err", 2), ("err", 3)]
    ev = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/events?limit=10000").read()
    assert b"job_watch" in ev and b"s3cr3tline" not in ev
    j.call("notices", ack=ns[-1]["seq"])
    assert not [n for n in j.call("notices")["notices"] if n["job_id"] == jid
                and n["event"] == "job_watch"]
    with pytest.raises(PlutoError, match="no_watch_rules"):
        j.wait(job_id=j.start(command="true")["job_id"], until="watch")


def test_watch_permissions_and_management(srv):
    j, other = srv.jobs("own"), srv.jobs("stranger")
    jid = j.start(command="sleep 60")["job_id"]
    with pytest.raises(PlutoError, match="not_owner"):
        other.watch(job_id=jid, match="x", then="cancel")
    j.watch(job_id=jid, match="x", name="a", request_id="same")
    assert j.watch(job_id=jid, match="x", request_id="same")["jobs"] == {jid: "R1"}
    j.watch(job_id=jid, idle_s=100, name="b")
    with pytest.raises(PlutoError, match="not_owner"):
        other.watch("remove", job_id=jid)
    rules = other.watch("list", job_id=jid)["jobs"][jid]
    assert [(x["id"], x["spec"]["name"], x["active"]) for x in rules] == [
        ("R1", "a", True), ("R2", "b", True)]
    with pytest.raises(PlutoError, match="no_such_rule"):
        j.watch("remove", job_id=jid, rule="zz")
    assert j.watch("remove", job_id=jid, rule="a")["jobs"] == {jid: ["R1"]}
    assert srv.jobs("u", as_user=True).watch("remove", job_id=jid)["jobs"] == {jid: ["R2"]}
    for i in range(16):
        j.watch(job_id=jid, match=f"m{i}")
    with pytest.raises(PlutoError, match="too_many_rules"):
        j.watch(job_id=jid, match="m")
    for bad, term in (({"match": "re:("}, "bad regex"), ({"match": "x", "then": "rm"}, "rm"),
                      ({}, "say when"), ({"idle_s": -1}, "idle_s"), ({"match": 5}, "match"),
                      ({"where": ["a >"]}, "ends where")):
        with pytest.raises(PlutoError, match=term):
            j.watch(job_id=jid, **bad)
    j.call("control", job_id=jid, action="cancel")
    done(j, jid)
    with pytest.raises(PlutoError, match="already_finished"):
        j.watch(job_id=jid, match="x")
    r = j.watch(job_ids=[jid, (b := j.start(command="sleep 2")["job_id"])], match="x")
    assert r["jobs"] == {b: "R1"} and r["errors"] == {jid: "job_already_finished"}


def test_watch_actions_act_as_the_owner(srv):
    j = srv.jobs("act")
    loop = "i=0; while true; do i=$((i+1)); echo loss=$i; sleep 0.1; done"
    a, b = (j.start(command=loop)["job_id"] for _ in "ab")
    j.watch(job_id=a, where=["loss > 5"], then="cancel", once=True)
    j.watch(job_id=b, where=["loss > 5"], then="pause", once=True)
    assert done(j, a)["state"] == "cancelled"
    until(lambda: j.call("status", job_id=b)["state"] == "paused")
    (n,) = hits(j, b)
    assert n["did"] == "pause" and hits(j, a)[0]["did"] == "cancel"
    j.call("control", job_id=b, action="resume")
    time.sleep(1.5)
    assert j.call("status", job_id=b)["state"] == "running"       # once
    j.call("control", job_id=b, action="cancel")


def test_stalls_arrive_as_events_and_pauses_do_not_count(srv):
    j = srv.jobs("stall")
    a = j.start(command="echo up; sleep 60")["job_id"]
    b = j.start(command="while true; do echo step=7; sleep 0.1; done")["job_id"]
    c = j.start(command="while true; do echo tick; sleep 0.1; done")["job_id"]
    p = j.start(command="echo up; sleep 60")["job_id"]
    until(lambda: j.call("status", job_id=p)["size"])
    j.call("control", job_id=p, action="pause")
    until(lambda: j.call("status", job_id=p)["state"] == "paused")
    j.watch(job_ids=[a, b, p], idle_s=2)
    j.watch(job_id=b, stalled="step", stalled_n=5)
    j.watch(job_id=c, match="heartbeat", absent_s=2)
    assert "last line: up" in hits(j, a)[0]["text"]
    assert "step=7 unchanged for 5" in hits(j, b)[0]["text"]
    assert "no matching line" in hits(j, c)[0]["text"]
    time.sleep(1)
    assert len(hits(j, b)) == 1 and len(hits(j, a)) == 1
    assert not [n for n in j.call("notices")["notices"] if n["job_id"] == p]
    j.call("control", job_id=p, action="resume")
    assert hits(j, p)
    for x in (a, b, c, p):
        j.call("control", job_id=x, action="cancel")


def test_transfer_moves_rules_and_unseen_hits(srv):
    j, k = srv.jobs("from1"), srv.jobs("to1")
    jid = j.start(command="echo ERR 1; sleep 3; echo ERR 2; sleep 60")["job_id"]
    j.watch(job_id=jid, match="ERR", cooldown_s=0)
    hits(j, jid)
    j.call("transfer", job_id=jid, to="to1")
    assert [n["line"] for n in hits(k, jid)] == [1]
    assert [n["line"] for n in hits(k, jid, 2)] == [1, 2]
    assert not [n for n in j.call("notices")["notices"] if n["event"] == "job_watch"]
    with pytest.raises(PlutoError, match="not_owner"):
        j.watch("remove", job_id=jid)
    k.watch("remove", job_id=jid)
    k.call("control", job_id=jid, action="cancel")


@pytest.mark.parametrize("seed", range(3))
def test_kill9_neither_loses_nor_repeats_hits(tmp_path, seed):
    """The server dies again and again while lines match; another agent owns
    the job at the end, and everything is announced exactly once."""
    s = Server(tmp_path).start()
    try:
        j = s.jobs("k9")
        n = 250
        jid = j.start(command=f"i=0; while [ $i -lt {n} ]; do i=$((i+1)); "
                              "echo \"HIT $i\"; sleep 0.04; done")["job_id"]
        j.watch(job_id=jid, match="HIT", cooldown_s=0, max_hits=1000, name="all")
        j.watch(job_id=jid, match="HIT", where=["_n > 100"], once=True, name="one")
        seen, ack = [], 0

        def drain():
            nonlocal ack
            while True:
                ns = s.jobs("k9").call("notices", ack=ack)["notices"]
                if not ns:
                    return
                seen.extend(ns)
                ack = max(x["seq"] for x in ns)
        for i in range(6):
            time.sleep(0.6 + 0.17 * seed * (i % 2))
            if i % 2:
                drain()
            s.kill()
            if i == 2:
                time.sleep(1.5)         # lines arrive while nobody looks
            s.start()
        done(s.jobs("k9"), jid)
        until(lambda: drain() or any(x["event"] == "job_finished" for x in seen), 30)
        until(lambda: not s.jobs("k9").watch("list", job_id=jid)["jobs"][jid][0]["active"])
        drain()
        lines = [x["line"] for x in seen if x.get("watch") == "all"]
        assert lines == list(range(1, n + 1)), (len(lines), len(set(lines)))
        assert len([x for x in seen if x.get("watch") == "one"]) == 1
    finally:
        s.cleanup()


def test_helper_death_and_hostile_rules(srv):
    j = srv.jobs("hr")
    a = j.start(command="i=0; while true; do i=$((i+1)); echo HIT $i; sleep 0.2; done")["job_id"]
    b = j.start(command=f"{PY} -c \"print('a'*3000+'!', flush=True); import time; time.sleep(60)\"")["job_id"]
    j.watch(job_id=a, match="HIT", cooldown_s=0, max_hits=1000)
    j.watch(job_id=b, match="re:^(a+)+$", name="bomb")
    assert "too slow" in hits(j, b)[0]["text"]
    hits(j, a, 5)
    for pid in helpers():
        os.kill(pid, signal.SIGKILL)
    got = hits(j, a, len(hits(j, a)) + 5)
    assert [x["line"] for x in got] == list(range(1, len(got) + 1))[:len(got)]
    before = len(got)
    for pid in helpers():                     # hung, not dead
        os.kill(pid, signal.SIGSTOP)
    time.sleep(13)
    ns, ack = [], 0
    while True:
        page = j.call("notices", ack=ack)["notices"]
        if not page:
            break
        ns += page
        ack = page[-1]["seq"]
    lines = [x["line"] for x in ns if x["job_id"] == a]
    assert len(lines) > before + 30 and lines == list(range(1, len(lines) + 1))
    for x in (a, b):
        j.call("control", job_id=x, action="cancel")


def test_no_rules_no_helper(tmp_path):
    s = Server(tmp_path).start()
    try:
        j = s.jobs("nil")
        jid = j.start(command="while true; do echo x; sleep 0.1; done")["job_id"]
        mine = set(helpers())
        j.watch(job_id=jid, match="y")
        new = until(lambda: set(helpers()) - mine)
        j.watch("remove", job_id=jid)
        until(lambda: not new & set(helpers()))
        j.watch(job_id=jid, match="y")
        new = until(lambda: set(helpers()) - mine)
        j.call("control", job_id=jid, action="cancel")
        until(lambda: not new & set(helpers()))
        assert len(j.watch("list", job_id=jid)["jobs"][jid]) == 1
    finally:
        s.cleanup()


def test_rules_travel_inert(srv, tmp_path):
    j = srv.jobs("exp")
    jid = j.start(command="echo HIT; sleep 1; echo HIT")["job_id"]
    j.watch(job_id=jid, match="HIT", then="cancel", name="stop", where=["_n > 5"])
    done(j, jid)
    path = str(tmp_path / "x.pjob")
    j.export(jid, path)
    other = Server(tmp_path / "other").start()
    try:
        k = other.jobs("imp")
        before = set(helpers())
        assert k.import_(path)["state"] == "succeeded"
        (r,) = k.watch("list", job_id=jid)["jobs"][jid]
        assert r["inert"] and not r["active"] and r["spec"]["then"] == "cancel"
        other.kill()
        other.start()
        k = other.jobs("imp")
        time.sleep(2)
        assert not set(helpers()) - before
        assert not [n for n in k.call("notices")["notices"] if n["event"] == "job_watch"]
        with pytest.raises(PlutoError, match="no_watch_rules"):
            k.wait(job_id=jid, until="watch")
        with pytest.raises(PlutoError, match="finished"):
            k.watch(job_id=jid, match="x")
        assert "2 lines" in k.analyze(job_id=jid)["out"]
        doc = json.load(open(other.path(jid, "watch.json")))
        assert doc["inert"] is True and "hits" not in doc and "states" not in doc
        # what is imported can be exported again
        k.export(jid, str(tmp_path / "y.pjob"))
    finally:
        other.cleanup()
        os.environ["PLUTO_SMARTSTREAMS_HOME"] = srv.home


def test_queued_and_removed_jobs(tmp_path):
    s = Server(tmp_path, max_running=1).start()
    try:
        j = s.jobs("q")
        j.start(command="sleep 60")
        a, b = (j.start(command="echo HIT; sleep 60")["job_id"] for _ in "ab")
        j.watch(job_ids=[a, b], match="HIT")
        j.watch(job_ids=[a, b], idle_s=0.5)
        time.sleep(1.5)
        assert not j.call("notices")["notices"]           # queued time is no idle time
        j.call("control", job_id=a, action="cancel")
        r = j.wait(job_id=a, until="watch", timeout_ms=8000)
        assert r["hits"][a]["reason"] == "exit"
        import shutil
        shutil.rmtree(os.path.dirname(s.path(b, "x")))
        time.sleep(2)
        assert j.call("list")["total"] == 2
    finally:
        s.cleanup()


def test_over_mcp(srv):
    """One call answers, rules speak up, and nothing is told twice."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command=PY, env=dict(os.environ, PLUTO_SMARTSTREAMS_HOME=srv.home), args=[
            os.path.join(ROOT, "src_py/agent_smart_streams/smart_streams.py"),
            "--tools", "all", "--agent-id", "mcpout", "--host", "127.0.0.1", "--http-port", str(srv.port)])

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()

            async def call(tool, **kw):
                return json.loads((await s.call_tool(tool, kw)).content[0].text)
            t = await call("job_start", each={"n": "1-5"}, command=(
                "for i in 1 2 3 4 5 6; do echo step $i loss=$(( i == 4 ? {n}/5 : i )); "
                "sleep 0.5; done; echo end {n}"))
            g = t["group"]
            w_ = await call("job_watch", job_id=g, where="loss < prev", name="drop")
            assert w_ == {"added": {"R1": "5 jobs"}}, w_
            told = []
            while True:
                m = await call("job_wait", job_id=g, until="watch", timeout_s=20)
                assert not m.get("timed_out"), m
                told += [(x["job_id"], h) for x in m["jobs"] if isinstance(x.get("hit"), list)
                         for h in x["hit"]]
                told += [(x["job_id"], x["hit"]) for x in m.get("_job_notices", [])
                         if x["event"] == "job_watch"]
                if len({x["job_id"] for x in m["jobs"] if x["state"] == "succeeded"}) == 5 \
                        or not m["jobs"]:
                    break
                st = await call("job_status", job_id=g)
                st.pop("_job_notices", None)
                if len(told) >= 5 and all(x["state"] == "succeeded" for x in st.values()):
                    break
            assert len(told) == 5 == len({j for j, _ in told}), told
            assert all(h.startswith("drop L4: step 4 loss=") for _, h in told)
            o = await call("job_analyze", job_id=g)
            assert "30  " in o["out"] and set(o) <= {"out", "_job_notices"}
            o = await call("job_analyze", job_id=g, extract="step {s:int} loss={v:int}",
                           by="s", compare="v")
            assert "differ: 1" in o["out"] and "s=4: 0 (4 jobs) | 1 (5(" in o["out"]
            o = await call("job_analyze", job_id=g, agg=["count", "max:loss"], sort="-count")
            assert o["out"].count("\n") == 7, o["out"]
            assert "bad" in (await call("job_analyze", job_id=g, extract="re:("))["error"]
            ls = await call("job_watch", action="list", job_id=g)
            assert all("drop: where=" in v[0] and "fired 1x" in v[0]
                       for k, v in ls.items() if k != "_job_notices"), ls
    asyncio.run(go())
