"""pss and the reduced tool set, against a live server."""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_smart_streams_live import PY, srv  # noqa: E402,F401

PSS = os.path.join(ROOT, "bin", "pss")


def pss(srv, *args, agent="cli1", cwd=None, code=0):  # noqa: F811
    env = dict(os.environ, PSS_AGENT=agent, PSS_HOST="127.0.0.1", PSS_PORT=str(srv.port),
               PLUTO_SMARTSTREAMS_HOME=srv.home,
               PATH=os.path.dirname(PSS) + os.pathsep + os.environ["PATH"])
    r = subprocess.run([PSS, *args], env=env, cwd=cwd, capture_output=True, text=True,
                       timeout=90)
    assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
    return r.stdout


def test_whole_round_in_the_shell(srv, tmp_path):  # noqa: F811
    g = pss(srv, "run", "-q", "-e", "n=1-3", "-e", "k=a,b",
            "echo step={n} v=$(( {n} * 10 )) k={k}; exit $(( {n} == 3 ))", cwd=tmp_path).strip()
    out = pss(srv, "wait", g, code=1)
    assert out.count("succeeded") == 4 and out.count("failed exit=1") == 2
    assert "step=2 v=20 k=a" in out
    assert "step" in pss(srv, "analyze", g) and "v" in pss(srv, "analyze", g, "-a", "sum:v")
    assert "3" in pss(srv, "analyze", g, "-w", "v > 25", "--rows")
    assert pss(srv, "grep", "step=2", g).count("step=2") == 2
    pss(srv, "grep", "nothing here", g, code=1)
    assert len(pss(srv, "log", g).split()) == 6
    assert pss(srv, "cat", g).count("step=") == 6
    assert pss(srv, "ps", g).count("\n") == 6
    assert pss(srv, "tail", "-n", "1", g).count("==>") == 6
    assert "6 given to other" in pss(srv, "give", "other", g)
    assert pss(srv, "ps", "--owner", "other").count("other") == 6


def test_waits_tell_once_across_calls_and_time_out(srv):  # noqa: F811
    g = pss(srv, "run", "-q", "-e", "n=1-2", "echo EV one {n}; sleep 2; echo EV two; sleep 60").strip()
    seen = []
    while len(seen) < 4:
        seen += [x for x in pss(srv, "wait", g, "--match", "^EV", "-t", "20").splitlines()
                 if "EV" in x]
    assert len(seen) == 4
    assert "timed out" in pss(srv, "wait", g, "--match", "^EV", "-t", "2", code=124)
    assert "ok" in pss(srv, "stop", g)
    pss(srv, "wait", g, code=1)
    assert "finished" in pss(srv, "stop", g)


def test_watch_and_jobs_that_start_jobs(srv):  # noqa: F811
    j = pss(srv, "run", "-q", "for i in 1 2 3 4; do echo loss=$i; sleep 1; done; sleep 60").strip()
    pss(srv, "watch", j, "-w", "loss >= 3", "--then", "cancel", "--once")
    pss(srv, "wait", j, "-t", "30", code=1)
    # a driver script as a job, picking the next step from a result
    drv = pss(srv, "run", "-q", "a=$(pss run -q 'echo cost=7'); pss wait $a >/dev/null; "
              "c=$(pss cat $a | cut -d= -f2); pss run -q -w 20 \"echo next $((c+1))\"").strip()
    out = pss(srv, "wait", drv, "-t", "40")
    assert "next 8" in out


def test_errors_are_readable(srv):  # noqa: F811
    for args in (["wait", "ffffff"], ["call", "status", "{"], ["run", "-e", "n=9-1", "{n}"],
                 ["watch", "gffffff", "--match", "x"]):
        pss(srv, *args, code=1)
    assert "pss run" in pss(srv, "help")
    pss(srv, "stop", code=2)


@pytest.mark.parametrize("tools,count", [("core", 10), ("none", 0)])
def test_toolsets(srv, tools, count):  # noqa: F811
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command=PY, env=dict(os.environ, PLUTO_SMARTSTREAMS_HOME=srv.home), args=[
            os.path.join(ROOT, "src_py/agent_smart_streams/smart_streams.py"), "--tools", tools,
            "--agent-id", "ts-" + tools, "--host", "127.0.0.1", "--http-port", str(srv.port)])

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            init = await s.initialize()
            listed = (await s.list_tools()).tools
            assert len(listed) == count
            if not count:
                assert "pss" in init.instructions
                return

            async def call(tool, **kw):
                return json.loads((await s.call_tool(tool, kw)).content[0].text)
            j = await call("job_start", command="read x; echo got $x", stdin=True)
            assert "stdin" in (await call("job_more", op="input"))["use"]
            assert "op must be" in (await call("job_more", op="nope"))["error"]
            assert "error" in await call("job_more", op="input", args={"nope": 1})
            sent = await call("job_more", op="input", args={"job_id": j["job_id"], "text": "hi\n"})
            w8 = await call("job_wait", job_id=j["job_id"], timeout_s=15)
            assert "error" not in sent, sent
            assert w8["jobs"], (sent, w8, await call("job_window", job_id=j["job_id"]))
            assert w8["jobs"][0]["last"] == "got hi"
            c = await call("job_control", job_id=j["job_id"], action="kill")
            assert c["finished"] == [j["job_id"]]
            r_ = await call("job_list", job_ids=[j["job_id"]])
            assert r_["total"] == 1
            b = await call("job_start", command="seq 1 30", each={"n": "1-3"})
            assert "error" in b
            b = await call("job_start", command="seq {n} 40", each={"n": "1-3"}, wait_s=20)
            g = await call("job_grep", job_id=b["group"], pattern=".", max_matches=25)
            assert all(v["matches"] == 25 for v in g.values())      # per job, never shared
            assert "error" in await call("job_grep", job_id=b["group"], pattern=".",
                                         max_matches=500)
            assert "error" in await call("job_wait", job_id=b["group"], from_offset=3)
    t0 = time.time()
    asyncio.run(go())
    assert time.time() - t0 < 60


def test_replacement_after_a_crash_owns_its_jobs(srv):  # noqa: F811
    """kill -9 of the adapter, then a new one under the same id."""
    args = [PY, os.path.join(ROOT, "src_py/agent_smart_streams/smart_streams.py"),
            "--agent-id", "phoenix", "--host", "127.0.0.1", "--http-port", str(srv.port)]
    env = dict(os.environ, PLUTO_SMARTSTREAMS_HOME=srv.home)

    class Adapter:
        def __init__(self):
            self.p = subprocess.Popen(args, env=env, text=True, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.n = 0
            self.rpc("initialize", protocolVersion="2024-11-05", capabilities={},
                     clientInfo={"name": "t", "version": "1"})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def send(self, o):
            self.p.stdin.write(json.dumps(o) + "\n")
            self.p.stdin.flush()

        def rpc(self, method, **params):
            self.n += 1
            self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
            while True:
                r = json.loads(self.p.stdout.readline())
                if r.get("id") == self.n:
                    return r["result"]

        def call(self, tool, **kw):
            r = self.rpc("tools/call", name=tool, arguments=kw)
            return json.loads(r["content"][0]["text"])

    a = Adapter()
    job = a.call("job_start", command="sleep 120", name="ph")["job_id"]
    second = Adapter()                          # a live holder keeps the id
    assert "error" in json.dumps(second.call("job_control", job_id=job, action="pause"))
    second.p.kill()
    second.p.wait()
    a.p.kill()
    a.p.wait()
    b = Adapter()
    assert any(job in row for row in b.call("job_list")["jobs"])
    assert b.call("job_control", job_id=job, action="stop")["ok"] == [job]
    b.p.kill()
    b.p.wait()
