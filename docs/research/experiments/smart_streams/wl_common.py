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


class Ledger:
    """Heartbeat file per launch: lets the grader measure compute-seconds and
    launch continuity, even for a process that was SIGKILLed."""

    def __init__(self, label, **extra):
        here = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(os.path.dirname(here), ".runs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, f"{label}-{os.getpid()}-{time.time_ns()}.json")
        now = time.time()
        self.rec = dict(extra, label=label, t0=now, t_last=now, done=False)
        self._write()

    def _write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.rec, f)
        os.replace(tmp, self.path)

    def beat(self, done=False):
        now = time.time()
        if done or now - self.rec["t_last"] >= 1.0:
            self.rec["t_last"], self.rec["done"] = now, done
            self._write()


def run_paced(items, duration, ledger):
    """Print (fraction, line) pairs so that ``fraction`` tracks wall time."""
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    t0 = time.time()
    out = sys.stdout
    try:
        for i, (frac, line) in enumerate(items):
            out.write(line + "\n")
            if i % 32 == 0:
                ahead = t0 + frac * duration - time.time()
                while ahead > 0.02:
                    out.flush()
                    time.sleep(min(ahead, 0.5))
                    ledger.beat()
                    ahead = t0 + frac * duration - time.time()
                ledger.beat()
        out.flush()
    except BrokenPipeError:
        os._exit(141)
