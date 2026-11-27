"""pst and the reduced tool set, against a live server."""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_smart_tasks_live import PY, srv  # noqa: E402,F401

PST = os.path.join(ROOT, "bin", "pst")


def pst(srv, *args, agent="cli1", cwd=None, code=0):  # noqa: F811
    env = dict(os.environ, PST_AGENT=agent, PST_HOST="127.0.0.1", PST_PORT=str(srv.port),
               PLUTO_SMARTTASKS_HOME=srv.home,
               PATH=os.path.dirname(PST) + os.pathsep + os.environ["PATH"])
    r = subprocess.run([PST, *args], env=env, cwd=cwd, capture_output=True, text=True,
                       timeout=90)
    assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
    return r.stdout


def test_whole_round_in_the_shell(srv, tmp_path):  # noqa: F811
    g = pst(srv, "run", "-q", "-e", "n=1-3", "-e", "k=a,b",
            "echo step={n} v=$(( {n} * 10 )) k={k}; exit $(( {n} == 3 ))", cwd=tmp_path).strip()
    out = pst(srv, "wait", g, code=1)
    assert out.count("2x succeeded") == 2 and out.count("failed exit=1") == 2
    assert "/1:a,2:a 2x" in out.replace("2:a,1:a", "1:a,2:a")
    assert "step" in pst(srv, "analyze", g) and "v" in pst(srv, "analyze", g, "-a", "sum:v")
    assert "3" in pst(srv, "analyze", g, "-w", "v > 25", "--rows")
    assert pst(srv, "grep", "step=2", g).count("step=2") == 2
    pst(srv, "grep", "nothing here", g, code=1)
    assert len(pst(srv, "log", g).split()) == 6
    assert pst(srv, "cat", g).count("step=") == 6
    assert pst(srv, "ps", g).count("\n") == 2 and pst(srv, "ps", "-v", g).count("\n") == 6
    assert pst(srv, "ps", "-v", f"{g}/2:b").startswith(f"{g}/2:b ")
    assert pst(srv, "tail", "-n", "1", g).count("==>") == 6
    assert "6 given to other" in pst(srv, "give", "other", g)
    assert pst(srv, "ps", "-v", "--owner", "other").count("other") == 6


def test_waits_tell_once_across_calls_and_time_out(srv):  # noqa: F811
    g = pst(srv, "run", "-q", "-e", "n=1-2", "echo EV one {n}; sleep 2; echo EV two; sleep 60").strip()
    seen = []
    while len(seen) < 4:
        seen += [x for x in pst(srv, "wait", g, "--match", "^EV", "-t", "20").splitlines()
                 if "EV" in x]
    assert len(seen) == 4
    assert "timed out" in pst(srv, "wait", g, "--match", "^EV", "-t", "2", code=124)
    assert "ok" in pst(srv, "stop", g)
    pst(srv, "wait", g, code=1)
    assert "finished" in pst(srv, "stop", g)


def test_watch_and_jobs_that_start_jobs(srv):  # noqa: F811
    j = pst(srv, "run", "-q", "for i in 1 2 3 4; do echo loss=$i; sleep 1; done; sleep 60").strip()
    pst(srv, "watch", j, "-w", "loss >= 3", "--then", "cancel", "--once")
    pst(srv, "wait", j, "-t", "30", code=1)
    # a driver script as a job, picking the next step from a result
    drv = pst(srv, "run", "-q", "a=$(pst run -q 'echo cost=7'); pst wait $a >/dev/null; "
              "c=$(pst cat $a | cut -d= -f2); pst run -q -w 20 \"echo next $((c+1))\"").strip()
    out = pst(srv, "wait", drv, "-t", "40")
    assert "next 8" in out


SOAK = """echo run=r$1; for i in 1 2 3 4; do echo E$i p=$i; done; echo want tok$1
echo E5 CAL limit=2; echo E6 p=9; [ $1 = 3 ] && sleep 60; echo FINAL $1; exit $(( $1 == 2 ))"""


def test_a_supervised_batch_is_one_command(srv, tmp_path):  # noqa: F811
    (tmp_path / "soak.sh").write_text(SOAK)
    out = pst(srv, "run", "-n", "soak", "-e", "k=1-4", "sh soak.sh {k}",
              "--on", "want {t}", "--append", "control/{k}.ack", "{t}",
              "--idle", "2", "--stop", cwd=tmp_path)
    assert out.startswith("soak/1-4: 4 started; the server holds 2 rules")
    out = pst(srv, "wait", "soak", "-t", "40", cwd=tmp_path, code=1)
    assert "soak/1,4 2x succeeded" in out and "failed exit=1" in out and "soak/3 " in out
    assert "cancelled" in out and len(out) < 400, out
    for k in "1234":
        assert (tmp_path / f"control/{k}.ack").read_text() == f"tok{k}\n"
    # One program per job, with everything filled in.
    out = pst(srv, "each", "soak", "printf '{\"job\": {k}, \"lines\": %d}' $(wc -l < {log})",
              "-o", "out/{k}.json", cwd=tmp_path)
    assert out == "4 ok, 0 failed\n"
    assert json.loads((tmp_path / "out/1.json").read_text()) == {"job": 1, "lines": 9}
    # The same without a program.
    out = pst(srv, "report", "soak", "-x", "E{e:int} p={p:int}", "-x", "E{e:int} CAL limit={limit:int}",
              "-x", "run={run}", "-f", "bad=first:e,p where p > last.limit", "-f", "run=run",
              "-f", "end=_state", "-f", "exit_code=_exit", "-o", "rep/{key}.json", cwd=tmp_path)
    assert out == "4 files written\n"
    assert json.loads((tmp_path / "rep/2.json").read_text()) == {
        "bad": {"e": 6, "p": 9}, "run": "r2", "end": "failed", "exit_code": 1}
    pst(srv, "each", "soak/2", "exit 3", cwd=tmp_path, code=1)
    assert "both write" in pst_err(srv, "each", "soak", "echo", "-o", "x", cwd=tmp_path)


def pst_err(srv, *args, cwd=None, agent="cli1"):  # noqa: F811
    env = dict(os.environ, PST_AGENT=agent, PST_HOST="127.0.0.1", PST_PORT=str(srv.port),
               PLUTO_SMARTTASKS_HOME=srv.home)
    r = subprocess.run([PST, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    return r.stderr


def test_handover_by_name(srv, tmp_path):  # noqa: F811
    pst(srv, "run", "-n", "shift", "-e", "k=1-3", "echo start {k}; sleep 40",
        "--on", "BAD {x}", "--tell", cwd=tmp_path, agent="day")
    assert "3 given to night" in pst(srv, "give", "night", "shift", cwd=tmp_path, agent="day")
    out = pst(srv, "brief", agent="night", cwd=tmp_path)
    assert "shift: 3 jobs of night" in out and "BAD {x}" in out and "active on 3" in out
    assert "start" in out and len(out) < 2000, out
    assert "ok: shift/1 shift/2 shift/3" in pst(srv, "stop", "shift", agent="night", cwd=tmp_path)
    assert "not_owner" in pst_err(srv, "stop", "shift", agent="day", cwd=tmp_path)


def test_a_rule_that_tells_ends_the_wait_once(srv, tmp_path):  # noqa: F811
    pst(srv, "run", "-n", "tell", "echo a; sleep 1; echo BAD 7; sleep 3; echo done",
        "--on", "BAD {x}", cwd=tmp_path)
    t = time.time()
    out = pst(srv, "wait", "tell", "-t", "30", cwd=tmp_path)
    assert "BAD 7" in out and "running" in out and time.time() - t < 3.5
    out = pst(srv, "wait", "tell", "-t", "30", cwd=tmp_path)
    assert "succeeded" in out and "BAD 7" not in out
    # a later rule on a live job, and its listing
    pst(srv, "run", "-n", "late", "sleep 2; echo want z", cwd=tmp_path)
    pst(srv, "watch", "late", "--on", "want {t}", "--append", "late.txt", "{t}", cwd=tmp_path)
    pst(srv, "wait", "late", cwd=tmp_path)
    assert "append" in pst(srv, "watch", "late", cwd=tmp_path)
    assert (tmp_path / "late.txt").read_text() == "z\n"


def test_a_closed_pipe_is_silent(srv):  # noqa: F811
    env = dict(os.environ, PST_AGENT="cli1", PST_HOST="127.0.0.1", PST_PORT=str(srv.port),
               PLUTO_SMARTTASKS_HOME=srv.home)
    pst(srv, "run", "-n", "pipe", "-w", "30", "seq 1 300000")
    r = subprocess.run(f"{PST} cat pipe | head -1", shell=True, env=env, capture_output=True)
    assert r.stdout == b"1\n" and r.stderr == b""


def test_errors_are_readable(srv):  # noqa: F811
    for args in (["wait", "ffffff"], ["call", "status", "{"], ["run", "-e", "n=9-1", "{n}"],
                 ["watch", "gffffff", "--match", "x"], ["run", "-n", "abcdef", "-e", "n=1", "{n}"],
                 ["run", "x", "--on", "a {t}", "--input", "{nope}"], ["each", "nope", "x"]):
        pst(srv, *args, code=1)
    assert "pst run" in pst(srv, "help")
    pst(srv, "stop", code=2)


@pytest.mark.parametrize("tools,count", [("core", 10), ("none", 0)])
def test_toolsets(srv, tools, count):  # noqa: F811
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command=PY, env=dict(os.environ, PLUTO_SMARTTASKS_HOME=srv.home), args=[
            os.path.join(ROOT, "src_py/agent_smart_tasks/smart_tasks.py"), "--tools", tools,
            "--agent-id", "ts-" + tools, "--host", "127.0.0.1", "--http-port", str(srv.port)])

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            init = await s.initialize()
            listed = (await s.list_tools()).tools
            assert len(listed) == count
            if not count:
                assert "pst" in init.instructions
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
    args = [PY, os.path.join(ROOT, "src_py/agent_smart_tasks/smart_tasks.py"),
            "--agent-id", "phoenix", "--host", "127.0.0.1", "--http-port", str(srv.port)]
    env = dict(os.environ, PLUTO_SMARTTASKS_HOME=srv.home)

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


JUDGE = """import sys
for line in sys.stdin:
    if 'GATE' in line:
        open('ctl.%s' % sys.argv[1], 'a').write({what} + '\\n')
    sys.stdout.write(line)
"""


def test_a_wrong_program_is_replaced_without_touching_the_job(srv, tmp_path):  # noqa: F811
    (tmp_path / "j.py").write_text(JUDGE.replace("{what}", "nonsense"))
    pst(srv, "run", "-n", "lane", "-e", "k=1-3", "echo run=$$; echo GATE {k}a; sleep 5; echo GATE {k}b",
        cwd=tmp_path)
    pids = pst(srv, "cat", "lane", cwd=tmp_path)
    assert "judge: 3 started\n" == pst(srv, "follow", "lane", "python3 -u j.py {k}", "-n", "judge",
                                       cwd=tmp_path)
    assert "NameError" in (time.sleep(2), pst(srv, "tail", "judge/1", cwd=tmp_path))[1]
    (tmp_path / "j.py").write_text(JUDGE.replace("{what}", "line.split()[1]"))
    pst(srv, "follow", "lane", "python3 -u j.py {k}", "-n", "judge", cwd=tmp_path)
    pst(srv, "wait", "lane", "judge", "-t", "30", cwd=tmp_path)
    for k in "123":
        assert (tmp_path / f"ctl.{k}").read_text() == f"{k}a\n{k}b\n"       # all of it, once
    out = pst(srv, "cat", "lane/1", cwd=tmp_path)
    assert out.count("run=") == 1 and out.split()[0] in pids
    assert pst(srv, "cat", "judge/1", cwd=tmp_path) == out                  # byte for byte
    assert pst(srv, "cat", "-f", "lane/2", cwd=tmp_path) == pst(srv, "cat", "lane/2", cwd=tmp_path)


def test_following_again_ends_the_running_follower(srv, tmp_path):  # noqa: F811
    pst(srv, "run", "-n", "src", "echo a; sleep 6", cwd=tmp_path)
    pst(srv, "follow", "src", "cat; sleep 60", cwd=tmp_path)
    assert "1 replaced" in pst(srv, "follow", "src", "cat", cwd=tmp_path)
    out = pst(srv, "wait", "src", "follow-src", "-t", "30", cwd=tmp_path)
    assert out.count("succeeded") == 2
    assert "cancelled" in pst(srv, "ps", "-v", cwd=tmp_path)


def test_help_and_errors_are_short(srv):  # noqa: F811
    for op in ("give", "wait", "ps", "each", "stop", "brief", "follow", "tail"):
        out = pst(srv, "help", op)
        assert op in out and len(out) < 420, (op, out)
    assert "no such command" in pst(srv, "help", "frobnicate")
    r = subprocess.run([PST, "run", "x", "--stop"], capture_output=True, text=True)
    assert r.returncode == 2 and "needs a condition" in r.stderr and len(r.stderr) < 700


def test_run_says_what_the_server_will_do(srv, tmp_path):  # noqa: F811
    out = pst(srv, "run", "-n", "say", "-e", "k=1-2", "echo {k}", "--on", "X {t}", "--append", "f{k}",
              "{t}", "--idle", "60", "--stop", cwd=tmp_path)
    assert "2 rules per job: 'X {t}' -> append f{k}; 'idle 60s' -> cancel" in out
