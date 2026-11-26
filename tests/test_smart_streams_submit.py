"""Templates, lean schemas and wakeups of the SmartStreams adapter."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_py"))

from agent_smart_streams import each, names, render, report, rules, submit, wake  # noqa: E402
from agent_smart_streams.notices import NoticeBoard  # noqa: E402
from agent_smart_streams.schema import slim  # noqa: E402
from pluto_sdk import PlutoError  # noqa: E402


def test_values():
    assert names.values("1-3,8,x", 64) == ["1", "2", "3", "8", "x"]
    assert names.values("08-10", 64) == ["08", "09", "10"]
    assert names.values(["1-3", "a,b"], 64) == ["1-3", "a,b"]      # lists are literal
    for bad in ("5-1", "1-1000"):
        with pytest.raises(PlutoError):
            names.values(bad, 64)


@pytest.mark.parametrize("keys,text", [
    (["3", "1", "2", "9"], "1-3,9"), (["1", "2"], "1,2"), (["a", "1:b"], "a,1:b"),
    (["01", "02"], "01,02"), (["7"], "7"), (["10", "9", "8"], "8-10")])
def test_ranges_can_be_given_back(keys, text):
    assert names.ranges(keys) == text
    assert sorted(names.values(text, 64)) == sorted(keys)


@pytest.mark.parametrize("bad", ["abc123", "g12345f", "1x", "a/b", "", "a b", "-x", "é"])
def test_names_never_look_like_ids(bad):
    with pytest.raises(PlutoError):
        names.check_name(bad)


def test_expand():
    got = submit.expand("p {a} {b} | awk '{print $1} {c}'", {"a": "1-2", "b": "{a},y"})
    assert got[0] == {"name": "1:{a}", "params": {"a": "1", "b": "{a}"},
                      "command": "p 1 {a} | awk '{print $1} {c}'"}    # no re-expansion
    assert [x["name"] for x in got] == ["1:{a}", "1:y", "2:{a}", "2:y"]
    for tpl, each in (("p", {"a": "1-2"}), ("p", {}), ("p {a}", {"a": []}),
                      ("{a}{b}", {"a": "1-30", "b": "1-30"})):
        with pytest.raises(PlutoError):
            submit.expand(tpl, each)
    # One job may be named by a key it does not use (repairs: -n zr -e k=8).
    assert submit.expand("p --from 5", {"k": "8"}) == [
        {"name": "8", "params": {"k": "8"}, "command": "p --from 5"}]


class FakeJobs:
    def start(self, *, batch, **kw):
        return {"jobs": [{"name": b["name"], "error": "no"} if b["name"] == "b" else
                         {"name": b["name"], "job_id": f"J-20260101-{i:016x}"}
                         for i, b in enumerate(batch, 1)]}


def test_a_failed_start_does_not_hide_running_jobs():
    batch = [{"name": n, "command": n} for n in "abc"]
    r = submit.start_all(FakeJobs(), batch, {})
    assert r["jobs"] == "a=000001 c=000003" and r["failed"] == {"b": "no"}
    r = submit.start_all(FakeJobs(), batch, {}, "soak")
    assert r["jobs"] == "a,c" and r["name"] == "soak"


def card(i, name=None, state="running", tags=("=soak", "g00000a"), **kw):
    return dict({"job_id": f"J-20260101-{i:016x}", "name": name, "state": state, "tags": list(tags),
                 "owner": "me", "opener": "me", "opened_at": i, "elapsed_ms": 1000 * i,
                 "size": 1000 * i, "activity": "active", "cwd": os.getcwd()}, **kw)


class Listing:
    agent_id, as_user = "me", False

    def __init__(self, *cards):
        self.cards = cards

    def call(self, op, **kw):
        if op == "status":
            return next(c for c in self.cards if c["job_id"].endswith(kw["job_id"]))
        found = [c for c in self.cards if not kw["tags"] or kw["tags"][0] in c["tags"]]
        return {"total": len(found), "jobs": found[:kw["limit"]]}


def test_selectors():
    j = Listing(*[card(i, str(i)) for i in range(1, 9)], card(99, "x", tags=["=other"]))
    keys = lambda *h: [c["name"] for c in names.cards(j, list(h))]     # noqa: E731
    assert keys("soak/2") == ["2"] and keys("soak/1-3,8") == ["1", "2", "3", "8"]
    assert len(keys("soak")) == 8 and keys("g00000a/3") == ["3"]
    assert keys("soak/2", "soak/1-2", "other") == ["2", "1", "x"]
    assert names.members(j, ["soak/2", "000002"]) == ["000002"]
    for bad in ("soak/9", "nope", "gffffff", "soak/", "/x"):
        if bad == "soak/":
            continue
        with pytest.raises(PlutoError):
            keys(bad)


def test_a_name_means_the_newest_job_of_each_key_here():
    old = [card(i, str(i), tags=["=soak", "g00000a"]) for i in (1, 2, 3)]
    new = [card(10 + i, str(i), tags=["=soak", "g00000b"]) for i in (1, 2)]
    peer = [card(5, "7", tags=["=soak", "g00000c"], opener="p2", owner="p2")]
    far = [card(30, "9", cwd="/elsewhere", opener="p3", owner="p3", tags=["=soak", "g00000d"])]
    got = names.cards(Listing(*old, *new, *peer, *far), ["soak"])
    # A key run again is replaced; one not run again stays (names grow).
    assert sorted((c["name"], c["opened_at"]) for c in got) == [
        ("1", 11), ("2", 12), ("3", 3), ("7", 5)]
    assert [c["opened_at"] for c in names.cards(Listing(*old, *new), ["soak/3"])] == [3]
    with pytest.raises(PlutoError, match="has no 4"):
        names.cards(Listing(*old, *new), ["soak/4"])
    # As the user, who owns every job, a change needs the right directory.
    with pytest.raises(PlutoError, match="in this directory"):
        names.cards(Listing(*far), ["soak"], strict=True)
    assert names.cards(Listing(*far), ["soak"])          # looking is harmless
    # Jobs handed over keep their name for the new owner elsewhere.
    assert names.cards(Listing(card(30, "9", cwd="/elsewhere")), ["soak/9"])


def test_an_incomplete_listing_is_refused(monkeypatch):
    monkeypatch.setattr(names, "GROUP_MAX", 3)
    with pytest.raises(PlutoError, match="8 jobs"):
        names.cards(Listing(*[card(i, str(i)) for i in range(8)]), ["soak"])


def test_rows_say_the_same_thing_once():
    cs = [card(i, str(i)) for i in range(1, 12)] + [
        card(12, "12", "failed", exit_code=3), card(13, "13", activity="idle")]
    out = render.rows(cs, "me")
    assert out[0].startswith("soak/1-11 11x running 1-11s") and len(out) == 3
    assert "failed exit=3" in out[1] and "idle" in out[2]
    assert len(render.rows(cs, "me", verbose=True)) == 13
    last = {f"{i:06x}": f"E{i} FINAL digest=ab{i}" for i in range(1, 12)}
    assert len(render.rows(cs[:11], "me", last)) == 1
    last["000003"] = "Segmentation fault"
    assert len(render.rows(cs[:11], "me", last)) == 2


def test_spans():
    s = lambda *v: render.span(list(v), render.size)          # noqa: E731
    assert s(5) == "5B" and s(1000, 2000) == "1.0-2.0KB" and s(10, 2e6) == "10B-2.0MB"


def parse(*args):
    import argparse
    p = argparse.ArgumentParser()
    rules.add_options(p)
    return p.parse_args(args).rules


def test_rules_on_the_command_line():
    assert parse("--on", "a {t}", "--append", "f", "{t}", "--idle", "60", "--stop",
                 "--on", "x", "--where", "v > 1", "--once", "--where", "w > 2", "--pause",
                 "--where", "z > 3") == [
        {"match": "a {t}", "then": "append", "file": "f", "text": "{t}"},
        {"idle_s": 60.0, "then": "cancel"},
        {"match": "x", "where": ["v > 1", "w > 2"], "once": True, "then": "pause"},
        {"where": ["z > 3"]}]
    # Several actions on one condition are several rules.
    assert parse("--on", "x {t}", "--once", "--append", "f", "{t}", "--stop") == [
        {"match": "x {t}", "once": True, "then": "append", "file": "f", "text": "{t}"},
        {"match": "x {t}", "once": True, "then": "cancel"}]
    for bad in (["--stop"], ["--append", "f", "t"]):
        with pytest.raises(SystemExit):
            parse(*bad)


def test_each_fills_in_only_what_it_knows(tmp_path):
    c = card(1, "3", "failed", exit_code=7, params={"k": "3 4"})
    ok, text = each.run_one("echo {k} {key} {exit} $PSS_STATE {nope} | awk '{print $0}'", None, c)
    assert ok and text == "3 4 3 7 failed {nope}"
    out = str(tmp_path / "d/{key}.json")
    assert each.run_one("echo '{\"a\": 1}'", out, c) == (True, "")
    assert (tmp_path / "d/3.json").read_text() == '{"a": 1}\n'
    for cmd, why in (("echo no", "not JSON"), ("echo bad >&2; exit 4", "exit 4: bad")):
        ok, text = each.run_one(cmd, out, c)
        assert not ok and why in text
    assert (tmp_path / "d/3.json").read_text() == '{"a": 1}\n'      # kept
    with pytest.raises(PlutoError, match="both write"):
        each.check_outputs("same.json", [card(1, "1"), card(2, "2")])


def test_each_reports_every_job_and_bounds_what_it_prints():
    cs = [card(i, str(i), "succeeded") for i in range(1, 30)]
    r = each.run(None, cs, "test {key} != 5 && seq 1 5000", out=None, timeout_s=5)
    text = "\n".join(each.report(r))
    assert len(text) < each.EACH_TOTAL + 500 and "FAILED soak/5: exit 1" in text
    assert text.endswith("28 ok, 1 failed")


def test_report_columns_about_the_job():
    class Jobs:
        def analyze(self, **kw):
            assert kw["columns"] == ["v=first:e where p > 1"]
            return {"records": [{"v": 4}], "notes": []}
    c = card(1, "3", "failed", exit_code=7, params={"k": "3"})
    r = report.records(Jobs(), [c], [], ["job=k", "code=_exit", "v=first:e where p > 1", "s=_state"])
    assert r["records"] == {"soak/3": (c, {"job": "3", "code": 7, "v": 4, "s": "failed"})}


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
