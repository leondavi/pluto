"""Offline tests for experiments/subagents_vs_pluto (no server, no LLM)."""

import json
import os
import subprocess
import sys

import pytest

EXP = os.path.join(os.path.dirname(__file__), "..", "experiments", "subagents_vs_pluto")
sys.path.insert(0, os.path.abspath(EXP))

import metrics  # noqa: E402
import scenarios  # noqa: E402

LEDGER = os.path.abspath(os.path.join(EXP, "ledger.py"))


def _ledger(tmp_path, *args, wait=True):
    env = dict(os.environ, LEDGER_DIR=str(tmp_path))
    p = subprocess.Popen([sys.executable, LEDGER, *args], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not wait:
        return p
    out, _ = p.communicate(timeout=30)
    return p.returncode, json.loads(out)


def test_unsynchronised_appends_lose_updates(tmp_path):
    procs = [_ledger(tmp_path, "append", "--agent", f"w{i}", "--value", "x", wait=False)
             for i in range(4)]
    for p in procs:
        p.communicate(timeout=30)
    v = scenarios.validate("s2_contention", str(tmp_path))
    assert v["append_calls"] == 4
    assert v["entries"] < 4, "race window should make concurrent appends collide"


def test_sequential_appends_are_kept(tmp_path):
    for i in range(3):
        _ledger(tmp_path, "append", "--agent", "w1", "--value", str(i))
    data = json.loads((tmp_path / "ledger.json").read_text())
    assert data["count"] == 3 and len(data["entries"]) == 3


def test_fenced_put_rejects_stale_token(tmp_path):
    assert _ledger(tmp_path, "put", "--agent", "w2", "--value", "w2-fresh", "--token", "7")[0] == 0
    rc, out = _ledger(tmp_path, "put", "--agent", "w1", "--value", "w1-stale", "--token", "6")
    assert rc == 3 and out["error"] == "stale_token"
    v = scenarios.validate("s3_fencing", str(tmp_path))
    assert v["ok"] and v["stale_rejected"] and not v["corrupted"]


def test_unfenced_stale_put_is_corruption(tmp_path):
    _ledger(tmp_path, "put", "--agent", "w2", "--value", "w2-fresh")
    _ledger(tmp_path, "put", "--agent", "w1", "--value", "w1-stale")
    v = scenarios.validate("s3_fencing", str(tmp_path))
    assert not v["ok"] and v["corrupted"] and v["final_writer"] == "w1"


def test_ring_validator(tmp_path):
    for a in scenarios.WORKERS * 2 + ["w1"]:
        _ledger(tmp_path, "hop", "--agent", a, "--word", "x")
    v = scenarios.validate("s1_ring", str(tmp_path))
    assert v["ok"] and v["hops"] == 9 and len(v["hop_latency_s"]) == 8


def test_fanout_validator_counts_duplicates(tmp_path, monkeypatch):
    events = [{"ts": float(i), "op": op, "agent": "w1", "job": k}
              for i, (op, k) in enumerate([("job_start", 1), ("job_end", 1),
                                           ("job_start", 1), ("job_end", 1)])]
    v = scenarios.validate_s4(str(tmp_path), events)
    assert v["duplicates"] == 1 and len(v["missing"]) == 11 and not v["ok"]


def test_prompts_share_the_task_text():
    for sc in scenarios.SCENARIOS:
        a = scenarios.orchestrator_prompt(sc, "subagents")
        b = scenarios.orchestrator_prompt(sc, "pluto")
        assert scenarios.SCENARIOS[sc]["task"] in a and scenarios.SCENARIOS[sc]["task"] in b


@pytest.fixture
def transcript():
    usage = {"input_tokens": 2, "output_tokens": 5,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}
    tool = {"type": "tool_use", "id": "t1", "name": "Agent"}
    return [
        {"type": "assistant", "parent_tool_use_id": None, "_recv_ts": 1.0,
         "message": {"id": "m1", "usage": usage, "content": [tool]}},
        # duplicate emission of the same message must not double-count
        {"type": "assistant", "parent_tool_use_id": None, "_recv_ts": 1.1,
         "message": {"id": "m1", "usage": usage, "content": [tool]}},
        {"type": "assistant", "parent_tool_use_id": "t1", "_recv_ts": 2.0,
         "message": {"id": "m2", "usage": usage,
                     "content": [{"type": "tool_use", "id": "t2", "name": "Bash"}]}},
        {"type": "result", "total_cost_usd": 0.5, "is_error": False,
         "modelUsage": {"m": {"inputTokens": 4, "outputTokens": 50,
                              "cacheReadInputTokens": 200, "cacheCreationInputTokens": 20}}},
    ]


def test_transcript_summary(transcript):
    s = metrics.summarize_transcript(transcript)
    assert s["tokens"]["output_tokens"] == 50  # totals come from modelUsage
    assert s["tokens_by_role"]["orchestrator"]["cache_read_input_tokens"] == 100
    assert s["tokens_by_role"]["subagents"]["cache_read_input_tokens"] == 100
    assert s["tools"] == {"orchestrator:Agent": 1, "subagents:Bash": 1}
    assert metrics.count_coordination(s["tools"]) == 1
    assert s["cost_usd"] == 0.5 and not s["is_error"]


def test_merge_process_stats(transcript):
    s = metrics.summarize_transcript(transcript)
    m = metrics.merge_process_stats({"orch": s, "w1": s})
    assert m["tokens"]["output_tokens"] == 100
    assert m["tokens_orchestrator"]["output_tokens"] == 50
    assert m["cost_usd"] == 1.0


def test_stall_is_logged(tmp_path):
    rc, out = _ledger(tmp_path, "stall", "--agent", "w1", "--seconds", "0.1")
    assert rc == 0 and out["ok"]
    ops = [e["op"] for e in scenarios.load_events(str(tmp_path))]
    assert ops == ["stall_start", "stall_end"]
