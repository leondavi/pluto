"""Runtime shared by the workload programs.

The builder concatenates this file in front of one ``wl_<name>.py`` and
compiles the result to a single ``.pyc``, so a workspace contains neither
sources nor readable parameters. Standard library only.
"""

import hashlib
import json
import os
import signal
import sys
import time
import zlib

_PAD = b"cell-image-v1"


def _xor(data):
    out, n = bytearray(), 0
    while len(out) < len(data):
        out += hashlib.sha256(_PAD + n.to_bytes(8, "big")).digest()
        n += 1
    return bytes(a ^ b for a, b in zip(data, out))


def seal(obj):
    """Pack parameters into an opaque image."""
    return _xor(zlib.compress(json.dumps(obj, sort_keys=True).encode()))


def unseal(blob):
    return json.loads(zlib.decompress(_xor(blob)))


def load_image(name):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "img", name), "rb") as f:
        return unseal(f.read())


def _runs_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), ".runs")


def _pid_alive(pid):
    try:
        if pid:
            os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def live_labels(max_age=4.0, count_done=True):
    """Labels of launches that are alive now (or, optionally, finished)."""
    out, now = [], time.time()
    for name in os.listdir(_runs_dir()) if os.path.isdir(_runs_dir()) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_runs_dir(), name)) as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if r["done"]:
            alive = count_done
        else:
            alive = now - r["t_last"] < max_age and not r.get("ended") and _pid_alive(r.get("pid"))
        if alive:
            out.append(r["label"])
    return out


class Ledger:
    """Heartbeat file per launch: lets the grader measure stream-seconds, peak
    concurrency, reaction times and continuity, even after a SIGKILL."""

    def __init__(self, label, **extra):
        os.makedirs(_runs_dir(), exist_ok=True)
        self.path = os.path.join(_runs_dir(), f"{label}-{os.getpid()}-{time.time_ns()}.json")
        now = time.time()
        self.rec = dict(extra, label=label, t0=now, t_last=now, done=False, lag=0.0, cpu=0.0,
                        pid=os.getpid())
        self._write()
        signal.signal(signal.SIGTERM, self._term)

    def _term(self, *_):
        self.rec.update(t_last=time.time(), ended=True)
        self._write()
        os._exit(143)

    def _write(self):
        self.rec["cpu"] = round(time.process_time(), 3)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.rec, f)
        os.replace(tmp, self.path)

    def mark(self, **kw):
        self.rec.update(kw, t_last=time.time())
        self._write()

    def beat(self, done=False):
        now = time.time()
        if done or now - self.rec["t_last"] >= 1.0:
            self.rec["t_last"], self.rec["done"] = now, done
            self._write()


def say(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run_paced(items, duration, ledger, handlers=None, tick=None):
    """Print (fraction, line) pairs so that ``fraction`` tracks wall time.

    A non-string item (name, args...) calls handlers[name](*args) on schedule;
    the seconds it returns are excluded from the schedule. ``tick`` is called
    about twice a second. The largest schedule lag goes to the ledger."""
    t0 = time.time()
    out, last_tick = sys.stdout, 0.0
    try:
        for i, (frac, line) in enumerate(items):
            event = not isinstance(line, str)
            if i % 32 == 0 or event:
                while True:
                    now = time.time()
                    ahead = t0 + frac * duration - now
                    if tick and now - last_tick >= 0.5:
                        last_tick = now
                        tick()
                    if ahead <= 0.02:
                        if duration:
                            ledger.rec["lag"] = round(max(ledger.rec["lag"], -ahead), 3)
                        break
                    out.flush()
                    time.sleep(min(ahead, 0.5))
                    ledger.beat()
                ledger.beat()
            if event:
                out.flush()
                t0 += handlers[line[0]](*line[1:]) or 0.0
            else:
                out.write(line + "\n")
        out.flush()
    except BrokenPipeError:
        os._exit(141)
