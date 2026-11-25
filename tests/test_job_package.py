"""Hostile .pjob archives must be refused; no server needed."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

PRIV = os.path.join(os.path.dirname(__file__), "..", "src_erl", "priv")
PKG = os.path.join(PRIV, "pluto_job_package.py")
JID = "J-20260101-0123456789abcdef"


def call(*a):
    out = subprocess.run([sys.executable, "-I", "-B", PKG, *map(str, a)], text=True,
                         capture_output=True)
    return json.loads(out.stdout)


@pytest.fixture
def job(tmp_path):
    d = tmp_path / "job"
    (d / "control").mkdir(parents=True)
    subprocess.run([sys.executable, "-I", "-B", os.path.join(PRIV, "pluto_job_runner.py"),
                    "run", str(d)], text=True, capture_output=True,
                   input=json.dumps({"command": "seq 1 30000"}) + "\n")
    import time
    while "final_size" not in json.loads((d / "state.json").read_text()):
        time.sleep(0.05)
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"schema": 1, "id": JID, "owner": "a",
                               "opener": "a", "user": "u", "opened_at": 1,
                               "origin": {"host_id": "h"}}))
    dest = tmp_path / "x.pjob"
    assert not call("export", d, pkg, dest)["snapshot"]
    return dest


def rewrite(src, dst, change):
    """Copy the archive, letting *change* edit {name: bytes}; re-sum it."""
    with tarfile.open(src) as t:
        files = {m.name: t.extractfile(m).read() for m in t}
    files.pop("SHA256SUMS")
    change(files)
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(v).hexdigest()}  {k}\n" for k, v in files.items()).encode()
    with tarfile.open(dst, "w:gz") as t:
        for k, v in files.items():
            i = tarfile.TarInfo(k)
            i.size = len(v)
            t.addfile(i, io.BytesIO(v))
    return dst


def edit(files, name, old, new):
    assert old in files[name]
    files[name] = files[name].replace(old, new, 1)


def streams(**kw):
    s = {"id": "S1", "kind": "push", "state": "open", "name": "", "by": "a",
         "at": 1, "parent": None, **kw}
    return json.dumps({"seq": 1, "streams": [s], "followups": []}).encode()


def test_unknown_and_scheduling_fields_are_dropped(job, tmp_path):
    ok = rewrite(job, tmp_path / "n.pjob", lambda f: edit(
        f, "package.json", b'"schema"', b'"sched": "queued", "done": false, "schema"'))
    call("import", ok, tmp_path / "s")
    pkg = json.loads((tmp_path / "s" / "package.json").read_text())
    assert "sched" not in pkg and "done" not in pkg


def test_huge_extension_header_is_refused_cheaply(tmp_path):
    import gzip
    info = tarfile.TarInfo("././@LongLink")
    info.type, info.size = tarfile.GNUTYPE_LONGNAME, 3 << 30
    with gzip.open(tmp_path / "b.pjob", "wb", 1) as f:
        f.write(info.tobuf(tarfile.GNU_FORMAT))
        f.write(b"\0" * (64 << 20))
    assert "type" in call("import", tmp_path / "b.pjob", tmp_path / "s")["error"]


def test_round_trip(job, tmp_path):
    r = call("import", job, tmp_path / "s")
    assert r["id"] == JID and r["final"]
    assert "pid" not in json.loads((tmp_path / "s" / "state.json").read_text())


@pytest.mark.parametrize("name,change,why", [
    ("output", lambda f: f.__setitem__("output.log", b"X" + f["output.log"][1:]), "chunk 0"),
    ("extra", lambda f: f.__setitem__("output.log", f["output.log"] + b"z"), "size"),
    ("manifest", lambda f: f.__setitem__("manifest.jsonl", f["manifest.jsonl"][256:]), "chunk 0"),
    ("member", lambda f: f.__setitem__("evil.sh", b"x"), "unexpected"),
    ("escape", lambda f: f.__setitem__("../evil", b"x"), "unexpected"),
    ("escape2", lambda f: f.__setitem__("streams/../../evil", b"x"), "unexpected"),
    ("absolute", lambda f: f.__setitem__("/tmp/evil", b"x"), "unexpected"),
    ("id", lambda f: f.__setitem__("package.json", f["package.json"].replace(
        JID.encode(), b"../../../../x")), "bad job id"),
    ("schema", lambda f: f.__setitem__("package.json", f["package.json"].replace(
        b'"schema": 1', b'"schema": 2')), "schema"),
    ("state", lambda f: f.__setitem__("state.json", b"[]"), "not an object"),
    ("nan", lambda f: edit(f, "state.json", b'"paused_ms": 0', b'"paused_ms": NaN'), "NaN"),
    ("dup", lambda f: edit(f, "package.json", b'"id":', b'"id": "../../x", "id":'), "duplicate"),
    ("surrogate", lambda f: edit(f, "package.json", b'"user": "u"', b'"user": "\\ud800"'), "unreadable"),
    ("bool", lambda f: edit(f, "package.json", b'"opened_at": 1', b'"opened_at": true'), "opened_at"),
    ("origin", lambda f: edit(f, "package.json", b'{"host_id": "h"}', b'"x"'), "origin"),
    ("nostate", lambda f: edit(f, "state.json", b'"state"', b'"zzz"'), "lacks state"),
    ("cursor", lambda f: f.__setitem__("cursors.json", b'{"a":{"pos":"abc"}}'), "pos"),
    ("streams", lambda f: f.__setitem__("streams.json", b"{}"), "streams"),
    ("streamid", lambda f: f.__setitem__("streams.json", streams(id="../../x")), "stream"),
    ("streamrange", lambda f: f.__setitem__("streams.json", streams(
        kind="split", lo=0, hi=10**9)), "range"),
    ("review", lambda f: f.__setitem__("reviews.jsonl", b'{"id":1}\n'), "review"),
    ("missing", lambda f: f.pop("manifest.jsonl"), "missing"),
])
def test_tampered(job, tmp_path, name, change, why):
    bad = rewrite(job, tmp_path / "bad.pjob", change)
    assert why in call("import", bad, tmp_path / "s")["error"]
    assert not (tmp_path / "evil").exists()


def test_symlink_truncated_and_garbage(job, tmp_path):
    with tarfile.open(tmp_path / "l.pjob", "w:gz") as t:
        i = tarfile.TarInfo("output.log")
        i.type, i.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(i)
    assert "unexpected" in call("import", tmp_path / "l.pjob", tmp_path / "a")["error"]
    (tmp_path / "t.pjob").write_bytes(job.read_bytes()[:5000])
    assert call("import", tmp_path / "t.pjob", tmp_path / "b")["error"]
    (tmp_path / "g.pjob").write_bytes(b"nope")
    assert call("import", tmp_path / "g.pjob", tmp_path / "c")["error"]
