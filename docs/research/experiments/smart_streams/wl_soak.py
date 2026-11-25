"""Workload 3: long soak of an open-addressing hash map with tombstones.

The run id is drawn at launch and steers the whole run, so a relaunched job is
a different run: its answers differ from those of the original.
"""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)


def schedule(p, run_id):
    """Outcome-relevant values of one launch, a pure function of seed+run_id."""
    rng = random.Random(f"soak:{p['seed']}:{p['job']}:{run_id}")
    n = p["epochs"]
    stop = n if p["mode"] in ("ok", "warn") else int(n * rng.uniform(0.62, 0.74))
    lim1 = rng.randrange(44, 52)
    lim2 = rng.randrange(33, 40) if p["rev2"] else lim1
    ev = int(stop * rng.uniform(0.66, 0.90))
    return {"n": n, "stop": stop, "lim1": lim1, "lim2": lim2, "cal1": int(n * 0.055),
            "cal2": int(n * rng.uniform(0.30, 0.36)), "viol": ev,
            "bucket": rng.randrange(1, p["buckets"])}


def trace(p, run_id, info=None):
    s = schedule(p, run_id)
    rng = random.Random(f"body:{p['seed']}:{p['job']}:{run_id}")
    digest, live = 0, p["buckets"] // 2
    yield 0.0, f"# soak job={p['job']} run_id={run_id} buckets={p['buckets']} epochs={s['n']}"
    for e in range(1, s["stop"] + 1):
        frac = e / s["n"]
        if e == s["cal1"]:
            yield frac, f"E{e:05d} CAL rev=1 probe_limit={s['lim1']} effective=immediately"
        if p["rev2"] and e == s["cal2"]:
            yield frac, f"E{e:05d} CAL rev=2 probe_limit={s['lim2']} effective=immediately"
        limit = 10**6 if e < s["cal1"] else s["lim1"] if not p["rev2"] or e < s["cal2"] else s["lim2"]
        roof = min(limit, s["lim1"] + 8)            # warm-up may exceed the later limit
        bad = e == s["viol"] or (e > s["viol"] and rng.random() < 0.02)
        hit = rng.randrange(p["rows"])
        for j in range(p["rows"]):
            b = rng.randrange(p["buckets"])
            probe = min(int(rng.expovariate(0.16)) + 1, roof)
            if rng.random() < 0.004:
                probe = roof                        # exactly at the limit is legal
            if bad and j == hit:
                b = s["bucket"] if e == s["viol"] else b
                probe = limit + rng.randrange(1, 4)
            live += rng.randrange(-3, 4)
            line = (f"E{e:05d} bucket={b:06d} probe={probe:02d} live={live:06d} "
                    f"tomb={rng.randrange(9000):05d} disp={rng.randrange(64):02d} "
                    f"key={rng.getrandbits(40):010x} ck={rng.getrandbits(32):08x}")
            digest = zlib.crc32(line.encode(), digest)
            yield frac, line
        if rng.random() < 0.01:
            yield frac, f"E{e:05d} WARN probe alert suppressed (resize in progress)"
    if info is not None:
        info["digest"] = f"{digest:08x}"
    if p["mode"] in ("ok", "warn"):
        yield 1.0, f"E{s['stop']:05d} FINAL epochs={s['stop']} table_digest={digest:08x}"
    elif p["mode"] == "crash":
        yield s["stop"] / s["n"], f"E{s['stop']:05d} FATAL allocation failed"


def main():
    ap = argparse.ArgumentParser(description="Soak one hash-map configuration.")
    ap.add_argument("--job", type=int, required=True)
    a = ap.parse_args()
    p = load_image(f"soak{a.job}.img")
    run_id = os.urandom(4).hex()
    led = Ledger(f"job{a.job}", job=a.job, run_id=run_id)
    run_paced(trace(p, run_id), p["duration"], led)
    sys.stdout.flush()
    if p["mode"] == "stall":
        end = time.time() + 1800
        while time.time() < end:
            time.sleep(0.5)
            led.beat()
    led.beat(done=True)
    if p["exit"]:
        sys.stdout.flush()
        os._exit(p["exit"])


if __name__ == "__main__":
    main()
