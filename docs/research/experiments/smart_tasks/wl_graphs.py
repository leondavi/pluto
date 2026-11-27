"""Workload 10, part 1: graphs and the real search procedures behind ``gcol``.

Every procedure is a generator that yields one event per unit of work (a search
node, a local-search move), so that the program can pace the work and the plan
can replay it offline. Standard library only; inlined into the program.
"""

import math
import random


def gnp(rng, n, p):
    return sorted((a, b) for a in range(n) for b in range(a + 1, n) if rng.random() < p)


def planted(rng, n, k, p, n_near=3):
    """A k-colourable graph that contains a k-clique (so chi = omega = k), plus a
    few vertices adjacent to all of the clique but one member (near-cliques)."""
    cls = [i % k for i in range(n)]
    rng.shuffle(cls)
    groups = [[v for v in range(n) if cls[v] == c] for c in range(k)]
    clique = [rng.choice(g) for g in groups]
    e = {(a, b) for a in range(n) for b in range(a + 1, n)
         if cls[a] != cls[b] and rng.random() < p}
    e |= {(min(a, b), max(a, b)) for a in clique for b in clique if a != b}
    for _ in range(n_near):
        c = rng.choice(clique)
        u = rng.choice([v for v in groups[cls[c]] if v != c])
        e |= {(min(u, x), max(u, x)) for x in clique if x != c}
    return sorted(e), sorted(clique)


def make_graph(seed, name, spec):
    """(n, edges, planted clique or None) of one campaign graph."""
    rng = rng_for("graph", seed, name, spec.get("salt", 0))
    if spec.get("k"):
        return (spec["n"], *planted(rng, spec["n"], spec["k"], spec["p"]))
    return spec["n"], gnp(rng, spec["n"], spec["p"]), None


def adjacency(n, edges):
    adj = [set() for _ in range(n)]
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    return adj


def masks(adj):
    return [sum(1 << u for u in a) for a in adj]


def dimacs(n, edges, name):
    out = [f"c graph {name}", f"p edge {n} {len(edges)}"]
    return "\n".join(out + [f"e {a + 1} {b + 1}" for a, b in edges]) + "\n"


def proper(edges, col):
    return all(col[a] != col[b] for a, b in edges)


def is_clique(adj, vs):
    return len(set(vs)) == len(vs) and all(b in adj[a] for i, a in enumerate(vs) for b in vs[i + 1:])


def dsatur_greedy(adj):
    n = len(adj)
    col, seen = [-1] * n, [set() for _ in range(n)]
    for _ in range(n):
        v = max((u for u in range(n) if col[u] < 0), key=lambda u: (len(seen[u]), len(adj[u]), -u))
        c = 0
        while c in seen[v]:
            c += 1
        col[v] = c
        for u in adj[v]:
            seen[u].add(c)
    return col


def max_clique(adj):
    """Exact maximum clique (greedy-colouring bound on bitsets). Returns
    (clique, improvements as (nodes, clique), nodes)."""
    n, am = len(adj), masks(adj)
    best, steps, nodes = [], [], [0]

    def colour_sort(p):
        out, c, u = [], 0, p
        while u:
            c += 1
            q = u
            while q:
                b = q & -q
                q &= ~am[b.bit_length() - 1] & ~b
                u &= ~b
                out.append((b.bit_length() - 1, c))
        return out

    def expand(r, p):
        nonlocal best
        nodes[0] += 1
        if not p:
            if len(r) > len(best):
                best = r[:]
                steps.append((nodes[0], best))
            return
        for v, c in reversed(colour_sort(p)):
            if len(r) + c <= len(best):
                return
            r.append(v)
            expand(r, p & am[v])
            r.pop()
            p &= ~(1 << v)
    expand([], (1 << n) - 1)
    return sorted(best), steps, nodes[0]


def bnb(adj, clique, ctl, prio=None):
    """DSATUR branch and bound for the chromatic number. The clique is coloured
    first; new colours are opened in order. ``ctl["hint"]`` may hold an outside
    colouring (checked here). Keeps ctl["ub"], ctl["closed"] (share of the tree
    that is closed, by branching factors) and ctl["nodes"] current."""
    n = len(adj)
    edges = [(a, b) for a in range(n) for b in adj[a] if a < b]
    deg, prio = [len(a) for a in adj], prio or [-u for u in range(n)]
    col, cnt, sat = [-1] * n, [[0] * (n + 1) for _ in range(n)], [0] * n

    def put(v, c):
        col[v] = c
        for u in adj[v]:
            r = cnt[u]
            r[c] += 1
            if r[c] == 1:
                sat[u] += 1

    def take(v):
        c, col[v] = col[v], -1
        for u in adj[v]:
            r = cnt[u]
            r[c] -= 1
            if r[c] == 0:
                sat[u] -= 1
    for i, v in enumerate(clique):
        put(v, i)
    lb = len(clique)
    ub, closed, nodes, free, stack = n + 1, 0.0, 0, n - lb, []
    w, used = 1.0, lb
    ctl.update(ub=ub, closed=0.0, nodes=0, lb=lb)
    while True:
        if free == 0:
            ub, closed = used, closed + w
            ctl.update(ub=ub, closed=closed)
            yield ("inc", ub, col[:], "search")
            if ub <= lb:
                yield ("done", ub, "bound")
                return
        else:
            v = max((u for u in range(n) if col[u] < 0), key=lambda u: (sat[u], deg[u], prio[u]))
            row = cnt[v]
            ch = [c for c in range(min(used + 1, ub - 1)) if not row[c]]
            if ch:
                stack.append([v, ch, 0, w / len(ch), used])
            else:
                closed += w
        while stack:
            f = stack[-1]
            v, ch, i, wc, u0 = f
            if i:
                take(v)
                free += 1
            h = ctl.pop("hint", None)
            if h is not None:
                k = max(h) + 1
                bad = next(((a, b) for a, b in edges if h[a] == h[b]), None)
                if bad:
                    yield ("reject", bad)
                elif k < ub:
                    ub = k
                    ctl["ub"] = ub
                    yield ("inc", k, list(h), "hint")
                    if ub <= lb:
                        yield ("done", ub, "bound")
                        return
                else:
                    yield ("stale", k)
            if i < len(ch) and max(u0, ch[i] + 1) < ub:
                c = ch[i]
                f[2] = i + 1
                put(v, c)
                free -= 1
                used, w = max(u0, c + 1), wc
                nodes += 1
                ctl.update(closed=closed, nodes=nodes)
                yield ("node", nodes, len(stack), v, c)
                break
            closed += wc * (len(ch) - i)
            stack.pop()
        else:
            ctl.update(closed=1.0)
            yield ("done", ub, "exhausted")
            return


def _gamma(adj, col, k):
    g = [[0] * k for _ in adj]
    for v, a in enumerate(adj):
        for u in a:
            g[v][col[u]] += 1
    return g


def tabucol(adj, rng, maxit, start):
    """Galinier-Hao tabu search, one fixed k at a time, k decreasing."""
    n, col = len(adj), start[:]
    k = max(col) + 1
    yield ("col", k, col[:], True)
    while k > 1:
        k -= 1
        col = [c if c < k else rng.randrange(k) for c in col]
        g = _gamma(adj, col, k)
        conf = sum(g[v][col[v]] for v in range(n)) // 2
        tl, best = [[0] * k for _ in range(n)], conf
        for it in range(1, maxit + 1):
            if not conf:
                break
            bd, moves, ncf = None, [], 0
            for v in range(n):
                gv, cv = g[v], col[v]
                base = gv[cv]
                if not base:
                    continue
                ncf += 1
                tv = tl[v]
                for c in range(k):
                    d = gv[c] - base
                    if c == cv or (bd is not None and d > bd) or (tv[c] > it and conf + d >= best):
                        continue
                    if bd is None or d < bd:
                        bd, moves = d, [(v, c)]
                    else:
                        moves.append((v, c))
            if moves:
                v, c = rng.choice(moves)
                old, col[v], conf = col[v], c, conf + bd
                for u in adj[v]:
                    g[u][old] -= 1
                    g[u][c] += 1
                tl[v][old] = it + rng.randrange(10) + int(0.6 * ncf)
                best = min(best, conf)
            yield ("it", it, k, conf, best)
        if conf:
            yield ("stuck", k + 1, maxit)
            return
        yield ("col", k, col[:], True)


def anneal(adj, rng, maxit, start, t0=1.0, alpha=0.9996, slack=0):
    """Simulated annealing on conflicting vertices, k decreasing. A level counts
    as solved when at most ``slack`` edges conflict."""
    n, col = len(adj), start[:]
    k = max(col) + 1
    yield ("col", k, col[:], True)
    while k > 2:
        k -= 1
        col = [c if c < k else rng.randrange(k) for c in col]
        g = _gamma(adj, col, k)
        conf, temp = sum(g[v][col[v]] for v in range(n)) // 2, t0
        for it in range(1, maxit + 1):
            if conf <= slack:
                break
            bad = [v for v in range(n) if g[v][col[v]]]
            v = rng.choice(bad)
            c = rng.randrange(k - 1)
            c += c >= col[v]
            d = g[v][c] - g[v][col[v]]
            if d <= 0 or rng.random() < math.exp(-d / temp):
                old, col[v], conf = col[v], c, conf + d
                for u in adj[v]:
                    g[u][old] -= 1
                    g[u][c] += 1
            temp = max(0.05, temp * alpha)
            yield ("it", it, k, conf, temp)
        if conf > slack:
            yield ("stuck", k + 1, maxit)
            return
        yield ("col", k, col[:], conf == 0)


def run_offline(gen, ctl=None, hint_at=None, hint=None, limit=None):
    """Drive a procedure without pacing; returns [(units so far, event)] of the
    non-unit events and the number of work units. A hint is injected after
    ``hint_at`` units."""
    out, units = [], 0
    for ev in gen:
        if ev[0] in ("node", "it"):
            units += 1
            if hint is not None and units == hint_at:
                ctl["hint"] = hint
            if limit and units >= limit:
                out.append((units, ("limit",)))
                break
        else:
            out.append((units, ev))
    return out, units


def rng_for(*parts):
    return random.Random(":".join(map(str, parts)))
