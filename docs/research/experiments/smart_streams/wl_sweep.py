"""Workload 4: checkpointed Fenwick-tree sweeps that crash, hang or go bad.

Every unit inserts a batch of keys into a Fenwick tree and counts inversions.
A fault belongs to a unit of the job, not to a launch, and fires once: a launch
that repeats the unit does not repeat the fault.
"""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

TREE, KEYS, CK_EVERY = 512, 60, 15


def line_crc(text):
    return f"{zlib.crc32(text.encode()) & 0xffff:04x}"


def unit_rows(seed, unit, tree, chain):
    """One unit of real work: (rows of text, inversions, chain). Updates tree."""
    rng = random.Random(f"sweep:{seed}:{unit}")
    inv, rows = 0, []
    for i in range(KEYS):
        key, below = rng.randrange(1, TREE), 0
        k = key
        while k:
            below += tree[k]
            k &= k - 1
        k = key
        while k <= TREE:
            tree[k] += 1
            k += k & -k
        inv += (unit - 1) * KEYS + i - below
        chain = zlib.crc32(f"{key}:{below}".encode(), chain)
        if i % 5 == 0:
            rows.append(f"U{unit:04d} slot={i:02d} key={key:03d} below={below:05d} "
                        f"tag={rng.getrandbits(48):012x}")
    inv = (inv * 2654435761 + chain) % 10**6            # no trend over the units
    rows.append(f"U{unit:04d} SUM inv={inv:06d} chain={chain:08x}")
    return rows, inv, chain


def main():
    ap = argparse.ArgumentParser(description="Sweep one Fenwick-tree configuration.")
    ap.add_argument("--job", type=int, required=True)
    ap.add_argument("--resume", metavar="CHECKPOINT")
    a = ap.parse_args()
    p = load_image(f"sweep{a.job}.img")
    tree, chain, unit0, dirty = [0] * (TREE + 1), 0, 0, False
    if a.resume:
        try:
            with open(a.resume, "rb") as f:
                st = unseal(f.read())
            if st["job"] != a.job:
                raise ValueError
        except (OSError, ValueError, KeyError, zlib.error):
            say("# checkpoint unreadable or not of this job")
            raise SystemExit(65)
        tree, chain, unit0, dirty = st["tree"], st["chain"], st["unit"], st["dirty"]
    led = Ledger(f"job{a.job}", job=a.job, from_unit=unit0, units=0, dirty0=dirty, faults=[])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ckdir, last_ck = os.path.join(root, "ckpt", f"job_{a.job}"), None
    say(f"# sweep job={a.job} units={p['units']} start_after={unit0} checkpoints="
        + (f"every {CK_EVERY} units" if p["resumable"] else "not supported"))
    rng = random.Random(f"noise:{p['seed']}:{unit0}")
    t0 = time.time()
    for n, unit in enumerate(range(unit0 + 1, p["units"] + 1), 1):
        hit = None
        for i, f in enumerate(p["faults"]):
            if f["unit"] == unit:
                try:
                    os.close(os.open(os.path.join(_runs_dir(), f"fired-{a.job}-{i}"),
                                     os.O_CREAT | os.O_EXCL))
                    hit = f
                    led.rec["faults"].append([i, time.time()])
                except FileExistsError:
                    pass
        dirty = dirty or bool(hit and hit["kind"] == "CORRUPT")
        rows, _, chain = unit_rows(p["seed"], unit, tree, chain)
        if dirty:
            chain ^= 0x5a5a5a5a
        for x in rows:
            bad = dirty and (" SUM " in x or rng.random() < 0.3)
            sys.stdout.write(f"{x} crc={line_crc(x + ('!' if bad else ''))}\n")
        if rng.random() < 0.02:
            sys.stdout.write("# ERROR retry after EAGAIN succeeded\n")
        led.rec["units"] = n
        if p["resumable"] and unit % CK_EVERY == 0 and unit < p["units"]:
            os.makedirs(ckdir, exist_ok=True)
            last_ck = os.path.join(ckdir, f"{unit:04d}.ck")
            with open(last_ck + ".tmp", "wb") as f:
                f.write(seal({"job": a.job, "unit": unit, "tree": tree, "chain": chain,
                              "dirty": dirty}))
            os.replace(last_ck + ".tmp", last_ck)
            sys.stdout.write(f"# checkpoint ckpt/job_{a.job}/{unit:04d}.ck\n")
        sys.stdout.flush()
        led.beat()
        if hit and hit["kind"] == "CRASH":
            if hit["torn"] and p["resumable"]:
                torn = os.path.join(ckdir, f"{unit // CK_EVERY * CK_EVERY:04d}.ck")
                if os.path.exists(torn):
                    os.truncate(torn, 40)
            led.mark(ended=True)
            os._exit(hit["code"])
        if hit and hit["kind"] == "HANG":
            led.mark()
            end = time.time() + 2400
            while time.time() < end:
                time.sleep(0.5)
                led.beat()
            return
        while True:
            ahead = t0 + n * p["unit_s"] - time.time()
            if ahead <= 0:
                led.rec["lag"] = round(max(led.rec["lag"], -ahead), 3)
                break
            time.sleep(min(ahead, 0.5))
            led.beat()
    say(f"RESULT job={a.job} units={p['units']} digest={chain:08x}")
    led.rec["dirty"] = dirty
    led.beat(done=True)


if __name__ == "__main__":
    main()
