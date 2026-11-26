"""What agents typed in the study, and pss now understands; and the news
line. Offline: fake clients only."""

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src_py"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from agent_smart_streams import cli, cli_parser, meanwhile, names, render  # noqa: E402
from pluto_sdk import PlutoError  # noqa: E402
from test_smart_streams_submit import Listing, card  # noqa: E402


def parse(*argv):
    return cli_parser.build().parse_args(list(argv))


def test_names_with_commas_are_several_handles_unless_they_are_keys():
    j = Listing(card(1, "1", tags=["=r1", "g00000a"]), card(2, "2", tags=["=r2", "g00000b"]),
                *[card(10 + i, str(i), tags=["=z", "g00000c"]) for i in (1, 2, 3)])
    assert [c["name"] for c in names.cards(j, ["r1,r2"])] == ["1", "2"]
    assert [c["name"] for c in names.cards(j, ["z/1,3"])] == ["1", "3"]
    assert [c["opened_at"] for c in names.cards(j, ["z/2-3,r1"])] == [12, 13, 1]
    with pytest.raises(PlutoError, match="z has no 9"):
        names.cards(j, ["z/1,9"])


def test_grep_and_head_take_the_options_of_grep_and_head():
    a = parse("grep", "-B2", "-A8", "-E", "-n", "-c", "-v", "^Q", "z/13")
    assert (a.B, a.A, a.count, a.invert, a.pattern, a.ids) == (2, 8, True, True, "^Q", ["z/13"])
    assert parse("head", "-n", "3", "z/1").n == 3


def test_report_takes_several_columns_after_one_f():
    a = parse("report", "m/1-12", "-x", "step {n:int} c={c:float}", "-f", "cmax=max:c",
              "n=last:n")
    assert a.field == ["cmax=max:c", "n=last:n"] and a.ids == ["m/1-12"]


def test_admission_options_become_request_fields():
    j = Listing(card(1, "1"), card(2, "2"))
    a = parse("run", "--slot", "gpu:2", "--after", "soak", "--after-ok", "--lock", "db:read",
              "true")
    got = cli.admission(j, a)
    assert got == {"slot": "gpu", "slot_max": 2, "after_ok": True, "lock": "db",
                   "lock_mode": "read", "after_jobs": [card(1)["job_id"], card(2)["job_id"]]}
    assert cli.admission(j, parse("run", "--lock", "host:5432", "true")) == {
        "lock": "host:5432", "lock_mode": None}       # a colon of its own is kept
    for bad in (["--slot", "gpu"], ["--slot", "gpu:0"], ["--after-ok"]):
        with pytest.raises(PlutoError):
            cli.admission(j, parse("run", *bad, "true"))


def test_a_queued_job_says_what_it_waits_for():
    c = card(1, "1", "queued", after_jobs=["J-20260101-00000000000000aa"], after_ok=True,
             slot="gpu", slot_max=2, lock="db")
    assert render.status(c) == "queued (after 0000aa ok, slot gpu/2, lock db)"
    assert render.status(card(2, "2", lock="db", lock_lost=True)).endswith("lost-lock=db")


def test_the_help_has_what_agents_looked_up_and_stays_short():
    text = cli_parser.HELP
    for needed in ("--slot", "--after", "--lock", "--append", "--input", "--idle", "follow",
                   "head|tail", "instead of sleep", "several allowed"):
        assert needed in text, needed
    assert len(text) < 2400
    assert cli_parser.topic("head").strip().startswith("ps [JOBS]")
    assert "--after" in cli_parser.topic("run")


def note(seq, event, name, state=None, tags=("=z", "g00000c"), **kw):
    return dict({"seq": seq, "event": event, "job_id": f"J-20260101-{seq:016x}",
                 "name": name, "state": state, "tags": list(tags)}, **kw)


def test_news_groups_jobs_and_leaves_out_what_is_no_news():
    ns = [note(1, "job_finished", "1", "succeeded"), note(2, "job_finished", "2", "succeeded"),
          note(3, "job_finished", "3", "failed"), note(4, "job_finished", "4", "cancelled"),
          note(5, "job_finished", "1", "succeeded", tags=["=follow-z"], follows="x"),
          note(6, "job_watch", "1"), note(7, "job_transferred", "9", "running")]
    assert meanwhile.summary(ns) == ("ended: z/1,2 succeeded, z/3 failed; "
                                     "handed over: z/9")
    assert meanwhile.summary([note(6, "job_watch", "1")]) == ""


class Board:
    agent_id = "w1"

    def __init__(self, notices):
        self.notices, self.acks = notices, []

    def call(self, op, ack):
        self.acks.append(ack)
        return {"notices": [n for n in self.notices if n["seq"] > ack]}


def test_news_is_said_once_and_never_inside_a_job(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLUTO_SMARTSTREAMS_HOME", str(tmp_path))
    monkeypatch.delenv(meanwhile.IN_JOB_ENV, raising=False)
    b = Board([note(1, "job_finished", "1", "failed")])
    meanwhile.tell(b)
    meanwhile.tell(b)
    assert capsys.readouterr().err == "pss: meanwhile ended: z/1 failed\n"
    assert b.acks == [0, 1]
    monkeypatch.setenv(meanwhile.IN_JOB_ENV, "J-x")
    meanwhile.tell(Board([note(2, "job_finished", "2", "failed")]))
    assert capsys.readouterr().err == ""
