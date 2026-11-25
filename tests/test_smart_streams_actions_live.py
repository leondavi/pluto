"""Rules that act, and starting many jobs, against a live server."""

import collections
import os
import random
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from tests.smart_streams_server import Server  # noqa: E402
from test_smart_streams_live import PY, done, until  # noqa: E402

from pluto_sdk import PlutoError  # noqa: E402

ACK = dict(match="want {t}", then="append", file="acks/{k}.ack", text="{t}")


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    s = Server(tmp_path_factory.mktemp("act"), max_running=64, max_per_agent=64).start()
    yield s
    s.cleanup()


def lines(path):
    try:
        return path.read_text().split("\n")[:-1]
    except FileNotFoundError:
        return []


def settled(j, jid):
    """Nothing left to carry out."""
    return until(lambda: all(not r["waiting"] and not r["active"] for r in
                             j.watch("list", job_id=jid)["jobs"][jid]) or None)


def test_rules_given_at_start_miss_nothing_of_the_shortest_job(srv, tmp_path):
    j = srv.jobs("short")
    ids = [j.start(command="echo want a; echo want b", cwd=str(tmp_path),
                   params={"k": str(i)}, watch=[ACK])["job_id"] for i in range(10)]
    for i, jid in enumerate(ids):
        done(j, jid)
        settled(j, jid)
        assert lines(tmp_path / f"acks/{i}.ack") == ["a", "b"]
    assert not [n for n in j.call("notices")["notices"] if n["event"] == "job_watch"]


def test_a_batch_is_one_request_with_one_answer_per_job(srv, tmp_path):
    j = srv.jobs("batch")
    batch = [{"name": str(i), "command": f"echo want {i}; echo want {i}x", "params": {"k": str(i)}}
             for i in range(48)]
    t = time.time()
    r = j.call("start", batch=batch, cwd=str(tmp_path), tags=["t"], watch=[ACK], request_id="B")
    took = time.time() - t
    assert [x["name"] for x in r["jobs"]] == [str(i) for i in range(48)]
    again = j.call("start", batch=batch, cwd=str(tmp_path), tags=["t"], watch=[ACK], request_id="B")
    assert again == r
    for x in r["jobs"]:
        done(j, x["job_id"])
        settled(j, x["job_id"])
    for i in range(48):
        assert lines(tmp_path / f"acks/{i}.ack") == [str(i), f"{i}x"]
    assert took < 5, took


def test_a_wrong_rule_starts_nothing(srv, tmp_path):
    j = srv.jobs("wrong")
    for kw in (dict(watch=[dict(ACK, text="{nope}")]), dict(watch=[dict(ACK, then="run")]),
               dict(watch=[dict(ACK, file=5)]), dict(watch=["x"]),
               dict(watch=[ACK] * 17), dict(batch=[]), dict(batch=[{"command": 5}])):
        with pytest.raises(PlutoError):
            j.start(**dict(dict(command="sleep 60", cwd=str(tmp_path), params={"k": "1"}), **kw))
    with pytest.raises(PlutoError, match="params"):
        j.start(command="true", params={"a/b": "1"})
    assert j.call("list")["total"] == 0


def test_input_answers_a_job_even_one_that_is_only_starting(srv):
    j = srv.jobs("ask")
    jid = j.start(command="echo ask 1; read a; echo got $a; echo ask 2; read b; echo got $b",
                  stdin=True, watch=[dict(match="ask {n}", then="input", text="yes {n}")])["job_id"]
    done(j, jid)
    assert "got yes 1\nask 2\ngot yes 2" in j.call("window", job_id=jid, tail=5)["text"]


def test_a_failed_action_is_told(srv, tmp_path):
    j = srv.jobs("fail")
    (tmp_path / "acks").symlink_to("/tmp")
    jid = j.start(command="echo want a; sleep 30", cwd=str(tmp_path), params={"k": "zz"},
                  watch=[ACK])["job_id"]
    note = until(lambda: [n for n in j.call("notices")["notices"] if n["event"] == "job_watch"])[0]
    assert "action failed" in note["text"] and not os.path.exists("/tmp/zz.ack")
    r = j.wait(job_id=jid, until="watch", timeout_ms=2000)
    assert r["hits"][jid]["reason"] == "watch"
    j.call("control", job_id=jid, action="cancel")


def test_rules_outlive_a_handover_and_a_rerun(srv, tmp_path):
    a, b = srv.jobs("giver"), srv.jobs("taker")
    jid = a.start(command="sleep 1.5; echo want late", cwd=str(tmp_path), params={"k": "1"},
                  watch=[ACK])["job_id"]
    a.call("transfer", job_id=jid, to="taker")
    done(b, jid)
    settled(b, jid)
    assert lines(tmp_path / "acks/1.ack") == ["late"]
    again = b.start(rerun_of=jid)["job_id"]
    done(b, again)
    settled(b, again)
    assert lines(tmp_path / "acks/1.ack") == ["late", "late"]


def test_waiting_for_hits_ignores_what_was_silently_done(srv, tmp_path):
    j = srv.jobs("quiet")
    jid = j.start(command="echo want a; sleep 2; echo BAD; sleep 60", cwd=str(tmp_path),
                  params={"k": "1"}, watch=[ACK, dict(match="BAD")])["job_id"]
    r = j.wait(job_id=jid, until="watch", timeout_ms=15000)
    assert r["hits"][jid]["watch"][0]["text"] == "BAD"
    j.call("control", job_id=jid, action="cancel")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_kill9_loses_no_action(tmp_path, seed):
    """Kills between any two steps; repeats are allowed, losses are not."""
    r = random.Random(seed)
    s = Server(tmp_path / "s", max_running=32, max_per_agent=32).start()
    try:
        j = s.jobs("k")
        n, count = 4, 150
        code = (f"import time\nfor i in range({count}):\n print('want', i, flush=True)\n"
                " time.sleep(.04)")
        ids = [j.start(argv=[PY, "-c", code], cwd=str(tmp_path), params={"k": str(i)},
                       watch=[ACK])["job_id"] for i in range(n)]
        for _ in range(4):
            time.sleep(r.uniform(.3, 1.2))
            s.kill()
            s.start()
        for i, jid in enumerate(ids):
            done(j, jid)
            settled(j, jid)
            got = lines(tmp_path / f"acks/{i}.ack")
            assert set(got) == {str(x) for x in range(count)}
            times = collections.Counter(got)
            assert sum(times.values()) - count <= 60, times      # only what was in flight
        # Finished jobs do nothing more, however often the server starts.
        before = [lines(tmp_path / f"acks/{i}.ack") for i in range(n)]
        for _ in range(2):
            s.kill()
            s.start()
            j.call("list")
            time.sleep(1.5)
        assert [lines(tmp_path / f"acks/{i}.ack") for i in range(n)] == before
    finally:
        s.cleanup()


def test_actions_are_prompt_under_load(srv, tmp_path):
    j = srv.jobs("lat")
    code = ("import time\nfor i in range(8):\n time.sleep(.5)\n"
            " print('want', time.time(), flush=True)")
    (tmp_path / "p.py").write_text(code)
    r = j.call("start", batch=[{"name": str(i), "command": f"{PY} p.py", "params": {"k": str(i)}}
                               for i in range(32)], cwd=str(tmp_path), watch=[ACK])
    late, seen = [], {}
    end = time.time() + 30
    while time.time() < end and sum(seen.values()) < 32 * 8:
        for i in range(32):
            got = lines(tmp_path / f"acks/{i}.ack")
            for t in got[seen.get(i, 0):]:
                late.append(time.time() - float(t))
            seen[i] = len(got)
        time.sleep(.02)
    assert len(late) == 32 * 8
    late.sort()
    assert late[int(len(late) * .95)] < 1.0, late[-20:]
    print("action latency p50 %.2f p95 %.2f max %.2f" % (
        late[len(late) // 2], late[int(len(late) * .95)], late[-1]))
    for x in r["jobs"]:
        done(j, x["job_id"])
