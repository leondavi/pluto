"""Workload 3 (v2): long soak of an open-addressing hash map.

The run id is drawn at launch and steers the whole run, so a relaunched job is
a different run. Checkpoint requests must be acknowledged while the job runs.
"""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)


def schedule(p, run_id):
    """Outcome-relevant values of one launch, a pure function of seed+run_id."""
    rng = random.Random(f"soak:{p['seed']}:{p['job']}:{run_id}")
    n = p["epochs"]
    stop = n if p["mode"] in ("ok", "warn") else int(n * p["stop_frac"])
    lims = [rng.randrange(46, 54)]
    for _ in range(p["revs"] - 1):
        lims.append(lims[-1] - rng.randrange(4, 9))
    cals = sorted([int(n * 0.05)] + [int(n * rng.uniform(0.16 + 0.14 * i, 0.28 + 0.14 * i))
                                     for i in range(p["revs"] - 1)])
    ev = int(stop * rng.uniform(0.72, 0.92))
    leak = int(stop * rng.uniform(0.45, 0.90)) if p["leak"] else None
    return {"n": n, "stop": stop, "lims": lims, "cals": cals, "viol": ev, "leak": leak,
            "bucket": rng.randrange(1, p["buckets"])}


def requests(p, run_id):
    """[(epoch, token)] of the checkpoint requests this launch will emit."""
    rng = random.Random(f"ck:{p['seed']}:{p['job']}:{run_id}")
    s = schedule(p, run_id)
    out = [(int(s["n"] * f), f"{rng.getrandbits(28):07x}") for f in p["ckpt"]]
    return [(e, t) for e, t in out if e < s["stop"]]


def trace(p, run_id, info=None):
    s = schedule(p, run_id)
    rng = random.Random(f"body:{p['seed']}:{p['job']}:{run_id}")
    digest, live, limit, rev = 0, p["buckets"] // 2, 10**6, 0
    ck, spikes = dict(requests(p, run_id)), set(p["spikes"])
    yield 0.0, f"# soak job={p['job']} run_id={run_id} buckets={p['buckets']} epochs={s['n']}"
    for e in range(1, s["stop"] + 1):
        frac = e / s["n"]
        while rev < len(s["cals"]) and e >= s["cals"][rev]:
            limit = s["lims"][rev]
            rev += 1
            yield frac, f"E{e:05d} CAL rev={rev} probe_limit={limit} effective=immediately"
        if e in ck:
            yield frac, ("CKPT", e, ck[e])
        if e == p["pause_at"]:
            yield frac, ("PAUSE", e)
        roof = min(limit, s["lims"][0] + 8)             # warm-up may exceed later limits
        bad = e == s["viol"] or (e > s["viol"] and rng.random() < 0.02)
        leak = s["leak"] and (e == s["leak"] or (e > s["leak"] and rng.random() < 0.02))
        hit, spot = rng.randrange(p["rows"]), rng.randrange(1, p["rows"])
        for j in range(p["rows"]):
            b = rng.randrange(p["buckets"])
            probe = min(int(rng.expovariate(0.16)) + 1, roof)
            if rng.random() < 0.004:
                probe = roof                            # exactly at the limit is legal
            if bad and j == hit:
                b = s["bucket"] if e == s["viol"] else b
                probe = limit + rng.randrange(1, 4)
            live += rng.randrange(-3, 4)
            if leak and j == spot:
                live += rng.choice((-1, 1)) * rng.randrange(7, 30)
            disp = rng.randrange(64)
            if e in spikes and j == spot:
                disp = rng.randrange(90, 120)
            line = (f"E{e:05d} bucket={b:05d} probe={probe:02d} live={live:06d} "
                    f"disp={disp:03d} key={rng.getrandbits(40):010x} ck={rng.getrandbits(32):08x}")
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
    led = Ledger(f"job{a.job}", job=a.job, run_id=run_id, acks={})
    ack_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "control", f"job_{a.job}.ack")
    open_reqs = {}

    def ckpt(e, token):
        open_reqs[token] = time.time()
        say(f"E{e:05d} CKPT request token={token} ttl={int(p['ttl'])}s")

    def pause(e):
        say(f"E{e:05d} GC pause")
        t = time.time()
        while time.time() - t < p["pause_s"]:
            time.sleep(0.5)
            tick()
            led.beat()
        return time.time() - t

    def tick():
        if not open_reqs:
            return
        try:
            with open(ack_path, encoding="utf-8", errors="replace") as f:
                words = f.read().split()
        except OSError:
            words = []
        for token, t in list(open_reqs.items()):
            age = time.time() - t
            if token in words and age <= p["ttl"]:
                led.rec["acks"][token] = round(age, 1)
                say(f"# CKPT stored token={token}")
                del open_reqs[token]
            elif age > p["ttl"]:
                say(f"# CKPT expired token={token}")
                del open_reqs[token]

    run_paced(trace(p, run_id), p["duration"], led, {"CKPT": ckpt, "PAUSE": pause}, tick)
    sys.stdout.flush()
    if p["mode"] == "stall":
        led.mark(onset=time.time())
        end = time.time() + 1500
        while time.time() < end:                        # silent, but still alive
            time.sleep(0.5)
            led.beat()
    led.beat(done=True)
    if p["exit"]:
        os._exit(p["exit"])


if __name__ == "__main__":
    main()
