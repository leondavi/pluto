"""Offline tests of the SmartStreams experiment harness (no LLM, no server)."""

import copy
import importlib
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "docs", "research", "experiments", "smart_streams")
sys.path.insert(0, EXP)
try:
    exp1_shards = importlib.import_module("exp1_shards")
    exp2_sssp = importlib.import_module("exp2_sssp")
    exp3_relay = importlib.import_module("exp3_relay")
    ss_build = importlib.import_module("ss_build")
    ss_metrics = importlib.import_module("ss_metrics")
    ss_procs = importlib.import_module("ss_procs")
    ss_prompts = importlib.import_module("ss_prompts")
    ss_run = importlib.import_module("run")
    summarize = importlib.import_module("summarize")
    wl_common = importlib.import_module("wl_common")
    wl_shards = importlib.import_module("wl_shards")
    wl_soak = importlib.import_module("wl_soak")
    wl_sssp = importlib.import_module("wl_sssp")
finally:
    sys.path.remove(EXP)

OPS = 3000


def put_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


# ── experiment 1 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_e1_checker_recovers_planted_truth(seed):
    shards, cross = exp1_shards.plan(seed, ops=OPS, duration=0)
    bad = []
    for s, p in shards.items():
        lines = [x for _, x in wl_shards.trace(p)]
        assert lines == [x for _, x in wl_shards.trace(p)]
        first, b = exp1_shards.check_lines(lines)
        bad += [dict(x, shard=s) for x in b]
        assert first["kind"] == p["kind"]
        assert first["op"] == (None if p["kind"] == "NONE" else p["fault_op"])
    bad.sort(key=lambda x: x["lt"])
    assert [b["verb"] for b in bad].count("CELL") == 1
    assert (bad[0]["shard"], bad[0]["op"], bad[0]["cell"]) == (
        cross["shard"], cross["op"], cross["cell"])
    assert [bad[0]["shard"]] + [b["shard"] for b in bad if b["verb"] == "XIN"] == cross["path"]


def test_e1_grader_partial_credit(tmp_path):
    tr = exp1_shards.truth(5, ops=OPS, duration=0)
    ws, path = str(tmp_path), str(tmp_path / "answers" / "final.json")
    assert exp1_shards.grade(ws, tr)["score"] == 0
    full = {"shards": tr["shards"], "root_cause": tr["root_cause"], "path": tr["root_cause"]["path"]}
    put_json(path, full)
    assert exp1_shards.grade(ws, tr)["score"] == 1.0
    part = copy.deepcopy(full)
    del part["root_cause"]
    part["path"] = part["path"][:3] + [0, 0]
    s = exp1_shards.grade(ws, dict(tr, ))["score"]
    put_json(path, part)
    g = exp1_shards.grade(ws, tr)
    assert s == 1.0 and g["score"] == pytest.approx(0.68 + 0.12 * 3 / 5)
    # per-shard files are the fallback for missing shards
    put_json(path, {})
    k = next(s for s, t in tr["shards"].items() if t["kind"] != "NONE")
    put_json(str(tmp_path / "answers" / f"shard_{k}.json"), tr["shards"][k])
    assert exp1_shards.grade(ws, tr)["score"] == pytest.approx(0.085)
    for junk in ("[1]", '{"shards": 3, "path": "x", "root_cause": []}', "{bad"):
        (tmp_path / "answers" / "final.json").write_text(junk)
        assert 0 <= exp1_shards.grade(ws, tr)["score"] <= 0.1


def test_e1_claiming_none_everywhere_is_cheap(tmp_path):
    tr = exp1_shards.truth(5, ops=OPS, duration=0)
    put_json(str(tmp_path / "answers" / "final.json"),
             {"shards": {str(s): {"op": None, "kind": "NONE", "key": None} for s in range(1, 9)}})
    assert exp1_shards.grade(str(tmp_path), tr)["score"] == pytest.approx(0.085)


# ── experiment 2 ─────────────────────────────────────────────────────────────

def test_e2_variants_agree_and_plan_is_informative():
    adj = wl_sssp.graph(9, 300)
    ref = wl_sssp.solve("bin_heap", adj)[0]
    for a in wl_sssp.ALGOS:
        dist, order, work = wl_sssp.solve(a, adj)
        assert dist == ref and sorted(order) == list(range(300)) and work > 0
    for seed in range(4):
        assert exp2_sssp.plan(seed) == exp2_sssp.plan(seed)
        tr = exp2_sssp.truth(seed)
        assert tr["bug"]["variant"] not in tr["winners"]
        assert len(set(tr["winners"])) >= 3
        assert all(1 <= x <= 3 for x in tr["crossovers"])
        assert 1.3 * tr["ref_s"] < tr["full_s"]


def test_e2_trace_reveals_the_defect_only_by_comparison():
    p, tr = exp2_sssp.plan(3, lps=100), exp2_sssp.truth(3, lps=100)
    si, bug = tr["bug"]["size"], tr["bug"]["variant"]
    settle, results = {}, {}
    for v in p["variants"]:
        lines = [x for _, x in wl_sssp.trace(p, v, si)]
        settle[v] = {x.split()[1]: x.split()[2] for x in lines if x.startswith("SETTLE")}
        cum = [int(x.split()[1][4:]) for x in lines if x.startswith("TICK")]
        results[v] = p["variants"][v]["cost"][si]
        assert cum == sorted(cum) and cum[-1] <= results[v]
        assert lines[-1].startswith("RESULT") and f"cost={results[v]} " in lines[-1]
    good = [v for v in settle if v != bug]
    assert all(settle[v] == settle[good[0]] for v in good)
    wrong = [k for k in settle[bug] if settle[bug][k] != settle[good[0]][k]]
    assert min(int(k[2:]) for k in wrong) == tr["bug"]["vertex"]
    assert settle[good[0]][f"v={tr['bug']['vertex']:05d}"] == f"d={tr['bug']['distance']:05d}"


def test_e2_grader(tmp_path):
    tr, ws = exp2_sssp.truth(4), str(tmp_path)
    ans = {k: tr[k] for k in ("winners", "crossovers", "bug")}
    path = str(tmp_path / "answers" / "final.json")
    assert exp2_sssp.grade(ws, tr)["score"] == 0          # no answer: no efficiency credit
    put_json(path, ans)
    assert exp2_sssp.grade(ws, tr)["score"] == 1.0
    put_json(str(tmp_path / ".runs" / "a.json"), {"t0": 0, "t_last": tr["full_s"], "label": "x"})
    assert exp2_sssp.grade(ws, tr)["score"] == pytest.approx(0.85)
    put_json(path, dict(ans, bug={"variant": tr["bug"]["variant"].lower()}, crossovers=None))
    assert exp2_sssp.grade(ws, tr)["score"] == pytest.approx(0.35 + 0.12)


# ── experiment 3 ─────────────────────────────────────────────────────────────

def parse_soak(lines):
    lim, viol, dig, last = 10**9, None, None, None
    for x in lines[1:]:
        f = dict(t.split("=", 1) for t in x.split() if "=" in t)
        last = int(x[1:6])
        lim = int(f.get("probe_limit", lim))
        if "bucket" in f and int(f["probe"]) > lim and viol is None:
            viol = (last, int(f["bucket"]))
        dig = f.get("table_digest", dig)
    return viol, dig, last


@pytest.mark.parametrize("rid", ["00000001", "deadbeef"])
def test_e3_trace_matches_expected(rid):
    seen = set()
    for p in exp3_relay.plan(7, epochs=500, duration=0).values():
        e = exp3_relay.expected(p, rid)
        viol, dig, last = parse_soak([x for _, x in wl_soak.trace(p, rid)])
        assert (viol, dig, last) == ((e["epoch"], e["bucket"]), e["final_digest"], e["last_epoch"])
        seen.add((e["outcome"], e["exit_code"] not in (0, None)))
    assert seen == {("COMPLETED", False), ("COMPLETED", True), ("CRASHED", True), ("STALLED", False)}


def test_e3_grader_continuity_and_invented_runs(tmp_path):
    tr, ws = exp3_relay.truth(7, epochs=300, duration=0), str(tmp_path)
    meta, jobs = {"phase1_end": 100.0, "run_end": 500.0}, {}
    for j, p in tr["jobs"].items():
        rid = f"{int(j):08x}"
        e = exp3_relay.expected(p, rid)
        jobs[j] = dict(e, first_violation={"epoch": e["epoch"], "bucket": e["bucket"]})
        put_json(str(tmp_path / ".runs" / f"job{j}.json"),
                 {"job": int(j), "run_id": rid, "t0": 50.0, "t_last": 400.0, "label": f"job{j}"})
    put_json(str(tmp_path / "answers" / "final.json"), {"jobs": jobs})
    assert exp3_relay.grade(ws, tr, meta)["score"] == 1.0
    # a relaunch in shift 2 loses continuity for that job only
    put_json(str(tmp_path / ".runs" / "x.json"), {"job": 1, "run_id": "ff", "t0": 300, "t_last": 301})
    g = exp3_relay.grade(ws, tr, meta)
    assert g["score"] == pytest.approx(1 - 0.15 / 6, abs=1e-3) and g["relaunches"] == 1
    # a stalled job left alive loses its termination credit
    g = exp3_relay.grade(ws, tr, dict(meta, run_end=401.0))
    assert g["score"] == pytest.approx(1 - 0.30 / 6, abs=1e-3)
    # run ids that were never launched earn nothing
    put_json(str(tmp_path / "answers" / "final.json"),
             {"jobs": {j: dict(v, run_id="0badc0de") for j, v in jobs.items()}})
    assert exp3_relay.grade(ws, tr, meta)["score"] == 0


# ── workspaces ───────────────────────────────────────────────────────────────

def test_image_roundtrip_is_opaque():
    obj = {"seed": 123456789, "kind": "BALANCE"}
    blob = wl_common.seal(obj)
    assert wl_common.unseal(blob) == obj and b"BALANCE" not in blob and b"seed" not in blob


def test_workspace_runs_and_hides_sources(tmp_path):
    ws = str(tmp_path / "ws dir")                          # a space must not break the wrapper
    exp1_shards.build(ws, 11, ops=400, duration=1.0)
    files = [os.path.join(b, f) for b, _, fs in os.walk(ws) for f in fs]
    assert not [f for f in files if f.endswith(".py")]
    blob = b"".join(open(f, "rb").read() for f in files)
    assert os.path.realpath(EXP).encode() not in blob and b"check_lines" not in blob
    t = time.time()
    out = subprocess.run([ws + "/bin/shard_stress", "--shard", "2"], capture_output=True,
                         text=True, check=True).stdout.splitlines()
    assert time.time() - t > 0.7                           # paced
    p = exp1_shards.plan(11, ops=400, duration=1.0)[0][2]
    assert out == [x for _, x in wl_shards.trace(p)]
    rec, = ss_build.launches(ws)
    assert rec["done"] and rec["label"] == "shard2"


def test_exit_code_survives_the_wrapper(tmp_path):
    ws = str(tmp_path)
    exp3_relay.build(ws, 7, epochs=40, duration=0.2)
    p = next(p for p in exp3_relay.plan(7, epochs=40, duration=0.2).values() if p["mode"] == "warn")
    r = subprocess.run([ws + "/bin/soak", "--job", str(p["job"])], capture_output=True, text=True)
    assert r.returncode == p["exit"] != 0 and "FINAL" in r.stdout


# ── prompts and commands ─────────────────────────────────────────────────────

def test_prompts_differ_only_in_the_tools_paragraph():
    for exp in ss_prompts.EXPS:
        assert len(ss_prompts.roster(exp)) == 4
        for name, _ in ss_prompts.roster(exp):
            ids = {n: n for n, _ in ss_prompts.roster(exp)}
            a, b = (ss_prompts.agent_prompt(exp, c, name, "/w", ids, 900)
                    for c in ("smartstreams", "native"))
            ha, hb = a.split("\nTools. "), b.split("\nTools. ")
            assert ha[0] == hb[0] and len(ha) == len(hb) == 2
            assert "smartstreams" in a and "smartstreams" not in b.lower()


def test_commands_and_schedule(tmp_path):
    args = ss_run.parse_args(["--out", str(tmp_path), "--exp", "1", "--http-port", "9399",
                              "--smartstreams-home", "/h"])
    cmds = {c: ss_run.claude_command(c, "/cfg", args) for c in ss_run.CONDITIONS}
    for cmd in cmds.values():
        assert "--strict-mcp-config" in cmd and "--max-budget-usd" in cmd
        assert not any("dangerously" in x for x in cmd)
    diff = [(x, y) for x, y in zip(cmds["smartstreams"], cmds["native"]) if x != y]
    assert len(diff) == 1 and diff[0][0].startswith(diff[0][1] + ",mcp__smartstreams__job_start")
    assert ss_run.mcp_config("native", "x", args) == {"mcpServers": {}}
    srv = ss_run.mcp_config("smartstreams", "x", args)["mcpServers"]["smartstreams"]
    assert srv["args"][1:] == ["--agent-id", "x", "--host", "127.0.0.1", "--http-port", "9399",
                               "--framework", "claude"]
    assert srv["env"] == {"PLUTO_SMARTSTREAMS_HOME": "/h"}
    plan = ss_run.schedule(["1", "2", "3"], ss_run.CONDITIONS, 20, "m")
    assert plan == ss_run.schedule(["1", "2", "3"], ss_run.CONDITIONS, 20, "m")
    firsts = [c[0] for _, _, c in plan]
    assert len(plan) == 60 and 15 < firsts.count("native") < 45
    assert ss_run.run_seed("m", "1", 1) != ss_run.run_seed("m", "2", 1)


def test_agents_cannot_reach_the_parent_session(monkeypatch):
    for k in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_FUTURE_THING", "CLAUDE_PID",
              "PLUTO_SMARTSTREAMS_HOME"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "auth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "auth")
    env = ss_run.agent_env()
    left = {k for k in env if k.startswith("CLAUDE")}
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"} <= left
    assert not left & set(ss_run.session_vars(dict.fromkeys(left))) - {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY"}
    assert env["ANTHROPIC_API_KEY"] == "auth" and "PLUTO_SMARTSTREAMS_HOME" not in env


# ── metrics ──────────────────────────────────────────────────────────────────

def asst(mid, blocks, **usage):
    return {"type": "assistant", "message": {"id": mid, "usage": usage, "content": blocks}}


def use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def res(tid, content, **kw):
    return {"type": "user", "message": {"content": [
        dict(type="tool_result", tool_use_id=tid, content=content, **kw)]}}


EVENTS = [
    {"type": "system", "subtype": "init", "model": "claude-sonnet-x",
     "mcp_servers": [{"name": "smartstreams", "status": "connected"}]},
    asst("m1", [{"type": "text", "text": "hi"}], input_tokens=3, output_tokens=1),
    asst("m1", [use("t1", "Bash", command="ls")], input_tokens=3, output_tokens=1),
    res("t1", "héllo"), res("t1", "duplicate"),
    asst("m2", [use("t2", "mcp__smartstreams__job_grep", pattern="x")], input_tokens=5, output_tokens=2),
    res("t2", [{"type": "text", "text": "abc"}, {"type": "image", "source": {"data": "x" * 999}}]),
    asst("m3", [use("t3", "Write", file_path="/q")], input_tokens=1),
    res("t3", "Claude requested permissions to write to /q", is_error=True),
]
RESULT = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5,
          "permission_denials": [{"tool_name": "Write"}],
          "modelUsage": {"s": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
                               "cacheCreationInputTokens": 40}}}


def test_metrics_parsing():
    m = ss_metrics.parse_agent(EVENTS + [RESULT])
    assert m["tokens"] == {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
                           "cache_creation_input_tokens": 40}
    assert m["turns"] == 3 and m["cost_usd"] == 0.5 and m["permission_denials"] == 1
    assert m["tool_result_bytes_by_tool"]["Bash"] == 6
    assert m["tool_result_bytes_by_tool"]["mcp__smartstreams__job_grep"] == 3
    assert m["tool_errors"] == 1 and sum(m["tool_calls"].values()) == 3
    assert ss_metrics.failures(m, {}, "smartstreams") == ["permission_denied"]
    killed = ss_metrics.parse_agent(EVENTS)
    assert killed["cost_usd"] is None and killed["tokens"]["input_tokens"] == 9
    assert "timeout" in ss_metrics.failures(killed, {"timed_out": True}, "native")
    assert "cutoff" in ss_metrics.failures(
        killed, {"timed_out": True, "expected_cutoff": True}, "native")
    over = ss_metrics.parse_agent([dict(RESULT, subtype="error_max_budget_usd")])
    assert "error_max_budget_usd" in ss_metrics.failures(over, {}, "native")
    assert {"mcp_not_connected", "smartstreams_unused"} <= set(
        ss_metrics.failures(ss_metrics.parse_agent([RESULT]), {}, "smartstreams"))
    team = ss_metrics.merge({"a": m, "b": killed})
    assert team["cost_usd"] == 0.5 and team["cost_complete"] is False


@pytest.mark.parametrize("cmd,code", [
    ("cat /x/pluto-v0.6.0/docs/a", "harness"), ("ls /out/runs/bbb/ws", "other_run"),
    ("cat /out/runs/aaa/logs/w1.jsonl", "other_run"), ("python3 -m dis bin/x.pyc", "decompile"),
    ("strings bin/shard_stress.pyc", "program_internals"), ("cat .runs/x.json", "program_internals"),
    ("ls ../..", "parent_dir"), ("grep -r x ~/.claude/projects", "transcripts"),
    ("bin/shard_stress --shard 1 > /out/runs/aaa/ws/s1.log", None),
    ("head -c 99 /out/runs/aaa/pluto/home/jobs/J1/output.log", None),
    ("cat /out/runs/aaa/pluto/home/key", "other_run"),
    ("grep -m1 bf=+2 s1.log | head", None), ("python3 scratch_w1/check.py s1.log", None)])
def test_leak_detection(cmd, code):
    ev = [asst("m", [use("t", "Bash", command=cmd)])]
    hits = ss_metrics.find_leaks(ev, "/out/runs/aaa/ws", "/out")
    assert {h[0] for h in hits} == ({code} if code else set())


# ── runner with a fake claude ────────────────────────────────────────────────

FAKE = """\
#!{py}
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
me = prompt.split("You are agent ")[1].split()[0]
def emit(**e): print(json.dumps(e), flush=True)
emit(type="system", subtype="init", model="fake", mcp_servers=[])
if me == "p1":
    subprocess.Popen(["bin/soak", "--job", "1"], stdout=open("j1.log", "w"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(60)                                  # must be cut off by the harness
if me == "q1":
    time.sleep(1.5)
    rid = open("j1.log").readline().split("run_id=")[1].split()[0]
    json.dump({{"jobs": {{"1": {{"run_id": rid}}}}}}, open("answers/final.json", "w"))
emit(type="result", subtype="success", is_error=False, total_cost_usd=0.1, modelUsage={{}})
"""


def test_runner_end_to_end_with_fake_claude(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setattr(exp3_relay, "PHASE1_CUTOFF", 2)
    out = tmp_path / "out"
    (out / "runs").mkdir(parents=True)
    args = ss_run.parse_args(["--out", str(out), "--exp", "3", "--condition", "native",
                              "--claude", str(fake)])
    row, = ss_run.run_block("3", 1, ["native"], "m", args)
    assert row["status"]["p1"]["timed_out"] and row["status"]["q1"]["rc"] == 0
    assert row["grade"]["detail"]["job1"]["run_id"]          # job survived the shift change
    assert row["grade"]["detail"]["job1"]["continuity"]
    assert row["team"]["cost_complete"] is False and not row["tainted"]
    assert "cutoff" in row["protocol_failures"]["p1"]
    assert any("soak" in c for c in row["leftovers_killed"])  # and was cleaned up
    ws = os.path.join(args.out, "runs", row["run_id"], "ws")
    assert ss_procs.own_processes(None, ws, 0) == {}
    s = summarize.summarize([json.loads(x) for x in open(out / "results.jsonl")])
    assert s["experiments"]["3"]["conditions"]["native"]["score"]["n"] == 1
    assert "Experiment 3" in summarize.markdown(s)


def test_cleanup_spares_unrelated_processes(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    other = subprocess.Popen(["sleep", "30"], cwd=str(tmp_path), start_new_session=True,
                             stdin=subprocess.DEVNULL)
    try:
        ss_procs.cleanup(None, ws, time.time() - 5, rounds=1)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_summary_pairs_by_seed():
    def row(cond, seed, score):
        tok = dict.fromkeys(ss_metrics.KEYS, 1)
        return {"exp": "1", "name": "n", "condition": cond, "rep": 1, "run_id": "r", "seed": seed,
                "score": score, "wall_s": 10.0, "program_seconds": 1.0, "n_protocol_failures": 0,
                "tainted": seed == 2, "protocol_failures": {"w1": []}, "grade": {"parts": {}},
                "team": {"cost_usd": 1.0, "cost_complete": True, "tokens": tok, "turns": 1,
                         "tool_calls": {}, "tool_result_bytes": 5, "max_tool_result_bytes": 5}}
    rows = [row("smartstreams", 1, .9), row("native", 1, .7), row("smartstreams", 2, .5),
            row("native", 2, .5), row("native", 3, .1)]
    d = summarize.summarize(rows)["experiments"]["1"]["paired_diff_smartstreams_minus_native"]["score"]
    assert d["n"] == 2 and d["mean"] == pytest.approx(0.1)
    clean = summarize.summarize(rows, True)
    assert clean["n_used"] == 3 and len(clean["runs"]) == 5


def test_files_stay_small():
    for f in os.listdir(EXP):
        if f.endswith((".py", ".md")):
            assert len(open(os.path.join(EXP, f)).readlines()) < 1000
    assert textwrap.dedent(FAKE)
