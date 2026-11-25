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

OPS = 1500


def put_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


# ── experiment 1 ─────────────────────────────────────────────────────────────

def test_e1_checker_recovers_planted_truth():
    shards, cells = exp1_shards.plan(1, ops=OPS, duration=0)
    bad = []
    assert len(shards) == 48
    for s, p in shards.items():
        lines = list(wl_shards.text_lines(p))
        first, b = exp1_shards.check_lines(lines)
        bad += [dict(x, shard=s) for x in b]
        assert set(first) == {k for k, _ in p["faults"]}
        assert all(first[k]["op"] >= o for k, o in p["faults"])
        if p["spin"] == "LIVELOCK":
            assert all(v["op"] < p["spin_op"] for v in first.values())
    assert lines == list(wl_shards.text_lines(p))
    bad.sort(key=lambda x: x["lt"])
    assert {x["cell"] for x in bad} == {c["cell"] for c in cells} and len(cells) == 6
    for c in cells:
        b = [x for x in bad if x["cell"] == c["cell"]]
        assert (b[0]["verb"], b[0]["shard"], b[0]["op"]) == ("CELL", c["shard"], c["op"])
        assert [c["shard"]] + [x["shard"] for x in b if x["verb"] == "XIN"] == c["path"]
    for g in range(4):
        spins = [shards[s]["spin"] for s in range(12 * g + 1, 12 * g + 13)]
        assert spins.count("LIVELOCK") == 2 and spins.count("TRANSIENT") == 3


def test_e1_path_invariant():
    assert exp1_shards.path_ok([50, 20, 30, 25], 25)
    assert exp1_shards.path_ok([50, 20, 30], 25)            # key absent
    assert not exp1_shards.path_ok([50, 60, 30], 25)
    assert not exp1_shards.path_ok([50, 25, 30], 25)


def ledger(tmp_path, name, **rec):
    put_json(str(tmp_path / ".runs" / f"{name}.json"),
             dict({"t0": 0.0, "t_last": 10.0, "done": False, "lag": 0, "cpu": 0, "label": name},
                  **rec))


def test_e1_grader(tmp_path):
    tr = exp1_shards.truth(5, ops=400, duration=0)
    ws, path = str(tmp_path), str(tmp_path / "answers" / "final.json")
    assert exp1_shards.grade(ws, tr)["score"] == 0
    put_json(path, {"shards": tr["shards"], "cells": tr["cells"]})
    assert exp1_shards.grade(ws, tr)["score"] == pytest.approx(0.75)
    for s in tr["livelocked"]:
        ledger(tmp_path, f"shard{s}", shard=s, began=1.0, onset=100.0, t_last=180.0)
    assert exp1_shards.grade(ws, tr)["score"] == 1.0
    ok = next(s for s in range(1, 49) if s not in tr["livelocked"])
    ledger(tmp_path, f"shard{ok}", shard=ok, began=1.0)                     # false kill
    ledger(tmp_path, f"shard{tr['livelocked'][0]}", shard=tr["livelocked"][0], began=1.0,
           onset=100.0, t_last=200.0)                                      # too late
    g = exp1_shards.grade(ws, tr)
    assert g["parts"]["reactions"] == pytest.approx(6 / 8) and g["false_kills"] == 1
    put_json(path, {"cells": tr["cells"] * 2})              # duplicates are harmless
    g = exp1_shards.grade(ws, tr)
    assert g["parts"]["cells"] == 1 and g["parts"]["shards"] == 0
    for junk in ("[1]", '{"shards": 3, "cells": [1, {"cell": "x"}]}', "{bad"):
        (tmp_path / "answers" / "final.json").write_text(junk)
        assert exp1_shards.grade(ws, tr)["parts"]["shards"] == 0
    assert exp1_shards.shard_score({}, {}) == 1 and exp1_shards.shard_score({"X": {}}, {}) == 0
    w = {"SIZE": {"op": 5, "key": 7}}
    assert exp1_shards.shard_score({"SIZE": {"op": 5, "key": 1}, "PATH": {}}, w) == pytest.approx(.35)


# ── experiment 2 ─────────────────────────────────────────────────────────────

def test_e2_variants_agree_and_plan_is_informative():
    adj = wl_sssp.graph(9, 300)
    ref = wl_sssp.solve("bin_heap", adj)[0]
    for a in wl_sssp.ALGOS:
        dist, order, work = wl_sssp.solve(a, adj)
        assert dist == ref and sorted(order) == list(range(300)) and work > 0
    for seed in range(3):
        p = exp2_sssp.plan(seed, sizes=E2)
        assert p == exp2_sssp.plan(seed, sizes=E2)
        tr = exp2_sssp.truth(seed, sizes=E2)
        bad = {d["variant"] for d in tr["defects"].values()}
        assert len(bad) == 2 and not bad & {v for row in tr["podium"] for v, _ in row}
        assert len({row[0][0] for row in tr["podium"]}) >= 3
        assert exp2_sssp.EFF_SLACK * tr["ref_s"] < 0.75 * tr["full_s"]
        for v in p["variants"].values():
            for row in v["grid"]:
                c = [x["cost"] for x in row]
                k = c.index(min(c))
                assert c[:k + 1] == sorted(c[:k + 1], reverse=True) and c[k:] == sorted(c[k:])
                assert not any(row[x]["hang"] for x in (k - 1, k, k + 1) if 0 <= x < 8)


E2 = [60, 100, 160, 240]


def test_e2_reference_search_finds_every_optimum():
    p = exp2_sssp.plan(3, sizes=E2)
    for v in p["variants"].values():
        for row in v["grid"]:
            st = {}
            while ks := exp2_sssp.next_probes(st):
                known = [s[1] for s in st.values() if s[0] == "done"]
                fin = [row[k]["cost"] for k in ks if not row[k]["hang"]]
                b = min(known + fin, default=0)
                for k in ks:
                    c = row[k]["cost"]
                    st[k] = (("hang",) if row[k]["hang"] else
                             ("done", c) if c <= b else ("gt", b))
            best = min((s[1], k) for k, s in st.items() if s[0] == "done")
            assert best[0] == min(x["cost"] for x in row)


def test_e2_traces_show_both_defects():
    p, tr = exp2_sssp.plan(3, sizes=E2, lps=30), exp2_sssp.truth(3, sizes=E2, lps=30)
    dd, dc = tr["defects"]["DISTANCE"], tr["defects"]["COUNTER"]
    settle, si = {}, dd["size"]
    for v in p["variants"]:
        k = min(range(8), key=lambda k: wl_sssp.combo(p, v, si, k)["cost"])
        lines = [x for _, x in wl_sssp.trace(p, v, si, k)]
        settle[v] = {x.split()[1]: x.split()[2] for x in lines if x.startswith("SETTLE")}
        cum = [int(x.split()[1][4:]) for x in lines if x.startswith("TICK")]
        assert (cum != sorted(cum)) == (v == dc["variant"] and si >= dc["size"])
        assert f"cost={wl_sssp.combo(p, v, si, k)['cost']} " in lines[-1]
    good = [v for v in settle if v != dd["variant"]]
    assert all(settle[v] == settle[good[0]] for v in good)
    wrong = [k for k in settle[good[0]] if settle[dd["variant"]][k] != settle[good[0]][k]]
    assert min(int(k[2:]) for k in wrong) == dd["vertex"]
    assert settle[good[0]][f"v={dd['vertex']:05d}"] == f"d={dd['distance']:05d}"
    v, i, k = next((v, i, k) for v, q in p["variants"].items() for i, row in enumerate(q["grid"])
                   for k, c in enumerate(row) if c["hang"])
    assert [x for _, x in wl_sssp.trace(p, v, i, k)][-1][0] == "HANG"


def test_e2_grader(tmp_path):
    tr, ws = exp2_sssp.truth(4, sizes=E2), str(tmp_path)
    path = str(tmp_path / "answers" / "final.json")
    assert exp2_sssp.grade(ws, tr)["score"] == 0          # no answer: no efficiency credit
    ans = {"podium": tr["podium"], "defects": tr["defects"]}
    put_json(path, ans)
    assert exp2_sssp.grade(ws, tr)["score"] == 1.0
    ledger(tmp_path, "a", t_last=tr["full_s"])
    assert exp2_sssp.grade(ws, tr)["score"] == pytest.approx(0.8)
    put_json(path, {"podium": [[[v.lower(), 99] for v, _ in r] for r in tr["podium"]],
                    "defects": {"COUNTER": dict(tr["defects"]["COUNTER"]), "DISTANCE": None}})
    assert exp2_sssp.grade(ws, tr)["score"] == pytest.approx(.25 + .3 * .4)
    put_json(path, {"podium": [["V1"], 3, [[]]], "defects": []})
    assert exp2_sssp.grade(ws, tr)["score"] == 0


def test_e2_cap_and_window_are_enforced(tmp_path):
    ws = str(tmp_path)
    exp2_sssp.build(ws, 3, sizes=E2, best_s=0.2)
    cmd = [ws + "/bin/spbench", "--size", "0", "--tune", "3", "--variant"]
    now = time.time()
    for i in range(exp2_sssp.CAP):
        ledger(tmp_path, f"V2_s0_t{i}", t0=now, t_last=now + 5, label=f"V2_s0_t{i}", variant="V2")
    assert subprocess.run(cmd + ["V1"], capture_output=True).returncode == 76
    r = subprocess.run(cmd + ["V4"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and "RESULT variant=V4" in r.stdout       # another worker's
    ledger(tmp_path, "V1_s0_t0", t0=now, t_last=now + 5, label="V1_s0_t0", ended=True)
    for i in range(1, exp2_sssp.CAP):                                  # dead pid: not alive
        ledger(tmp_path, f"V2_s0_t{i}", t0=now, t_last=now + 5, label=f"V2_s0_t{i}", pid=2**22)
    r = subprocess.run(cmd + ["V1"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0
    ledger(tmp_path, "V2_s0_t0", t0=now - 10000, t_last=now - 9000)
    assert subprocess.run(cmd + ["V7"], capture_output=True).returncode == 77


# ── experiment 3 ─────────────────────────────────────────────────────────────

def parse_soak(lines):
    lim, viol, dig, last, leak, prev = 10**9, None, None, None, None, None
    for x in lines:
        if not isinstance(x, str) or x.startswith("#"):
            continue
        f = dict(t.split("=", 1) for t in x.split() if "=" in t)
        last = int(x[1:6])
        lim = int(f.get("probe_limit", lim))
        dig = f.get("table_digest", dig)
        if "bucket" in f:
            if int(f["probe"]) > lim and viol is None:
                viol = {"epoch": last, "bucket": int(f["bucket"])}
            if prev is not None and abs(int(f["live"]) - prev) > 3 and leak is None:
                leak = last
            prev = int(f["live"])
    return viol, leak, dig, last


def test_e3_trace_matches_expected():
    modes = []
    for p in exp3_relay.plan(7, epochs=500, duration=0).values():
        e = exp3_relay.expected(p, "deadbeef")
        lines = [x for _, x in wl_soak.trace(p, "deadbeef")]
        assert parse_soak(lines) == (e["first_violation"], e["first_leak"], e["final_digest"],
                                     e["last_epoch"])
        # every request leaves at least its ttl before the job stops
        assert all((e["last_epoch"] - ep) / p["epochs"] * 0.95 * exp3_relay.DURATION
                   >= exp3_relay.TTL - 1 for ep, _ in wl_soak.requests(p, "deadbeef"))
        asked = [x[2] for x in lines if not isinstance(x, str) and x[0] == "CKPT"]
        assert asked == [t for _, t in wl_soak.requests(p, "deadbeef")] and asked
        modes.append(e["outcome"])
    assert [modes.count(x) for x in ("COMPLETED", "CRASHED", "STALLED")] == [16, 4, 4]


def test_e3_node_groups_are_identifiable():
    for seed in range(4):
        ps = exp3_relay.plan(seed)

        def common(a, b):
            return sum(any(abs(x - y) <= 4 for y in ps[b]["spikes"]) for x in ps[a]["spikes"])
        for a in ps:
            for b in ps:
                if a != b:
                    assert (common(a, b) >= 5) == (ps[a]["node"] == ps[b]["node"])
            assert max(ps[a]["spikes"]) < 0.52 * ps[a]["epochs"]
    jobs = {str(j): p for j, p in ps.items()}
    groups = [[j for j, p in ps.items() if p["node"] == n] for n in range(6)]
    assert exp3_relay.node_f1(groups, jobs) == 1
    assert exp3_relay.node_f1([list(ps)], jobs) < 0.25
    assert exp3_relay.node_f1([["x", None], 3], jobs) == exp3_relay.node_f1(None, jobs) == 0


def test_e3_grader(tmp_path):
    tr, ws = exp3_relay.truth(7, epochs=200, duration=0), str(tmp_path)
    meta, jobs = {"phase1_end": 100.0}, {}
    for j, p in tr["jobs"].items():
        rid = f"{int(j):08x}"
        jobs[j] = exp3_relay.expected(p, rid)
        rec = {"job": int(j), "run_id": rid, "t0": 50.0, "t_last": 400.0, "done": p["mode"] != "stall",
               "acks": {t: 5.0 for _, t in wl_soak.requests(p, rid)}}
        if p["mode"] == "stall":
            rec["onset"] = 300.0
        ledger(tmp_path, f"job{j}", **rec)
    nodes = [[int(j) for j, p in tr["jobs"].items() if p["node"] == n] for n in range(6)]
    path = str(tmp_path / "answers" / "final.json")
    put_json(path, {"jobs": jobs, "nodes": nodes})
    assert exp3_relay.grade(ws, tr, meta)["score"] == 1.0
    late = next(j for j, p in tr["jobs"].items() if p["mode"] == "stall")
    rec = json.load(open(tmp_path / ".runs" / f"job{late}.json"))
    ledger(tmp_path, f"job{late}", **dict(rec, t_last=460.0, acks={}))  # late kill, no acks
    g = exp3_relay.grade(ws, tr, meta)
    assert g["score"] == pytest.approx(1 - 0.9 * 0.30 / 24, abs=1e-3)
    assert g["acks_in_time"] < g["acks_requested"]
    ledger(tmp_path, "x", job=int(late), run_id="ff", t0=300)           # relaunch
    g2 = exp3_relay.grade(ws, tr, meta)
    assert g2["relaunches"] == 1 and g2["score"] < g["score"]
    put_json(path, {"jobs": {j: dict(v, run_id="0badc0de") for j, v in jobs.items()}})
    assert exp3_relay.grade(ws, tr, meta)["score"] == 0


def test_e3_ack_is_seen_while_the_job_runs(tmp_path):
    ws = str(tmp_path)
    exp3_relay.build(ws, 7, epochs=40, duration=3.0, ttl=3.0, pause_s=0.2)
    ps = exp3_relay.plan(7, epochs=40, duration=3.0, ttl=3.0, pause_s=0.2)
    j = next(j for j, p in ps.items() if p["mode"] == "ok")
    out = open(tmp_path / "log", "w+")
    p = subprocess.Popen([ws + "/bin/soak", "--job", str(j)], stdout=out)
    while p.poll() is None:
        toks = [x.split("token=")[1].split()[0] for x in open(tmp_path / "log")
                if "CKPT request" in x and x.endswith("\n")]
        (tmp_path / "control" / f"job_{j}.ack").write_text(" ".join(toks))
        time.sleep(0.05)
    rec, = ss_build.launches(ws)
    want = wl_soak.requests(ps[j], rec["run_id"])
    assert rec["done"] and len(rec["acks"]) >= 1 and set(rec["acks"]) <= {t for _, t in want}


# ── workspaces ───────────────────────────────────────────────────────────────

def test_image_roundtrip_is_opaque():
    obj = {"seed": 123456789, "kind": "BALANCE"}
    blob = wl_common.seal(obj)
    assert wl_common.unseal(blob) == obj and b"BALANCE" not in blob and b"seed" not in blob


def test_workspace_runs_and_hides_sources(tmp_path):
    ws = str(tmp_path / "ws dir")                          # a space must not break the wrapper
    exp1_shards.build(ws, 11, ops=400, duration=1.0, transient_s=0.2)
    files = [os.path.join(b, f) for b, _, fs in os.walk(ws) for f in fs]
    assert not [f for f in files if f.endswith(".py")]
    blob = b"".join(open(f, "rb").read() for f in files)
    assert os.path.realpath(EXP).encode() not in blob and b"check_lines" not in blob
    t = time.time()
    def go(k):                                       # files: an unread pipe would block
        return subprocess.Popen([ws + "/bin/shard_stress", "--shard", str(k)],
                                stdout=open(tmp_path / f"{k}.log", "w"))
    procs = {k: go(k) for k in range(13, 24)}
    time.sleep(1.2)
    assert not any("began" in r for r in ss_build.launches(ws))     # barrier holds
    procs[24] = go(24)
    plan = exp1_shards.plan(11, ops=400, duration=1.0, transient_s=0.2)[0]
    live = {k for k in procs if plan[k]["spin"] == "LIVELOCK"}
    for k, p in procs.items():
        if k not in live:
            p.wait(15)
            out = [x for x in (tmp_path / f"{k}.log").read_text().splitlines()
                   if " SPIN " not in x and "waiting" not in x]
            assert p.returncode == 0 and out == list(wl_shards.text_lines(plan[k]))
    while sum("onset" in r for r in ss_build.launches(ws)) < 2:
        time.sleep(0.1)
    for k in live:
        procs[k].terminate()
        procs[k].wait()
    assert time.time() - t < 15
    recs = ss_build.launches(ws)
    assert exp1_shards.reactions(ws, live) == (2, 0)
    assert ss_build.peak_streams(recs, ["w1", "w2"], {}, exp1_shards.owner)["w2"] >= 10


def test_peak_streams_splits_shifts():
    assert ss_build.peak([(0, 2), (1, 3), (2, 4), (5, 5)]) == 2
    recs = [{"job": j, "t0": 1.0, "t_last": 9.0 + j} for j in (1, 2, 3, 14)]
    got = ss_build.peak_streams(recs, ["p1", "p2", "q1", "q2"], {"phase1_end": 5.0},
                                exp3_relay.owner, exp3_relay.owner2)
    assert got == {"p1": 3, "p2": 1, "q1": 2, "q2": 2}


def test_exit_code_survives_the_wrapper(tmp_path):
    ws = str(tmp_path)
    exp3_relay.build(ws, 7, epochs=40, duration=0.2, pause_s=0.1)
    p = next(p for p in exp3_relay.plan(7, epochs=40, duration=0.2, pause_s=0.1).values() if p["mode"] == "warn")
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
            assert "SmartStreams" in a and "smartstreams" not in b.lower() and "pss" not in hb[1]


def test_commands_and_schedule(tmp_path):
    args = ss_run.parse_args(["--out", str(tmp_path), "--exp", "1", "--http-port", "9399",
                              "--smartstreams-home", "/h"])
    cmds = {c: ss_run.claude_command(c, "/cfg", args) for c in ss_run.CONDITIONS}
    for cmd in cmds.values():
        assert "--strict-mcp-config" in cmd and "--max-budget-usd" in cmd
        assert not any("dangerously" in x for x in cmd)
    assert cmds["smartstreams"] == cmds["native"]
    both = ss_run.claude_command("ss_both", "/cfg", args)
    diff = [(x, y) for x, y in zip(both, cmds["native"]) if x != y]
    assert len(diff) == 1 and diff[0][0].startswith(diff[0][1] + ",mcp__smartstreams__job_start")
    assert ss_run.mcp_config("native", "x", args) == {"mcpServers": {}}
    srv = ss_run.mcp_config("smartstreams", "x", args)["mcpServers"]["smartstreams"]
    assert srv["args"][1:] == ["--agent-id", "x", "--host", "127.0.0.1", "--http-port", "9399",
                               "--framework", "claude", "--tools", "none"]
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
    ("cd scratch_w1/o && ls ../../board", None), ("cd scratch_w1/o; cat ../../../x", "parent_dir"),
    ("seq b0..b9; echo 37..48.json s0..; pkill -f 'bin/x.pyc --n 1'", None),
    ("cat > s/w.py <<'E'\npgrep('x.pyc --shard 3')\nE", None), ("cat bin/x.pyc | head", "program_internals"),
    ("ls ../..", "parent_dir"), ("grep -r x ~/.claude/projects", "transcripts"),
    ("bin/shard_stress --shard 1 > /out/runs/aaa/ws/s1.log", None),
    ("head -c 99 /out/runs/aaa/pluto/home/jobs/J1/output.log", None),
    ("cat /out/runs/aaa/pluto/home/key", "other_run"),
    ("grep -m1 bf=+2 s1.log | head", None), ("python3 scratch_w1/check.py s1.log", None)])
def test_leak_detection(cmd, code):
    ev = [asst("m", [use("t", "Bash", command=cmd)])]
    hits = ss_metrics.find_leaks(ev, "/out/runs/aaa/ws", "/out")
    found = {h[0] for h in hits}
    assert code in found if code else not found


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
    time.sleep(0.6)
    rid = open("j1.log").readline().split("run_id=")[1].split()[0]
    json.dump({{"jobs": {{"1": {{"run_id": rid}}}}}}, open("answers/final.json", "w"))
emit(type="result", subtype="success", is_error=False, total_cost_usd=0.1, modelUsage={{}})
"""


def test_runner_end_to_end_with_fake_claude(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setattr(exp3_relay, "PHASE1_CUTOFF", 1.6)
    out = tmp_path / "out"
    (out / "runs").mkdir(parents=True)
    args = ss_run.parse_args(["--out", str(out), "--exp", "3", "--condition", "native",
                              "--claude", str(fake)])
    row, = ss_run.run_block("3", 1, ["native"], "m", args)
    assert row["status"]["p1"]["timed_out"] and row["status"]["q1"]["rc"] == 0
    assert row["protocol_version"] == ss_run.PROTOCOL_VERSION and row["low_parallelism"]
    assert row["peak_streams"] == {"p1": 1, "p2": 0, "q1": 1, "q2": 0}
    assert row["timed_out"] == {"p1": True, "p2": False, "q1": False, "q2": False}
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
                "score": score, "wall_s": 10.0, "program_seconds": 1.0, "peak_streams": {"w1": 9},
                "timed_out": {"w1": False}, "low_parallelism": False, "cpu_share_max": 0.1,
                "max_pacing_lag_s": 0.0, "n_protocol_failures": 0,
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
