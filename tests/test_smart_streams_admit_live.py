"""When queued jobs start (slots, after_jobs, Pluto locks) and followers fed
by their runner, against a live server."""

import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from tests.smart_streams_server import Server  # noqa: E402
from test_smart_streams_live import done, until  # noqa: E402
from test_smart_streams_cli import pss  # noqa: E402

from pluto_sdk import PlutoError, PlutoHttpClient  # noqa: E402


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    s = Server(tmp_path_factory.mktemp("admit"), max_running=16, max_per_agent=2).start()
    yield s
    s.cleanup()


def spans(path):
    """[(start, end)] from lines "start end" the jobs wrote."""
    with open(path) as f:
        return [tuple(map(float, line.split())) for line in f if line.strip()]


def most_at_once(intervals):
    events = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals])
    now = top = 0
    for _, d in events:
        now += d
        top = max(top, now)
    return top


STAMP = "a=$(python3 -c 'import time;print(time.time())'); sleep 1; " \
        "echo $a $(python3 -c 'import time;print(time.time())') >> {log}"


def test_a_follower_reads_the_whole_output_and_is_not_held_by_the_limit(srv, tmp_path):
    j = srv.jobs("f1")
    busy = [j.start(command="sleep 30", cwd=str(tmp_path))["job_id"] for _ in range(2)]
    src = j.start(command="for i in $(seq 1 2000); do echo line $i; done; sleep 1; echo end",
                  cwd=str(tmp_path))["job_id"]
    fol = j.start(command="wc -l | tr -d ' '", follows=src, cwd=str(tmp_path))["job_id"]
    # The agent's two slots are taken: the job waits, its follower does not.
    assert until(lambda: j.call("status", job_id=fol)["state"] == "running")
    assert j.call("status", job_id=src)["state"] == "queued"
    for b in busy:
        j.call("control", job_id=b, action="cancel")
    assert done(j, src)["state"] == "succeeded"
    assert done(j, fol)["state"] == "succeeded"           # stdin closed at the end
    assert j.call("window", job_id=fol, tail=1)["text"].strip() == "2001"
    with pytest.raises(PlutoError, match="follower"):
        j.call("input", job_id=fol, text="x")
    with pytest.raises(PlutoError, match="unknown"):
        j.start(command="cat", follows="J-20260101-00000000deadbeef")


def test_a_slot_is_shared_by_all_agents(srv, tmp_path):
    log = tmp_path / "spans"
    ids = [srv.jobs(f"s{i % 3}").start(command=STAMP.format(log=log), cwd=str(tmp_path),
                                       slot="gpu", slot_max=2)["job_id"] for i in range(6)]
    for i, jid in enumerate(ids):
        done(srv.jobs(f"s{i % 3}"), jid)
    assert len(spans(log)) == 6 and most_at_once(spans(log)) == 2


def test_after_jobs_waits_and_after_ok_cancels(srv, tmp_path):
    j = srv.jobs("d1")
    a = j.start(command="sleep 1; echo a >> order", cwd=str(tmp_path))["job_id"]
    bad = j.start(command="exit 3", cwd=str(tmp_path))["job_id"]
    b = j.start(command="echo b >> order", cwd=str(tmp_path), after_jobs=[a, bad])["job_id"]
    c = j.start(command="echo c >> order", cwd=str(tmp_path), after_jobs=[bad],
                after_ok=True)["job_id"]
    assert done(j, b)["state"] == "succeeded"
    assert (tmp_path / "order").read_text() == "a\nb\n"
    card = done(j, c)
    assert card["state"] == "cancelled" and "did_not_succeed" in card.get("reason", "")
    with pytest.raises(PlutoError, match="unknown"):
        j.start(command="true", after_jobs=["J-20260101-00000000deadbeef"])


def test_a_job_holds_a_pluto_lock_that_agents_respect(srv, tmp_path):
    http = PlutoHttpClient("127.0.0.1", srv.port)
    held = http._post("/locks/acquire", {"agent_id": "human", "resource": "db",
                                         "ttl_ms": 60000})
    assert held["status"] == "ok"
    j = srv.jobs("l1")
    jid = j.start(command="echo token=$PLUTO_LOCK_TOKEN on $PLUTO_LOCK_RESOURCE",
                  cwd=str(tmp_path), lock="db")["job_id"]
    time.sleep(1.5)
    assert j.call("status", job_id=jid)["state"] == "queued"
    http._post("/locks/release", {"agent_id": "human", "lock_ref": held["lock_ref"]})
    assert done(j, jid)["state"] == "succeeded"
    out = j.call("window", job_id=jid, tail=1)["text"]
    assert out.startswith("token=") and out.strip().endswith("on db")
    # Released when the job ended: the agent gets it at once.
    again = http._post("/locks/try_acquire", {"agent_id": "human", "resource": "db"})
    assert again["status"] == "ok"
    with pytest.raises(PlutoError, match="lock_mode"):
        j.start(command="true", lock="db", lock_mode="exclusive")


def test_two_jobs_with_one_write_lock_never_overlap(srv, tmp_path):
    log = tmp_path / "spans"
    j = srv.jobs("l2")
    ids = [j.start(command=STAMP.format(log=log), cwd=str(tmp_path), lock="file:/x")["job_id"]
           for _ in range(2)]
    for jid in ids:
        done(j, jid)
    assert most_at_once(spans(log)) == 1


def test_the_shell_names_it_all(srv, tmp_path):
    pss(srv, "run", "-q", "-n", "pre", "-e", "k=1-2", "echo pre {k}", cwd=tmp_path, agent="c1")
    out = pss(srv, "run", "-n", "post", "--after", "pre", "--after-ok", "--slot", "cpu:1",
              "echo post", cwd=tmp_path, agent="c1")
    assert out.startswith("post")
    assert "succeeded" in pss(srv, "wait", "pre,post", "-t", "30", cwd=tmp_path, agent="c1")
    pss(srv, "run", "-q", "-n", "src", "for i in 1 2 3; do echo v=$i; sleep 0.3; done",
        cwd=tmp_path, agent="c1")
    pss(srv, "follow", "src", "grep -c v= > counted", cwd=tmp_path, agent="c1")
    pss(srv, "wait", "follow-src", "-t", "30", cwd=tmp_path, agent="c1")
    assert (tmp_path / "counted").read_text().strip() == "3"
    assert pss(srv, "head", "-n", "1", "src", cwd=tmp_path, agent="c1").strip() == "v=1"
    assert pss(srv, "grep", "-c", "v=", "src", cwd=tmp_path, agent="c1").strip() == "src:3"
