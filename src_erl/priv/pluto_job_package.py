#!/usr/bin/env python3
"""Portable .pjob archives for PlutoSmartTasks (stdlib only).

    pluto_job_package.py export <job_dir> <package.json> <dest.pjob>
    pluto_job_package.py import <src.pjob> <staging_dir>
    pluto_job_package.py merge  <old_dir> <new_dir>

Each prints one JSON object.

Archives are untrusted. Import accepts only known regular files, bounds
every size, validates and **rewrites** every JSON file so that what reaches
the server is what was checked, verifies every hash, and never runs
anything.
"""

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pluto_job_defs as d  # noqa: E402  (needs the path above)

JOB_ID = re.compile(d.JOB_ID)
STREAM_ID = re.compile(d.STREAM_ID)
STREAM_LOG = re.compile(r"\Astreams/(S[0-9]{1,6})\.log\Z")
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class PackageError(Exception):
    """The archive (or job) is unacceptable; the message says why."""


# ── Strict JSON ─────────────────────────────────────────────────────────────

def _no_constant(name):
    raise PackageError("%s is not allowed in JSON" % name)


def _no_duplicates(pairs):
    out = dict(pairs)
    if len(out) != len(pairs):
        raise PackageError("duplicate key in JSON")
    return out


def parse(text, what):
    """One JSON object. Rejects what Erlang's decoder would read
    differently: NaN, duplicate keys, lone surrogates, huge numbers."""
    try:
        obj = json.loads(text, parse_constant=_no_constant,
                         object_pairs_hook=_no_duplicates)
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise PackageError("unreadable %s: %s" % (what, str(exc)[:200]))
    if not isinstance(obj, dict):
        raise PackageError("%s is not an object" % what)
    return obj


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return parse(f.read(), os.path.basename(path))
    except (OSError, UnicodeError) as exc:
        raise PackageError("unreadable %s: %s" % (os.path.basename(path), exc))


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())


# ── Field checks: each returns the accepted value or raises ─────────────────

def is_int(v):
    return type(v) is int and 0 <= v <= d.INT_MAX        # bool is not an int


def is_str(v, limit=d.TEXT_MAX):
    return isinstance(v, str) and len(v) <= limit and "\0" not in v


def is_agent(v):
    return is_str(v, d.AGENT_ID_MAX) and v != ""


def is_strs(v):
    return isinstance(v, list) and len(v) <= d.LIST_MAX and all(map(is_str, v))


def is_num(v):
    return is_int(v) or (isinstance(v, float) and 0 < v < 1e15)


def is_origin(v):
    return isinstance(v, dict) and len(v) <= 16 and \
        all(is_str(x, 1000) for x in v.values())


def is_ownership(v):
    return isinstance(v, list) and len(v) <= d.LIST_MAX and all(
        isinstance(e, dict) and set(e) == {"agent", "at", "by"}
        and is_agent(e["agent"]) and is_agent(e["by"]) and is_int(e["at"])
        for e in v)


def is_job(v):
    return isinstance(v, str) and bool(JOB_ID.match(v))


def is_exit(v):
    return v is None or (type(v) is int and 0 <= v <= 255)


def is_hash(v):
    return isinstance(v, str) and bool(HEX64.match(v))


def is_params(v):
    return (isinstance(v, dict) and len(v) <= 64
            and all(isinstance(k, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", k)
                    and is_str(x) for k, x in v.items()))


PACKAGE_FIELDS = {
    "schema": lambda v: v == d.SCHEMA and type(v) is int, "id": is_job,
    "owner": is_agent, "opener": is_agent, "user": is_agent,
    "opened_at": is_int, "opened_ns": is_int, "origin": is_origin,
    "ownership": is_ownership, "command": is_str, "argv": is_strs,
    "cwd": is_str, "stdin": lambda v: isinstance(v, bool),
    "timeout_s": is_num, "max_output_bytes": is_int, "name": is_str,
    "tags": is_strs, "task_id": is_str, "framework": is_str,
    "session_id": is_str, "parent": is_job, "rerun_of": is_job,
    "env_keys": is_strs, "params": is_params, "slot": is_str, "slot_max": is_int,
    "lock": is_str, "lock_mode": lambda v: v in ("read", "write"),
    "after_jobs": lambda v: isinstance(v, list) and all(is_job(x) for x in v),
    "after_ok": lambda v: isinstance(v, bool), "follows": is_job,
}
PACKAGE_NEED = ("schema", "id", "owner", "opener", "user", "opened_at", "origin")
STATE_FIELDS = {
    "state": lambda v: v in d.FINAL + d.LIVE, "exit_code": is_exit,
    "signal": lambda v: is_str(v, 20), "reason": lambda v: is_str(v, 4096),
    "size": is_int, "final_size": is_int, "started_at": is_int,
    "ended_at": is_int, "paused_ms": is_int, "last_output_at": is_int,
    "sha256": is_hash, "leader_exited_at": is_int,
    "output_truncated": lambda v: isinstance(v, bool),
}
REVIEW_FIELDS = {
    "id": lambda v: is_str(v, 64), "by": is_agent, "at": is_int,
    "verdict": lambda v: is_str(v, 1000), "note": is_str,
    "from_offset": is_int, "to_offset": is_int,
    "sha256": lambda v: v is None or is_hash(v),
}
CURSOR_FIELDS = {"pos": is_int, "at": is_int, "delivered": is_int}


def is_secs(v):
    return (type(v) is int or isinstance(v, float)) and 0 <= v < 1e12


# Watch rules are only ever shown after an import, never evaluated.
WATCH_SPEC = dict.fromkeys(("name", "match", "stalled", "then", "file", "text"), is_str)
WATCH_SPEC["extract"] = lambda v: is_str(v) or is_strs(v)     # one template, or several in order
WATCH_SPEC.update(dict.fromkeys(("where", "by"), is_strs))
WATCH_SPEC.update(dict.fromkeys(("window_s", "idle_s", "absent_s", "cooldown_s"), is_secs))
WATCH_SPEC.update(dict.fromkeys(("count", "max_hits", "stalled_n"), is_int))
WATCH_SPEC.update(dict.fromkeys(("once", "from_now"), lambda v: isinstance(v, bool)))
WATCH_FIELDS = {
    "id": lambda v: is_str(v, 20), "by": is_agent, "at": is_int,
    "spec": lambda v: isinstance(v, dict),
}


def inert_rules(doc):
    """What travels of watch.json: the rules, without states and hits."""
    rules = doc.get("rules")
    if not isinstance(rules, list) or len(rules) > d.LIST_MAX:
        raise PackageError("bad watch.json")
    out = []
    for r in rules:
        r = checked(r, WATCH_FIELDS, tuple(WATCH_FIELDS), "watch rule")
        r["spec"] = checked(r["spec"], WATCH_SPEC, ("name",), "watch rule")
        out.append(r)
    return {"schema": d.SCHEMA, "inert": True, "rules": out}


def is_stream_id(v):
    return isinstance(v, str) and bool(STREAM_ID.match(v))


STREAM_FIELDS = {
    "id": is_stream_id, "kind": lambda v: v in d.STREAM_KINDS,
    "state": lambda v: v in d.STREAM_STATES, "name": is_str, "by": is_agent,
    "at": is_int, "parent": lambda v: v is None or is_stream_id(v),
    "lo": is_int, "hi": lambda v: v is None or is_int(v),
}
FOLLOWUP_FIELDS = {
    "id": lambda v: is_str(v, 20), "text": is_str, "assignee": is_agent,
    "status": lambda v: v in d.FOLLOWUP_STATUSES,
    "stream": lambda v: v is None or is_stream_id(v),
    "log": lambda v: isinstance(v, list) and len(v) <= d.LIST_MAX and all(
        isinstance(e, dict) and set(e) == {"at", "by", "status", "text"}
        and is_int(e["at"]) and is_agent(e["by"]) and is_str(e["text"])
        and e["status"] in d.FOLLOWUP_STATUSES for e in v),
}


def checked(obj, fields, need, what):
    """Known fields only, each valid; unknown ones are dropped."""
    if not isinstance(obj, dict):
        raise PackageError("%s is not an object" % what)
    for k in need:
        if k not in obj:
            raise PackageError("%s lacks %s" % (what, k))
    out = {}
    for k, ok in fields.items():
        if k not in obj:
            continue
        if ok(obj[k]):
            out[k] = obj[k]
        elif obj[k] is not None or k in need:   # null means absent
            raise PackageError("bad %s field %s" % (what, k))
    return out


def check_package(pkg):
    if not is_job(pkg.get("id")):
        raise PackageError("bad job id")
    if pkg.get("schema") != d.SCHEMA:
        raise PackageError("unsupported package schema %r" % (pkg.get("schema"),))
    return checked(pkg, PACKAGE_FIELDS, PACKAGE_NEED, "package")


def check_state(state):
    out = checked(state, STATE_FIELDS, ("state",), "state")
    if ("final_size" in out) != (out["state"] in d.FINAL):
        raise PackageError("state and final_size disagree")
    if "final_size" in out and "sha256" not in out:
        raise PackageError("state lacks sha256")
    return out


def check_streams(doc, size, logs):
    streams, followups = doc.get("streams"), doc.get("followups")
    if not (is_int(doc.get("seq")) and isinstance(streams, list)
            and isinstance(followups, list)
            and len(streams) <= d.STREAM_MAX and len(followups) <= d.STREAM_MAX):
        raise PackageError("bad streams.json")
    out, seen = [], set()
    for s in streams:
        s = checked(s, STREAM_FIELDS,
                    ("id", "kind", "state", "name", "by", "at", "parent"), "stream")
        if s["id"] in seen or int(s["id"][1:]) > doc["seq"]:
            raise PackageError("bad stream id")
        seen.add(s["id"])
        if s["kind"] == "split":
            lo, hi = s.get("lo"), s.get("hi")
            if lo is None or lo > size or (hi is not None and not lo <= hi <= size):
                raise PackageError("bad stream range")
            s.setdefault("hi", None)
        else:
            s.pop("lo", None)
            s.pop("hi", None)
        out.append(s)
    if logs - {s["id"] for s in out if s["kind"] == "push"}:
        raise PackageError("stream log without a stream")
    return {"seq": doc["seq"], "streams": out,
            "followups": [checked(f, FOLLOWUP_FIELDS, tuple(FOLLOWUP_FIELDS),
                                  "followup") for f in followups]}


# ── Manifest ────────────────────────────────────────────────────────────────

def records(path):
    """Whole records of a manifest (a torn tail is ignored)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        raw = f.read()
    raw = raw[:len(raw) - len(raw) % d.RECORD]
    try:
        return [json.loads(raw[i:i + d.RECORD])
                for i in range(0, len(raw), d.RECORD)]
    except ValueError:
        raise PackageError("corrupt manifest")


def verify(log, recs, size):
    """Check the hash chain and every chunk; returns the whole sha256."""
    total, chain, pos, line = hashlib.sha256(), d.ZERO, 0, 1
    with open(log, "rb") as f:
        for i, r in enumerate(recs):
            try:
                ok = (all(type(r[k]) is int for k in
                          ("seq", "start", "end", "first_line", "newlines"))
                      and r["seq"] == i and r["start"] == pos
                      and r["first_line"] == line
                      and 0 < r["end"] - pos <= d.CHUNK
                      and (r["end"] - pos == d.CHUNK or i == len(recs) - 1))
                data = f.read(r["end"] - pos) if ok else b""
                digest = hashlib.sha256(data).hexdigest()
                chain = d.next_chain(chain, digest)
                ok = (ok and digest == r["sha256"] and chain == r["chain"]
                      and data.count(b"\n") == r["newlines"])
            except (KeyError, TypeError):
                ok = False
            if not ok:
                raise PackageError("manifest chunk %d does not verify" % i)
            total.update(data)
            pos, line = r["end"], line + r["newlines"]
        if f.read(1) or pos != size:
            raise PackageError("output size does not match manifest")
    return total.hexdigest()


def encode_manifest(recs):
    return b"".join(d.encode_record(r["seq"], r["start"], r["end"],
                                    r["first_line"], r["newlines"],
                                    r["sha256"], r["chain"]) for r in recs)


# ── Export ──────────────────────────────────────────────────────────────────

class ArchiveWriter:
    """Adds members to a tar while hashing them for SHA256SUMS."""

    def __init__(self, tar):
        self.tar, self.sums = tar, {}

    class _Hashing:
        def __init__(self, f):
            self.f, self.h = f, hashlib.sha256()

        def read(self, n=-1):
            data = self.f.read(n)
            self.h.update(data)
            return data

    def put(self, name, f, size):
        info = tarfile.TarInfo(name)
        info.size, info.mode = size, d.MODE_FILE
        h = self._Hashing(f)
        self.tar.addfile(info, h)
        self.sums[name] = h.h.hexdigest()

    def blob(self, name, data):
        self.put(name, io.BytesIO(data), len(data))

    def finish(self):
        self.blob(d.FILE_SUMS, "".join(
            "%s  %s\n" % (v, k) for k, v in sorted(self.sums.items())).encode())


def export(job_dir, package, dest):
    """A finished job exports whole; a live one up to its last sealed chunk."""
    pkg = check_package(load(package))
    state_path = os.path.join(job_dir, d.FILE_STATE)
    state = load(state_path) if os.path.exists(state_path) else {"state": "running"}
    recs = records(os.path.join(job_dir, d.FILE_MANIFEST))
    final = "final_size" in state
    if final:
        size = state["final_size"]
    else:
        recs = [r for r in recs if r["end"] - r["start"] == d.CHUNK]
        size = recs[-1]["end"] if recs else 0
        state.update(size=size)
    state = check_state({k: v for k, v in state.items() if k not in d.LOCAL_STATE})
    log = os.path.join(job_dir, d.FILE_OUTPUT)
    if not os.path.exists(log):
        raise PackageError("job has no output yet")
    if os.path.getsize(log) < size:
        raise PackageError("output is shorter than its manifest")
    # Unique name, then link: concurrent exports cannot collide and an
    # existing file is never replaced.
    fd, tmp = tempfile.mkstemp(prefix=".pjob-", dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as raw, \
                tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6,
                             format=tarfile.USTAR_FORMAT
                             if size < 8 << 30 else tarfile.GNU_FORMAT) as tar:
            w = ArchiveWriter(tar)
            w.blob(d.FILE_PACKAGE, json.dumps(pkg).encode())
            w.blob(d.FILE_STATE, json.dumps(state).encode())
            with open(log, "rb") as f:
                w.put(d.FILE_OUTPUT, f, size)
            w.blob(d.FILE_MANIFEST, encode_manifest(recs))
            export_notes(w, job_dir, pkg, size)
            w.finish()
        os.link(tmp, dest)
    except FileExistsError:
        raise PackageError("path already exists")
    finally:
        os.unlink(tmp)
    return {"path": dest, "bytes": os.path.getsize(dest), "output_bytes": size,
            "snapshot": not final}


def export_notes(w, job_dir, pkg, size):
    """Reviews, nested streams, and the owner's cursor."""
    reviews = os.path.join(job_dir, d.FILE_REVIEWS)
    if os.path.exists(reviews):
        with open(reviews, "rb") as f:
            w.blob(d.FILE_REVIEWS, f.read())
    streams = os.path.join(job_dir, d.FILE_STREAMS)
    if os.path.exists(streams):
        doc = load(streams)
        # A snapshot may end before a split does.
        doc["streams"] = [s for s in doc["streams"] if s["kind"] != "split" or (
            s["lo"] <= size and (s["hi"] is None or s["hi"] <= size))]
        w.blob(d.FILE_STREAMS, json.dumps(doc).encode())
        for s in doc["streams"]:
            path = os.path.join(job_dir, d.DIR_STREAMS, s["id"] + ".log")
            if s["kind"] == "push" and os.path.exists(path):
                with open(path, "rb") as f:
                    w.blob("%s/%s.log" % (d.DIR_STREAMS, s["id"]), f.read())
    watch = os.path.join(job_dir, d.FILE_WATCH)
    if os.path.exists(watch):
        w.blob(d.FILE_WATCH, json.dumps(inert_rules(load(watch))).encode())
    cursors = os.path.join(job_dir, d.FILE_CURSORS)
    if os.path.exists(cursors):
        cur = load(cursors).get(pkg["owner"])
        if cur:
            keep = {k: cur[k] for k in CURSOR_FIELDS if k in cur}
            w.blob(d.FILE_CURSORS, json.dumps({pkg["owner"]: keep}).encode())


# ── Import ──────────────────────────────────────────────────────────────────

class PlainHeaders(tarfile.TarInfo):
    """Refuses extension headers. tarfile loads those into memory before we
    see a size, and no archive we write needs them."""

    def _proc_member(self, tar):
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise PackageError("unexpected archive member type")
        return super()._proc_member(tar)


def unpack(src, staging):
    """Extract allowed members; returns {name: sha256}."""
    sums = {}
    os.makedirs(os.path.join(staging, d.DIR_STREAMS), mode=d.MODE_DIR)
    with gzip.open(src, "rb") as raw, \
            tarfile.open(fileobj=raw, mode="r|", tarinfo=PlainHeaders) as tar:
        for m in tar:
            cap = d.MEMBER_CAPS.get(m.name)
            if cap is None and STREAM_LOG.match(m.name):
                cap = d.STREAM_LOG_MAX
            if cap is None or not m.isreg() or m.name in sums \
                    or len(sums) >= d.MEMBERS_MAX:
                raise PackageError("unexpected archive member %r" % m.name[:80])
            if m.size > cap:
                raise PackageError("%s is too large" % m.name)
            if m.size > shutil.disk_usage(staging).free - d.MIN_FREE:
                raise PackageError("not enough disk space")
            sums[m.name] = extract(tar, m, os.path.join(staging, m.name))
    return sums


def extract(tar, m, path):
    f, h = tar.extractfile(m), hashlib.sha256()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, d.MODE_FILE)
    with os.fdopen(fd, "wb") as out:
        for data in iter(lambda: f.read(d.FEED_BLOCK), b""):
            h.update(data)
            out.write(data)
        out.flush()
        os.fsync(out.fileno())
    if os.path.getsize(path) != m.size:
        raise PackageError("archive is truncated")
    return h.hexdigest()


def check_sums(staging, sums):
    for need in (d.FILE_PACKAGE, d.FILE_STATE, d.FILE_OUTPUT, d.FILE_MANIFEST,
                 d.FILE_SUMS):
        if need not in sums:
            raise PackageError("archive is missing " + need)
    sums.pop(d.FILE_SUMS)
    path = os.path.join(staging, d.FILE_SUMS)
    with open(path, encoding="utf-8") as f:
        listed = dict(reversed(x.split("  ", 1)) for x in f.read().splitlines())
    if listed != sums:
        raise PackageError("SHA256SUMS does not match contents")
    os.unlink(path)


def import_(src, staging):
    """Validate an archive into *staging*, every JSON file rewritten."""
    sums = unpack(src, staging)
    check_sums(staging, sums)

    def at(name):
        return os.path.join(staging, name)
    pkg = check_package(load(at(d.FILE_PACKAGE)))
    state = check_state(load(at(d.FILE_STATE)))
    recs = records(at(d.FILE_MANIFEST))
    size = os.path.getsize(at(d.FILE_OUTPUT))
    total = verify(at(d.FILE_OUTPUT), recs, size)
    final = "final_size" in state
    if final and (state["final_size"] != size or state["sha256"] != total):
        raise PackageError("output hash does not match state")
    state["size"] = size
    save(at(d.FILE_PACKAGE), pkg)
    save(at(d.FILE_STATE), state)
    with open(at(d.FILE_MANIFEST), "wb") as f:
        f.write(encode_manifest(recs))
        f.flush()
        os.fsync(f.fileno())
    import_notes(at, sums, size)
    return {"id": pkg["id"], "final": final, "size": size, "sha256": total}


def import_notes(at, sums, size):
    logs = {m.group(1) for m in map(STREAM_LOG.match, sums) if m}
    if d.FILE_STREAMS in sums:
        save(at(d.FILE_STREAMS), check_streams(load(at(d.FILE_STREAMS)), size, logs))
    elif logs:
        raise PackageError("stream log without a stream")
    if d.FILE_WATCH in sums:
        save(at(d.FILE_WATCH), inert_rules(load(at(d.FILE_WATCH))))
    if d.FILE_CURSORS in sums:
        doc = load(at(d.FILE_CURSORS))
        if len(doc) > 1 or not all(map(is_agent, doc)):
            raise PackageError("bad cursors.json")
        out = {}
        for who, c in doc.items():
            c = checked(c, CURSOR_FIELDS, ("pos",), "cursor")
            c["pos"] = min(c["pos"], size)
            out[who] = c
        save(at(d.FILE_CURSORS), out)
    if d.FILE_REVIEWS in sums:
        with open(at(d.FILE_REVIEWS), encoding="utf-8") as f:
            lines = f.read().splitlines()
        out = [checked(parse(x, "review"), REVIEW_FIELDS, ("id", "by", "at"), "review")
               for x in lines if x.strip()]
        with open(at(d.FILE_REVIEWS), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(x) + "\n" for x in out)


# ── Merge ───────────────────────────────────────────────────────────────────

def compare(a, a_final, b, b_final):
    """Relation of package b to the package a we already have, by chain.

    identical  same chunks and completeness
    extends    a is an unfinished prefix of b
    older      b is an unfinished prefix of a
    diverged   anything else
    """
    chains_a = [r["chain"] for r in a]
    chains_b = [r["chain"] for r in b]
    if chains_a == chains_b and a_final == b_final:
        return "identical"
    if not a_final and len(b) >= len(a) and chains_b[:len(a)] == chains_a:
        return "extends"
    if not b_final and len(b) <= len(a) and chains_a[:len(b)] == chains_b:
        return "older"
    return "diverged"


def merge(old, new):
    """Compare an existing import with a newer one; when it extends ours,
    carry the notes made locally into the new directory."""
    result = compare(
        records(os.path.join(old, d.FILE_MANIFEST)),
        "final_size" in load(os.path.join(old, d.FILE_STATE)),
        records(os.path.join(new, d.FILE_MANIFEST)),
        "final_size" in load(os.path.join(new, d.FILE_STATE)))
    if result == "extends":
        keep_local_notes(old, new)
    return {"result": result}


def keep_local_notes(old, new):
    lines, path = [], os.path.join(new, d.FILE_REVIEWS)
    for p in (path, os.path.join(old, d.FILE_REVIEWS)):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                lines += [x for x in f.read().splitlines() if x not in lines]
    if lines:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if os.path.exists(os.path.join(old, d.FILE_STREAMS)):
        shutil.copy(os.path.join(old, d.FILE_STREAMS), new)
        shutil.rmtree(os.path.join(new, d.DIR_STREAMS), ignore_errors=True)
        if os.path.isdir(os.path.join(old, d.DIR_STREAMS)):
            shutil.copytree(os.path.join(old, d.DIR_STREAMS),
                            os.path.join(new, d.DIR_STREAMS))
    if os.path.exists(os.path.join(old, d.FILE_CURSORS)):
        shutil.copy(os.path.join(old, d.FILE_CURSORS), new)


def main(argv):
    try:
        fn = {"export": export, "import": import_, "merge": merge}[argv[0]]
        print(json.dumps(fn(*argv[1:])))
        return 0
    except (PackageError, OSError, EOFError, tarfile.TarError, ValueError,
            KeyError, TypeError, zlib.error) as exc:
        print(json.dumps({"error": (str(exc) or type(exc).__name__)[:500]}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
