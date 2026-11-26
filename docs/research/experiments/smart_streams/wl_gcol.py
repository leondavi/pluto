"""Workload 10, part 2: the ``gcol`` program (one solver run on one graph).

The builder concatenates wl_common, wl_graphs and this file. Work is real and
paced: the exact solver spends 1/rate s per search node, a heuristic 1/rate s
per move. The exact solver reads outside colourings from bounds/<graph>.ub.
"""

import argparse

from wl_common import *  # noqa: F401,F403  (inlined by the builder)
from wl_common import _runs_dir  # noqa: F401  (inlined by the builder)
from wl_graphs import *  # noqa: F401,F403  (inlined by the builder)

LINES_PER_S = 80


def owner_of(name, per):
    return (int(name[1:]) - 1) // per


def first_start():
    t = []
    for name in os.listdir(_runs_dir()) if os.path.isdir(_runs_dir()) else []:
        if name.endswith(".json"):
            try:
                with open(os.path.join(_runs_dir(), name)) as f:
                    t.append(json.load(f)["t0"])
            except (OSError, ValueError, KeyError):
                pass
    return min(t, default=None)


def tie_order(seed, n):
    if seed == 1:
        return None
    order = list(range(n))
    rng_for("tie", seed).shuffle(order)
    return order


def colours(col):
    return " ".join(str(c + 1) for c in col)


class Run:
    def __init__(self, p, name, method, seed):
        self.p, self.name, self.method, self.seed = p, name, method, seed
        self.spec = p["graphs"][name]
        self.n, self.edges, _ = make_graph(p["seed"], name, self.spec)
        bad = self.spec.get("misread")
        self.own = self.edges + ([tuple(bad)] if bad and method == "exact"
                                 and seed in self.spec.get("misread_seeds", []) else [])
        self.adj = adjacency(self.n, self.own)
        self.rate = self.spec["rate"][method] * p.get("speed", 1.0)
        self.every = max(1, round(self.rate / LINES_PER_S))
        self.t0 = time.time()
        self.led = Ledger(f"{name}_{method}_s{seed}", graph=name, method=method, seed=seed)

    def t(self):
        return round(time.time() - self.t0, 2)

    def drive(self, gen, handle, poll=None, limit_s=None):
        """Pace the work units of ``gen``; returns True if the time limit hit."""
        out, units, nxt = sys.stdout, 0, 0.0
        for ev in gen:
            if ev[0] in ("node", "it"):
                units += 1
                now = time.time()
                ahead = self.t0 + units / self.rate - now
                if ahead > 0.05:
                    out.flush()
                    time.sleep(ahead)
                    self.led.beat()
                elif ahead < -0.05:
                    self.led.rec["lag"] = round(max(self.led.rec["lag"], -ahead), 3)
                if poll and now >= nxt:
                    nxt = now + 1.0
                    poll()
                if limit_s and now - self.t0 > limit_s:
                    return True
            handle(ev, units)
        return False

    def heuristic(self):
        rng = rng_for("run", self.p["seed"], self.name, self.method, self.seed)
        start = dsatur_greedy(self.adj)
        sp = self.spec
        if self.method == "tabu":
            gen = tabucol(self.adj, rng, sp["maxit"]["tabu"], start)
        else:
            gen = anneal(self.adj, rng, sp["maxit"]["anneal"], start, slack=int(sp.get("bug", 0)))
        found = []

        def handle(ev, units):
            if ev[0] == "it":
                if units % self.every == 0:
                    if self.method == "tabu":
                        say(f"IT it={ev[1]} k={ev[2]} conflicts={ev[3]} best={ev[4]}")
                    else:
                        say(f"SA it={ev[1]} k={ev[2]} conflicts={ev[3]} temp={ev[4]:.3f}")
            elif ev[0] == "col":
                found.append([self.t(), ev[1], proper(self.edges, ev[2])])
                self.led.mark(found=found)
                say(f"COLORING k={ev[1]} c={colours(ev[2])}")
                if ev[1] > 2:
                    say(f"TRY k={ev[1] - 1}")
            elif ev[0] == "stuck":
                say(f"STUCK k={ev[1] - 1} after={ev[2]} best={ev[1]}")
        self.drive(gen, handle)

    def exact(self):
        hint_path = os.path.join(os.path.dirname(_runs_dir()), "bounds", f"{self.name}.ub")
        clique, steps, _ = max_clique(self.adj)
        for _, c in steps:
            say(f"CLIQUE size={len(c)} v={' '.join(str(v + 1) for v in sorted(c))}")
        say(f"LB {len(clique)} source=clique")
        ctl, inc, seen = {}, [], [None]
        gen = bnb(self.adj, clique, ctl, tie_order(self.seed, self.n))
        last = [time.time()]

        def poll():
            try:
                st = os.stat(hint_path)
            except OSError:
                return
            key = (st.st_mtime_ns, st.st_size)
            if key == seen[0]:
                return
            seen[0] = key
            try:
                with open(hint_path) as f:
                    raw = [int(x) for x in f.read().split()]
            except (OSError, ValueError):
                raw = []
            if len(raw) != self.n:
                say("HINT unreadable")
                return
            labels = {c: i for i, c in enumerate(sorted(set(raw)))}
            ctl["hint"] = [labels[c] for c in raw]

        def handle(ev, units):
            if ev[0] == "node":
                if units % self.every == 0:
                    say(f"BRANCH n={ev[1]} d={ev[2]} v={ev[3] + 1} c={ev[4] + 1}")
                if time.time() - last[0] >= 5.0:
                    last[0] = time.time()
                    say(f"PROGRESS t={self.t():.1f} nodes={ctl['nodes']} lb={ctl['lb']} "
                        f"ub={ctl['ub']} closed={ctl['closed']:.6f}")
            elif ev[0] == "inc":
                inc.append([self.t(), ev[1], ev[3]])
                self.led.mark(inc=inc)
                say(f"INCUMBENT k={ev[1]} source={ev[3]}")
                if ev[3] == "hint":
                    say(f"HINT accepted k={ev[1]}")
                say(f"COLORING k={ev[1]} c={colours(ev[2])}")
            elif ev[0] == "reject":
                say(f"HINT rejected conflict={ev[1][0] + 1}-{ev[1][1] + 1}")
            elif ev[0] == "stale":
                say(f"HINT not better k={ev[1]}")
            elif ev[0] == "done":
                say(f"OPTIMAL chi={ev[1]} nodes={ctl['nodes']} reason={ev[2]}")
                self.led.mark(end="optimal")
        if self.drive(gen, handle, poll, self.p["limit_s"] / self.p.get("speed", 1.0)):
            say(f"LIMIT nodes={ctl['nodes']} lb={ctl['lb']} ub={ctl['ub']} "
                f"closed={ctl['closed']:.6f}")
            self.led.mark(end="limit")


def main():
    ap = argparse.ArgumentParser(description="Run one colouring solver on one graph.")
    ap.add_argument("--graph", required=True)
    ap.add_argument("--method", required=True, choices=("exact", "tabu", "anneal"))
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    p = load_image("campaign.img")
    if a.graph not in p["graphs"] or a.seed < 1:
        ap.error("unknown graph or bad seed")
    me = owner_of(a.graph, p["per"])
    alive = [x for x in live_labels(count_done=False) if owner_of(x.split("_")[0], p["per"]) == me]
    if len(alive) >= p["cap"]:
        say(f"# refused: {len(alive)} solvers of this owner are already running")
        os._exit(76)
    first = first_start()
    if first and time.time() - first > p["window_s"] / p.get("speed", 1.0):
        say("# refused: the campaign window is over")
        os._exit(77)
    r = Run(p, a.graph, a.method, a.seed)
    say(f"# gcol {a.method} graph={a.graph} n={r.n} m={len(r.edges)} seed={a.seed}")
    try:
        r.exact() if a.method == "exact" else r.heuristic()
        sys.stdout.flush()
    except BrokenPipeError:
        os._exit(141)
    r.led.beat(done=True)


if __name__ == "__main__":
    main()
