#!/usr/bin/env python3
"""Portable .pjob archives for PlutoSmartStreams (stdlib only).

    pluto_job_package.py export <job_dir> <package.json> <dest.pjob>
    pluto_job_package.py import <src.pjob> <staging_dir>
    pluto_job_package.py merge  <old_dir> <new_dir>

Each prints one JSON object. Archives are untrusted input: import accepts
only known regular files, caps sizes, verifies every hash, and never runs
anything from the package.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile

CHUNK, RECORD, ZERO = 65536, 256, "0" * 64
MEMBERS = {
    "package.json": 1 << 20, "state.json": 1 << 20,
    "output.log": 16 << 30, "manifest.jsonl": 128 << 20,
    "reviews.jsonl": 16 << 20, "streams.json": 16 << 20,
    "cursors.json": 1 << 20, "SHA256SUMS": 1 << 20,
}
STREAM = re.compile(r"\Astreams/S[0-9]{1,6}\.log\Z")
STREAM_MAX = 64 << 20
MAX_MEMBERS = 1100
JOB_ID = re.compile(r"\AJ-[0-9]{8}-[0-9a-f]{16}\Z")
LOCAL_STATE = ("runner_pid", "runner_ident", "pid", "pgid", "child_ident", "_pub")


class Bad(Exception):
    pass


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError, RecursionError) as exc:
        raise Bad("unreadable %s: %s" % (os.path.basename(path), exc))
    if not isinstance(obj, dict):
        raise Bad("%s is not an object" % os.path.basename(path))
    return obj


def records(path):
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) % RECORD:
        raw = raw[:len(raw) - len(raw) % RECORD]
    try:
        return [json.loads(raw[i:i + RECORD]) for i in range(0, len(raw), RECORD)]
    except ValueError:
        raise Bad("corrupt manifest")


def verify(log, recs, size):
    """Check the hash chain and every chunk; returns the whole sha256."""
    total, chain, pos, line = hashlib.sha256(), ZERO, 0, 1
    with open(log, "rb") as f:
        for i, r in enumerate(recs):
            try:
                ok = (r["seq"] == i and r["start"] == pos
                      and r["first_line"] == line
                      and 0 < r["end"] - pos <= CHUNK
                      and (r["end"] - pos == CHUNK or i == len(recs) - 1))
                data = f.read(r["end"] - pos) if ok else b""
                digest = hashlib.sha256(data).hexdigest()
                chain = hashlib.sha256((chain + digest).encode()).hexdigest()
                ok = (ok and digest == r["sha256"] and chain == r["chain"]
                      and data.count(b"\n") == r["newlines"])
            except (KeyError, TypeError):
                ok = False
            if not ok:
                raise Bad("manifest chunk %d does not verify" % i)
            total.update(data)
            pos, line = r["end"], line + r["newlines"]
        if f.read(1) or pos != size:
            raise Bad("output size does not match manifest")
    return total.hexdigest()


def export(job_dir, package, dest):
    pkg = load(package)
    state_path = os.path.join(job_dir, "state.json")
    state = load(state_path) if os.path.exists(state_path) else {}
    for k in LOCAL_STATE:
        state.pop(k, None)
    recs = records(os.path.join(job_dir, "manifest.jsonl"))
    if "final_size" in state:
        size = state["final_size"]
    else:
        recs = [r for r in recs if r["end"] - r["start"] == CHUNK]
        size = recs[-1]["end"] if recs else 0
        state.update(snapshot=True, size=size)
    log = os.path.join(job_dir, "output.log")
    if not os.path.exists(log):
        raise Bad("job has no output yet")
    sums = {}
    tmp = dest + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, \
                tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
            class Hashing:
                def __init__(self, f):
                    self.f, self.h = f, hashlib.sha256()

                def read(self, n=-1):
                    d = self.f.read(n)
                    self.h.update(d)
                    return d

            def put(name, f, n):
                info = tarfile.TarInfo(name)
                info.size, info.mode = n, 0o600
                h = Hashing(f)
                tar.addfile(info, h)
                sums[name] = h.h.hexdigest()

            def blob(name, data):
                put(name, io.BytesIO(data), len(data))
            blob("package.json", json.dumps(pkg).encode())
            blob("state.json", json.dumps(state).encode())
            with open(log, "rb") as f:
                put("output.log", f, size)
            blob("manifest.jsonl", b"".join(
                json.dumps(r, separators=(",", ":")).ljust(RECORD - 1).encode()
                + b"\n" for r in recs))
            for name in ("reviews.jsonl", "streams.json", "cursors.json"):
                path = os.path.join(job_dir, name)
                if not os.path.exists(path):
                    continue
                if name == "cursors.json":
                    cur = load(path).get(pkg.get("owner", ""))
                    blob(name, json.dumps({pkg["owner"]: cur} if cur else {}).encode())
                else:
                    with open(path, "rb") as f:
                        blob(name, f.read())
            sdir = os.path.join(job_dir, "streams")
            for n in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
                if STREAM.match("streams/" + n):
                    with open(os.path.join(sdir, n), "rb") as f:
                        blob("streams/" + n, f.read())
            blob("SHA256SUMS", "".join(
                "%s  %s\n" % (v, k) for k, v in sorted(sums.items())).encode())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"path": dest, "bytes": os.path.getsize(dest), "output_bytes": size,
            "snapshot": "final_size" not in state}


def import_(src, staging):
    os.makedirs(os.path.join(staging, "streams"), mode=0o700)
    sums = {}
    with tarfile.open(src, mode="r|gz") as tar:
        for n, m in enumerate(tar):
            cap = MEMBERS.get(m.name)
            if cap is None and STREAM.match(m.name):
                cap = STREAM_MAX
            if cap is None or not m.isreg() or m.name in sums or n > MAX_MEMBERS:
                raise Bad("unexpected archive member %r" % m.name[:80])
            if m.size > cap:
                raise Bad("%s is too large" % m.name)
            if m.size > shutil.disk_usage(staging).free - (256 << 20):
                raise Bad("not enough disk space")
            f, h = tar.extractfile(m), hashlib.sha256()
            path = os.path.join(staging, m.name)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as out:
                while True:
                    d = f.read(1 << 20)
                    if not d:
                        break
                    h.update(d)
                    out.write(d)
            if os.path.getsize(path) != m.size:
                raise Bad("archive is truncated")
            sums[m.name] = h.hexdigest()
    for need in ("package.json", "state.json", "output.log", "manifest.jsonl",
                 "SHA256SUMS"):
        if need not in sums:
            raise Bad("archive is missing " + need)
    sums.pop("SHA256SUMS")
    with open(os.path.join(staging, "SHA256SUMS"), encoding="utf-8") as f:
        listed = dict(reversed(x.split("  ", 1)) for x in f.read().splitlines())
    if listed != sums:
        raise Bad("SHA256SUMS does not match contents")
    pkg = load(os.path.join(staging, "package.json"))
    state = load(os.path.join(staging, "state.json"))
    if not (isinstance(pkg.get("id"), str) and JOB_ID.match(pkg["id"])):
        raise Bad("bad job id")
    if pkg.get("schema") != 1:
        raise Bad("unsupported package schema %r" % (pkg.get("schema"),))
    for key in ("owner", "opener", "user"):
        if not isinstance(pkg.get(key), str):
            raise Bad("bad package field " + key)
    if not isinstance(pkg.get("opened_at"), int):
        raise Bad("bad package field opened_at")
    for k in LOCAL_STATE:
        state.pop(k, None)
    final = "final_size" in state
    for key in ("size", "final_size", "started_at", "ended_at", "paused_ms",
                "last_output_at"):
        if key in state and not (isinstance(state[key], int) and state[key] >= 0):
            raise Bad("bad state field " + key)
    if not isinstance(state.get("state", ""), str):
        raise Bad("bad state field state")
    for name in ("streams.json", "cursors.json"):
        if name in sums:
            load(os.path.join(staging, name))
    recs = records(os.path.join(staging, "manifest.jsonl"))
    size = os.path.getsize(os.path.join(staging, "output.log"))
    total = verify(os.path.join(staging, "output.log"), recs, size)
    if final and (state["final_size"] != size or state.get("sha256") != total):
        raise Bad("output hash does not match state")
    with open(os.path.join(staging, "state.json"), "w") as f:
        json.dump(state, f)
    os.unlink(os.path.join(staging, "SHA256SUMS"))
    return {"id": pkg["id"], "final": final, "size": size, "sha256": total,
            "chain": recs[-1]["chain"] if recs else ZERO}


def merge(old, new):
    """Compare an existing import with a newer one. Local notes survive."""
    a = records(os.path.join(old, "manifest.jsonl"))
    b = records(os.path.join(new, "manifest.jsonl"))
    a_final = "final_size" in load(os.path.join(old, "state.json"))
    b_final = "final_size" in load(os.path.join(new, "state.json"))
    if [r["chain"] for r in a] == [r["chain"] for r in b] and a_final == b_final:
        return {"result": "identical"}
    if a_final or len(b) < len(a) or \
            any(x["chain"] != y["chain"] for x, y in zip(a, b)):
        shorter = not a_final or not b_final
        ok = shorter and len(b) <= len(a) and all(
            x["chain"] == y["chain"] for x, y in zip(a, b))
        return {"result": "older" if ok else "diverged"}
    path_a = os.path.join(old, "reviews.jsonl")
    path_b = os.path.join(new, "reviews.jsonl")
    lines = []
    for p in (path_b, path_a):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                lines += [x for x in f.read().splitlines() if x not in lines]
    if lines:
        with open(path_b, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if os.path.exists(os.path.join(old, "streams.json")):
        shutil.copy(os.path.join(old, "streams.json"), new)
        shutil.rmtree(os.path.join(new, "streams"), ignore_errors=True)
        if os.path.isdir(os.path.join(old, "streams")):
            shutil.copytree(os.path.join(old, "streams"),
                            os.path.join(new, "streams"))
    if os.path.exists(os.path.join(old, "cursors.json")):
        shutil.copy(os.path.join(old, "cursors.json"), new)
    return {"result": "extends"}


def main(argv):
    try:
        fn = {"export": export, "import": import_, "merge": merge}[argv[0]]
        print(json.dumps(fn(*argv[1:])))
        return 0
    except (Bad, OSError, EOFError, tarfile.TarError, ValueError) as exc:
        print(json.dumps({"error": str(exc) or type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
