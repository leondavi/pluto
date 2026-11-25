"""Templates, lean schemas and wakeups of the SmartStreams adapter."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_py"))

from agent_smart_streams import submit, wake  # noqa: E402
from agent_smart_streams.notices import NoticeBoard  # noqa: E402
from agent_smart_streams.schema import slim  # noqa: E402
from pluto_sdk import PlutoError  # noqa: E402


def test_values():
    assert submit.values("1-3,8,x") == ["1", "2", "3", "8", "x"]
    assert submit.values("08-10") == ["08", "09", "10"]
    assert submit.values(["1-3", "a,b"]) == ["1-3", "a,b"]      # lists are literal
    for bad in ("5-1", "1-1000"):
        with pytest.raises(PlutoError):
            submit.values(bad)


def test_expand():
    got = submit.expand("p {a} {b} | awk '{print $1} {c}'", {"a": "1-2", "b": "{a},y"})
    assert got[0] == ("1-{a}", "p 1 {a} | awk '{print $1} {c}'")    # no re-expansion
    assert [n for n, _ in got] == ["1-{a}", "1-y", "2-{a}", "2-y"]
    for tpl, each in (("p", {"a": "1"}), ("p", {}), ("p {a}", {"a": []}),
                      ("{a}{b}", {"a": "1-30", "b": "1-30"})):
        with pytest.raises(PlutoError):
            submit.expand(tpl, each)


class FakeJobs:
    def __init__(self, fail_at=()):
        self.n, self.fail_at = 0, fail_at

    def start(self, **kw):
        self.n += 1
        if self.n in self.fail_at:
            raise PlutoError("no")
        return {"job_id": f"J-20260101-{self.n:016x}"}


def test_a_failed_start_does_not_hide_running_jobs():
    batch = [("a", "x"), ("b", "y"), ("c", "z")]
    r = submit.start_all(FakeJobs(fail_at=(2,)), batch, {})
    assert r["jobs"] == "a=000001 c=000003" and r["failed"] == {"b": "no"}
    with pytest.raises(PlutoError):
        submit.start_all(FakeJobs(fail_at=(1,)), batch, {})


def test_slim_schema_keeps_meaning():
    s = slim({"title": "T", "properties": {
        "title": {"title": "Title", "type": "string"},
        "n": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        "k": {"default": 3, "type": "integer"}}, "required": ["title"]})
    assert s == {"properties": {"title": {"type": "string"}, "n": {"type": "integer"},
                                "k": {"default": 3, "type": "integer"}},
                 "required": ["title"]}


def notifier(monkeypatch):
    monkeypatch.setattr(wake, "WAKE_GRACE_S", 0.02)
    monkeypatch.setattr(wake, "WAKE_POLL_S", 0.01)
    n = wake.JobNotifier("/nowhere", None, "me", enabled=True)
    n.DEBOUNCE_S = 0.02
    n.board = NoticeBoard(None)
    n.sent = []

    async def send(text):
        n.sent.append(text)
    n._send = send
    return n


EVENT = {"event": "job_finished", "job_id": "J-20260101-00000000000000ab",
         "state": "failed", "from": "smartstreams"}


def test_wake_only_for_news(monkeypatch):
    async def go():
        n = notifier(monkeypatch)
        await n.notify_new_messages([EVENT])
        await asyncio.sleep(0.15)
        assert n.sent == ["[smartstreams:me] 0000ab failed"]

        known = notifier(monkeypatch)
        known.board.expect_finish(["0000ab"])
        await known.notify_new_messages([EVENT])
        await asyncio.sleep(0.15)
        assert known.sent == []

        busy = notifier(monkeypatch)
        busy.board.busy = 1
        await busy.notify_new_messages([EVENT])
        await asyncio.sleep(0.1)
        assert busy.sent == []
        busy.board.busy = 0       # the call ended without telling the agent
        await asyncio.sleep(0.15)
        assert len(busy.sent) == 1
        mail = notifier(monkeypatch)
        await mail.notify_new_messages([{"event": "message", "from": "peer"}])
        await asyncio.sleep(0.1)
        assert "from peer" in mail.sent[0]
    asyncio.run(go())
