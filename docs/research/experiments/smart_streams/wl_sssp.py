"""Workload 2: single-source shortest path variants under a cost model."""

import argparse
import bisect
import random
from collections import deque

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

MAXW = 60
ALGOS = ["bin_heap", "quad_heap", "oct_heap", "pairing", "dial", "fifo", "slf", "sorted"]


def graph(seed, n, deg=4):
    rng = random.Random(f"graph:{seed}:{n}")
    adj = [[] for _ in range(n)]
    for v in range(1, n):                       # a random tree keeps everything reachable
        adj[rng.randrange(v)].append((v, rng.randint(1, MAXW)))
    for _ in range(deg * n):
        adj[rng.randrange(n)].append((rng.randrange(n), rng.randint(1, MAXW)))
    return adj


class _DHeap:
    def __init__(self, d):
        self.d, self.a, self.work = d, [], 0

    def push(self, x):
        a, d = self.a, self.d
        a.append(x)
        i = len(a) - 1
        while i:
            p = (i - 1) // d
            self.work += 1
            if a[p] <= a[i]:
                break
            a[p], a[i] = a[i], a[p]
            i = p

    def pop(self):
        a, d = self.a, self.d
        top, last = a[0], a.pop()
        if a:
            a[0], i = last, 0
            while True:
                kids = a[d * i + 1:d * i + 1 + d]
                if not kids:
                    break
                self.work += len(kids)
                m = min(kids)
                if a[i] <= m:
                    break
                j = d * i + 1 + kids.index(m)
                a[i], a[j] = a[j], a[i]
                i = j
        return top


class _Pairing:
    def __init__(self):
        self.r, self.a, self.work = None, 0, 0

    def _meld(self, x, y):
        if not x or not y:
            return x or y
        self.work += 1
        if y[0] < x[0]:
            x, y = y, x
        x[1].append(y)
        return x

    def push(self, x):
        self.r = self._meld(self.r, [x, []])
        self.a += 1

    def pop(self):
        top, kids = self.r
        self.a -= 1
        ms = [self._meld(kids[i], kids[i + 1] if i + 1 < len(kids) else None)
              for i in range(0, len(kids), 2)]
        r = None
        for m in reversed(ms):
            r = self._meld(r, m)
        self.r = r
        return top


class _Sorted:
    def __init__(self):
        self.a, self.work = [], 0

    def push(self, x):
        i = bisect.bisect(self.a, (-x[0], -x[1]))
        self.work += 1 + (len(self.a) - i) // 8
        self.a.insert(i, (-x[0], -x[1]))

    def pop(self):
        d, v = self.a.pop()
        return (-d, -v)


def solve(algo, adj):
    """Return (dist, settle order, work units); counters are exact."""
    n, inf = len(adj), float("inf")
    dist, order, work = [inf] * n, [], 0
    dist[0] = 0
    if algo in ("fifo", "slf"):
        q, inq = deque([0]), [False] * n
        while q:
            u = q.popleft()
            inq[u] = False
            for v, w in adj[u]:
                work += 1
                if dist[u] + w < dist[v]:
                    dist[v] = dist[u] + w
                    if not inq[v]:
                        inq[v] = True
                        if algo == "slf" and q and dist[v] < dist[q[0]]:
                            q.appendleft(v)
                        else:
                            q.append(v)
        return dist, list(range(n)), work
    if algo == "dial":
        buckets, d, left = {0: [0]}, 0, 1
        done = [False] * n
        while left:
            work += 1
            for u in buckets.pop(d, ()):
                left -= 1
                if done[u]:
                    continue
                done[u] = True
                order.append(u)
                for v, w in adj[u]:
                    work += 1
                    if d + w < dist[v]:
                        dist[v] = d + w
                        buckets.setdefault(d + w, []).append(v)
                        left += 1
            d += 1
        return dist, order, work
    h = {"bin_heap": lambda: _DHeap(2), "quad_heap": lambda: _DHeap(4),
         "oct_heap": lambda: _DHeap(8), "pairing": _Pairing, "sorted": _Sorted}[algo]()
    h.push((0, 0))
    done = [False] * n
    while h.a:
        d, u = h.pop()
        if done[u]:
            continue
        done[u] = True
        order.append(u)
        for v, w in adj[u]:
            work += 1
            if d + w < dist[v]:
                dist[v] = d + w
                h.push((d + w, v))
    return dist, order, work + h.work


def reported(p, variant, si):
    """(distances as reported, settle order) including the seeded defect."""
    v = p["variants"][variant]
    dist, order, _ = solve(v["algo"], graph(p["seed"], p["sizes"][si]))
    dist = list(dist)
    for vert, delta in p["defect"]["sets"].get(str(si), []) if variant == p["defect"]["variant"] else []:
        dist[vert] += delta
    return dist, order


def trace(p, variant, si):
    v, n = p["variants"][variant], p["sizes"][si]
    cost, dur = v["cost"][si], v["dur"][si]
    dist, order = reported(p, variant, si)
    adj = graph(p["seed"], n)
    late = v["algo"] in ("fifo", "slf")
    rng = random.Random(f"tr:{p['seed']}:{variant}:{si}")
    total = max(int(dur * p["lps"]), n * 3)
    yield 0.0, f"# variant={variant} size={si} n={n} sources=1 unit=cost"
    lo, hi = (0.93, 0.995) if late else (0.02, 0.97)
    done = 0
    for i in range(total):
        frac = i / total
        if i % 60 == 0:
            yield frac, f"TICK cum={int(cost * frac)} queue={rng.randrange(1, n)} settled={done}"
        while done < len(order) and (done + 1) / len(order) * (hi - lo) + lo <= frac:
            x = order[done]
            done += 1
            yield frac, f"SETTLE v={x:05d} d={dist[x]:05d} rank={done}"
        u = rng.randrange(n)
        if adj[u]:
            x, w = rng.choice(adj[u])
            yield frac, (f"RELAX u={u:05d} v={x:05d} w={w:02d} cand={rng.randrange(1, 900):05d} "
                         f"slot={rng.randrange(1 << 20):07d} probe={rng.randrange(64):02d} "
                         f"tag={rng.getrandbits(32):08x}")
    while done < len(order):
        x = order[done]
        done += 1
        yield 1.0, f"SETTLE v={x:05d} d={dist[x]:05d} rank={done}"
    for b in range(0, n, 64):
        dg = zlib.crc32(repr(dist[b:b + 64]).encode())
        yield 1.0, f"BLOCK b={b // 64:03d} first={b:05d} dg={dg:08x}"
    yield 1.0, f"RESULT variant={variant} size={si} n={n} cost={cost} settled={len(order)}"


def main():
    ap = argparse.ArgumentParser(description="Benchmark one variant at one size.")
    ap.add_argument("--variant", required=True)
    ap.add_argument("--size", type=int, required=True)
    a = ap.parse_args()
    p = load_image("bench.img")
    if a.variant not in p["variants"] or not 0 <= a.size < len(p["sizes"]):
        ap.error("unknown variant or size index")
    led = Ledger(f"{a.variant}_s{a.size}", variant=a.variant, size=a.size)
    run_paced(trace(p, a.variant, a.size), p["variants"][a.variant]["dur"][a.size], led)
    led.beat(done=True)


if __name__ == "__main__":
    main()
