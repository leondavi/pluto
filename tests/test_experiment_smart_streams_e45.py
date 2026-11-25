"""Offline tests of experiments 4 and 5, the native_sub arm and the call classes."""

import importlib
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_streams")
sys.path.insert(0, EXP)
try:
    e4 = importlib.import_module("exp4_crash")
    e5 = importlib.import_module("exp5_probe")
    baselines = importlib.import_module("exp5_baselines")
    ss_build = importlib.import_module("ss_build")
    ss_chaos = importlib.import_module("ss_chaos")
    ss_classify = importlib.import_module("ss_classify")
    ss_metrics = importlib.import_module("ss_metrics")
    ss_procs = importlib.import_module("ss_procs")
    ss_prompts = importlib.import_module("ss_prompts")
    ss_run = importlib.import_module("run")
    summarize = importlib.import_module("summarize")
    verify = importlib.import_module("verify_e45")
    wl_probe = importlib.import_module("wl_probe")
    wl_sweep = importlib.import_module("wl_sweep")
finally:
    sys.path.remove(EXP)

ARMS = ss_run.CONDITIONS
FAST4 = {"unit_s": 0.02, "units": (60, 70)}


def put_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


# ── experiment 4: plan, program, grader ──────────────────────────────────────

def test_e4_plan_is_seeded_and_equal_per_agent():
    assert e4.plan(3) == e4.plan(3) != e4.plan(4)
    for seed in range(3):
        per = {}
        for j, p in e4.plan(seed).items():
            kinds = [f["kind"] for f in p["faults"]]
            per.setdefault(e4.owner({"job": j}), []).append((p["resumable"], *kinds))
            assert all(f["unit"] < p["units"] - 10 for f in p["faults"])
        assert len({tuple(sorted(x)) for x in per.values()}) == 1 and len(per) == 4
        ev = e4.chaos(seed)
        assert ev == e4.chaos(seed) == sorted(ev)
        assert len({w for _, k, w in ev if k == "agent"}) == 3
        assert [k for _, k, _ in ev].count("server") == 1 and ev[-1][0] < 300


def sweep(ws, j, *args):
    return subprocess.run([ws + "/bin/sweep", "--job", str(j), *args], capture_output=True,
                          text=True, cwd=ws, timeout=30)


def facts(tmp_path, text):
    (tmp_path / "x.log").write_text(text)
    return verify.read_log(str(tmp_path / "x.log"))


def test_e4_program_faults_fire_once_and_resume_is_exact(tmp_path):
    ws = str(tmp_path / "ws")
    e4.build(ws, 5, **FAST4)
    tr = e4.truth(5, **FAST4)
    seen = set()
    for j, p in tr.items():
        kinds = [f["kind"] for f in p["faults"]]
        if kinds not in (["CRASH"], ["CORRUPT"], []):
            continue
        key = (p["resumable"], *kinds)
        if key in seen:
            continue
        seen.add(key)
        r = sweep(ws, j)
        lg = facts(tmp_path, r.stdout)
        if not kinds:
            assert r.returncode == 0 and lg["digest"] == p["want"]["digest"]
            best = max(lg["sums"], key=lambda u: (lg["sums"][u], -u))
            assert (best, lg["sums"][best]) == (p["want"]["peak_unit"], p["want"]["peak_inv"])
            continue
        f, want = p["faults"][0], p["want"]["failures"][0]
        if kinds == ["CRASH"]:
            assert (r.returncode, lg["last"]) == (want["exit_code"], want["unit"])
        else:
            assert r.returncode == 0 and lg["bad"] == want["unit"]
            assert lg["digest"] != p["want"]["digest"]
        ck = verify.best_checkpoint(ws, int(j), [lg])
        assert (int(ck[-7:-3]) if ck else 0) == e4.good_checkpoint(p, f)
        if f["torn"] and p["resumable"] and kinds == ["CRASH"]:
            assert sweep(ws, j, "--resume", lg["ck"][-1]).returncode == 65
        r2 = sweep(ws, j, *(["--resume", ck] if ck else []))
        lg2 = facts(tmp_path, r2.stdout)
        assert r2.returncode == 0 and lg2["digest"] == p["want"]["digest"] and not lg2["bad"]
        sums = {**lg2["sums"], **lg["sums"]}
        assert max(sums.values()) == p["want"]["peak_inv"]
    assert len(seen) >= 4
    assert len(ss_build.launches(ws)) == len(
        [r for r in ss_build.launches(ws) if "from_unit" in r])     # refusals leave no record


def rec(j, t0, t_last, units, done=False, from_unit=0, faults=(), **kw):
    return dict({"job": int(j), "label": f"job{j}", "t0": t0, "t_last": t_last, "units": units,
                 "done": done, "from_unit": from_unit, "faults": [list(f) for f in faults],
                 "dirty0": False, "dirty": False, "lag": 0, "cpu": 0}, **kw)


def ideal(p):
    """Launches of a perfect team for one job (starts at 100)."""
    out, t, u = [], 100.0, 0
    for i, f in enumerate(p["faults"]):
        t2 = t + (f["unit"] - u)
        out.append(rec(p["job"], t, t2, f["unit"] - u, from_unit=u, faults=[(i, t2)],
                       ended=True))
        t, u = t2 + 3, e4.good_checkpoint(p, f)
    out.append(rec(p["job"], t, t + p["units"] - u, p["units"] - u, True, u))
    return out


def write_runs(ws, recs):
    d = os.path.join(ws, ".runs")
    for f in os.listdir(d) if os.path.isdir(d) else []:
        os.remove(os.path.join(d, f))
    for i, r in enumerate(recs):
        put_json(os.path.join(d, f"{i}.json"), r)


def test_e4_grader(tmp_path):
    tr, ws = e4.truth(6), str(tmp_path)
    ids = list(tr)
    runs = {j: ideal(p) for j, p in tr.items()}
    put_json(ws + "/answers/final.json", {"jobs": {j: p["want"] for j, p in tr.items()}})
    meta = {"run_end": 2000.0, "crashes": []}

    def score(**change):
        write_runs(ws, [r for j in ids for r in {**runs, **change}[j]])
        return e4.grade(ws, tr, meta)
    g = score()
    assert g["score"] == 1.0 and g["final_valid"]
    assert g["process"]["recomputed_program_s"] == g["process"]["unneeded_relaunches"] == 0
    clean = next(j for j, p in tr.items() if not p["faults"])
    n = tr[clean]["units"]
    two = runs[clean] + [rec(clean, 110, 110 + n, n, True)]                # duplicate launch
    g = score(**{clean: two})
    assert g["process"]["unneeded_relaunches"] == 1 and g["process"]["duplicate_s"] == n - 10
    assert 1 - g["score"] == pytest.approx((.10 + .20 * (1 - e4.SLACK)) / 40, abs=2e-3)
    late = runs[clean] + [rec(clean, 900, 900 + n, n, True)]               # after it finished
    assert score(**{clean: late})["process"]["unneeded_relaunches"] == 1
    left = runs[clean] + [rec(clean, 101, 1999.5, 5, faults=[])]
    assert score(**{clean: left})["process"]["orphans"] == 1
    left[-1].update(ended=True)                           # stopped by its agent in time
    assert score(**{clean: left})["process"]["orphans"] == 0
    left[-1].update(t_last=2001.0)                        # stopped by the cleanup
    assert score(**{clean: left})["process"]["orphans"] == 1
    j = next(j for j, p in tr.items() if [f["kind"] for f in p["faults"]] == ["CRASH"]
             and p["resumable"])
    f, first = tr[j]["faults"][0], runs[j][0]
    slow = [first, dict(runs[j][1], t0=first["t_last"] + 130, t_last=first["t_last"] + 400)]
    g = score(**{j: slow})
    assert g["process"]["faults_recovered_in_time"] == g["process"]["faults_planned"] - 1
    restart = [first, rec(j, first["t_last"] + 5, 900, tr[j]["units"], True)]
    g = score(**{j: restart})
    assert g["process"]["recomputed_program_s"] == e4.good_checkpoint(tr[j], f)
    assert g["parts"]["economy"] < 1 and g["parts"]["timely"] == 1
    c = next(j for j, p in tr.items() if [f["kind"] for f in p["faults"]] == ["CORRUPT"])
    bad = [runs[c][0], dict(runs[c][1], dirty0=True, dirty=True)]          # bad checkpoint
    g = score(**{c: bad})
    assert g["parts"]["timely"] < 1 and g["process"]["jobs_done"] == 39
    h = next(j for j, p in tr.items() if [f["kind"] for f in p["faults"]] == ["HANG"])
    hung = [dict(runs[h][0], t_last=1999.0, ended=False), runs[h][1]]      # never terminated
    g = score(**{h: hung})
    assert g["process"]["orphans"] == 1 and g["parts"]["timely"] < 1
    # a healthy run that died with its agent is told apart from one that was killed
    died = [rec(clean, 100, 150, 50), rec(clean, 160, 160 + n, n, True)]
    meta["crashes"] = [(150.5, "w1")]
    g = score(**{clean: died})
    assert g["process"]["died_in_agent_crash"] == 1 and g["process"]["healthy_kills"] == 0
    meta["crashes"] = []
    assert score(**{clean: died})["process"]["healthy_kills"] == 1
    write_runs(ws, [])
    g = e4.grade(ws, tr, meta)                                             # did nothing
    assert g["score"] == 0
    (tmp_path / "answers" / "final.json").write_text('{"jobs": [1]}')
    assert e4.grade(ws, tr, meta)["score"] == 0


def test_e4_history_score():
    w = [{"unit": 5, "kind": "CRASH", "exit_code": 7}, {"unit": 9, "kind": "HANG", "exit_code": None}]
    assert e4.history_f1(w, w) == 1 and e4.history_f1([], []) == 1
    assert e4.history_f1([], w) == 0 and e4.history_f1(w, []) == 0
    assert e4.history_f1([dict(w[0], exit_code=1)], w) == pytest.approx(1.4 / 3)
    assert e4.history_f1("x", w) == 0 and e4.history_f1([3, w[0]], w[:1]) == 1


def test_no_item_is_worth_more_than_two_percent():
    n4 = sum(len(p["faults"]) for p in e4.plan(1).values())
    for part, items in (("results", 40), ("history", 40), ("economy", 40), ("relaunch", 40),
                        ("timely", n4), ("orphans", 40)):
        assert e4.WEIGHTS[part] / items < 0.008
    assert sum(e4.WEIGHTS.values()) == pytest.approx(1)
    n5 = sum(len(w["plan"]) for p in e5.plan(1).values() for w in p["writers"])
    assert n5 > 90 and e5.W_FIND / n5 < 0.01 and e5.W_ACK / e5.N_ASKS < 0.006


# ── crashes ──────────────────────────────────────────────────────────────────

def test_crash_kills_the_tree_and_spares_detached(tmp_path):
    script = tmp_path / "a.sh"
    script.write_text("cd %s\nsleep 301 & echo $! > child\n"
                      "sh -c 'nohup sleep 302 >/dev/null 2>&1 & echo $! > detached'\n"
                      "sh -c 'sleep 303 & echo $! > orphan_child; wait'\n" % tmp_path)
    tracker = ss_procs.Tracker()
    a = ss_procs.Agent("w1", ["sh", str(script)], "", str(tmp_path), str(tmp_path),
                       dict(os.environ), tracker)
    while not (tmp_path / "orphan_child").exists() or not (tmp_path / "orphan_child").read_text():
        time.sleep(0.05)

    def alive(name):
        tab = ss_procs.ps_table()
        p = int((tmp_path / name).read_text())
        return p in tab and "sleep" in tab[p]["cmd"] and "defunct" not in tab[p]["cmd"]
    try:
        # the detached one left the tree, but shares the group of this plain shell; claude
        # gives every Bash command a group of its own, as the second agent does here
        cmds = ss_procs.crash_tree(a)
        assert a.p.returncode == -9 and len(cmds) >= 4
        time.sleep(0.2)
        assert not alive("child") and not alive("orphan_child") and not alive("detached")
    finally:
        a.err.close()
    py = ("import subprocess,time;subprocess.run(['sh','-c','nohup sleep 304 >/dev/null 2>&1 & "
          "echo $! > detached2'],start_new_session=True);"
          "subprocess.Popen(['sh','-c','echo $$ > task; sleep 305; true'],start_new_session=True);"
          "time.sleep(60)")
    b = ss_procs.Agent("w2", [sys.executable, "-c", py], "", str(tmp_path), str(tmp_path),
                       dict(os.environ), tracker)
    while not (tmp_path / "task").exists() or not (tmp_path / "task").read_text():
        time.sleep(0.05)
    try:
        ss_procs.crash_tree(b)
        time.sleep(0.2)
        assert alive("detached2")                         # detached: survives in any arm
        assert int((tmp_path / "task").read_text()) not in ss_procs.ps_table()
    finally:
        os.kill(int((tmp_path / "detached2").read_text()), 9)
        b.err.close()


def test_chaos_respawns_and_restarts_the_server():
    class Fake:
        def __init__(self):
            self.log = []

        def kill(self):
            self.log.append("kill")

        def start(self):
            self.log.append("start")
    srv, events = Fake(), [(0, "agent", "w1"), (0, "server", None), (99, "agent", "w2")]
    c = ss_chaos.Chaos(events, time.time() - 1, lambda n, k: (n, k), 0.05, srv, 0.01)
    assert c.step({}, {}) == [] and c.log[0]["missed"]       # w1 had finished already
    c.finish()
    assert srv.log == ["kill", "start"] and c.crashes() == []
    assert c.log[-1] == {"kind": "agent", "agent": "w2", "missed": True}
    none = ss_chaos.Chaos(events, time.time() - 1, None, 1, None)
    assert [e[1] for e in none.events] == ["agent", "agent"]  # no server fault without a server


# ── experiment 5 ─────────────────────────────────────────────────────────────

FAST5 = {"records": 2500}


def stream_lines(p):
    return [x if isinstance(x, str) else x[1] for _, x in wl_probe.trace(p)]


@pytest.fixture(scope="module")
def five():
    pl = e5.plan(11, **FAST5)
    return pl, e5.truth(11, **FAST5), {s: stream_lines(p) for s, p in pl.items()}


def answer5(ws, five, fn, acks=True):
    pl, tr, lines = five
    out = {}
    for s, p in pl.items():
        r = fn(p, lines[s])
        codes = []
        if isinstance(r, tuple):
            r, codes = r
        out[str(s)] = r
        want = tr[str(s)]["ask"]
        if acks:
            put_json(f"{ws}/.runs/{s}.json", {
                "t0": 1.0, "stream": s, "acks": {c: 9.0 for c in codes if c == want},
                "wrong": [c for c in codes if c != want]})
    put_json(ws + "/answers/final.json", {"streams": out})
    return e5.grade(ws, tr)


def test_e5_privileged_reader_is_perfect_and_baselines_are_not(tmp_path, five):
    g = answer5(str(tmp_path / "p"), five, baselines.privileged)
    assert g["score"] == 1.0 and len(g["by_kind"]) == 11
    for name, fn in {**baselines.BASELINES, **baselines.BOUNDS}.items():
        assert answer5(str(tmp_path / name), five, fn, False)["score"] < 0.35, name
    for s, p in five[0].items():                          # deterministic, one item per line
        assert stream_lines(p) == five[2][s] and not any("\n" in x for x in five[2][s])
        assert len(five[2][s]) == five[1][str(s)]["lines"] + 0
        break


def test_e5_formats_vary_and_drift():
    kinds, drift = set(), 0
    for p in e5.plan(2, records=60).values():
        notes = {"anomalies": []}
        list(wl_probe.trace(p, notes))
        for a, b in notes["styles"]:
            kinds.add(a["kind"])
            drift += a["kind"] != b["kind"]
            for st in (a, b):
                assert len(set(st["names"].values())) == len(st["names"])
    assert kinds == set(wl_probe.STYLES) and drift == 96
    for g in range(4):                                    # equal load: asks and clean streams
        ps = [p for s, p in e5.plan(2).items() if (s - 1) // 12 == g]
        assert sum(any(x[1] == "ask" for x in p["extra"]) for p in ps) == 7


def test_e5_grader_partial_credit(tmp_path):
    w = [{"line": 100, "kind": "GAP"}, {"line": 130, "kind": "COUNT"}]
    assert sum(c for *_, c in e5.match([(100, "GAP", None), (131, "COUNT", None)], w)) == 2
    assert [c for *_, c in e5.match([(150, "GAP", None)], w)] == [.75]     # far, kind right
    assert [c for *_, c in e5.match([(131, "ORDER", None)], w)] == [.5]
    assert e5.match([(400, "GAP", None)], w) == []
    assert len(e5.match([(100, "GAP", None)] * 5, w)) == 2                 # one claim per truth
    ws = str(tmp_path)
    tr = {"1": {"anomalies": [dict(x, cause=0, lane=3) for x in w], "ask": "AAA-0001"},
          "2": {"anomalies": [{"line": 5, "kind": "GAP", "cause": 1, "lane": 4}], "ask": None}}
    assert e5.grade(ws, tr)["score"] == 0
    put_json(ws + "/answers/stream_1.json", {"anomalies": [
        {"line": "100", "kind": "gap", "cause": "X"}, {"line": 130, "kind": "COUNT", "cause": "x"}, 7]})
    put_json(ws + "/answers/stream_2.json", {"anomalies": [{"line": 5, "kind": "GAP", "cause": "y"}]})
    put_json(ws + "/.runs/a.json", {"t0": 1, "stream": 1, "acks": {"AAA-0001": 89}, "wrong": []})
    put_json(ws + "/.runs/b.json", {"t0": 2, "stream": 1, "acks": {}, "wrong": ["BBB-0001"]})
    g = e5.grade(ws, tr)
    assert g["parts"] == {"anomalies": pytest.approx(6 / 7, abs=1e-4), "answers": 1.0, "causes": 1.0}
    put_json(ws + "/.runs/a.json", {"t0": 1, "stream": 1, "acks": {"AAA-0001": 91}, "wrong": ["C"]})
    assert e5.grade(ws, tr)["parts"]["answers"] == 0
    put_json(ws + "/answers/stream_2.json", {"anomalies": [{"line": 5, "kind": "GAP", "cause": "x"}]})
    assert e5.grade(ws, tr)["parts"]["causes"] == pytest.approx(2 / 4)     # one lumped group


def test_e5_program_prints_the_trace_and_records_answers(tmp_path):
    ws = str(tmp_path)
    kw = {"records": 300, "duration": 3.0}
    e5.build(ws, 4, **kw)
    s, p = next((s, p) for s, p in e5.plan(4, **kw).items()
                if any(x[1] == "ask" for x in p["extra"]))
    code = next(x[3] for x in p["extra"] if x[1] == "ask")
    with open(tmp_path / "log", "w") as out:
        q = subprocess.Popen([ws + "/bin/probe", "--stream", str(s)], stdout=out)
        while q.poll() is None:
            if code in (tmp_path / "log").read_text():
                (tmp_path / "control" / f"stream_{s}.ack").write_text(f"{code} XXX-1234")
            time.sleep(0.05)
    text = (tmp_path / "log").read_bytes().decode()       # \r must not end a line
    assert text.split("\n")[:-1] == stream_lines(p)
    r, = ss_build.launches(ws)
    assert r["done"] and list(r["acks"]) == [code] and r["wrong"] == ["XXX-1234"]


# ── arms, prompts, commands ──────────────────────────────────────────────────

def test_all_arms_share_everything_but_the_tools_paragraph(tmp_path):
    for exp in ss_prompts.EXPS:
        for name, _ in ss_prompts.roster(exp):
            ids = {n: n for n, _ in ss_prompts.roster(exp)}
            ps = {c: ss_prompts.agent_prompt(exp, c, name, "/w", ids, 900) for c in ARMS}
            assert len({p.split("\nTools. ")[0] for p in ps.values()}) == 1
            assert "sub-agent" in ps["native_sub"] and "sub-agent" not in ps["native"]
            assert ps["native_sub"].count("smartstreams") == 0
            n, s = ps["native"].split("\nTools. ")[1], ps["native_sub"].split("\nTools. ")[1]
            assert n.replace(ss_prompts.NO_OTHER, "")[:500] == s[:500]
    for exp, cs in (("4", ("nohup", "pid")), ("1", ("nohup",))):
        p = ss_prompts.agent_prompt(exp, "native", "w1", "/w", ids, 900)
        assert all(c in p for c in cs)
    args = ss_run.parse_args(["--out", str(tmp_path)])
    assert args.conditions == list(ARMS) and len(args.exps) == 5
    cmd = {c: ss_run.claude_command(c, "/cfg", args) for c in ARMS}
    at = {c: dict(zip(cmd[c], cmd[c][1:])) for c in ARMS}
    assert "Agent" in at["native_sub"]["--allowedTools"].split(",")
    assert not {"Agent", "Task"} & set(at["native_sub"]["--disallowedTools"].split(","))
    for c in ("native", "smartstreams"):
        assert {"Agent", "Task"} <= set(at[c]["--disallowedTools"].split(","))
    assert all(at[c]["--max-budget-usd"] == "3.0" and at[c]["--model"] == "sonnet" for c in ARMS)
    assert ss_run.mcp_config("native_sub", "x", args) == {"mcpServers": {}}


def test_subagent_environment(monkeypatch):
    for k in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"):
        monkeypatch.setenv(k, "opus")
    env = ss_run.agent_env("native_sub", "sonnet")
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    for c in ("native", "smartstreams"):                  # nothing of the parent leaks in
        assert not [k for k in ss_run.agent_env(c) if "SUBAGENT" in k]


def test_condition_list_and_order(tmp_path):
    a = ss_run.parse_args(["--out", str(tmp_path), "--conditions", "native,native_sub",
                           "--exp", "4,1"])
    assert a.conditions == ["native", "native_sub"] and a.exps == ["4", "1"]
    for bad in ("x", "native,native"):
        with pytest.raises(SystemExit):
            ss_run.parse_args(["--out", str(tmp_path), "--condition", bad])
    plan = ss_run.schedule(["1", "2", "3", "4", "5"], ARMS, 12, "m")
    assert len(plan) == 60 and all(sorted(c) == sorted(ARMS) for *_, c in plan)
    assert len({tuple(c) for *_, c in plan}) == 6         # every order occurs


# ── metrics: sub-agents, estimates, classes ──────────────────────────────────

def asst(mid, blocks, parent=None, model="m", **usage):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": mid, "model": model, "usage": usage, "content": blocks}}


def use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def res(tid, content, parent=None, **kw):
    return {"type": "user", "parent_tool_use_id": parent, "message": {"content": [
        dict(type="tool_result", tool_use_id=tid, content=content, **kw)]}}


INIT = {"type": "system", "subtype": "init", "model": "m", "mcp_servers": []}
SUB = [INIT,
       asst("a1", [use("T", "Agent", prompt="do not read .runs/ or bin/img")],
            cache_read_input_tokens=100),
       asst("s1", [use("t1", "Bash", command="grep x a.log")], "T", cache_read_input_tokens=40),
       res("t1", "12345", "T"),
       asst("s2", [{"type": "text", "text": "done"}], "T", cache_read_input_tokens=60),
       res("T", "report")]


def result(cache_read, **kw):
    return dict({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.0,
                 "modelUsage": {"m": {"inputTokens": 1, "outputTokens": 500,
                                      "cacheReadInputTokens": cache_read,
                                      "cacheCreationInputTokens": 0}}}, **kw)


def test_subagents_are_counted():
    m = ss_metrics.parse_agent(SUB + [result(200, subagent_stats={"spawned": 1, "max_depth": 1})])
    assert m["turns"] == 3 and m["tool_calls"] == {"Agent": 1, "Bash": 1}
    assert m["tool_result_bytes"] == 11
    assert m["sub"] == {"turns": 2, "tool_calls": 1, "tool_result_bytes": 5, "models": ["m"],
                        "agents": 1, "cache_read_input_tokens": 100}
    assert m["usage_missing"] == 0 and m["cost_est_usd"] == 1.0
    assert ss_metrics.failures(m, {}, "native_sub") == []
    assert ss_metrics.failures(m, {}, "native") == ["subagent_call"]
    # a result that left the sub-agents out is noticed, and the estimate adds them
    m = ss_metrics.parse_agent(SUB + [result(100)])
    assert m["usage_missing"] == 100 and m["cost_est_usd"] > 1.0
    assert "subagent_usage_missing" in ss_metrics.failures(m, {}, "native_sub")
    ev = [dict(e) for e in SUB]
    ev[2] = asst("s1", [], "T", "haiku")
    assert "subagent_model" in ss_metrics.failures(
        ss_metrics.parse_agent(ev + [result(200)]), {}, "native_sub")
    deep = result(200, subagent_stats={"max_depth": 2})
    assert "subagent_nested" in ss_metrics.failures(
        ss_metrics.parse_agent(SUB + [deep]), {}, "native_sub")
    t = ss_metrics.merge({"a": m, "b": m})
    assert t["sub"]["turns"] == 4 and t["cost_est_usd"] == round(2 * m["cost_est_usd"], 6)


def test_killed_session_has_an_estimate():
    ev = [INIT, {"type": "system", "subtype": "thinking_tokens", "estimated_tokens_delta": 1000},
          asst("a", [{"type": "text", "text": "x" * 400}], cache_read_input_tokens=10**6)]
    m = ss_metrics.parse_agent(ev)
    assert m["cost_usd"] is None
    assert m["tokens"]["output_tokens"] == int(.82 * 1000 + .27 * 400 + 174)
    assert m["cost_est_usd"] == pytest.approx(.1 + m["tokens"]["output_tokens"] * 1e-5, abs=1e-6)
    assert ss_metrics.failures(m, {"crashed": True}, "native") == ["crash_injected"]


def test_leak_checks_cover_subagents():
    def hits(ev, denied=()):
        return [h[0] for h in ss_metrics.find_leaks(ev, "/out/runs/aaa/ws", "/out", denied)]
    assert hits(SUB) == []                                # rules repeated in a prompt: no leak
    assert hits([asst("s", [use("x", "Bash", command="cat .runs/a.json")], "T")]) == [
        "program_internals"]
    assert hits([asst("s", [use("x", "Read", file_path="/out/runs/bbb/ws/a")], "T")]) == ["other_run"]
    assert hits([asst("s", [use("x", "Agent", prompt="p", isolation="worktree")])]) == ["isolation"]
    ev = [asst("s", [use("x", "WebFetch", url="u")], "T")]
    assert hits(ev, ["WebFetch"]) == ["forbidden_tool"] and hits(ev) == []
    assert hits(ev + [res("x", "denied", is_error=True)], ["WebFetch"]) == []
    # every session has a working directory of its own
    ev = [asst("m", [use("1", "Bash", command="cd scratch/o")]),
          asst("s", [use("2", "Bash", command="ls ../../board")], "T"),
          asst("m", [use("3", "Bash", command="ls ../../board")])]
    assert hits(ev) == ["parent_dir"]


@pytest.mark.parametrize("name,arg,cls", [
    ("mcp__smartstreams__job_wait", {}, "process"), ("mcp__smartstreams__job_grep", {}, "analysis"),
    ("mcp__smartstreams__job_transfer", {}, "coordination"), ("ToolSearch", {}, "other"),
    ("mcp__smartstreams__job_start", {"commands": ["bin/x 1"]}, "process"),
    ("mcp__smartstreams__job_start", {"command": "grep -c x a.log | sort"}, "analysis"),
    ("Bash", {"command": "cd /a/b/ws; grep -c SPIN logs/*.log"}, "analysis"),
    ("Bash", {"command": "nohup bin/x --shard 3 > a.log 2>&1 & echo $! > a.pid"}, "process"),
    ("Bash", {"command": "cat > board/w1.md <<'E'\ngrep the log, then kill\nE"}, "coordination"),
    ("Bash", {"command": "ls board answers"}, "coordination"),
    ("Bash", {"command": "while true; do grep x a.log && pkill -f y; sleep 1; done"}, "process"),
    ("Bash", {"command": "echo '{}' > answers/shard_3.json"}, "answer"),
    ("Bash", {"command": "cat docs/X.md"}, "other"), ("Read", {"file_path": "/w/docs/X.md"}, "other"),
    ("Write", {"file_path": "/w/answers/a.json", "content": "{}"}, "answer"),
    ("Read", {"file_path": "/w/logs/a.log"}, "analysis"), ("Agent", {"prompt": "x"}, "other")])
def test_call_classes(name, arg, cls):
    assert ss_classify.classify(name, arg) == cls


def test_class_totals_are_complete():
    ev = SUB + [asst("e", [use("u", "Bash", command="ls board"), use("v", "Bash", command="sleep 9")])]
    p = ss_metrics.parse_agent(ev + [result(200)])
    c = ss_classify.by_category(ev, p)
    assert sum(x["calls"] for x in c.values()) == 4 and c["text"]["turns"] == 1
    assert sum(x["turns"] for x in c.values()) == p["turns"] == 4
    assert sum(x["output_tokens"] for x in c.values()) == pytest.approx(500, abs=.1)
    assert c["process"]["turns"] == c["coordination"]["turns"] == .5
    assert ss_classify.merge({"a": c, "b": c})["other"]["calls"] == 2


def test_verdict_rule():
    v = summarize.verdict
    assert v("score", [.1] * 4, 1) is None
    assert v("score", [.06, .06, .06, .06, -.01], 1) == "tie"              # mean .046
    assert v("score", [.08, .08, .08, .08, -.01], 1) == "a>b"
    assert v("score", [.2, .2, .2, -.01, -.01], 1) == "tie"                # signs
    assert v("wall_s", [-20] * 5, 100) == "a<b" and v("wall_s", [-10] * 5, 100) == "tie"
    assert v("orphans", [0, 0, 1, 1, 1], 0) == "tie" and v("orphans", [1] * 5, 0) == "a>b"


# ── runner with a fake claude: crashes, replacements, sub-agents ─────────────

FAKE = """\
#!{py}
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
me = prompt.split("You are agent ")[1].split()[0]
def emit(**e): print(json.dumps(e), flush=True)
emit(type="system", subtype="init", model="fake", mcp_servers=[])
emit(type="assistant", message=dict(id="m1", model="fake", usage=dict(cache_read_input_tokens=10**6),
     content=[dict(type="tool_use", id="T", name="Agent", input=dict(prompt="help"))]))
emit(type="assistant", parent_tool_use_id="T", message=dict(id="m2", model="fake",
     usage=dict(cache_read_input_tokens=5), content=[dict(type="tool_use", id="t", name="Bash",
     input=dict(command="ls board"))]))
emit(type="user", parent_tool_use_id="T", message=dict(content=[dict(type="tool_result",
     tool_use_id="t", content="abc")]))
first = int(me[1:]) * 10 - 9
if "You replace" in prompt:
    open("board/" + me + ".md", "w").write(prompt.split("You replace")[1])
    print(json.dumps(dict(type="user", message={{}})))
    time.sleep(.5)
else:
    n = lambda j: ["bin/sweep", "--job", str(j)]
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen(n(first), **kw)                                        # dies with us
    subprocess.run(["sh", "-c", "nohup bin/sweep --job %d >/dev/null 2>&1 &" % (first + 1)],
                   start_new_session=True, **kw)                            # survives
    time.sleep(4.5)
emit(type="result", subtype="success", is_error=False, total_cost_usd=0.1,
     modelUsage=dict(fake=dict(inputTokens=0, outputTokens=10, cacheReadInputTokens=1000005,
                               cacheCreationInputTokens=0)),
     subagent_stats=dict(spawned=1, max_depth=1))
"""


def test_runner_crashes_and_replaces_agents(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setattr(e4, "chaos", lambda seed: [(2.0, "agent", "w2"), (2.0, "server", None),
                                                   (90.0, "agent", "w3")])
    monkeypatch.setattr(e4, "RESPAWN_S", 0.3)
    monkeypatch.setattr(e4, "ORPHAN_S", 1.5)
    out = tmp_path / "out"
    (out / "runs").mkdir(parents=True)
    args = ss_run.parse_args(["--out", str(out), "--exp", "4", "--condition", "native,native_sub",
                              "--claude", str(fake)])
    rows = ss_run.run_block("4", 1, args.conditions, "m", args)
    assert [r["condition"] for r in rows] == ["native", "native_sub"]
    assert len({r["seed"] for r in rows}) == 1
    for row in rows:
        assert row["sessions"] == ["w1", "w2", "w3", "w4", "w2.r1"]
        assert row["status"]["w2"]["crashed"] and row["status"]["w2.r1"]["rc"] == 0
        assert row["protocol_failures"]["w2"] == ["crash_injected"]
        assert [(x["kind"], x["agent"], bool(x.get("missed"))) for x in row["chaos"]] == [
            ("agent", "w2", False), ("agent", "w3", True)]   # no server fault in native arms
        assert row["team"]["cost_complete"] is False
        assert row["team"]["cost_est_usd"] == pytest.approx(
            .4 + row["per_agent"]["w2"]["cost_est_usd"])
        assert row["per_agent"]["w2"]["cost_est_usd"] > .1
        assert row["team"]["sub"]["agents"] == 5 and row["team"]["sub"]["tool_result_bytes"] == 15
        assert row["categories"]["coordination"]["calls"] == 5
        # sub-agents are denied in native, so a successful call there is a violation
        assert {x["code"] for x in row["leaks"]} == (
            {"forbidden_tool"} if row["condition"] == "native" else set())
        assert row["timed_out"] == dict.fromkeys(["w1", "w2", "w3", "w4"], False)
        ws = os.path.join(args.out, "runs", row["run_id"], "ws")
        note = open(os.path.join(ws, "board", "w2.md")).read()
        assert "an agent w2 that crashed" in note and "remain" in note
        by = {r["job"]: r for r in ss_build.launches(ws)}
        assert by[12]["t_last"] - by[11]["t_last"] > .8     # detached outlived the crash
        assert by[11]["t_last"] - row["chaos"][0]["at"] < 1.5
        assert row["grade"]["detail"]["11"]["died_in_agent_crash"] == 1
        assert row["grade"]["process"]["orphans"] >= 1 and row["leftovers_killed"]
        assert ss_procs.own_processes(None, ws, 0) == {}
    f = {r["condition"]: r["protocol_failures"]["w1"] for r in rows}
    assert f == {"native": ["subagent_call"], "native_sub": []}
    s = summarize.summarize([json.loads(x) for x in open(out / "results.jsonl")])
    d = s["experiments"]["4"]["paired_diff_native_sub_minus_native"]
    assert d["orphans"]["n"] == 1 and "subagent_turns" in summarize.markdown(s)


def test_files_stay_small():
    assert len(open(__file__).readlines()) < 1000
