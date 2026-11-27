#!/usr/bin/env python3
"""Appends lines to files for acting rules (stdlib only).

Spoken to in JSON frames with a 4-byte length on stdin and stdout:

In:  {"id": n, "root": dir, "file": path, "text": line}
Out: {"id": n} or {"id": n, "error": why}

The file must stay below *root*. Every component is opened relative to the
one before without following links, so that whatever the job does to the
tree meanwhile, nothing outside it is touched; and only regular files with
one name are written, so that a pipe cannot block and a hard link cannot
lead elsewhere. A line is written with one call, hence never interleaved.
"""

import errno
import json
import os
import stat
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pluto_job_defs as d  # noqa: E402  (needs the path above)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
         | os.O_CLOEXEC)


class Refused(Exception):
    """The request must not be carried out."""


def parts(root, path):
    """Components of *path* below *root*."""
    if os.path.isabs(path):
        path = os.path.relpath(path, root)
    out = [p for p in path.split("/") if p not in ("", ".")]
    if not out or ".." in out:
        raise Refused("file is not inside the job's directory")
    return out


def step(fd, name):
    """Open a directory below *fd*, making it if need be."""
    try:
        return os.open(name, _DIR, dir_fd=fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, dir_fd=fd)
        except FileExistsError:
            pass
        return os.open(name, _DIR, dir_fd=fd)


def append(root, path, text):
    data = text.encode("utf-8", "replace") + b"\n"
    *dirs, name = parts(root, path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for p in dirs:
            below = step(fd, p)
            os.close(fd)
            fd = below
        out = os.open(name, _FILE, 0o666, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        st = os.fstat(out)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Refused("not a plain file")
        if st.st_size + len(data) > d.ACT_FILE_MAX:
            raise Refused("file larger than %d bytes" % d.ACT_FILE_MAX)
        if os.write(out, data) != len(data):
            raise OSError(errno.ENOSPC, "could not be written completely")
    finally:
        os.close(out)


def answer(req):
    try:
        append(req["root"], req["file"], req["text"])
        return {"id": req["id"]}
    except Refused as exc:
        why = str(exc)
    except OSError as exc:
        why = "a link or a file in its path" if exc.errno in (
            errno.ELOOP, errno.ENOTDIR, errno.EMLINK) else exc.strerror or str(exc)
    return {"id": req["id"], "error": "%s: %s" % (req["file"][:200], why)}


def main():
    src, out = sys.stdin.buffer, sys.stdout.buffer
    while True:
        head = src.read(4)
        if len(head) < 4:
            return
        body = src.read(struct.unpack(">I", head)[0])
        data = json.dumps(answer(json.loads(body))).encode()
        out.write(struct.pack(">I", len(data)) + data)
        out.flush()


if __name__ == "__main__":
    main()
