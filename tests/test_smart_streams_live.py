"""SmartStreams end to end against a private server (PLUTO_LIVE_TESTS=1).

Needs erl and a compiled src_erl (``rebar3 compile``).
"""

import base64
import hashlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from tests.smart_streams_server import ROOT, Server
from pluto_sdk import PlutoError

PY = sys.executable


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    s = Server(tmp_path_factory.mktemp("ss"), max_running=16,
               max_per_agent=16).start()
    yield s
    s.cleanup()


def done(j, jid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        r = j.wait(job_id=jid, timeout_ms=5000)
        if not r["timed_out"]:
            return r["jobs"][0]
    raise AssertionError("job did not finish")


def run(j, command, **kw):
    jid = j.start(command=command, **kw)["job_id"]
    return jid, done(j, jid)


def raw(j, jid, **kw):
    """Read everything through the cursor as raw bytes."""
    out = b""
    while True:
        m = j.call("next", job_id=jid, encoding="base64", **kw)
        out += base64.b64decode(m["data"])
        if m["eof"]:
            return out


def until(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("condition not met")


def test_start_status_and_failure(srv):
    j = srv.jobs()
    jid, s = run(j, "echo hi; exit 4", name="x", tags=["t"])
    assert (s["state"], s["exit_code"], s["owner"]) == ("failed", 4, "a1")
    assert s["user"] and s["opened_at"] and s["elapsed_ms"] >= 0
    with pytest.raises(PlutoError, match="cwd"):
        j.start(command="true", cwd="/no/such")
    with pytest.raises(PlutoError, match="exactly_one"):
        j.start(command="true", argv=["true"])
    bad = j.start(argv=["/no/such/bin"])["job_id"]
    assert done(j, bad)["state"] == "failed"
    assert jid in [x["job_id"] for x in j.call("list", tags=["t"])["jobs"]]


def test_cursor_is_byte_exact_with_awkward_bytes(srv):
    j = srv.jobs("cur")
    code = ("import sys,random;r=random.Random(1);w=sys.stdout.buffer.write;"
            "[w(r.choice(['a','é','漢','😀','\\n']).encode()*3) for _ in range(20000)];"
            "w(bytes(r.randrange(256) for _ in range(50000)));w(b'\\xf0\\x9f')")
    jid, s = run(j, f'{PY} -c "{code}"')
    data = open(srv.path(jid, "output.log"), "rb").read()
    assert raw(j, jid, max_bytes=777) == data
    assert raw(srv.jobs("other"), jid) == data
    # text mode over the same bytes never fails
    while True:
        m = j.call("next", job_id=jid, from_offset=None, max_bytes=500)
        if m["eof"]:
            break
    m = j.call("window", job_id=jid, offset=0, max_bytes=4096)
    assert hashlib.sha256(data[m["start"]:m["end"]]).hexdigest() == m["sha256"]


def test_utf8_never_split(srv):
    j = srv.jobs("u8")
    jid, _ = run(j, f"{PY} -c \"print('漢'*5000)\"")
    text = ""
    while True:
        m = j.call("next", job_id=jid, max_bytes=1000)
        text += m["text"]
        if m["eof"]:
            break
    assert text == "漢" * 5000 + "\n"


def test_retry_replays_same_range(srv):
    j = srv.jobs("retry")
    jid, _ = run(j, "seq 1 5000")
    a = j.call("next", job_id=jid, max_bytes=100, request_id="r1")
    b = j.call("next", job_id=jid, max_bytes=100, request_id="r1")
    c = j.call("next", job_id=jid, max_bytes=100, request_id="r2")
    assert a["text"] == b["text"] and c["start"] == a["end"]
    one = j.start(command="true", request_id="s1")["job_id"]
    assert j.start(command="true", request_id="s1")["job_id"] == one


def test_window_positions(srv):
    j = srv.jobs("win")
    jid, _ = run(j, "seq 1 200000")
    data = open(srv.path(jid, "output.log"), "rb").read()
    for line in (1, 2, 9999, 13000, 150001, 200000):
        m = j.call("window", job_id=jid, line=line, lines=2)
        want = "".join(f"{n}\n" for n in (line, line + 1) if n <= 200000)
        assert m["text"] == want and m["first_line"] == line
    m = j.call("window", job_id=jid, line=100, before=3, lines=4)
    assert m["text"] == "97\n98\n99\n100\n" and m["last_line"] == 100
    assert j.call("window", job_id=jid, tail=2)["text"] == "199999\n200000\n"
    m = j.call("window", job_id=jid, offset=65530, max_bytes=100)
    assert m["first_line"] == data[:m["start"]].count(b"\n") + 1
    assert j.call("window", job_id=jid, max_bytes=10**9)["end"] == len(data)
    run2, _ = run(j, "printf '\\033[31mred\\033[0m\\n'")
    assert j.call("window", job_id=run2, strip_ansi=True)["text"] == "red\n"


def test_grep_matches_system_grep(srv):
    j = srv.jobs("grep")
    jid, _ = run(j, "seq 1 3000000")
    log = srv.path(jid, "output.log")
    for pat, c in ((r"99999$", 2), (r"^1[0-9]{2}77$", 0), (r"^(5|6|9)$", 1)):
        out = subprocess.run(["grep", "-n", "-E", "-C", str(c), pat, log],
                             text=True, capture_output=True,
                             env=dict(os.environ, LC_ALL="C")).stdout
        m = j.call("grep", job_id=jid, pattern=pat, before=c, after=c,
                   max_matches=500)
        mine = "\n--\n".join("\n".join(
            f"{e['line']}{':' if e['match'] else '-'}{e['text']}" for e in g)
            for g in m["groups"])
        if c == 0:          # grep prints no separators without context
            mine = mine.replace("\n--\n", "\n")
        assert mine == out.rstrip("\n") and m["complete"]
    m = j.call("grep", job_id=jid, pattern="7", max_matches=3)
    assert m["matches"] == 3 and not m["complete"]
    m2 = j.call("grep", job_id=jid, pattern="7", max_matches=1,
                from_offset=m["resume_at"])
    assert m2["groups"][0][0]["line"] == 37
    w = j.call("window", job_id=jid, offset=m2["groups"][0][0]["offset"], lines=1)
    assert w["text"] == "37\n"
    m = j.call("grep", job_id=jid, pattern="1", fixed=True, from_line=100, to_line=101)
    assert [e["line"] for g in m["groups"] for e in g] == [100, 101]
    with pytest.raises(PlutoError):
        j.call("grep", job_id=jid, pattern="(")


def test_grep_hostile_pattern_keeps_server_responsive(srv):
    j = srv.jobs("evil")
    jid, _ = run(j, f"{PY} -c \"[print('a'*40000+'b') for _ in range(300)]\"")
    t = threading.Thread(target=lambda: j.call("grep", job_id=jid, pattern="(a+)+$"))
    start = time.time()
    t.start()
    worst = 0
    while t.is_alive():
        t0 = time.time()
        j.http._get("/ping")
        worst = max(worst, time.time() - t0)
    assert worst < 1 and time.time() - start < 20


def test_manifest_verify_finds_altered_byte(srv):
    j = srv.jobs("man")
    jid, s = run(j, "seq 1 100000")
    m = j.call("manifest", job_id=jid, verify=True)
    assert m["verified"] and m["sha256"] == s["sha256"]
    log = srv.path(jid, "output.log")
    with open(log, "r+b") as f:
        f.seek(65536 * 3 + 5)
        f.write(b"X")
    m = j.call("manifest", job_id=jid, verify=True)
    assert m["bad_chunks"] == [3]


def test_wait_modes(srv):
    j = srv.jobs("wait")
    a = j.start(command="sleep 1; echo READY now; sleep 30")["job_id"]
    r = j.wait(job_id=a, until="pattern", pattern="READY", timeout_ms=10000)
    assert r["hits"][a]["reason"] == "pattern" and r["hits"][a]["line"] == 1
    r = j.wait(job_id=a, until="idle", idle_ms=1500, timeout_ms=10000)
    assert r["hits"][a]["reason"] == "idle"
    t0 = time.time()
    assert j.wait(job_id=a, timeout_ms=12000)["timed_out"]
    assert 11 < time.time() - t0 < 15     # beyond the client's 10 s default
    b = j.start(command="sleep 1")["job_id"]
    r = j.wait(job_ids=[a, b], timeout_ms=10000)
    assert list(r["hits"]) == [b]
    j.call("control", job_id=a, action="cancel")
    assert not j.wait(job_ids=[a, b], all=True, timeout_ms=15000)["timed_out"]


def test_input_and_ownership_and_transfer(srv):
    a, b = srv.jobs("own-a"), srv.jobs("own-b")
    jid = a.start(command="while read x; do echo got $x; done", stdin=True)["job_id"]
    until(lambda: a.call("status", job_id=jid)["state"] == "running")
    a.call("input", job_id=jid, text="one\n")
    a.wait(job_id=jid, until="pattern", pattern="got one")
    m = a.call("next", job_id=jid)
    assert m["text"] == "got one\n"
    for op, kw in (("control", {"action": "cancel"}), ("input", {"text": "x"}),
                   ("transfer", {"to": "own-b"})):
        with pytest.raises(PlutoError, match="not_owner"):
            b.call(op, job_id=jid, **kw)
    a.call("transfer", job_id=jid, to="own-b")
    b.call("input", job_id=jid, text="two\n", close=True)
    assert done(b, jid)["owner"] == "own-b"
    assert b.call("next", job_id=jid)["text"] == "got two\n"   # cursor inherited
    with pytest.raises(PlutoError, match="not_owner"):
        a.call("control", job_id=jid, action="cancel")
    user = srv.jobs("user:x", as_user=True)
    user.call("transfer", job_id=jid, to="own-a")
    evs = [n["event"] for n in b.call("notices")["notices"]]
    assert "job_finished" in evs and evs.count("job_transferred") >= 1


def test_notices_survive_until_acked(srv):
    j = srv.jobs("noti")
    run(j, "true", name="n1")
    ns = until(lambda: j.call("notices")["notices"])
    assert ns[0]["name"] == "n1" and j.call("notices")["notices"]
    assert j.call("notices", ack=ns[-1]["seq"])["notices"] == []


def test_control_pause_tree(srv):
    j = srv.jobs("tree")
    p = j.start(command="sleep 300")["job_id"]
    c = j.start(command="while :; do echo x; sleep 0.05; done", parent=p)["job_id"]
    until(lambda: j.call("status", job_id=c)["size"] > 10)
    j.call("control", job_id=p, action="pause", tree=True)
    until(lambda: j.call("status", job_id=c)["state"] == "paused")
    n = os.path.getsize(srv.path(c, "output.log"))
    time.sleep(0.6)
    assert os.path.getsize(srv.path(c, "output.log")) == n
    assert j.call("list", parent=p)["jobs"][0]["job_id"] == c
    j.call("control", job_id=p, action="halt", tree=True)
    assert done(j, p)["state"] == done(j, c)["state"] == "cancelled"


def test_nested_streams(srv):
    j, other = srv.jobs("ns"), srv.jobs("ns2")
    jid, _ = run(j, "for s in A B C; do echo == $s; seq 1 5; done")
    r = j.call("stream", job_id=jid, action="split", pattern="^== ")
    ids = [s["id"] for s in r["streams"]]
    assert len(ids) == 3
    texts = [j.call("next", job_id=jid, stream=i)["text"] for i in ids]
    assert texts[1] == "== B\n1\n2\n3\n4\n5\n"
    m = j.call("grep", job_id=jid, stream=ids[2], pattern="^3$")
    assert m["matches"] == 1 and m["groups"][0][0]["line"] == 16
    r = j.call("stream", job_id=jid, action="push", text="look here\n",
               stream=ids[1], name="notes")
    sid = r["stream"]["id"]
    j.call("stream", job_id=jid, action="push", text="more\n", stream=sid)
    assert r["stream"]["parent"] == ids[1]
    assert j.call("next", job_id=jid, stream=sid)["text"] == "look here\nmore\n"
    with pytest.raises(PlutoError, match="not_owner"):
        other.call("stream", job_id=jid, action="halt", stream=sid)
    j.call("stream", job_id=jid, action="pause", stream=sid)
    assert j.call("next", job_id=jid, stream=sid)["paused"]
    with pytest.raises(PlutoError, match="paused"):
        j.call("stream", job_id=jid, action="push", text="x", stream=sid)
    j.call("stream", job_id=jid, action="resume", stream=sid)
    j.call("stream", job_id=jid, action="halt", stream=sid)
    with pytest.raises(PlutoError, match="halted"):
        j.call("stream", job_id=jid, action="resume", stream=sid)
    f = j.call("stream", job_id=jid, action="followup", stream=ids[0],
               text="check A", assignee="ns2")["followup"]
    other.call("stream", job_id=jid, action="followup", followup_id=f["id"],
               status="done", text="fine")
    fs = j.call("stream", job_id=jid, action="list")["followups"]
    assert fs[0]["status"] == "done" and len(fs[0]["log"]) == 2


def test_concurrency_limit_and_queue(tmp_path):
    s = Server(tmp_path, max_running=4, max_per_agent=3).start()
    try:
        j, k = s.jobs("q1"), s.jobs("q2")
        mark = tmp_path / "runs"
        ids = [c.start(command=f"echo x >> {mark}; sleep 0.7")["job_id"]
               for _ in range(7) for c in (j, k)]
        peak = 0
        while True:
            st = [x["state"] for x in j.call("list", scope="all", limit=99)["jobs"]]
            peak = max(peak, st.count("running") + st.count("starting"))
            if all(x == "succeeded" for x in st):
                break
        assert peak <= 4 and len(mark.read_text().split()) == len(ids)
        blk = [j.start(command="sleep 60")["job_id"] for _ in range(3)]
        q = j.start(command=f"echo no >> {mark}.no")["job_id"]
        assert j.call("status", job_id=q)["state"] == "queued"
        j.call("control", job_id=q, action="cancel")
        assert done(j, q)["state"] == "cancelled"
        keep = j.start(command="echo kept")["job_id"]
        secret = j.start(command="echo $S", env={"S": "hunter2"})["job_id"]
        assert "hunter2" not in subprocess.run(
            ["grep", "-r", "hunter2", s.home], capture_output=True, text=True).stdout
        s.kill()
        s.start()
        j = s.jobs("q1")
        for b in blk:
            j.call("control", job_id=b, action="cancel")
        assert done(j, keep)["state"] == "succeeded"
        x = done(j, secret)
        assert x["state"] == "failed" and "env" in x["reason"]
        assert not os.path.exists(f"{mark}.no")
    finally:
        s.cleanup()


def test_server_kill9_job_and_cursor_survive(tmp_path):
    s = Server(tmp_path).start()
    try:
        j = s.jobs("dur")
        jid = j.start(command="i=0; while [ $i -lt 80 ]; do i=$((i+1)); "
                              "echo line $i; sleep 0.1; done")["job_id"]
        short = j.start(command="sleep 2; echo bye")["job_id"]
        until(lambda: j.call("status", job_id=jid)["size"] > 50)
        a = j.call("next", job_id=jid, max_bytes=40, encoding="base64")
        s.kill()
        time.sleep(3)
        s.start()
        j = s.jobs("dur")
        assert j.call("status", job_id=jid)["state"] == "running"
        rest = raw(j, jid) if done(j, jid) else b""
        assert base64.b64decode(a["data"]) + rest == \
            "".join(f"line {n}\n" for n in range(1, 81)).encode()
        until(lambda: short in [n["job_id"] for n in j.call("notices")["notices"]])
        # two servers must never share one home
        s2 = Server(tmp_path / "two")
        s2.home = s.home
        s2.start()
        with pytest.raises(PlutoError, match="another_server"):
            s2.jobs("dur")
        s2.kill()
    finally:
        s.cleanup()


def test_runner_death_is_orphaned_then_cancellable(srv):
    j = srv.jobs("orph")
    jid = j.start(command="exec sleep 120")["job_id"]
    until(lambda: j.call("status", job_id=jid)["state"] == "running")
    st = json.load(open(srv.path(jid, "state.json")))
    os.kill(st["runner_pid"], signal.SIGKILL)
    until(lambda: j.call("status", job_id=jid)["state"] == "orphaned", 30)
    j.call("control", job_id=jid, action="cancel")
    assert done(j, jid)["state"] == "lost"
    with pytest.raises(ProcessLookupError):
        os.kill(st["pid"], 0)


def test_review_open_rerun_stats_history(srv):
    j = srv.jobs("rev")
    jid, _ = run(j, "echo one; echo two")
    r = srv.jobs("rev2").call("review", job_id=jid, verdict="approve", note="ok",
                              from_offset=0, to_offset=4)
    assert r["sha256"] == hashlib.sha256(b"one\n").hexdigest()
    with pytest.raises(PlutoError, match="bad_range"):
        j.call("review", job_id=jid, note="x", to_offset=999)
    c = j.call("open", job_id=jid)
    assert c["reviews"][0]["verdict"] == "approve" and c["head"]["text"] == "one\ntwo\n"
    again = j.start(rerun_of=jid)["job_id"]
    assert done(j, again)["rerun_of"] == jid
    assert j.call("stats")["by_state"]["succeeded"] >= 2
    assert any(h["id"] == jid for h in j.call("history")["history"])


def test_export_import_between_hosts(srv, tmp_path):
    other = Server(tmp_path / "other").start()
    try:
        j = srv.jobs("exp")
        jid = j.start(command="seq 1 60000; sleep 4; seq 1 60000; printf end")["job_id"]
        until(lambda: j.call("manifest", job_id=jid)["chunks"] >= 5)
        snap = str(tmp_path / "snap.pjob")
        assert j.export(jid, snap)["snapshot"]
        k = other.jobs("imp")
        r = k.import_(snap)
        assert (r["state"], r["owner"], r["opener"]) == ("snapshot", "imp", "exp")
        k.call("review", job_id=jid, note="local note")
        with pytest.raises(PlutoError, match="finished"):
            k.call("control", job_id=jid, action="cancel")
        os.environ["PLUTO_SMARTSTREAMS_HOME"] = srv.home
        done(j, jid)
        full = str(tmp_path / "full.pjob")
        assert not j.export(jid, full)["snapshot"]
        with pytest.raises(PlutoError, match="exists"):
            j.export(jid, full)
        os.environ["PLUTO_SMARTSTREAMS_HOME"] = other.home
        assert k.import_(full)["result"] == "extended"
        assert k.import_(full)["result"] == "identical"
        assert k.import_(snap)["result"] == "older"
        assert k.call("review", job_id=jid, action="list")["reviews"]
        a = open(srv.path(jid, "output.log"), "rb").read()
        assert open(other.path(jid, "output.log"), "rb").read() == a
        assert k.call("manifest", job_id=jid, verify=True)["verified"]
        assert "pid" not in json.load(open(other.path(jid, "state.json")))
        assert j.import_(snap)["result"] == "older"     # origin is never overwritten
        re_id = k.start(rerun_of=jid, command="echo x")["job_id"]
        assert done(k, re_id)["rerun_of"] == jid
    finally:
        other.cleanup()
        os.environ["PLUTO_SMARTSTREAMS_HOME"] = srv.home


def test_prune_only_finished(srv):
    j = srv.jobs("pru")
    jid, _ = run(j, "true")
    live = j.start(command="sleep 20")["job_id"]
    time.sleep(0.2)
    ids = j.call("prune", older_than_ms=0, dry_run=True)["jobs"]
    assert jid in ids and live not in ids
    assert j.call("status", job_id=jid)
    j.call("control", job_id=live, action="cancel")


def post(srv, path, key, body=b"{}", method="POST"):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=body,
                                 method=method, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


def test_security(srv):
    j = srv.jobs("sec")
    key = open(os.path.join(srv.home, "key")).read().strip()
    for bad in ("", "x", key[:-1]):
        code, h = post(srv, "/jobs/list", bad)
        assert code == 401 and "Access-Control-Allow-Origin" not in h
    code, h = post(srv, "/jobs/list", key, b'{"agent_id":"a"}')
    assert code == 200 and "Access-Control-Allow-Origin" not in h
    assert post(srv, "/jobs/start", key, None, "OPTIONS")[0] == 405
    for jid in ("../../etc", "J-20260101-zz", "J-20260101-0123456789abcdef\n"):
        with pytest.raises(PlutoError, match="bad_job_id"):
            j.call("status", job_id=jid)
    with pytest.raises(PlutoError, match="unknown_stream"):
        x, _ = run(j, "true")
        j.call("window", job_id=x, stream="../../key")
    run(j, "echo s3cr3tcmd")
    ev = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/events?limit=10000").read()
    assert b"job_finished" in ev and b"s3cr3tcmd" not in ev
    mode = os.stat(os.path.join(srv.home, "key")).st_mode
    assert stat.S_IMODE(mode) == 0o600
    os.chmod(os.path.join(srv.home, "key"), 0o644)
    try:
        assert post(srv, "/jobs/list", key)[0] == 503
    finally:
        os.chmod(os.path.join(srv.home, "key"), 0o600)
    assert post(srv, "/jobs/list", key, b'{"agent_id":5}')[0] == 400
    assert post(srv, "/jobs/window", key, b'{"agent_id":"a","offset":-1}')[0] == 400
    assert post(srv, "/jobs/list", key, b"[1]")[0] == 400


def test_mcp_stdio_session(srv):
    """The real MCP server over stdio, as an agent would use it."""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = dict(os.environ, PLUTO_SMARTSTREAMS_HOME=srv.home)
    params = StdioServerParameters(command=PY, env=env, args=[
        os.path.join(ROOT, "src_py/agent_smart_streams/smart_streams.py"),
        "--agent-id", "mcp1", "--host", "127.0.0.1", "--http-port", str(srv.port)])

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                assert not res.isError, res
                return json.loads(res.content[0].text)
            job = await call("job_start", command="seq 1 100; echo BAD")
            jid = job["job_id"]
            w8 = await call("job_wait", job_id=jid)
            g = await call("job_grep", job_id=jid, pattern="BAD", before=1)
            n = await call("job_next_chunk", job_id=jid)
            # job_wait already told the agent, so no notice repeats it ...
            dup = [x for r_ in (w8, g, n) for x in r_.get("_job_notices", [])]
            two = await call("job_start", command="true")
            await asyncio.sleep(2)
            many = await call("job_grep", job_ids=[jid, "J-x"], pattern="BAD")
            # ... but a finish nobody reported does arrive.
            seen = many.get("_job_notices", []) + (await call(
                "job_grep", job_id=jid, pattern="x")).get("_job_notices", [])
            seen = [x for x in seen if x["job_id"] == two["job_id"]]
            err = await call("job_status", job_id="nope")
            agents = srv.jobs().http.list_agents()
            return tools, g, n, seen, err, agents, dup, many, jid
    tools, g, n, seen, err, agents, dup, many, jid = asyncio.run(go())
    assert len(tools) == 15 and g["lines"].splitlines() == ["100-100", "101@292:BAD"]
    assert n["text"].endswith("BAD\n") and "bad_job_id" in err["error"]
    assert "mcp1" in agents
    assert not dup and any(x["event"] == "job_finished" for x in seen)
    assert many[jid]["matches"] == 1 and "bad_job_id" in many["J-x"]["error"]


def test_cli_reports(srv, tmp_path):
    jid, _ = run(srv.jobs("cli"), "echo cli", name="evil\x1b[2J")

    def cli(*a, **kw):
        return subprocess.run(
            [PY, os.path.join(ROOT, "src_py/agent_smart_streams/smart_streams.py"),
             "--host", "127.0.0.1", "--http-port", str(srv.port), *a],
            capture_output=True, env=dict(os.environ, PLUTO_SMARTSTREAMS_HOME=srv.home), **kw)
    out = cli("--manage").stdout.decode()
    assert jid in out and "\x1b" not in out
    assert jid in cli("--history", "--agent", "cli").stdout.decode()
    assert json.loads(cli("--stats", "--json").stdout)["jobs"] > 0
    piped = cli("--export", jid).stdout
    assert tarfile.open(fileobj=io.BytesIO(piped)).getnames()[0] == "package.json"
    assert b"identical" in cli("--import", "-", input=piped).stdout
    r = cli("--prune", "--older-than", "999d")
    assert b"Nothing" in r.stdout
    assert re.match(rb"\[smartstreams\]", cli("--export", "nope").stderr.splitlines()[-1])


def test_only_the_claude_host_is_ever_woken(monkeypatch):
    from agent_smart_streams.server import SmartStreamsServer
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/some.sock")
    kw = {"agent_id": "a", "host": "127.0.0.1", "http_port": 1}
    assert SmartStreamsServer(framework="claude", **kw).push._enabled
    assert not SmartStreamsServer(framework="copilot", **kw).push._enabled
    assert not SmartStreamsServer(framework="claude", attach=True, **kw).push._enabled


@pytest.mark.parametrize("framework,flag,prefix", [
    ("claude", "--mcp-config", ""), ("copilot", "--additional-mcp-config", "@")])
def test_launcher_writes_private_config(srv, tmp_path, framework, flag, prefix):
    venv = os.path.dirname(os.path.dirname(PY))
    marker = os.path.join(venv, ".requirements-installed")
    if sys.prefix == sys.base_prefix:
        pytest.skip("launcher needs a venv")
    if not os.path.exists(marker):
        open(marker, "w").close()
    r = subprocess.run(
        [os.path.join(ROOT, "PlutoSmartStreams.sh"), "--agent-id", "l/1",
         "--framework", framework, "--with-pluto", "--no-launch",
         "--host", "127.0.0.1", "--http-port", str(srv.port)],
        capture_output=True, text=True,
        env=dict(os.environ, PLUTO_VENV=venv, PLUTO_STATE_DIR=str(tmp_path),
                 PLUTO_SMARTSTREAMS_HOME=srv.home))
    assert r.returncode == 0, r.stderr
    cli, got, path = r.stdout.split()
    assert (cli, got) == (framework, flag) and path.startswith(prefix)
    path = path[len(prefix):]
    cfg = json.load(open(path))["mcpServers"]
    assert set(cfg) == {"smartstreams", "pluto"}
    assert "--attach" in cfg["smartstreams"]["args"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ── Regression tests for the v0.6.0 review ──────────────────────────────────

def test_broken_package_does_not_break_the_others(srv):
    j = srv.jobs("brk")
    jid, _ = run(j, "echo x")
    for text in ('{"brk":{"pos":"abc"}}', "[]", "", "nonsense"):
        with open(srv.path(jid, "cursors.json"), "w") as f:
            f.write(text)
        assert j.call("list")["total"] == 1
        assert j.call("next", job_id=jid)["text"] == "x\n"


def test_foreign_queued_package_never_runs(tmp_path):
    s = Server(tmp_path).start()
    try:
        j = s.jobs("f")
        jid, _ = run(j, "true")
        s.kill()
        path = s.path("J-20260101-00000000000000aa", "package.json")
        os.makedirs(os.path.join(os.path.dirname(path), "control"))
        pkg = json.load(open(s.path(jid, "package.json")))
        pkg.update(id="J-20260101-00000000000000aa", sched="queued", done=False,
                   command=f"touch {tmp_path}/PWNED", env_keys=[],
                   origin={"host_id": "elsewhere"})
        json.dump(pkg, open(path, "w"))
        s.start()
        j = s.jobs("f")
        time.sleep(2)
        assert j.call("status", job_id=pkg["id"])["state"] == "lost"
        assert not os.path.exists(tmp_path / "PWNED")
    finally:
        s.cleanup()


def test_disk_errors_hit_one_job_not_the_server(srv):
    j = srv.jobs("disk")
    a = j.start(command="sleep 1")["job_id"]
    until(lambda: j.call("status", job_id=a)["state"] == "running")
    d = os.path.dirname(srv.path(a, "x"))
    blocker = srv.path(a, "package.json.tmp")
    os.mkdir(blocker)                       # every write of package.json fails
    time.sleep(8)
    b, s = run(j, "echo fine")
    assert s["state"] == "succeeded"
    assert j.call("status", job_id=a)["state"] == "succeeded"
    os.rmdir(blocker)
    until(lambda: a in [n["job_id"] for n in j.call("notices")["notices"]])
    assert os.path.isdir(d)


def test_removed_directory_frees_the_slot(srv):
    import shutil
    j = srv.jobs("gone")
    a = j.start(command="sleep 300")["job_id"]
    until(lambda: j.call("status", job_id=a)["state"] == "running")
    pg = json.load(open(srv.path(a, "state.json")))["pgid"]
    shutil.rmtree(os.path.dirname(srv.path(a, "x")))
    until(lambda: not [x for x in j.call("list")["jobs"] if x["job_id"] == a])
    os.killpg(pg, signal.SIGKILL)


def test_slow_split_does_not_block_other_calls(srv):
    j, o = srv.jobs("slow"), srv.jobs("slow2")
    jid, _ = run(j, f"{PY} -c \"[print('a'*40000+'b') for _ in range(400)]\"")
    t = threading.Thread(target=lambda: pytest.raises(PlutoError, j.call, "stream",
                         job_id=jid, action="split", pattern="(a+)+$"))
    t.start()
    worst = 0
    while t.is_alive():
        t0 = time.time()
        o.call("status", job_id=jid)
        worst = max(worst, time.time() - t0)
    assert worst < 1


def test_concurrent_exports_and_next(srv):
    from concurrent.futures import ThreadPoolExecutor
    j = srv.jobs("conc")
    jid, _ = run(j, "seq 1 200000")
    with ThreadPoolExecutor(6) as ex:
        paths = list(ex.map(lambda _: j.export(jid)["path"], range(3)))
        assert len(set(paths)) == 3
        parts = list(ex.map(lambda _: j.call("next", job_id=jid, max_bytes=5000),
                            range(40)))
    spans = sorted((p["start"], p["end"]) for p in parts)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_hostile_imports_leave_the_server_alone(srv, tmp_path):
    from tests.test_job_package import rewrite, streams
    j = srv.jobs("host")
    jid, _ = run(j, "echo x")
    good = str(tmp_path / "g.pjob")
    j.export(jid, good)
    secret = tmp_path / "secret.log"
    secret.write_text("TOPSECRET")
    changes = [
        lambda f: f.__setitem__("streams.json", streams(id=str(secret)[:-4])),
        lambda f: f.__setitem__("streams.json", b"{}"),
        lambda f: f.__setitem__("cursors.json", b'{"host":{"pos":"abc"}}'),
        lambda f: f.__setitem__("state.json", f["state.json"].replace(
            b'"paused_ms": 0', b'"paused_ms": NaN')),
        lambda f: f.__setitem__("package.json", f["package.json"].replace(
            b'"id"', b'"id": "../../x", "id"', 1))]
    for _ in range(3):
        for n, ch in enumerate(changes):
            bad = str(rewrite(good, tmp_path / f"b{n}.pjob", ch))
            with pytest.raises(PlutoError):
                j.import_(bad)
    with pytest.raises(PlutoError):
        j.call("window", job_id=jid, stream=str(secret)[:-4])
    log = open(os.path.join(srv.base, "server.log"), errors="replace").read()
    assert "pluto_job_mgr terminating" not in log
    assert j.call("status", job_id=jid)["state"] == "succeeded"


def test_missing_python_does_not_stop_pluto(tmp_path):
    s = Server(tmp_path, python="/no/such/python").start()
    try:
        with pytest.raises(PlutoError, match="python3"):
            s.jobs("np")
        assert s.proc.poll() is None
    finally:
        s.kill()


def test_helper_failure_never_declares_a_live_job_lost(tmp_path):
    py = tmp_path / "py"
    py.write_text(f'#!/bin/sh\n[ -e {tmp_path}/broken ] && exit 1\nexec {PY} "$@"\n')
    py.chmod(0o755)
    s = Server(tmp_path / "s", python=str(py)).start()
    try:
        j = s.jobs("hf")
        a = j.start(command="sleep 60")["job_id"]
        until(lambda: j.call("status", job_id=a)["state"] == "running")
        (tmp_path / "broken").touch()
        st = json.load(open(s.path(a, "state.json")))
        os.kill(st["runner_pid"], signal.SIGSTOP)      # heartbeat goes stale
        time.sleep(14)
        assert j.call("status", job_id=a)["state"] == "running"
        os.kill(st["runner_pid"], signal.SIGCONT)
        j.call("control", job_id=a, action="cancel")
        assert done(j, a)["state"] == "cancelled"
    finally:
        (tmp_path / "broken").unlink(missing_ok=True)
        s.cleanup()


def test_home_from_config_file(tmp_path, monkeypatch):
    from pluto_sdk.jobs import smartstreams_home
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"smartstreams": {"home": str(tmp_path / "h")}}))
    monkeypatch.delenv("PLUTO_SMARTSTREAMS_HOME", raising=False)
    monkeypatch.setenv("PLUTO_CONFIG", str(cfg))
    assert smartstreams_home() == str(tmp_path / "h")


def test_short_jobs_finish_promptly(srv):
    j = srv.jobs("quick")
    for _ in range(4):
        t0 = time.time()
        run(j, "true")
        assert time.time() - t0 < 2.5
