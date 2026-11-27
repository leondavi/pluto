"""Workspace construction shared by the three experiments."""

import json
import os
import py_compile
import shlex
import stat
import sys
import tempfile

import wl_common

HERE = os.path.dirname(os.path.abspath(__file__))
IMPORT = "from wl_common import "


def write(ws, rel, text):
    path = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return None


def install(ws, prog, module, images):
    """Compile wl_common + module to bin/<prog>.pyc, plus a wrapper and images."""
    bindir = os.path.join(ws, "bin")
    os.makedirs(os.path.join(bindir, "img"), exist_ok=True)
    for d in ("answers", "board"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    src = ""
    for name in ("wl_common", module):
        with open(os.path.join(HERE, name + ".py"), encoding="utf-8") as f:
            src += "\n" + "\n".join(x for x in f.read().splitlines()
                                    if not x.startswith(IMPORT)) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        s = os.path.join(tmp, prog + ".py")
        with open(s, "w", encoding="utf-8") as f:
            f.write(src)
        pyc = os.path.join(bindir, prog + ".pyc")
        py_compile.compile(s, cfile=pyc, dfile=prog, optimize=2, doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    for name, params in images.items():
        with open(os.path.join(bindir, "img", name), "wb") as f:
            f.write(wl_common.seal(params))
    wrapper = os.path.join(bindir, prog)
    with open(wrapper, "w") as f:
        f.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(pyc)} \"$@\"\n")
    os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)


def launches(ws):
    """All ledger records written by workload processes, oldest first."""
    d, out = os.path.join(ws, ".runs"), []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        rec = read_json(os.path.join(d, name)) if name.endswith(".json") else None
        if isinstance(rec, dict) and "t0" in rec:
            out.append(rec)
    return sorted(out, key=lambda r: r["t0"])


def tree_digest(ws, sub="bin"):
    """{relative path: sha256} used to detect tampering with the programs."""
    import hashlib
    out = {}
    for base, _, files in os.walk(os.path.join(ws, sub)):
        for f in files:
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, ws)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def peak(intervals):
    """Largest number of simultaneously open [start, end] intervals."""
    ev = sorted([(a, 1) for a, b in intervals if b > a] + [(b, -1) for a, b in intervals if b > a])
    n = best = 0
    for _, d in ev:
        n += d
        best = max(best, n)
    return best


def peak_streams(recs, owners, meta, owner, owner2=None):
    """{agent: peak concurrent streams}. With ``owner2`` the streams alive after
    phase1_end belong to the shift-2 owner. Streams waiting at a barrier do not
    count until they began."""
    cut = meta.get("phase1_end")
    out = {o: [] for o in owners}
    for r in recs:
        a, b = r.get("began", r["t0"] if "shard" not in r else r["t_last"]), r["t_last"]
        if owner2 is None or cut is None:
            out.setdefault(owner(r), []).append((a, b))
        else:
            if a < cut:
                out.setdefault(owner(r), []).append((a, min(b, cut)))
            if b > cut:
                out.setdefault(owner2(r), []).append((max(a, cut), b))
    return {o: peak(x) for o, x in out.items()}
