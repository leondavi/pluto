"""Bounded line reading of job output, and CPU limits for patterns."""

import os
import resource
import signal
import sys
import threading

import pluto_job_defs as d


class Reader:
    """Complete lines of bytes [pos, hi) of a file. A line longer than
    LINE_MAX keeps only its start, whatever its length, in constant memory.
    """

    def __init__(self, path, pos, hi, line, closed):
        self.path = path
        self.pos = pos              # offset of the first byte not yet given out
        self.hi = hi
        self.line = line            # number of the line at pos, None if unknown
        self.closed = closed        # an unterminated last line counts
        self.long_lines = 0
        self.carry = b""            # start of a line not complete yet
        self.held = 0               # bytes of that line read so far

    def block(self, size):
        """[(offset, line bytes)] of about *size* bytes; None at the end."""
        left = self.hi - self.pos - self.held
        data = b""
        if left > 0:
            # Opened per block: hundreds of jobs would exhaust descriptors.
            with open(self.path, "rb", 0) as f:
                f.seek(self.pos + self.held)
                data = f.read(min(size, left))
        if not data:
            if not (self.closed and self.held):
                return None
            out = [(self.pos, self.carry)]
            self.pos += self.held
            self.carry, self.held = b"", 0
            return out
        parts = data.split(b"\n")
        out = []
        for part in parts[:-1]:
            held = self.held + len(part)
            text = self.carry + part if self.held else part
            if held > d.LINE_MAX:
                self.long_lines += 1
                text = text[:d.LINE_MAX]
            out.append((self.pos, text))
            self.pos += held + 1
            if self.held:
                self.carry, self.held = b"", 0
        tail = parts[-1]
        if tail:
            if len(self.carry) < d.LINE_MAX:
                self.carry = (self.carry + tail)[:d.LINE_MAX]
            self.held += len(tail)
        return out


def head_lines(path, hi, count=200, size=65536):
    """First lines of the file as text, for format detection."""
    from pluto_out_fields import clean
    try:
        with open(path, "rb") as f:
            data = f.read(min(size, hi))
    except OSError:
        return []
    lines = data.split(b"\n")
    if len(data) < hi:
        lines = lines[:-1] or lines
    return [clean(x) for x in lines[:count]]


class TooSlow(Exception):
    """A pattern burned its CPU allowance on one piece of input."""


class CpuGuard:
    """Interrupts matching that takes too long.

    A repeating timer of *consumed CPU* looks whether the marker changed
    since the last tick, so the normal path pays one assignment per piece.
    The regex engine polls for signals, which is what makes this work.
    """

    def __init__(self, limit_s, step_s=0.1):
        self.mark = 0
        self.armed = False
        self._last, self._same = None, 0
        self._max = max(1, int(limit_s / step_s))
        signal.signal(signal.SIGVTALRM, self._tick)
        signal.setitimer(signal.ITIMER_VIRTUAL, step_s, step_s)

    def _tick(self, *_):
        if not self.armed or self.mark != self._last:
            self._last, self._same = self.mark, 0
            return
        self._same += 1
        if self._same >= self._max:
            self._same = 0
            raise TooSlow()

    def stop(self):
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)


def confine(memory, cpu_s=None, wall_s=None, file_size=0):
    """Limits on this process, enforced by the kernel where it can. macOS
    ignores RLIMIT_AS, hence the watching thread."""
    os.nice(10)
    if cpu_s:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory * 2, memory * 2))
    except (ValueError, OSError):
        pass
    if wall_s:
        # Default action: the kernel ends us even inside C code.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(wall_s)
    unit = 1 if sys.platform == "darwin" else 1024
    stop = threading.Event()

    def watch():
        while not stop.wait(0.25):
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit > memory:
                os._exit(d.EXIT_MEMORY)
    threading.Thread(target=watch, daemon=True).start()
