"""Acting rules without a server: templates, rules, and the append helper."""

import json
import os
import random
import struct
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRIV = os.path.join(ROOT, "src_erl", "priv")
sys.path.insert(0, PRIV)

import pluto_job_act as helper  # noqa: E402
import pluto_job_defs as d  # noqa: E402
from pluto_out_fields import QueryError  # noqa: E402
from pluto_out_rules import Rule, check_rule  # noqa: E402


class Job:
    label, name, params = "abc123", "3", {"k": "3"}


ACK = dict(match="CKPT token={t} ttl={s}", then="append", file="control/job_{k}.ack", text="{t}")


def fire(lines, params=("k",), **spec):
    r = Rule("R1", check_rule(spec, params))
    for n, text in enumerate(lines, 1):
        if not r.done:
            r.tick(float(n), 0, float(n), lambda: (None, ""), True, Job)
            r.feed(text, n, 0, float(n), Job)
    return r.take()


def test_every_match_is_its_own_action_with_text_kept_as_written():
    hits = fire(["CKPT token=007 ttl=75s", "noise", "CKPT token=a;b ttl=75s"] * 2, **ACK)
    assert [h["act"] for h in hits] == [
        {"kind": "append", "file": "control/job_3.ack", "text": t} for t in ("007", "a;b")] * 2
    typed = dict(ACK, match="CKPT token={t:int} ttl={s}")
    assert fire(["CKPT token=007 ttl=1"], **typed)[0]["act"]["text"] == "007"


def test_placeholders_of_line_parameter_and_job():
    hit, = fire(["x v=1"], match="x", then="input", text='{v} {k} {_name} {_job} {_line} {"a": 1} {')
    assert hit["act"] == {"kind": "input", "text": '1 3 3 abc123 x v=1 {"a": 1} {'}
    # A parameter alone needs nothing from the line.
    assert fire(["plain"], match="plain", then="append", file="{k}.txt", text="seen")[0]["act"]


def test_rules_by_time_can_act_with_parameters():
    r = Rule("R1", check_rule(dict(idle_s=5, then="append", file="idle", text="{k}"), ("k",)))
    r.tick(1.0, 10, 1.0, lambda: (1, "x"), True, Job)
    r.tick(7.0, 10, 1.0, lambda: (1, "x"), True, Job)
    assert [h["act"]["text"] for h in r.take()] == ["3"]


@pytest.mark.parametrize("change,why", [
    (dict(file=None), "needs file"), (dict(text=""), "needs text"),
    (dict(cooldown_s=3), "cooldown"), (dict(text="{nope}"), "neither captured"),
    (dict(then="notify"), "goes with"), (dict(then="run"), "use notify"),
    (dict(then="input"), "goes with"), (dict(text="x" * 5000), "longer"),
    (dict(match=None, idle_s=3, text="{t}"), "neither captured")])
def test_bad_actions_are_refused_when_added(change, why):
    with pytest.raises(QueryError, match=why):
        check_rule(dict(ACK, **change), ("k",))


@pytest.mark.parametrize("line", [
    "CKPT token=../x ttl=1", "CKPT token=.env ttl=1", "CKPT token=-rf ttl=1",
    "CKPT token=a/b ttl=1", "CKPT token=" + "a" * 101 + " ttl=1", "CKPT token=é ttl=1"])
def test_a_captured_value_cannot_choose_a_path(line):
    hit, = fire([line], **dict(ACK, file="out/{t}"))
    assert "act" not in hit and hit["then"] == "notify" and "not done" in hit["text"]


def test_too_long_is_refused_not_cut():
    hit, = fire(["CKPT token=%s ttl=1" % ("a" * 5000)], **ACK)
    assert "act" not in hit and "longer" in hit["text"]
    hit, = fire(["CKPT token=a ttl=1" + "x" * d.MATCH_LINE_MAX], **ACK)
    assert "act" not in hit


def test_control_characters_do_not_reach_the_file():
    hit, = fire(["CKPT token=a\x1b\x00b ttl=1"], **dict(ACK, match="re:token=(?P<t>.*) ttl"))
    assert hit["act"]["text"] == "a  b"


# ── the append helper ───────────────────────────────────────────────────────

def ask(*reqs):
    data = b"".join(struct.pack(">I", len(b)) + b for b in
                    (json.dumps(dict(r, id=i)).encode() for i, r in enumerate(reqs)))
    r = subprocess.run([sys.executable, "-I", "-B", os.path.join(PRIV, "pluto_job_act.py")],
                       input=data, stdout=subprocess.PIPE, timeout=20)
    out, at = [], 0
    while at < len(r.stdout):
        n = struct.unpack(">I", r.stdout[at:at + 4])[0]
        out.append(json.loads(r.stdout[at + 4:at + 4 + n]))
        at += 4 + n
    return out


def test_appends_make_directories_and_keep_order(tmp_path):
    reqs = [dict(root=str(tmp_path), file="a/b/f", text=f"{i} é") for i in range(50)]
    assert ask(*reqs) == [{"id": i} for i in range(50)]
    assert (tmp_path / "a/b/f").read_text() == "".join(f"{i} é\n" for i in range(50))
    assert ask(dict(root=str(tmp_path), file=str(tmp_path / "x"), text="abs")) == [{"id": 0}]


def test_nothing_outside_the_directory_is_touched(tmp_path):
    out, job = tmp_path / "outside", tmp_path / "job"
    out.mkdir(), job.mkdir()
    (out / "f").write_text("keep")
    (job / "dirlink").symlink_to(out)
    (job / "filelink").symlink_to(out / "f")
    os.link(out / "f", job / "hard")
    os.mkfifo(job / "pipe")
    (job / "dir").mkdir()
    (job / "file").write_text("")
    bad = ["../outside/f", "a/../../outside/f", str(out / "f"), "dirlink/f", "dirlink/new",
           "filelink", "hard", "pipe", "dir", "file/x", "", ".", "/"]
    for got, name in zip(ask(*[dict(root=str(job), file=f, text="evil") for f in bad]), bad):
        assert "error" in got, name
    assert sorted(os.listdir(out)) == ["f"] and (out / "f").read_text() == "keep"


def test_a_full_file_is_refused(tmp_path):
    with open(tmp_path / "big", "wb") as f:
        f.truncate(d.ACT_FILE_MAX)
    assert "larger" in ask(dict(root=str(tmp_path), file="big", text="x"))[0]["error"]


def test_random_paths_stay_inside(tmp_path):
    r = random.Random(5)
    job = tmp_path / "j"
    job.mkdir()
    for _ in range(3000):
        path = "/".join(r.choices(["..", ".", "", "a", "b", "~", "\\", " "], k=r.randrange(1, 6)))
        try:
            assert ".." not in helper.parts(str(job), path)
        except helper.Refused:
            pass
