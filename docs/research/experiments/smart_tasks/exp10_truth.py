"""Experiment 10: independent exact computations for the ground truth.

Written apart from the solvers in wl_graphs (different algorithms, different
code): Bron-Kerbosch with pivoting for the clique number, and a k-colourability
test by backtracking over colour classes held as bitsets (most constrained
vertex first, colour classes opened in order). Standard library only.
"""


def _bits(x):
    while x:
        b = x & -x
        yield b.bit_length() - 1
        x ^= b


def neighbour_masks(n, edges):
    m = [0] * n
    for a, b in edges:
        m[a] |= 1 << b
        m[b] |= 1 << a
    return m


def clique_number(n, edges):
    """(omega, one maximum clique) by Bron-Kerbosch with Tomita pivoting."""
    nb = neighbour_masks(n, edges)
    best = [0, []]

    def bk(r, p, x):
        if not p and not x:
            if len(r) > best[0]:
                best[:] = [len(r), r[:]]
            return
        if len(r) + bin(p).count("1") <= best[0]:
            return
        piv = max(_bits(p | x), key=lambda u: bin(p & nb[u]).count("1"))
        for v in list(_bits(p & ~nb[piv])):
            r.append(v)
            bk(r, p & nb[v], x & nb[v])
            r.pop()
            p &= ~(1 << v)
            x |= 1 << v
    bk([], (1 << n) - 1, 0)
    return best[0], sorted(best[1])


def colourable(n, edges, k, seed_clique=()):
    """A proper colouring with at most k colours, or None if none exists."""
    nb = neighbour_masks(n, edges)
    classes = [1 << v for v in seed_clique]
    if len(classes) > k:
        return None
    left = ((1 << n) - 1) & ~sum(classes)

    def search(left):
        if not left:
            return True
        pick, opts = None, None
        for v in _bits(left):
            o = [i for i, c in enumerate(classes) if not c & nb[v]]
            if len(classes) < k:
                o.append(len(classes))
            if pick is None or len(o) < len(opts) or (len(o) == len(opts) and
                    bin(nb[v] & left).count("1") > bin(nb[pick] & left).count("1")):
                pick, opts = v, o
            if not o:
                return False
        bit = 1 << pick
        for i in opts:
            if i == len(classes):
                classes.append(bit)
            else:
                classes[i] |= bit
            if search(left & ~bit):
                return True
            if i == len(classes) - 1 and classes[i] == bit:
                classes.pop()
            else:
                classes[i] &= ~bit
        return False
    if not search(left):
        return None
    col = [0] * n
    for i, c in enumerate(classes):
        for v in _bits(c):
            col[v] = i
    return col


def chromatic(n, edges, upper):
    """Exact chi, given a proper colouring with ``upper`` colours; the clique
    breaks colour symmetry."""
    _, cl = clique_number(n, edges)
    k = upper
    while k > len(cl):
        col = colourable(n, edges, k - 1, cl)
        if col is None:
            break
        assert all(col[a] != col[b] for a, b in edges)
        k = max(col) + 1
    return k
