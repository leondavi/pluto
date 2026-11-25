"""PlutoSmartStreams runner: no Pluto server needed."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

RUNNER = os.path.join(os.path.dirname(__file__), "..", "src_erl", "priv",
                      "pluto_job_runner.py")


def start(tmp_path, **spec):
    d = tmp_path / "J-test"
    (d / "control").mkdir(parents=True)
    out = subprocess.run([sys.executable, RUNNER, "run", str(d)],
                         input=json.dumps(spec) + "\n", text=True,
                         capture_output=True, timeout=15)
    return d, out.stdout.strip()


def state(d):
    return json.loads((d / "state.json").read_text())


def wait(d, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        s = state(d)
        if pred(s):
            return s
        time.sleep(0.05)
    raise AssertionError(f"timeout, last state {state(d)}")


def finished(d, timeout=20):
    return wait(d, lambda s: "final_size" in s, timeout)


def control(d, n, **req):
    tmp = d / "control" / f"{n}.tmp"
    tmp.write_text(json.dumps(req))
    tmp.rename(d / "control" / f"{n:08d}.json")


def test_success_failure_and_merged_output(tmp_path):
    d, ready = start(tmp_path, command="echo out; echo err >&2; exit 3")
    assert ready == "ready"
    s = finished(d)
    assert (s["state"], s["exit_code"]) == ("failed", 3)
    assert (d / "output.log").read_bytes() == b"out\nerr\n"


def test_manifest_matches_bytes(tmp_path):
    d, _ = start(tmp_path, command="seq 1 60000; printf tail")
    s = finished(d)
    data = (d / "output.log").read_bytes()
    raw = (d / "manifest.jsonl").read_bytes()
    assert len(raw) % 256 == 0
    chain, line = "0" * 64, 1
    recs = [json.loads(x) for x in raw.splitlines()]
    for i, r in enumerate(recs):
        part = data[r["start"]:r["end"]]
        assert r["start"] == i * 65536 and r["first_line"] == line
        assert hashlib.sha256(part).hexdigest() == r["sha256"]
        chain = hashlib.sha256((chain + r["sha256"]).encode()).hexdigest()
        assert chain == r["chain"]
        line += part.count(b"\n")
    assert recs[-1]["end"] == len(data) == s["final_size"]
    assert s["sha256"] == hashlib.sha256(data).hexdigest()


def test_start_failure_is_reported(tmp_path):
    d, ready = start(tmp_path, argv=["/no/such/program"])
    assert ready.startswith("error")
    assert state(d)["state"] == "failed"


def test_pause_resume_cancel_kills_group(tmp_path):
    d, _ = start(tmp_path, command="(sleep 300 & echo $!; "
                 "while :; do echo x; sleep 0.05; done)")
    wait(d, lambda s: s["size"] > 20)
    control(d, 1, action="pause")
    wait(d, lambda s: s["state"] == "paused")
    time.sleep(0.3)
    a = os.path.getsize(d / "output.log")
    time.sleep(0.5)
    assert os.path.getsize(d / "output.log") == a
    control(d, 2, action="resume")
    wait(d, lambda s: s["state"] == "running")
    time.sleep(0.5)
    assert os.path.getsize(d / "output.log") > a
    control(d, 3, action="cancel")
    s = finished(d)
    assert s["state"] == "cancelled" and s["paused_ms"] > 0
    pid = int((d / "output.log").read_bytes().split()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stdin_timeout_output_limit(tmp_path):
    d, _ = start(tmp_path / "a", command="cat", stdin=True)
    control(d, 1, action="input", data="aGkK", close=True)
    assert finished(d)["state"] == "succeeded"
    assert (d / "output.log").read_bytes() == b"hi\n"
    d, _ = start(tmp_path / "b", command="sleep 60", timeout_s=1)
    assert finished(d)["state"] == "timed_out"
    d, _ = start(tmp_path / "c", command="yes", max_output_bytes=100000)
    assert finished(d)["state"] == "output_limit"


def test_command_survives_runner_and_kill_checks_identity(tmp_path):
    d, _ = start(tmp_path, command="exec sleep 60")
    s = state(d)
    os.kill(s["runner_pid"], signal.SIGKILL)

    def run(*a):
        return subprocess.run([sys.executable, RUNNER, *map(str, a)], text=True,
                              capture_output=True).stdout.strip()
    time.sleep(0.2)
    assert run("ident", s["runner_pid"]) == ""
    assert run("ident", s["pid"]) == s["child_ident"] != ""
    assert run("kill", s["pid"], "bogus", 15) == "gone"
    assert run("kill", s["pid"], s["child_ident"], 15) == "ok"
