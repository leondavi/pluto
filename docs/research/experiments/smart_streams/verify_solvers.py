#!/usr/bin/env python3
"""Scripted solvers that use only what an agent sees (program output + docs).

    python3 verify_solvers.py DIR [--exp 1,2,3] [--style plain|jobs] [--seed N]

Each builds a full-size workspace in DIR and must score 1.0. Style ``plain``
starts subprocesses that write log files; style ``jobs`` starts, stops and
reaps every stream through the real PlutoJobsClient against a private server
(output is still analysed from the job's own output.log). ``--scale`` shortens
the runs (reaction windows are not shortened, so keep it above about 0.4).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exp1_shards  # noqa: E402
import exp2_sssp  # noqa: E402
import exp3_relay  # noqa: E402
import run as ss_run  # noqa: E402
import ss_build  # noqa: E402
import ss_procs  # noqa: E402

DONE = ("succeeded", "failed", "cancelled", "signaled", "timed_out", "output_limit",
        "orphaned", "lost")


class Plain:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    def start(self, agent, *cmd):
        self.n += 1
        path = os.path.join(self.ws, f"plain{self.n}.log")
        f = open(path, "wb")
        p = subprocess.Popen([os.path.join(self.ws, "bin", cmd[0]), *cmd[1:]], cwd=self.ws,
                             stdout=f, stderr=f, start_new_session=True)
        return {"p": p, "log": path}

    def poll(self, h):
        return h["p"].poll()

    def stop(self, h):
        if h["p"].poll() is None:
            h["p"].terminate()
            h["p"].wait()

    def close(self):
        pass


class Jobs:
    """Streams as SmartStreams jobs, polled with one ``list`` per second."""

    def __init__(self, ws):
        self.server = ss_run.private_pluto(ws + "_pluto")
        self.server.start()
        self.clients, self.ws, self.states, self.at = {}, ws, {}, 0.0
        self.calls = []

    def client(self, agent):
        if agent not in self.clients:
            self.clients[agent] = self.server.jobs(agent)
        return self.clients[agent]

    def call(self, agent, op, **kw):
        t = time.time()
        r = self.client(agent).call(op, **kw)
        self.calls.append((op, time.time() - t))
        return r

    def start(self, agent, *cmd):
        t = time.time()
        r = self.client(agent).start(argv=["bin/" + cmd[0], *cmd[1:]], cwd=self.ws)
        self.calls.append(("start", time.time() - t))
        return {"id": r["job_id"], "agent": agent,
                "log": self.server.path(r["job_id"], "output.log")}

    def poll(self, h):
        if time.time() - self.at > 1.0:
            rows = self.call(h["agent"], "list", scope="all", limit=999)["jobs"]
            self.states, self.at = {r["job_id"]: r for r in rows}, time.time()
        r = self.states.get(h["id"])
        if not r or r["state"] not in DONE:
            return None
        if "rc" not in h:
            h["rc"] = self.call(h["agent"], "status", job_id=h["id"]).get("exit_code", -1)
        return h["rc"]

    def stop(self, h):
        self.call(h["agent"], "control", job_id=h["id"], action="cancel")

    def close(self):
        ss_run.stop_pluto(self.server)
        by = {}
        for op, dt in self.calls:
            by.setdefault(op, []).append(dt)
        for op, x in sorted(by.items()):
            print(f"  api {op}: n={len(x)} mean={sum(x) / len(x) * 1e3:.0f}ms max={max(x) * 1e3:.0f}ms")


class Tail:
    """Incremental line reader of a growing log."""

    def __init__(self, h):
        self.h, self.pos, self.rest = h, 0, b""

    def lines(self):
        try:
            with open(self.h["log"], "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        self.pos += len(data)
        *out, self.rest = (self.rest + data).split(b"\n")
        return [x.decode() for x in out]


def _answer(ws, obj):
    with open(os.path.join(ws, "answers/final.json"), "w") as f:
        json.dump(obj, f)


def solve1(ws, be):
    hs = {s: be.start(exp1_shards.owner({"shard": s}), "shard_stress", "--shard", str(s))
          for s in range(1, 49)}
    tails, log, spin = {s: Tail(h) for s, h in hs.items()}, {s: [] for s in hs}, {}
    left = set(hs)
    while left:
        time.sleep(0.7)
        for s in sorted(left):
            rc = be.poll(hs[s])
            new = tails[s].lines()
            log[s] += new
            for x in new:
                if " SPIN " in x:
                    if x.endswith("resolved"):
                        spin.pop(s, None)
                    else:
                        spin.setdefault(s, time.time())
            if rc is not None:
                assert rc == 0, (s, rc, log[s][-2:])
                log[s] += tails[s].lines()
                left.discard(s)
            elif s in spin and time.time() - spin[s] > 22:
                be.stop(hs[s])
                left.discard(s)
    shards, bad = {}, []
    for s in hs:
        shards[s], b = exp1_shards.check_lines(log[s])
        bad += [dict(x, shard=s) for x in b]
    bad.sort(key=lambda x: x["lt"])
    cells = []
    for c in {b["cell"] for b in bad}:
        b = [x for x in bad if x["cell"] == c]
        cells.append({"cell": c, "shard": b[0]["shard"], "op": b[0]["op"],
                      "path": [b[0]["shard"]] + [x["shard"] for x in b if x["verb"] == "XIN"]})
    _answer(ws, {"shards": shards, "cells": cells})


def solve2(ws, be):
    names = [f"V{i}" for i in range(1, 13)]
    cells = {(v, i): {} for v in names for i in range(4)}
    running, queue, settle, drop = {}, [], {}, {}
    cum_seen = {}

    def agent(v):
        return exp2_sssp.owner({"variant": v})

    def refill():
        for c, st in cells.items():
            if not any(k[:2] == c for k in running) and not any(q[:2] == c for q in queue):
                queue.extend((*c, k) for k in exp2_sssp.next_probes(st))
        for q in list(queue):
            if sum(agent(r[0]) == agent(q[0]) for r in running) < exp2_sssp.CAP - 1:
                h = be.start(agent(q[0]), "spbench", "--variant", q[0], "--size", str(q[1]),
                             "--tune", str(q[2]))
                running[q] = [h, Tail(h), 0, time.time()]
                queue.remove(q)

    refill()
    while running:
        time.sleep(0.5)
        for q, r in list(running.items()):
            rc = be.poll(r[0])
            v, i, k = q
            st, final = cells[v, i], None
            for x in r[1].lines():
                if x.startswith("TICK"):
                    c = int(re.search(r"cum=(\d+)", x)[1])
                    if c < r[2]:
                        drop.setdefault(v, set()).add(i)
                    if c != r[2]:
                        r[3] = time.time()
                    r[2] = c
                elif x.startswith("SETTLE"):
                    m = re.search(r"v=(\d+) d=(\d+)", x)
                    settle.setdefault((i, int(m[1])), {}).setdefault(int(m[2]), set()).add(v)
                elif x.startswith("RESULT"):
                    final = int(re.search(r"cost=(\d+)", x)[1])
                elif x.startswith("#") and "refused" in x:
                    raise SystemExit(x)
            if final is not None:
                st[k] = ("done", final)
                del running[q]
                continue
            done = [s[1] for s in st.values() if s[0] == "done"]
            if done and r[2] > min(done):
                st[k] = ("gt", min(done))
            elif r[2] and time.time() - r[3] > 11:
                st[k] = ("hang",)
            else:
                assert rc is None, (q, rc)
                continue
            be.stop(r[0])
            del running[q]
        refill()
    ctr, sizes = next(iter(drop.items()))
    odd, wrong = None, []
    for (i, x), votes in sorted(settle.items()):
        if len(votes) > 1:
            good = max(votes, key=lambda d: len(votes[d]))
            odd = next(next(iter(v)) for d, v in votes.items() if d != good)
            wrong.append((i, x, good))
    podium = []
    for i in range(4):
        best = sorted(min((s[1], k) for k, s in cells[v, i].items() if s[0] == "done") + (v,)
                      for v in names if v not in (ctr, odd))
        podium.append([[v, k] for _, k, v in best[:3]])
    i, x, good = min(wrong)
    _answer(ws, {"podium": podium, "defects": {
        "DISTANCE": {"variant": odd, "size": i, "vertex": x, "distance": good},
        "COUNTER": {"variant": ctr, "size": min(sizes)}}})


def solve3(ws, be):
    hs = {j: be.start(exp3_relay.owner({"job": j}), "soak", "--job", str(j))
          for j in range(1, 25)}
    time.sleep(3)
    meta = {"phase1_end": time.time()}
    if isinstance(be, Jobs):                # hand over as agents would
        for j, h in hs.items():
            q = exp3_relay.owner2({"job": j})
            be.call(h["agent"], "transfer", job_id=h["id"], to=q)
            h["agent"] = q
    tails, log, quiet = {j: Tail(h) for j, h in hs.items()}, {j: [] for j in hs}, {}
    left, stalled, tokens = set(hs), set(), {j: "" for j in hs}
    while left:
        time.sleep(0.6)
        for j in sorted(left):
            rc = be.poll(hs[j])
            new = tails[j].lines()
            log[j] += new
            if new or j not in quiet:
                quiet[j] = time.time()
            for x in new:
                m = re.search(r"CKPT request token=(\w+)", x)
                if m:
                    tokens[j] += m[1] + "\n"
                    with open(os.path.join(ws, "control", f"job_{j}.ack"), "w") as f:
                        f.write(tokens[j])
            if rc is not None:
                hs[j]["rc"] = rc
                log[j] += tails[j].lines()
                left.discard(j)
            elif time.time() - quiet[j] > 46:
                be.stop(hs[j])
                stalled.add(j)
                left.discard(j)
    jobs, spikes = {}, {}
    for j in hs:
        lim, viol, leak, last, dig, rid, prev = 10**9, None, None, None, None, None, None
        spikes[j] = set()
        for x in log[j]:
            if x.startswith("#"):
                m = re.search(r"run_id=(\w+)", x)
                rid = m[1] if m else rid
                continue
            last = int(x[1:6])
            f = dict(t.split("=", 1) for t in x.split() if "=" in t)
            lim = int(f.get("probe_limit", lim))
            dig = f.get("table_digest", dig)
            if "bucket" not in f:
                continue
            if int(f["probe"]) > lim and viol is None:
                viol = {"epoch": last, "bucket": int(f["bucket"])}
            live = int(f["live"])
            if prev is not None and abs(live - prev) > 3 and leak is None:
                leak = last
            prev = live
            if int(f["disp"]) >= 90:
                spikes[j].add(last)
        jobs[j] = {"run_id": rid, "first_violation": viol, "first_leak": leak,
                   "final_digest": dig, "last_epoch": last,
                   "outcome": "STALLED" if j in stalled else "COMPLETED" if dig else "CRASHED",
                   "exit_code": None if j in stalled else hs[j]["rc"]}
    groups, todo = [], set(hs)
    while todo:
        a = min(todo)
        g = [b for b in sorted(todo) if b == a or sum(
            any(abs(x - y) <= 4 for y in spikes[b]) for x in spikes[a]) >= 5]
        groups.append(g)
        todo -= set(g)
    _answer(ws, {"jobs": jobs, "nodes": groups})
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--exp", default="1,2,3")
    ap.add_argument("--style", default="plain", choices=("plain", "jobs"))
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    for e in a.exp.split(","):
        mod, solver = {"1": (exp1_shards, solve1), "2": (exp2_sssp, solve2),
                       "3": (exp3_relay, solve3)}[e]
        ws = os.path.join(os.path.realpath(a.dir), f"v{e}{a.style}")
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(ws + "_pluto", ignore_errors=True)
        kw = {}
        if a.scale != 1:
            kw = ({"best_s": exp2_sssp.BEST_S * a.scale} if e == "2"
                  else {"duration": mod.DURATION * a.scale})
        mod.build(ws, a.seed, **kw)
        be = Plain(ws) if a.style == "plain" else Jobs(ws)
        tracker = ss_procs.Tracker()
        tracker.start()
        t = time.time()
        try:
            meta = solver(ws, be) or {}
        finally:
            wall = time.time() - t
            tracker.stop()
            be.close()
            killed = ss_procs.cleanup(tracker, ws, t)
        g = mod.grade(ws, mod.truth(a.seed, **kw), meta)
        recs = ss_build.launches(ws)
        peaks = ss_build.peak_streams(recs, [], meta, mod.owner, getattr(mod, "owner2", None))
        logs = [os.path.join(b, f) for d in (ws, ws + "_pluto") for b, _, fs in os.walk(d)
                for f in fs if f == "output.log" or f.startswith("plain")]
        cpu = tracker.cpu or [0.0]
        g.pop("detail", None)
        print(f"exp{e} {a.style}: score={g['score']} wall={wall:.0f}s "
              f"MB={sum(map(os.path.getsize, logs)) / 1e6:.0f} launches={len(recs)} "
              f"peaks={dict(sorted(peaks.items()))} "
              f"total_peak={ss_build.peak([(r['t0'], r['t_last']) for r in recs])} "
              f"program_cpu={sum(r['cpu'] for r in recs):.0f}s "
              f"machine_cpu mean={sum(cpu) / len(cpu):.2f} max={max(cpu):.2f} "
              f"max_lag={max(r['lag'] for r in recs):.2f}s leftovers={len(killed)}", flush=True)
        print("  " + json.dumps({k: v for k, v in g.items() if k != "parts" or e != "3"}))
        if e == "3":
            print("  low parts:", {k: v for k, v in g["parts"].items() if v < 1})


if __name__ == "__main__":
    main()
