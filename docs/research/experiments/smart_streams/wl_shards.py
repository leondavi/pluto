"""Workload 1: stress trace of a persistent AVL map, one process per shard."""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

WARM = 1500


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


def trace(p):
    """Yield (fraction, line). ``p`` is one shard's parameter dict."""
    rng = random.Random(f"shard:{p['seed']}:{p['shard']}")
    n, sh = p["ops"], p["shard"]
    kind, fop = p["kind"], p["fault_op"]
    cells = {}
    for op, text in p["cells"]:
        cells.setdefault(op, []).append(text)
    root, ver, snaps, sid, scratch = None, 0, [], 0, []
    present = {}
    for _ in range(WARM):
        k = rng.randrange(1000, 400000)
        root = put(root, k, 0, scratch)
        present[k] = 0
    keys = sorted(present)
    yield 0.0, f"# shard={sh} ops={n} warm={len(keys)} fmt=3"
    for i in range(1, n + 1):
        frac = i / n
        damage = kind != "NONE" and (i == fop or (i > fop and rng.random() < 1 / 300))
        lt = i * 10 + sh
        thr = rng.randrange(8)
        for text in cells.get(i, ()):
            yield frac, f"{i:06d} lt={lt:07d} {text}"
        r = rng.random()
        rots = []
        if damage and kind == "STALE" or r < 0.30:
            k = rng.choice(keys)
            want = present.get(k, 0)
            repl = 1 if rng.random() < 0.25 else 0
            got = want
            if repl and want and rng.random() < 0.15:
                got = rng.randrange(want)
            if damage and kind == "STALE":
                repl, want = 0, max(want, 1)
                got = rng.randrange(want)
            op = f"GET key={k:06d} ver={got:05d} want={want:05d} repl={repl}"
        elif r < 0.78 or len(present) < 900:
            k = rng.randrange(1000, 400000) if rng.random() < 0.5 else rng.choice(keys)
            ver += 1
            root = put(root, k, ver, rots)
            if k not in present:
                keys.append(k)
            present[k] = ver
            op = f"PUT key={k:06d} ver={ver:05d} val={rng.randrange(10**6):06d}"
        else:
            j = rng.randrange(len(keys))
            k = keys[j]
            keys[j] = keys[-1]
            keys.pop()
            present.pop(k, None)
            root = delete(root, k, rots)
            op = f"DEL key={k:06d} ver={ver:05d}"
        (nd, lo, hi), path = locate(root, k)
        yield frac, (f"{i:06d} lt={lt:07d} thr=t{thr} {op} size={_s(root)} h={_h(root)} "
                     f"root={zlib.crc32(repr((root[0], root[1], i)).encode()):08x} "
                     f"path={'/'.join(map(str, path[-9:]))}")
        for rk, b in rots:
            yield frac, f"{i:06d} ROT node={rk:06d} bf={b:+d} -> settled thr=t{thr}"
        key, lh, rh, ls, rs = nd[0], _h(nd[4]), _h(nd[5]), _s(nd[4]), _s(nd[5])
        bf, sz = lh - rh, nd[3]
        if damage and kind == "BALANCE":
            sgn = rng.choice((-1, 1))
            rh = lh + 2 if sgn < 0 else rh
            lh = rh + 2 if sgn > 0 else lh
            bf = lh - rh
        elif damage and kind == "SIZE":
            sz += rng.choice((-1, 1, 2))
        elif damage and kind == "ORDER":
            if rng.random() < 0.5:
                hi = max(lo, key - rng.randrange(1, 40))
                hi = min(hi, key)
                lo = min(lo, hi - 1)
            else:
                lo = min(key + rng.randrange(1, 40), 999998)
                hi = max(hi, lo + 1)
        yield frac, (f"{i:06d} AUDIT node={key:06d} lo={lo:06d} hi={hi:06d} lh={lh} rh={rh} "
                     f"bf={bf:+d} lsz={ls} rsz={rs} sz={sz}")
        if i % 90 == 0:
            sid += 1
            pin = rng.choice(keys)
            dg = zlib.crc32(f"{p['seed']}:{sh}:{sid}".encode())
            snaps.append((sid, pin, dg))
            yield frac, f"{i:06d} SNAP id={sid:05d} pin={pin:06d} dg={dg:08x}"
        bad = damage and kind == "PERSIST"
        if bad or (snaps and i % 45 == 20):
            s = rng.choice(snaps)
            dg = s[2] ^ (1 << rng.randrange(32)) if bad else s[2]
            yield frac, f"{i:06d} RECHK id={s[0]:05d} pin={s[1]:06d} dg={dg:08x}"
        if rng.random() < 0.002:
            yield frac, (f"{i:06d} ERROR latch timeout thr=t{thr} node={rng.choice(keys):06d} "
                         "retry=1 recovered=yes")
        if rng.random() < 0.0008:
            yield frac, f"{i:06d} WARN invariant recheck deferred node={rng.choice(keys):06d} ok"
    yield 1.0, f"# done shard={sh} size={_s(root)} h={_h(root)}"


def main():
    ap = argparse.ArgumentParser(description="Stress one shard of the persistent map.")
    ap.add_argument("--shard", type=int, required=True)
    a = ap.parse_args()
    p = load_image(f"shard{a.shard}.img")
    led = Ledger(f"shard{a.shard}")
    run_paced(trace(p), p["duration"], led)
    led.beat(done=True)


if __name__ == "__main__":
    main()
