#!/usr/bin/env python3
"""Evaluates watch rules while output arrives (stdlib only).

    pluto_job_watch.py <marker file>

One process for all watched jobs of a server, spoken to in JSON frames with
a 4-byte length on stdin and stdout. It decides nothing durable: hits and
rule states go to the server, which saves them, and after any crash a new
process continues from what was saved. Ends with its stdin.

In:  {"op": "set", "job", "path", "state_path", "rules": [{id, spec, st}]}
     {"op": "del", "job"}, {"op": "drop", "job", "rule"}, {"op": "end", "job"}
Out: {"hb": 1}, {"job", "states": {rule: st}, "hits": [...], "final": bool}
"""

import json
import mmap
import os
import select
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pluto_job_defs as d  # noqa: E402  (needs the path above)
import pluto_out_fields as fields  # noqa: E402
import pluto_out_lines as lines  # noqa: E402
from pluto_out_rules import Rule  # noqa: E402


class Job:
    def __init__(self, msg):
        self.id = self.label = msg["job"]
        self.name = msg.get("name") or msg["job"]
        self.params = msg.get("params") or {}
        self.path, self.state_path = msg["path"], msg["state_path"]
        self.rules = {}
        self.readers = {}           # (offset, line) -> [Reader, [rules]]
        self.state, self.state_sig = {}, None
        self.ended = False
        self.size = -1
        self.last_out_c = 0.0
        self.dirty = False
        self.saved = time.monotonic()
        self.add(msg["rules"])

    def add(self, rules):
        for r in rules:
            if r["id"] in self.rules:
                continue
            head = lines.head_lines(self.path, 65536)
            try:
                rule = Rule(r["id"], r["spec"], r["st"], head)
            except fields.QueryError as exc:     # checked before; but be sure
                rule = Rule(r["id"], {"cooldown_s": 0, "name": r["id"], "match": "\0",
                                      "max_hits": 1, "once": True, "count": 1,
                                      "then": "notify"}, r["st"])
                rule.disable(str(exc))
            self.rules[r["id"]] = rule

    # ── the job's own clock ──
    def refresh(self):
        """Read the runner's state when it changed, and the output size."""
        try:
            s = os.stat(self.state_path)
            sig = (s.st_mtime_ns, s.st_size, s.st_ino)
            if sig != self.state_sig:
                with open(self.state_path, encoding="utf-8") as f:
                    self.state = json.load(f)
                self.state_sig = sig
        except (OSError, ValueError):
            pass
        try:
            s = os.stat(self.path)
            size, self.mtime = s.st_size, s.st_mtime
        except OSError:
            size, self.mtime = 0, 0.0
        final = self.state.get("final_size")
        return size if final is None else min(size, final)

    def clock(self, wall=None):
        """Seconds the job has run, without pauses; None before it starts."""
        st = self.state
        started = st.get("started_at")
        if started is None:
            return None
        ms = (wall if wall is not None else time.time()) * 1000
        for stop in (st.get("paused_since"), st.get("ended_at")):
            if stop is not None:
                ms = min(ms, stop)
        return max(0.0, (ms - started - st.get("paused_ms", 0)) / 1000)

    def over(self):
        return self.ended or "final_size" in self.state

    def last_line(self):
        """(number or None, text) of the last line written."""
        try:
            with open(self.path, "rb") as f:
                f.seek(max(0, self.size - 4096))
                data = f.read(4096).rstrip(b"\n")
        except OSError:
            return None, ""
        n = None
        for r in self.rules.values():
            if r.by_lines and r.st["offset"] >= self.size:
                n = max(1, r.st["line"] - 1)
        return n, fields.clean(data.rsplit(b"\n", 1)[-1])


class Watcher:
    def __init__(self, marker):
        self.jobs = {}
        self.out = sys.stdout.buffer
        self.buf = b""
        self.guard = lines.CpuGuard(d.WATCH_RULE_CPU_S)
        fd = os.open(marker, os.O_CREAT | os.O_RDWR, d.MODE_FILE)
        os.ftruncate(fd, 4096)
        self.mark = mmap.mmap(fd, 4096)      # read by the server if we hang
        os.close(fd)

    # ── frames ──
    def send(self, obj):
        data = json.dumps(obj).encode()
        self.out.write(struct.pack(">I", len(data)) + data)
        self.out.flush()

    def receive(self, timeout):
        ready, _, _ = select.select([0], [], [], timeout)
        if not ready:
            return
        data = os.read(0, 1 << 20)
        if not data:
            os._exit(0)
        self.buf += data
        while len(self.buf) >= 4:
            n = struct.unpack(">I", self.buf[:4])[0]
            if len(self.buf) < 4 + n:
                break
            msg, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
            self.handle(json.loads(msg))

    def handle(self, msg):
        op, job = msg["op"], self.jobs.get(msg["job"])
        if op == "set":
            if job is None:
                self.jobs[msg["job"]] = Job(msg)
            else:
                job.add(msg["rules"])
        elif op == "del":
            self.jobs.pop(msg["job"], None)
        elif op == "drop" and job is not None:
            job.rules.pop(msg["rule"], None)
        elif op == "end" and job is not None:
            job.ended = True

    # ── one job, one turn ──
    def turn(self, job):
        """True when there is more to read right away."""
        size, over = job.refresh(), job.over()
        c = job.clock()
        if c is None and not over:
            return False
        c = c or 0.0
        for r in job.rules.values():
            if r.st["c"] is None:            # added "from now"
                r.st["c"] = r.st["from_c"] = c
        if size != job.size:
            if size < job.size:
                self.restart(job)
            at = job.clock(job.mtime)
            job.last_out_c = min(c, at) if at is not None else c
            if job.size < 0:
                # Picked up again: a pause since then must not count as quiet.
                for r in job.rules.values():
                    if r.st.get("size") == size:
                        job.last_out_c = max(job.last_out_c, r.st["out_c"])
            job.size = size
            job.dirty = True
        more = self.read(job, size, over, c)
        for r in job.rules.values():
            if not r.done:
                self.guarded(job, r, lambda r=r: r.tick(
                    c, size, job.last_out_c, job.last_line,
                    not over and (not r.by_lines or r.st["offset"] >= size
                                  or not more), job))
                if over and not more:
                    r.end(c)
        hits = [h for r in job.rules.values() for h in r.take()]
        final = over and not more
        now = time.monotonic()
        if hits or final or job.dirty and now - job.saved > d.WATCH_CHECKPOINT_S:
            self.send({"job": job.id, "hits": hits, "final": final,
                       "states": {r.id: r.st for r in job.rules.values()}})
            job.dirty, job.saved = False, now
            if final:
                del self.jobs[job.id]
                return False
        return more

    def restart(self, job):
        """The command truncated its own output: start over with what is left."""
        job.readers.clear()
        for r in job.rules.values():
            r.st["offset"], r.st["line"] = 0, 1
        job.dirty = True

    def read(self, job, size, over, c):
        """Give each rule its next block. Rules at one position share it."""
        at, more = {}, False
        for r in job.rules.values():
            if r.by_lines and not r.done:
                if r.st["offset"] is None:         # from_now: decided here
                    self.from_now(job, r, size, c)
                at.setdefault((r.st["offset"], r.st["line"]), []).append(r)
        readers, job.readers = job.readers, {}
        c_end = min(c, job.clock(job.mtime) or 0.0) if job.mtime else c
        for pos, rules in at.items():
            if pos[0] >= size:
                continue
            rd = readers.get(pos) or lines.Reader(job.path, pos[0], size, pos[1], over)
            rd.hi, rd.closed = size, over
            try:
                block = rd.block(d.WATCH_BLOCK)
            except OSError:
                continue
            if block is None:
                job.readers[pos] = rd
                continue
            if rd.pos + rd.held < size or over and rd.held:
                more = True
            job.dirty = True
            span = max(1, size - pos[0])
            texts = [(i, o, fields.clean(raw)) for i, (o, raw) in enumerate(block)]
            for r in rules:
                # Lines found late arrived some time since the last look.
                c0 = min(r.st["c"], c_end)
                rate = (c_end - c0) / span
                self.guarded(job, r, lambda r=r, c0=c0, rate=rate: self.feed(
                    job, r, texts, pos, c0, rate))
                r.st["offset"], r.st["line"] = rd.pos, pos[1] + len(block)
                r.st["c"] = round(c0 + rate * (rd.pos - pos[0]), 3)
            job.readers[rd.pos, pos[1] + len(block)] = rd
        return more

    @staticmethod
    def feed(job, r, texts, pos, c0, rate):
        line = pos[1]
        for i, offset, text in texts:
            if text:
                r.feed(text, line + i, offset, c0 + rate * (offset - pos[0]), job)
                if r.done:
                    return

    def from_now(self, job, r, size, c):
        """Skip what is there; whole lines only."""
        offset, line = 0, 1
        rd = lines.Reader(job.path, 0, size, 1, False)
        while size:
            block = rd.block(1 << 20)
            if block is None:
                break
            offset, line = rd.pos, line + len(block)
        r.st.update(offset=offset, line=line, c=c, from_c=c)

    def guarded(self, job, rule, work):
        """Run a piece of a rule's work; a rule that misbehaves is disabled
        and nothing else notices."""
        tag = ("%s %s" % (job.id, rule.id)).encode()[:120]
        self.mark[0:128] = tag.ljust(128)
        self.guard.mark += 1
        try:
            try:
                self.guard.armed = True
                work()
            finally:
                self.guard.armed = False
        except lines.TooSlow:
            rule.disable("pattern too slow (backtracking)")
        except MemoryError:
            rule.disable("needs too much memory")
        except Exception as exc:
            rule.disable("internal error: %s" % str(exc)[:200])
        self.mark[0:128] = b" " * 128

    def run(self):
        beat = 0.0
        more = False
        while True:
            self.receive(0 if more else d.WATCH_TICK_S)
            now = time.monotonic()
            if now - beat > d.WATCH_HEARTBEAT_S:
                beat = now
                self.send({"hb": 1})
            more = False
            for job in list(self.jobs.values()):
                more = self.turn(job) or more


def main(argv):
    lines.confine(d.WATCH_MEMORY, file_size=4096)
    Watcher(argv[0]).run()


if __name__ == "__main__":
    main(sys.argv[1:])
