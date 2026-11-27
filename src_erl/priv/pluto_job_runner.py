#!/usr/bin/env python3
"""Detached supervisor for one PlutoSmartTasks job (stdlib only).

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
import errno
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pluto_job_defs as d  # noqa: E402  (needs the path above)


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


def write_retry(path, obj, reserve, limit_s):
    """Persist a result that must not be lost: give back the reserved disk
    space first, then keep trying."""
    end = time.monotonic() + limit_s
    while True:
        try:
            return write_atomic(path, obj)
        except OSError:
            if os.path.exists(reserve):
                os.unlink(reserve)
            elif time.monotonic() > end:
                raise
            else:
                time.sleep(1)


class Manifest:
    """Hash-chained, fixed-width chunk records over the output file.

    Records are queued in memory and written when the disk allows it, so a
    full disk delays the manifest without corrupting it.
    """

    def __init__(self, job_dir):
        self.src = open(os.path.join(job_dir, d.FILE_OUTPUT), "rb")
        self.out = os.open(os.path.join(job_dir, d.FILE_MANIFEST),
                           os.O_CREAT | os.O_APPEND | os.O_WRONLY, d.MODE_FILE)
        self.reset()

    def reset(self):
        """Start over: new job, or the command rewrote its own output."""
        os.ftruncate(self.out, 0)
        self.src.seek(0)
        self.total = hashlib.sha256()
        self.buf = b""
        self.pos = 0            # bytes hashed so far
        self.seq = 0
        self.lines = 0
        self.chain = d.ZERO
        self.pending = b""

    def feed(self, limit, budget=d.FEED_PER_TICK):
        """Consume up to *budget* bytes below offset *limit*. True when the
        manifest has caught up (or the file ended early)."""
        while budget > 0 and self.pos < limit:
            data = self.src.read(min(d.FEED_BLOCK, limit - self.pos, budget))
            if not data:
                return True
            budget -= len(data)
            self.pos += len(data)
            self.total.update(data)
            self.buf += data
            while len(self.buf) >= d.CHUNK:
                self._seal(self.buf[:d.CHUNK])
                self.buf = self.buf[d.CHUNK:]
        self._flush()
        return self.pos >= limit

    def _seal(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.chain = d.next_chain(self.chain, digest)
        start = self.seq * d.CHUNK
        newlines = data.count(b"\n")
        self.pending += d.encode_record(self.seq, start, start + len(data),
                                        self.lines + 1, newlines, digest,
                                        self.chain)
        self.lines += newlines
        self.seq += 1

    def _flush(self):
        # A short write leaves the rest pending, so records stay whole.
        try:
            while self.pending:
                n = os.write(self.out, self.pending)
                self.pending = self.pending[n:]
        except OSError as exc:
            if exc.errno not in (errno.ENOSPC, errno.EDQUOT, errno.EINTR):
                raise

    def finish(self, reserve):
        """Seal the last partial chunk; returns the whole-output sha256."""
        if self.buf:
            self._seal(self.buf)
            self.buf = b""
        self._flush()
        end = time.monotonic() + d.FINAL_WRITE_RETRY_S
        while self.pending and time.monotonic() < end:
            if os.path.exists(reserve):
                os.unlink(reserve)
            else:
                time.sleep(1)
            self._flush()
        os.fsync(self.out)
        return self.total.hexdigest()


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Feed:
    """The output of another job, read as it grows until that job has ended
    and all of it was read: the stdin of a follower. Read here, it costs no
    process and no request to the server."""

    def __init__(self, job_dir):
        self.dir = job_dir
        self.src = None
        self.pos = 0
        self.final = None            # its size once it has ended
        self.checked = 0.0

    def read(self, room):
        """Up to *room* new bytes, b"" if there are none yet, None at the end."""
        if self.src is None:
            try:
                self.src = open(os.path.join(self.dir, d.FILE_OUTPUT), "rb", 0)
            except FileNotFoundError:            # not started yet
                return None if self.ended() and self.final == 0 else b""
        data = self.src.read(room) if room > 0 else b""
        self.pos += len(data)
        if data or room <= 0:
            return data
        return None if self.ended() and self.pos >= self.final else b""

    def ended(self):
        if self.final is not None:
            return True
        if time.monotonic() - self.checked < d.HEARTBEAT_S:
            return False
        self.checked = time.monotonic()
        state = read_json(os.path.join(self.dir, d.FILE_STATE))
        if "final_size" in state:
            self.final = state["final_size"]
            return True
        package = read_json(os.path.join(self.dir, d.FILE_PACKAGE))
        if package.get("done") or not (state or package):
            # Ended without running, or lost (runner gone, no final size),
            # or removed: what was written is all there is.
            try:
                self.final = os.fstat(self.src.fileno()).st_size if self.src else 0
            except OSError:
                self.final = self.pos
        return self.final is not None


class Runner:
    """Starts one command and supervises it until its group is empty."""

    def __init__(self, job_dir, spec, umask):
        self.dir = job_dir
        self.spec = spec
        self.umask = umask
        self.control = self.path(d.DIR_CONTROL)
        self.log = self.path(d.FILE_OUTPUT)
        self.reserve = self.path(d.FILE_RESERVE)
        self.stdin_buf = b""
        self.stdin_close = False
        self.feed = Feed(spec["stdin_from"]) if spec.get("stdin_from") else None
        self.control_seen = None     # mtime of control/ when last listed
        self.disk_checked = 0.0
        self.kill_at = None          # when TERM escalates to KILL
        self.killed_at = None        # when KILL was sent
        self.verdict = None          # why we are stopping the command
        self.paused_since = None
        self.published_size = None
        self.state = {"state": "starting", "runner_pid": os.getpid(),
                      "runner_ident": identity(os.getpid()),
                      "paused_ms": 0, "size": 0}

    def path(self, name):
        return os.path.join(self.dir, name)

    def publish(self):
        """Best effort: a full disk must not end supervision."""
        try:
            write_atomic(self.path(d.FILE_STATE), self.state)
            self.published_size = self.state["size"]
        except OSError as exc:
            sys.stderr.write("state: %s\n" % exc)

    # ── start ────────────────────────────────────────────────────────────
    def start(self):
        with open(self.reserve, "wb") as f:
            f.write(b"\0" * d.RESERVE_BYTES)
        self.publish()
        os.close(os.open(self.path(d.FILE_MANIFEST),
                         os.O_CREAT | os.O_APPEND | os.O_WRONLY, d.MODE_FILE))
        fd = os.open(self.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, d.MODE_FILE)
        try:
            # The server gave up on us (very slow start): never run late.
            if os.path.exists(self.path(d.FILE_ABORT)):
                raise OSError("aborted by the server before start")
            self.proc = self.popen(fd)
        except (OSError, ValueError, TypeError) as exc:
            os.write(fd, ("pluto: cannot start: %s\n" % exc).encode())
            self.start_failed(exc)
            raise
        finally:
            os.close(fd)
        if self.proc.stdin:
            os.set_blocking(self.proc.stdin.fileno(), False)
        pid = self.proc.pid
        self.state.update(state="running", pid=pid, pgid=pid,
                          child_ident=identity(pid), started_at=now_ms(),
                          last_output_at=now_ms())
        self.publish()

    def popen(self, fd):
        spec = self.spec
        env = dict(os.environ)
        env.update(spec.get("base_env") or {})
        env.update(spec.get("env") or {})
        env["PLUTO_JOB_ID"] = os.path.basename(self.dir)
        env.setdefault("PYTHONUNBUFFERED", "1")
        argv = spec.get("argv") or ["/bin/sh", "-c", spec["command"]]
        umask = self.umask           # the command keeps the user's umask
        return subprocess.Popen(
            argv, cwd=spec.get("cwd") or None, env=env,
            stdin=subprocess.PIPE if spec.get("stdin") else subprocess.DEVNULL,
            stdout=fd, stderr=fd, start_new_session=True,
            preexec_fn=lambda: os.umask(umask))

    def start_failed(self, exc):
        man = Manifest(self.dir)
        size = os.path.getsize(self.log)
        man.feed(size)
        self.state.update(state="failed", reason="start_failed: %s" % exc,
                          exit_code=d.EXIT_CANNOT_START, started_at=now_ms(),
                          ended_at=now_ms(), size=size, final_size=size,
                          sha256=man.finish(self.reserve))
        self.save_final()

    def save_final(self):
        write_retry(self.path(d.FILE_STATE), self.state, self.reserve,
                    d.FINAL_WRITE_RETRY_S)
        if os.path.exists(self.reserve):
            os.unlink(self.reserve)

    # ── signalling ───────────────────────────────────────────────────────
    def group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def group_alive(self):
        """Members left after the leader exited. While there are any, the
        group id cannot be reused; we stop looking once there are none."""
        try:
            os.killpg(self.proc.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:      # only zombies left (macOS)
            return False

    def stop(self, verdict, sig=signal.SIGTERM):
        if self.verdict is None:
            self.verdict = verdict
            self.kill_at = time.monotonic() + d.GRACE_S
        self.group(sig)
        self.group(signal.SIGCONT)   # a stopped process cannot handle TERM

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

    # ── requests ─────────────────────────────────────────────────────────
    def handle(self, req):
        action = req.get("action")
        if action == "cancel":
            self.stop("cancelled")
        elif action == "pause":
            self.pause()
        elif action == "resume":
            self.resume()
        elif action == "signal" and req.get("signal") in d.SIGNALS:
            name = req["signal"]
            if name == "STOP":
                self.pause()
            elif name == "CONT":
                self.resume()
            else:
                self.group(getattr(signal, "SIG" + name))
        elif action == "input" and self.proc.stdin:
            self.stdin_buf += base64.b64decode(req.get("data", ""))
            self.stdin_close = self.stdin_close or bool(req.get("close"))

    def read_control(self, recheck=False):
        """Apply requests in order. Input that does not fit the buffer stays
        on disk for a later tick; cancel and pause still overtake it. True
        if there were any. Listing a directory costs far more than looking
        at its mtime, so it is listed when that changed, or on *recheck*."""
        try:
            stamp = os.stat(self.control).st_mtime_ns
            if stamp == self.control_seen and not recheck:
                return False
            self.control_seen = stamp
            names = sorted(n for n in os.listdir(self.control)
                           if n.endswith(".json"))
        except OSError:
            return False
        input_blocked = False
        for name in names:
            path = os.path.join(self.control, name)
            try:
                with open(path) as f:
                    req = json.load(f)
                if req.get("action") == "input":
                    size = len(req.get("data", "")) * 3 // 4
                    if self.stdin_buf and \
                            len(self.stdin_buf) + size > d.STDIN_BUFFER_MAX:
                        input_blocked = True
                    if input_blocked:
                        self.control_seen = None      # look again next tick
                        continue
                os.unlink(path)
                self.handle(req)
            except (OSError, ValueError, AttributeError) as exc:
                sys.stderr.write("control %s: %s\n" % (name, exc))
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return bool(names)

    def pump_stdin(self):
        """True if anything was passed on."""
        pipe = self.proc.stdin
        if not pipe:
            return False
        if self.feed and not self.stdin_close:
            data = self.feed.read(d.STDIN_BUFFER_MAX - len(self.stdin_buf))
            if data is None:
                self.stdin_close = True
            else:
                self.stdin_buf += data
        before = len(self.stdin_buf)
        try:
            while self.stdin_buf:
                n = os.write(pipe.fileno(), self.stdin_buf)
                self.stdin_buf = self.stdin_buf[n:]
            # Close only once nothing is waiting on disk either.
            if self.stdin_close:
                pipe.close()
                self.proc.stdin = None
        except BlockingIOError:
            pass
        except OSError:              # the command closed its stdin
            self.proc.stdin = None
            self.stdin_buf = b""
        return len(self.stdin_buf) != before

    # ── limits ───────────────────────────────────────────────────────────
    def limits(self, size):
        if self.verdict is not None:
            if self.kill_at and time.monotonic() > self.kill_at:
                self.group(signal.SIGKILL)
                self.kill_at = None
                self.killed_at = time.monotonic()
            return
        if size > self.spec.get("max_output_bytes", d.DEFAULT_MAX_OUTPUT):
            self.stop("output_limit")
            return
        if time.monotonic() - self.disk_checked > d.HEARTBEAT_S:
            self.disk_checked = time.monotonic()
            st = os.statvfs(self.dir)
            if st.f_bavail * st.f_frsize < self.spec.get("min_free_bytes", 0):
                self.state["reason"] = "low_disk"
                self.stop("output_limit", signal.SIGKILL)   # every MB counts
                return
        limit = self.spec.get("timeout_s")
        if limit and self.paused_since is None:
            ran = now_ms() - self.state["started_at"] - self.state["paused_ms"]
            if ran > limit * 1000:
                self.stop("timed_out")

    # ── main loop ────────────────────────────────────────────────────────
    def loop(self):
        """A job that has been quiet for a while is looked at less often:
        most of a long job's life is waiting, and dozens of jobs run at once."""
        man = Manifest(self.dir)
        last_hb = quiet_since = 0.0
        while True:
            now = time.monotonic()
            busy = self.read_control(recheck=now - last_hb > d.HEARTBEAT_S)
            busy = self.pump_stdin() or busy
            grown = self.state["size"]
            size = self.observe(man)
            self.limits(size)
            caught_up = man.feed(size)
            if self.finished():
                break
            if now - last_hb > d.HEARTBEAT_S:
                last_hb = now
                self.heartbeat()
            if busy or size != grown or self.verdict is not None or self.stdin_buf:
                quiet_since = now
            if caught_up:
                time.sleep(d.TICK_S if now - quiet_since < d.QUIET_AFTER_S else d.TICK_QUIET_S)
        self.finalize(man)

    def observe(self, man):
        """Current output size. A shrinking file means the command truncated
        its own log (on Linux, even ``> /dev/stderr`` does); say so and
        rebuild the manifest over what is left."""
        size = os.path.getsize(self.log)
        if size < man.pos:
            self.state["output_truncated"] = True
            man.reset()
        if size != self.state["size"]:
            self.state.update(size=size, last_output_at=now_ms())
        return size

    def finished(self):
        """The job ends when the whole group is gone, so background children
        stay under limits and cancel. After KILL we wait only briefly."""
        rc = self.proc.poll()
        if rc is None:
            return False
        if "leader_exited_at" not in self.state:
            self.state["leader_exited_at"] = now_ms()
            self.state["leader_exit_code"] = rc
        if not self.group_alive():
            return True
        return (self.killed_at is not None
                and time.monotonic() - self.killed_at > d.KILLED_S)

    def heartbeat(self):
        try:
            with open(self.path(d.FILE_HB), "w") as f:
                f.write(str(now_ms()))
        except OSError:
            pass
        if self.state["size"] != self.published_size or \
                "leader_exited_at" in self.state:
            self.publish()

    def finalize(self, man):
        ended = now_ms()
        rc = self.proc.returncode
        if self.verdict is not None:
            self.group(signal.SIGKILL)
        # Writes already in flight land before the size is fixed.
        time.sleep(d.DRAIN_S)
        size = self.observe(man)
        while not man.feed(size):
            pass
        size = man.pos
        if self.paused_since is not None:
            self.state["paused_ms"] += ended - self.paused_since
            self.state.pop("paused_since", None)
        self.state.update(size=size, final_size=size, ended_at=ended,
                          sha256=man.finish(self.reserve))
        self.state.pop("leader_exit_code", None)
        if rc < 0:
            self.state.update(signal=signal.Signals(-rc).name, exit_code=None)
        else:
            self.state["exit_code"] = rc
        self.state["state"] = self.verdict or (
            "signaled" if rc < 0 else "succeeded" if rc == 0 else "failed")
        self.save_final()


def daemonize(job_dir, spec, umask):
    """Detach fully (own session, no controlling tty, reparented) so neither
    the server nor its port can take the job down. The pipe carries one line
    back: "ready", or why the command could not start."""
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
    err = os.open(os.path.join(job_dir, d.FILE_RUNNER_LOG),
                  os.O_CREAT | os.O_APPEND | os.O_WRONLY, d.MODE_FILE)
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
        # The prefix tells "dead" (empty) from "the helper did not work".
        print("ident:" + identity(int(argv[1])))
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
