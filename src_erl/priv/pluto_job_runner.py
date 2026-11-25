#!/usr/bin/env python3
"""Detached supervisor for one PlutoSmartStreams job (stdlib only).

    pluto_job_runner.py run <job_dir>       spec JSON on stdin, prints "ready"
    pluto_job_runner.py ident <pid>         prints a process identity string
    pluto_job_runner.py kill <pid> <ident> <signum>
                                            signal the process group of <pid>,
                                            only if its identity still matches

The command writes straight into ``output.log``, so neither a server nor a
runner crash interrupts it. This process only tails that file (to build the
chunk manifest), applies limits, obeys ``control/`` requests and publishes
``state.json``. It is the unreaped parent of the command, so the signals it
sends can never reach a recycled pid.
"""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

CHUNK = 65536
RECORD = 256                       # fixed-width manifest line, incl. newline
TICK_S = 0.1
DRAIN_S = 0.5
GRACE_S = 5.0
READ_PER_TICK = 8 * 1024 * 1024
MAX_STDIN_BUFFER = 1024 * 1024
ZERO = "0" * 64
SIGNALS = {"INT", "TERM", "HUP", "USR1", "USR2", "QUIT", "KILL", "STOP", "CONT"}


def now_ms():
    return int(time.time() * 1000)


def identity(pid):
    """String that changes if *pid* is recycled; '' if not alive."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            fields = f.read().rpartition(b")")[2].split()
        if fields[0] in (b"Z", b"X"):
            return ""
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() + ":" + fields[19].decode()
    except OSError:
        pass
    if os.path.isdir("/proc/self"):
        return ""
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    out = subprocess.run(["ps", "-o", "state=,lstart=", "-p", str(pid)],
                         env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL).stdout.decode().split()
    if not out or out[0].startswith("Z"):
        return ""
    return " ".join(out[1:])


def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Manifest:
    """Hash-chained, fixed-width chunk records over output.log."""

    def __init__(self, job_dir):
        self.src = open(os.path.join(job_dir, "output.log"), "rb")
        self.out = open(os.path.join(job_dir, "manifest.jsonl"), "ab")
        self.total = hashlib.sha256()
        self.buf = b""
        self.pos = 0            # bytes hashed into self.total / buf
        self.seq = 0
        self.lines = 0
        self.chain = ZERO

    def feed(self, limit, budget=READ_PER_TICK):
        """Consume up to *budget* bytes below offset *limit*."""
        while budget > 0 and self.pos < limit:
            data = self.src.read(min(1 << 20, limit - self.pos, budget))
            if not data:
                break
            budget -= len(data)
            self.pos += len(data)
            self.total.update(data)
            self.buf += data
            while len(self.buf) >= CHUNK:
                self._seal(self.buf[:CHUNK])
                self.buf = self.buf[CHUNK:]
        self.out.flush()
        return self.pos >= limit

    def _seal(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.chain = hashlib.sha256(
            (self.chain + digest).encode()).hexdigest()
        start = self.seq * CHUNK
        newlines = data.count(b"\n")
        rec = json.dumps({"seq": self.seq, "start": start,
                          "end": start + len(data),
                          "first_line": self.lines + 1, "newlines": newlines,
                          "sha256": digest, "chain": self.chain},
                         separators=(",", ":"))
        self.out.write(rec.ljust(RECORD - 1).encode() + b"\n")
        self.lines += newlines
        self.seq += 1

    def finish(self):
        if self.buf:
            self._seal(self.buf)
            self.buf = b""
        self.out.flush()
        os.fsync(self.out.fileno())
        return self.total.hexdigest()


class Runner:
    def __init__(self, job_dir, spec, umask):
        self.dir = job_dir
        self.spec = spec
        self.umask = umask
        self.control = os.path.join(job_dir, "control")
        self.log = os.path.join(job_dir, "output.log")
        self.stdin_buf = b""
        self.stdin_close = False
        self.kill_at = None
        self.verdict = None          # why we are stopping the command
        self.paused_since = None
        self.state = {"state": "starting", "runner_pid": os.getpid(),
                      "runner_ident": identity(os.getpid()),
                      "paused_ms": 0, "size": 0}

    def publish(self):
        write_atomic(os.path.join(self.dir, "state.json"), self.state)
        self.published = time.monotonic()

    def start(self):
        self.publish()
        os.close(os.open(os.path.join(self.dir, "manifest.jsonl"),
                         os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600))
        fd = os.open(self.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        spec = self.spec
        env = dict(os.environ)
        env.update(spec.get("base_env") or {})
        env.update(spec.get("env") or {})
        env["PLUTO_JOB_ID"] = os.path.basename(self.dir)
        env.setdefault("PYTHONUNBUFFERED", "1")
        argv = spec.get("argv") or ["/bin/sh", "-c", spec["command"]]
        want_stdin = bool(spec.get("stdin"))

        def child_setup():
            os.umask(self.umask)
        try:
            self.proc = subprocess.Popen(
                argv, cwd=spec.get("cwd") or None, env=env,
                stdin=subprocess.PIPE if want_stdin else subprocess.DEVNULL,
                stdout=fd, stderr=fd, start_new_session=True,
                preexec_fn=child_setup)
        except (OSError, ValueError) as exc:
            os.write(fd, ("pluto: cannot start: %s\n" % exc).encode())
            os.close(fd)
            m = Manifest(self.dir)
            size = os.path.getsize(self.log)
            m.feed(size)
            self.state.update(state="failed", reason="start_failed: %s" % exc,
                              exit_code=127, started_at=now_ms(),
                              ended_at=now_ms(), size=size, final_size=size,
                              sha256=m.finish())
            self.publish()
            raise
        os.close(fd)
        if want_stdin:
            os.set_blocking(self.proc.stdin.fileno(), False)
        pid = self.proc.pid
        self.state.update(state="running", pid=pid, pgid=pid,
                          child_ident=identity(pid), started_at=now_ms(),
                          last_output_at=now_ms())
        self.publish()

    # ── signalling ───────────────────────────────────────────────────────
    def group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, verdict, sig=signal.SIGTERM):
        if self.verdict is None:
            self.verdict = verdict
            self.kill_at = time.monotonic() + GRACE_S
        self.group(sig)
        self.group(signal.SIGCONT)

    def pause(self):
        if self.paused_since is None and self.verdict is None:
            self.group(signal.SIGSTOP)
            self.paused_since = now_ms()
            self.state.update(state="paused", paused_since=self.paused_since)
            self.publish()

    def resume(self):
        if self.paused_since is not None:
            self.state["paused_ms"] += now_ms() - self.paused_since
            self.paused_since = None
            self.state.pop("paused_since", None)
            self.state["state"] = "running"
            self.group(signal.SIGCONT)
            self.publish()

    def handle(self, req):
        action = req.get("action")
        if action == "cancel":
            self.stop("cancelled")
        elif action == "pause":
            self.pause()
        elif action == "resume":
            self.resume()
        elif action == "signal" and req.get("signal") in SIGNALS:
            name = req["signal"]
            if name == "STOP":
                self.pause()
            elif name == "CONT":
                self.resume()
            else:
                self.group(getattr(signal, "SIG" + name))
        elif action == "input" and self.proc.stdin:
            data = base64.b64decode(req.get("data", ""))
            if len(self.stdin_buf) + len(data) <= MAX_STDIN_BUFFER:
                self.stdin_buf += data
            self.stdin_close = self.stdin_close or bool(req.get("close"))

    def read_control(self):
        try:
            names = sorted(n for n in os.listdir(self.control)
                           if n.endswith(".json"))
        except OSError:
            return
        for name in names:
            path = os.path.join(self.control, name)
            try:
                with open(path) as f:
                    req = json.load(f)
                os.unlink(path)
                self.handle(req)
            except (OSError, ValueError) as exc:
                sys.stderr.write("control %s: %s\n" % (name, exc))
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def pump_stdin(self):
        pipe = self.proc.stdin
        if not pipe:
            return
        try:
            if self.stdin_buf:
                n = os.write(pipe.fileno(), self.stdin_buf)
                self.stdin_buf = self.stdin_buf[n:]
            if self.stdin_close and not self.stdin_buf:
                pipe.close()
                self.proc.stdin = None
        except BlockingIOError:
            pass
        except OSError:
            self.proc.stdin = None
            self.stdin_buf = b""

    def limits(self, size):
        if self.verdict is None:
            if size > self.spec.get("max_output_bytes", 1 << 30):
                self.stop("output_limit")
            elif (time.monotonic() - self.disk_checked) > 5:
                self.disk_checked = time.monotonic()
                st = os.statvfs(self.dir)
                if st.f_bavail * st.f_frsize < self.spec.get("min_free_bytes", 0):
                    self.state["reason"] = "low_disk"
                    self.stop("output_limit")
            limit = self.spec.get("timeout_s")
            if limit and self.paused_since is None:
                ran = now_ms() - self.state["started_at"] - self.state["paused_ms"]
                if ran > limit * 1000:
                    self.stop("timed_out")
        elif self.kill_at and time.monotonic() > self.kill_at:
            self.group(signal.SIGKILL)
            self.kill_at = None

    # ── main loop ────────────────────────────────────────────────────────
    def loop(self):
        man = Manifest(self.dir)
        hb = os.path.join(self.dir, "hb")
        last_hb = 0.0
        self.disk_checked = 0.0
        seen = 0
        while True:
            self.read_control()
            self.pump_stdin()
            size = os.path.getsize(self.log)
            if size != seen:
                seen = size
                self.state.update(size=size, last_output_at=now_ms())
            self.limits(size)
            caught_up = man.feed(size)
            rc = self.proc.poll()
            if rc is not None:
                break
            mono = time.monotonic()
            if mono - last_hb > 1:
                with open(hb, "w") as f:
                    f.write(str(now_ms()))
                last_hb = mono
                if self.state.get("size") != self.state.get("_pub"):
                    self.state["_pub"] = self.state["size"]
                    self.publish()
            if caught_up:
                time.sleep(TICK_S)
        ended = now_ms()
        if self.verdict is not None:
            self.group(signal.SIGKILL)    # leftovers of a stopped job
        time.sleep(DRAIN_S)
        size = os.path.getsize(self.log)
        while not man.feed(size):
            pass
        if self.paused_since is not None:
            self.state["paused_ms"] += ended - self.paused_since
        for k in ("_pub", "paused_since"):
            self.state.pop(k, None)
        self.state.update(size=size, final_size=size, sha256=man.finish(),
                          ended_at=ended)
        if rc < 0:
            self.state.update(signal=signal.Signals(-rc).name, exit_code=None)
        else:
            self.state["exit_code"] = rc
        self.state["state"] = self.verdict or (
            "signaled" if rc < 0 else "succeeded" if rc == 0 else "failed")
        self.publish()


def daemonize(job_dir, spec, umask):
    """Double-fork. Returns in the grandchild with a pipe for the verdict."""
    r, w = os.pipe()
    if os.fork():
        os.close(w)
        with os.fdopen(r) as f:
            print(f.read().strip() or "error runner died", flush=True)
        os._exit(0)
    os.close(r)
    os.setsid()
    if os.fork():
        os._exit(0)
    null = os.open(os.devnull, os.O_RDWR)
    err = os.open(os.path.join(job_dir, "runner.log"),
                  os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.dup2(null, 0)
    os.dup2(null, 1)
    os.dup2(err, 2)
    runner = Runner(job_dir, spec, umask)
    try:
        runner.start()
    except Exception as exc:
        os.write(w, ("error %s" % exc).encode())
        os._exit(1)
    os.write(w, b"ready")
    os.close(w)
    runner.loop()


def main(argv):
    if argv[0] == "ident":
        print(identity(int(argv[1])))
    elif argv[0] == "kill":
        pid = int(argv[1])
        if not argv[2] or identity(pid) != argv[2]:
            print("gone")
            return 1
        sig = int(argv[3])
        os.killpg(pid, sig)
        if sig != signal.SIGKILL:
            os.killpg(pid, signal.SIGCONT)
        print("ok")
    elif argv[0] == "run":
        umask = os.umask(0o077)
        spec = json.loads(sys.stdin.buffer.readline())
        daemonize(os.path.abspath(argv[1]), spec, umask)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
