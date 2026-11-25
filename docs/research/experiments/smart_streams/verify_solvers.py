#!/usr/bin/env python3
"""Scripted solvers that use only what an agent sees (program output + docs).

    python3 verify_solvers.py DIR [--exp 1,2,3] [--seed N] [--duration S]

Each builds a full-size workspace in DIR, runs the real programs, writes the
answer and must score 1.0. Durations are the experiment's own unless given.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exp1_shards  # noqa: E402
import exp2_sssp  # noqa: E402
import exp3_relay  # noqa: E402
import ss_build  # noqa: E402


def _start(ws, log, *cmd):
    f = open(os.path.join(ws, log), "w")
    return subprocess.Popen([os.path.join(ws, "bin", cmd[0]), *cmd[1:]], stdout=f, stderr=f)


def _answer(ws, obj):
    with open(os.path.join(ws, "answers/final.json"), "w") as f:
        json.dump(obj, f)


def _lines(ws, log):
    with open(os.path.join(ws, log)) as f:
        return f.read().splitlines()


def solve1(ws):
    ps = [_start(ws, f"s{s}.log", "shard_stress", "--shard", str(s)) for s in range(1, 9)]
    assert all(p.wait() == 0 for p in ps)
    shards, bad, size = {}, [], 0
    for s in range(1, 9):
        size += os.path.getsize(os.path.join(ws, f"s{s}.log"))
        shards[s], b = exp1_shards.check_lines(_lines(ws, f"s{s}.log"))
        bad += [dict(x, shard=s) for x in b]
    bad.sort(key=lambda x: x["lt"])
    root = next(b for b in bad if b["verb"] == "CELL")
    path = [root["shard"]] + [b["shard"] for b in bad if b["verb"] == "XIN"]
    _answer(ws, {"shards": shards, "path": path, "root_cause":
                 {"shard": root["shard"], "op": root["op"], "cell": root["cell"]}})
    print(f"  log bytes: {size:,}")


def solve2(ws):
    with open(os.path.join(ws, "docs/BENCH.md")) as f:
        pairs = re.findall(r"pair \d: X=(V\d), Y=(V\d)", f.read())
    vs = [f"V{i}" for i in range(1, 9)]
    cost, low, settle, size = {}, {}, {}, 0

    def stop_dominated(running, si):
        for v, p in list(running.items()):
            if p.poll() is not None:
                del running[v]
                for line in _lines(ws, f"{v}_{si}.log"):
                    if line.startswith("RESULT"):
                        cost[v, si] = int(re.search(r"cost=(\d+)", line)[1])
                continue
            m = re.findall(r"cum=(\d+)", "\n".join(_lines(ws, f"{v}_{si}.log")[-200:]))
            low[v, si] = int(m[-1]) if m else 0
            # the defective variant may look best, so require two better finals
            better = sorted(c for (x, i), c in cost.items() if i == si)
            if len(better) >= 2 and low[v, si] > better[1]:
                p.send_signal(signal.SIGTERM)
                p.wait()
                del running[v]

    # sizes run concurrently here only within a size; agents may overlap sizes
    for si in range(5):
        running = {v: _start(ws, f"{v}_{si}.log", "spbench", "--variant", v, "--size", str(si))
                   for v in vs}
        while running:
            time.sleep(0.4)
            stop_dominated(running, si)
        for v in vs:
            size += os.path.getsize(os.path.join(ws, f"{v}_{si}.log"))
            settle[v, si] = {int(a): int(b) for a, b in re.findall(
                r"^SETTLE v=(\d+) d=(\d+)", "\n".join(_lines(ws, f"{v}_{si}.log")), re.M)}
    bug = None
    for si in range(5):
        votes = {}
        for v in vs:
            for x, d in settle[v, si].items():
                votes.setdefault(x, {}).setdefault(d, []).append(v)
        for x in sorted(votes):
            if len(votes[x]) > 1 and bug is None:
                good = max(votes[x], key=lambda d: len(votes[x][d]))
                odd = next(v[0] for d, v in votes[x].items() if d != good)
                bug = {"variant": odd, "size": si, "vertex": x, "distance": good}
    ok = [v for v in vs if v != bug["variant"]]
    winners = [min((cost[v, i], v) for v in ok if (v, i) in cost)[1] for i in range(5)]

    def cheaper(x, y, i):
        if (x, i) in cost and (y, i) in cost:
            return cost[x, i] < cost[y, i]
        if (x, i) in cost:
            return cost[x, i] < low[y, i]
        if (y, i) in cost and low[x, i] >= cost[y, i]:
            return False
        for v, n in ((x, i), (y, i)):           # undecided: complete the pair
            if (v, n) not in cost:
                p = _start(ws, f"{v}_{n}.log", "spbench", "--variant", v, "--size", str(n))
                p.wait()
                cost[v, n] = int(re.search(r"^RESULT.*cost=(\d+)",
                                           "\n".join(_lines(ws, f"{v}_{n}.log")), re.M)[1])
        return cost[x, i] < cost[y, i]

    cross = [next(i for i in range(5) if cheaper(x, y, i)) for x, y in pairs]
    _answer(ws, {"winners": winners, "crossovers": cross, "bug": bug})
    print(f"  log bytes: {size:,}")


def solve3(ws):
    ps = {j: _start(ws, f"j{j}.log", "soak", "--job", str(j)) for j in range(1, 7)}
    time.sleep(2)
    meta = {"phase1_end": time.time()}
    jobs, quiet = {}, {j: (0, time.time()) for j in ps}
    left, stalled = dict(ps), set()
    while left:
        time.sleep(0.5)
        for j, p in list(left.items()):
            size = os.path.getsize(os.path.join(ws, f"j{j}.log"))
            if size != quiet[j][0]:
                quiet[j] = (size, time.time())
            if p.poll() is not None:
                del left[j]
            elif time.time() - quiet[j][1] > 8:
                p.terminate()
                p.wait()
                stalled.add(j)
                del left[j]
    for j, p in ps.items():
        lim, viol, last, dig, rid = 10**9, None, None, None, None
        for line in _lines(ws, f"j{j}.log"):
            if line.startswith("#"):
                rid = re.search(r"run_id=(\w+)", line)[1]
                continue
            last = int(line[1:6])
            m = re.search(r"probe_limit=(\d+)", line)
            lim = int(m[1]) if m else lim
            m = re.search(r"bucket=(\d+) probe=(\d+)", line)
            if m and int(m[2]) > lim and viol is None:
                viol = {"epoch": last, "bucket": int(m[1])}
            m = re.search(r"table_digest=(\w+)", line)
            dig = m[1] if m else dig
        jobs[j] = {"run_id": rid, "first_violation": viol, "final_digest": dig, "last_epoch": last,
                   "outcome": "STALLED" if j in stalled else "COMPLETED" if dig else "CRASHED",
                   "exit_code": None if j in stalled else p.returncode}
    _answer(ws, {"jobs": jobs})
    time.sleep(5)
    print(f"  log bytes: {sum(os.path.getsize(os.path.join(ws, f'j{j}.log')) for j in ps):,}")
    return dict(meta, run_end=time.time())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--exp", default="1,2,3")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--duration", type=float, help="scale jobs to about this many seconds")
    a = ap.parse_args()
    for e in a.exp.split(","):
        mod, solver = {"1": (exp1_shards, solve1), "2": (exp2_sssp, solve2),
                       "3": (exp3_relay, solve3)}[e]
        ws = os.path.join(os.path.realpath(a.dir), f"verify{e}")
        shutil.rmtree(ws, ignore_errors=True)
        kw = {}
        if a.duration:
            kw = {"best_large": a.duration} if e == "2" else {"duration": a.duration}
        t = time.time()
        mod.build(ws, a.seed, **kw)
        tb = time.time() - t
        meta = solver(ws)
        g = mod.grade(ws, mod.truth(a.seed, **kw), meta)
        print(f"exp{e}: score={g['score']} build={tb:.1f}s total={time.time() - t:.0f}s "
              f"{json.dumps({k: v for k, v in g.items() if k != 'detail'})}")
        for p in ss_build.launches(ws):
            assert p["t_last"] >= p["t0"]


if __name__ == "__main__":
    main()
