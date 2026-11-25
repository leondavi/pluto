"""Workload 1 (v2): stress trace of a persistent AVL map, one process per shard.

Shards run in groups of twelve that start together (barrier).
"""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

WARM = 1200
AUDITS = ("ORDER", "BALANCE", "SIZE")


def cell_ck(key, val):
    return (key * 40503 + val * 7) & 0xFFFF


# persistent AVL node: (key, ver, height, size, left, right)
def _h(t):
    return t[2] if t else 0


def _s(t):
    return t[3] if t else 0


def _mk(k, v, l, r):
    return (k, v, 1 + max(_h(l), _h(r)), 1 + _s(l) + _s(r), l, r)


def _fix(k, v, l, r, rots):
    b = _h(l) - _h(r)
    if b > 1:
        rots.append((k, b))
        if _h(l[4]) < _h(l[5]):
            lr = l[5]
            return _mk(lr[0], lr[1], _mk(l[0], l[1], l[4], lr[4]), _mk(k, v, lr[5], r))
        return _mk(l[0], l[1], l[4], _mk(k, v, l[5], r))
    if b < -1:
        rots.append((k, b))
        if _h(r[5]) < _h(r[4]):
            rl = r[4]
            return _mk(rl[0], rl[1], _mk(k, v, l, rl[4]), _mk(r[0], r[1], rl[5], r[5]))
        return _mk(r[0], r[1], _mk(k, v, l, r[4]), r[5])
    return _mk(k, v, l, r)


def put(t, k, v, rots):
    if not t:
        return _mk(k, v, None, None)
    if k < t[0]:
        return _fix(t[0], t[1], put(t[4], k, v, rots), t[5], rots)
    if k > t[0]:
        return _fix(t[0], t[1], t[4], put(t[5], k, v, rots), rots)
    return _mk(k, v, t[4], t[5])


def _popmin(t, rots):
    if not t[4]:
        return t, t[5]
    m, l = _popmin(t[4], rots)
    return m, _fix(t[0], t[1], l, t[5], rots)


def delete(t, k, rots):
    if not t:
        return t
    if k < t[0]:
        return _fix(t[0], t[1], delete(t[4], k, rots), t[5], rots)
    if k > t[0]:
        return _fix(t[0], t[1], t[4], delete(t[5], k, rots), rots)
    if not t[4] or not t[5]:
        return t[4] or t[5]
    m, r = _popmin(t[5], rots)
    return _fix(m[0], m[1], t[4], r, rots)


def locate(t, k):
    lo, hi, path, last = 0, 999999, [], None
    while t:
        path.append(t[0])
        last = (t, lo, hi)
        if k == t[0]:
            break
        if k < t[0]:
            hi, t = t[0], t[4]
        else:
            lo, t = t[0], t[5]
    return last, path


def _spoil_path(rng, path, k):
    """Make one inner element contradict the first one, or None if too short."""
    if len(path) < 4 or k == path[0]:
        return None
    path = list(path)
    j = rng.randrange(1, len(path) - 1)
    path[j] = path[0] + rng.randrange(1, 60) if k < path[0] else path[0] - rng.randrange(1, 60)
    return path


def trace(p):
    """Yield (fraction, line-or-event). ``p`` is one shard's parameter dict."""
    rng = random.Random(f"shard:{p['seed']}:{p['shard']}")
    n, sh = p["ops"], p["shard"]
    cells = {}
    for op, text in p["cells"]:
        cells.setdefault(op, []).append(text)
    root, ver, snaps, sid, scratch = None, 0, [], 0, []
    present, pending, rebuild, rid = {}, set(), 0, 0
    for _ in range(WARM):
        k = rng.randrange(1000, 400000)
        root = put(root, k, 0, scratch)
        present[k] = 0
    keys = sorted(present)
    fresh = []                              # keys written in this trace
    yield 0.0, f"# shard={sh} ops={n} warm={len(keys)} fmt=4"
    for i in range(1, n + 1):
        frac = i / n
        for kind, fop in p["faults"]:
            if i == fop or (i > fop and rng.random() < 1 / 150):
                pending.add(kind)
        lt, thr = i * 50 + sh, rng.randrange(8)
        if i == p["spin_op"]:
            yield frac, (p["spin"], i)
        for text in cells.get(i, ()):
            yield frac, f"{i:06d} lt={lt:07d} {text}"
        if rebuild:
            rebuild -= 1
            if not rebuild:
                yield frac, f"{i:06d} REBUILD end id={rid:04d} thr=t{thr}"
        elif rng.random() < 1 / 300:
            rebuild, rid = rng.randrange(15, 45), rid + 1
            yield frac, f"{i:06d} REBUILD begin id={rid:04d} thr=t{thr}"
        r, rots, forced = rng.random(), [], None
        for kind in ("VERSION", "STALE"):
            if kind in pending and forced is None and (kind == "VERSION" or fresh):
                forced = kind
        if forced == "STALE":
            live = [k for k in fresh[-200:] if present.get(k)]
            forced = "STALE" if live else None
        if forced == "STALE" or (forced is None and r < 0.30):
            k = rng.choice(live if forced else fresh[-300:] if fresh and r < 0.18 else keys)
            want = present.get(k, 0)
            repl = 1 if rng.random() < 0.25 and not forced else 0
            got = want
            if forced or (repl and want and rng.random() < 0.2):
                got = rng.randrange(want)
            pending.discard("STALE" if forced else None)
            op = f"GET key={k:06d} ver={got:05d} repl={repl}"
        elif forced == "VERSION" or r < 0.78 or len(present) < 900:
            k = rng.randrange(1000, 400000) if rng.random() < 0.5 else rng.choice(keys)
            ver += 1
            if forced == "VERSION":
                ver += rng.randrange(1, 3)
                pending.discard("VERSION")
            root = put(root, k, ver, rots)
            if k not in present:
                keys.append(k)
            present[k] = ver
            fresh.append(k)
            op = f"PUT key={k:06d} ver={ver:05d} val={rng.randrange(10**6):06d}"
        else:
            j = rng.randrange(len(keys))
            k = keys[j]
            keys[j] = keys[-1]
            keys.pop()
            present.pop(k, None)
            root = delete(root, k, rots)
            op = f"DEL key={k:06d}"
        (nd, lo, hi), path = locate(root, k)
        path = path[-9:]
        if "PATH" in pending:
            bad = _spoil_path(rng, path, k)
            if bad:
                path = bad
                pending.discard("PATH")
        yield frac, (f"{i:06d} lt={lt:07d} thr=t{thr} {op} size={_s(root)} h={_h(root)} "
                     f"path={'/'.join(map(str, path))}")
        for rk, b in rots:
            yield frac, f"{i:06d} ROT node={rk:06d} bf={b:+d} -> settled thr=t{thr}"
        key, lh, rh, ls, rs = nd[0], _h(nd[4]), _h(nd[5]), _s(nd[4]), _s(nd[5])
        sz, hit = nd[3], set()
        if rebuild:                         # transient states are legal here
            hit = {k for k in AUDITS if rng.random() < 0.06}
        else:
            hit = {k for k in AUDITS if k in pending}
            pending -= hit
        if "BALANCE" in hit:
            if rng.random() < 0.5:
                rh = lh + 2
            else:
                lh = rh + 2
        if "SIZE" in hit:
            sz += rng.choice((-1, 1, 2))
        if "ORDER" in hit:
            if rng.random() < 0.5:
                hi = max(1, key - rng.randrange(0, 40))
                lo = min(lo, hi - 1)
            else:
                lo = key + rng.randrange(0, 40)
                hi = max(hi, lo + 1)
        yield frac, (f"{i:06d} AUDIT node={key:06d} lo={lo:06d} hi={hi:06d} lh={lh} rh={rh} "
                     f"bf={lh - rh:+d} lsz={ls} rsz={rs} sz={sz}")
        if i % 60 == 0:
            sid += 1
            pin = rng.choice(keys)
            dg = zlib.crc32(f"{p['seed']}:{sh}:{sid}".encode())
            snaps.append((sid, pin, dg))
            yield frac, f"{i:06d} SNAP id={sid:05d} pin={pin:06d} dg={dg:08x}"
        bad = "PERSIST" in pending and bool(snaps)
        if bad or (snaps and i % 30 == 20):
            s = rng.choice(snaps)
            dg = s[2] ^ (1 << rng.randrange(32)) if bad else s[2]
            if bad:
                pending.discard("PERSIST")
            yield frac, f"{i:06d} RECHK id={s[0]:05d} pin={s[1]:06d} dg={dg:08x}"
        if rng.random() < 0.002:
            yield frac, (f"{i:06d} ERROR latch timeout thr=t{thr} node={rng.choice(keys):06d} "
                         "retry=1 recovered=yes")
        if rng.random() < 0.0008:
            yield frac, f"{i:06d} WARN invariant recheck deferred node={rng.choice(keys):06d} ok"
    yield 1.0, f"# done shard={sh} size={_s(root)} h={_h(root)}"


def text_lines(p):
    """The trace as text up to a livelock, without live-only SPIN lines."""
    for _, x in trace(p):
        if isinstance(x, str):
            yield x
        elif x[0] == "LIVELOCK":
            return


def main():
    ap = argparse.ArgumentParser(description="Stress one shard of the persistent map.")
    ap.add_argument("--shard", type=int, required=True)
    a = ap.parse_args()
    p = load_image(f"shard{a.shard}.img")
    led = Ledger(f"shard{a.shard}", shard=a.shard)
    group = {f"shard{s}" for s in p["group"]}
    limit = time.time() + p["barrier_s"]
    say(f"# shard={a.shard} waiting for its group of {len(group)}")
    while not group <= set(live_labels()):
        if time.time() > limit:
            say("# barrier timeout: not all shards of the group are running")
            os._exit(75)
        time.sleep(0.5)
        led.beat()
    led.mark(began=time.time())

    def spin(i, seconds):
        t, k = time.time(), 0
        while time.time() - t < seconds:
            say(f"{i:06d} SPIN latch={i % 97:02d} turns={k}")
            k += 1
            time.sleep(1.0)
            led.beat()
        return time.time() - t

    def livelock(i):
        led.mark(onset=time.time())
        spin(i, p["spin_s"])
        os._exit(70)

    def transient(i):
        led.mark(transient=time.time())
        dt = spin(i, p["spin_s"])
        say(f"{i:06d} SPIN resolved")
        return dt

    run_paced(trace(p), p["duration"], led, {"LIVELOCK": livelock, "TRANSIENT": transient})
    led.beat(done=True)


if __name__ == "__main__":
    main()
