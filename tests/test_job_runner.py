"""PlutoSmartTasks runner: no Pluto server needed."""

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
    out = subprocess.run([sys.executable, "-I", "-B", RUNNER, "run", str(d)],
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
        return subprocess.run([sys.executable, "-I", "-B", RUNNER, *map(str, a)], text=True,
                              capture_output=True).stdout.strip()
    time.sleep(0.2)
    assert run("ident", s["runner_pid"]) == "ident:"
    assert run("ident", s["pid"]) == "ident:" + s["child_ident"] != "ident:"
    assert run("kill", s["pid"], "bogus", 15) == "gone"
    assert run("kill", s["pid"], s["child_ident"], 15) == "ok"


def test_background_children_stay_under_limits(tmp_path):
    loop = "(while :; do head -c 200000 /dev/zero; sleep 0.02; done) & echo started"
    d, _ = start(tmp_path / "a", command=loop, max_output_bytes=40_000_000)
    s = wait(d, lambda s: "leader_exited_at" in s)
    assert s["state"] == "running" and s["leader_exit_code"] == 0
    assert finished(d)["state"] == "output_limit"
    n = os.path.getsize(d / "output.log")
    time.sleep(0.5)
    assert os.path.getsize(d / "output.log") == n
    d, _ = start(tmp_path / "b", command="sleep 60 & exit 0")
    wait(d, lambda s: "leader_exited_at" in s)
    control(d, 1, action="cancel")
    assert finished(d)["state"] == "cancelled"
    d, _ = start(tmp_path / "c", command="sleep 1 & exit 3")
    s = finished(d)
    assert (s["state"], s["exit_code"]) == ("failed", 3)


def test_stdin_burst_is_not_lost_and_cancel_overtakes_it(tmp_path):
    import base64
    d, _ = start(tmp_path / "a", command="sleep 1; wc -c", stdin=True)
    chunk = base64.b64encode(b"x" * 65536).decode()
    for i in range(40):
        control(d, i, action="input", data=chunk, close=i == 39)
    assert finished(d)["state"] == "succeeded"
    assert int((d / "output.log").read_text()) == 40 * 65536
    d, _ = start(tmp_path / "b", command="sleep 60", stdin=True)
    for i in range(40):
        control(d, i, action="input", data=chunk)
    control(d, 99, action="cancel")
    assert finished(d)["state"] == "cancelled"


def test_truncated_output_is_reported_and_manifest_matches(tmp_path):
    code = ("import os,time;os.write(1,b'x'*300000);time.sleep(.6);"
            "os.ftruncate(1,0);os.write(1,b'after\\n')")
    d, _ = start(tmp_path, argv=[sys.executable, "-c", code])
    s = finished(d)
    data = (d / "output.log").read_bytes()
    assert s["output_truncated"] and data == b"after\n"
    assert s["sha256"] == hashlib.sha256(data).hexdigest()
    rec = json.loads((d / "manifest.jsonl").read_text())
    assert rec["end"] == len(data)


def test_abort_file_prevents_a_late_start(tmp_path):
    d = tmp_path / "J-test"
    (d / "control").mkdir(parents=True)
    (d / "abort").write_text("")
    subprocess.run([sys.executable, "-I", "-B", RUNNER, "run", str(d)], text=True,
                   input=json.dumps({"command": f"touch {tmp_path}/ran"}) + "\n",
                   capture_output=True)
    assert state(d)["state"] == "failed" and not (tmp_path / "ran").exists()


def test_low_disk_stops_the_job(tmp_path):
    d, _ = start(tmp_path, command="yes", min_free_bytes=1 << 60)
    s = finished(d)
    assert (s["state"], s["reason"]) == ("output_limit", "low_disk")
