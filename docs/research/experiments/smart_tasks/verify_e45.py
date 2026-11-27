#!/usr/bin/env python3
"""Scripted solvers of experiments 4 and 5 (no LLM); each must score 1.0.

    python3 verify_e45.py DIR [--exp 4,5] [--style plain|jobs] [--seed N] [--policy P]

E4 runs one supervisor process per agent. The harness crashes three of them (and, in
style ``jobs``, the Pluto server) with the real schedule and code of the runner; a
replacement starts with nothing but the workspace. ``plain`` detaches each program
with a wrapper, a pid file and an exit-code file; ``jobs`` uses PlutoJobsClient.
Policy ``naive`` (plain only) starts programs as children, so that they die with
their session, and always restarts from zero: it must score clearly lower.
E5 has the privileged reader answer questions live and read the logs afterwards.
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
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exp4_crash as e4  # noqa: E402
import exp5_baselines  # noqa: E402
import exp5_probe as e5  # noqa: E402
import run as pst_run  # noqa: E402
import pst_build  # noqa: E402
import pst_chaos  # noqa: E402
import pst_procs  # noqa: E402
import verify_solvers as vs  # noqa: E402

LAUNCH = ('(bin/sweep "$@" > "$OUT.log" 2>&1 & p=$!; echo $p > "$OUT.pid.t"; '
          'mv "$OUT.pid.t" "$OUT.pid"; wait $p; echo $? > "$OUT.exit") >/dev/null 2>&1 &')


class PlainFleet:
    """Detached programs, found again through files; needs nothing else."""

    def __init__(self, ws, me, detach=True):
        self.ws, self.detach, self.kids, self.missing = ws, detach, {}, {}
        self.dir = os.path.join(ws, "logs")
        os.makedirs(self.dir, exist_ok=True)

    def runs(self, j):
        """[(name, log path)] of a job's launches, oldest first."""
        names = [f[:-4] for f in os.listdir(self.dir) if re.fullmatch(rf"j{j}_\d+\.log", f)]
        names.sort(key=lambda n: int(n.split("_")[1]))
        return [(n, os.path.join(self.dir, n + ".log")) for n in names]

    def start(self, j, args):
        n = f"j{j}_{len(self.runs(j)) + 1}"
        base = os.path.join(self.dir, n)
        if not self.detach:
            with open(base + ".log", "wb") as f:
                p = subprocess.Popen(["bin/sweep", *args], cwd=self.ws, stdout=f, stderr=f)
            self.kids[n] = p
            with open(base + ".pid", "w") as f:
                f.write(str(p.pid))
            return
        open(base + ".log", "wb").close()
        subprocess.run(["sh", "-c", "nohup sh -c '" + LAUNCH.replace("'", "'\\''") + "' x \"$@\"",
                        "x", *args], cwd=self.ws, env=dict(os.environ, OUT=base), check=True,
                       start_new_session=True, stdin=subprocess.DEVNULL)

    def _read(self, name, ext):
        try:
            with open(os.path.join(self.dir, name + ext)) as f:
                return int(f.read())
        except (OSError, ValueError):
            return None

    def state(self, name):
        """('run', None), ('exit', code) or ('gone', None)"""
        if name in self.kids:
            rc = self.kids[name].poll()
            if rc is not None:
                with open(os.path.join(self.dir, name + ".exit"), "w") as f:
                    f.write(str(rc))
            return ("run", None) if rc is None else ("exit", rc)
        rc, pid = self._read(name, ".exit"), self._read(name, ".pid")
        if rc is not None:
            return "exit", rc
        if pid is None:
            return "run", None
        try:
            os.kill(pid, 0)
            return "run", None
        except ProcessLookupError:                      # the wrapper may be about to write the code
            first = self.missing.setdefault(name, time.time())
            return ("gone" if time.time() - first > 3 else "run"), None

    def stop(self, name):
        pid = self._read(name, ".pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class JobsFleet:
    """SmartTasks jobs through the real client; waits while the server is down.

    The scripted session uses the agent id directly, as the HTTP API allows, so it
    owns the jobs of the session it replaces (a replaced MCP adapter may not)."""

    def __init__(self, ws, me):
        sys.path.insert(0, os.path.join(pst_run.REPO, "src_py"))
        from pluto_sdk import PlutoHttpClient, PlutoJobsClient
        cfg = json.load(open(ws + "_pluto/conn.json"))
        os.environ["PLUTO_SMARTTASKS_HOME"] = self.home = cfg["home"]
        self.ws, self.client = ws, PlutoJobsClient(
            PlutoHttpClient("127.0.0.1", cfg["port"], agent_id=me))
        self.ids, self.down = {}, 0

    def call(self, op, **kw):
        end = time.time() + 60
        while True:
            try:
                return self.client.call(op, **kw)
            except Exception as e:  # noqa: BLE001 - server down
                if time.time() > end or "job_" in str(e):
                    raise
                self.down += 1
                time.sleep(0.5)

    def rows(self):
        return self.call("list", scope="all", limit=999)["jobs"]

    def refresh(self):
        self.snap = {r["job_id"]: r for r in self.rows()}

    def runs(self, j):
        xs = [r for r in self.snap.values() if str(r.get("name", "")).startswith(f"j{j}_")]
        xs.sort(key=lambda r: int(r["name"].split("_")[1]))
        return [(r["job_id"], os.path.join(self.home, "jobs", r["job_id"], "output.log"))
                for r in xs]

    def start(self, j, args):
        n = len(self.runs(j)) + 1
        self.call("start", argv=["bin/sweep", *args], cwd=self.ws, name=f"j{j}_{n}",
                  request_id=f"{os.getpid()}-{j}-{n}")
        self.refresh()

    def state(self, name):
        r = self.snap.get(name)
        if not r or r["state"] not in vs.DONE:
            return "run", None
        if r["state"] in ("succeeded", "failed"):
            return "exit", self.call("status", job_id=name).get("exit_code")
        return "gone", None

    def stop(self, name):
        try:
            self.call("control", job_id=name, action="cancel")
        except Exception:  # noqa: BLE001 - already over
            pass


def read_log(path):
    """Facts of one launch from its output: (last unit, first bad unit, sums, digest)."""
    last = bad = digest = None
    sums, ckpts, note = {}, [], False
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        text = ""
    for x in text.split("\n")[:-1]:
        if x.startswith("U") and " crc=" in x:
            body, crc = x.rsplit(" crc=", 1)
            unit = int(x[1:5])
            if " SUM " in x:
                last = unit
            if f"{zlib.crc32(body.encode()) & 0xffff:04x}" != crc:
                bad = bad or unit
            elif " SUM " in x and not bad:
                sums[unit] = int(re.search(r"inv=(\d+)", x)[1])
        elif x.startswith("# checkpoint ckpt/"):
            ckpts.append(x.split()[2])
        elif x.startswith("# checkpoint unreadable"):
            note = True
        elif x.startswith("RESULT"):
            digest = x.split("digest=")[1]
    return {"last": last, "bad": bad, "sums": sums, "ck": ckpts, "digest": digest,
            "refused": note, "size": len(text)}


def supervise(ws, me, style, policy, hang_s):
    """One session: take stock of the workspace, then keep ten jobs going."""
    fleet = JobsFleet(ws, me) if style == "jobs" else PlainFleet(ws, me, policy != "naive")
    jobs = [j for j in range(1, e4.JOBS + 1) if e4.owner({"job": j}) == me]
    grown, answered = {}, set()
    while len(answered) < len(jobs):
        if style == "jobs":
            fleet.refresh()
        for j in jobs:
            if j in answered:
                continue
            runs = fleet.runs(j)
            if not runs:
                fleet.start(j, ["--job", str(j)])
                continue
            name, path = runs[-1]
            lg, (st, rc) = read_log(path), fleet.state(name)
            if grown.get(name, (0, 0))[0] != lg["size"]:
                grown[name] = (lg["size"], time.time())
            silent = time.time() - grown.setdefault(name, (lg["size"], time.time()))[1]
            if st == "run" and not lg["bad"] and silent < hang_s:
                continue
            if st == "exit" and rc == 0 and not lg["bad"]:
                report(ws, fleet, j, runs)
                answered.add(j)
                continue
            if st == "run":
                fleet.stop(name)
            args = ["--job", str(j)]
            good = best_checkpoint(ws, j, [read_log(p) for _, p in runs])
            if good and policy != "naive":
                args += ["--resume", good]
            fleet.start(j, args)
        time.sleep(0.6)


def best_checkpoint(ws, j, logs):
    """Newest checkpoint before the first corrupt unit of its launch that has not been refused."""
    ok = {}
    for lg in logs:
        for c in lg["ck"]:
            unit = int(os.path.basename(c)[:4])
            ok[c] = not lg["bad"] or unit < lg["bad"]
    path = [c for c in sorted(ok) if ok[c]]
    while path:
        p = os.path.join(ws, path[-1])                  # a torn file is much smaller
        if os.path.exists(p) and os.path.getsize(p) > 100:
            return path[-1]
        path.pop()
    return None


def report(ws, fleet, j, runs):
    sums, fails, digest = {}, [], None
    for i, (name, path) in enumerate(runs):
        lg, (st, rc) = read_log(path), fleet.state(name)
        sums.update({u: v for u, v in lg["sums"].items() if u not in sums})
        digest = lg["digest"] or digest
        if i == len(runs) - 1 or lg["refused"]:
            continue
        if lg["bad"]:
            fails.append({"unit": lg["bad"], "kind": "CORRUPT", "exit_code": None})
        elif st == "exit" and rc not in (0, 143, 137, -9, -15):
            fails.append({"unit": lg["last"], "kind": "CRASH", "exit_code": rc})
        elif os.path.exists(os.path.join(ws, "ckpt")) and lg["last"]:
            fails.append({"unit": lg["last"], "kind": "HANG", "exit_code": None})
    best = max(sums, key=lambda u: (sums[u], -u))
    path = os.path.join(ws, f"answers/job_{j}.json")
    with open(path + ".t", "w") as f:
        json.dump({"digest": digest, "peak_unit": best, "peak_inv": sums[best],
                   "failures": fails}, f)
    os.replace(path + ".t", path)


def run4(ws, style, policy, seed, scale=1.0, **kw):
    """Returns (meta, chaos log). Supervisors are real processes so they can be killed."""
    server = None
    if style == "jobs":
        server = pst_run.private_pluto(ws + "_pluto")
        server.start()
        with open(ws + "_pluto/conn.json", "w") as f:
            json.dump({"port": server.port, "home": server.home}, f)
    tracker, t0 = pst_procs.Tracker(), time.time()
    tracker.start()
    logs = ws + "_logs"
    os.makedirs(logs, exist_ok=True)

    def launch(me, session):
        cmd = [sys.executable, os.path.abspath(__file__), ws, "--as", me, "--style", style,
               "--policy", policy]
        return pst_procs.Agent(session, cmd, "", ws, logs, dict(os.environ), tracker)
    chaos = pst_chaos.Chaos(e4.chaos(seed, scale), t0, lambda me, n: launch(me, f"{me}.r{n}"),
                           e4.RESPAWN_S, server, e4.SERVER_DOWN_S)
    try:
        status = pst_procs.run_phase([launch(f"w{i}", f"w{i}") for i in range(1, 5)], e4.TIMEOUT,
                                    False, chaos)
        chaos.finish()
        meta = {"run_end": time.time(), "crashes": chaos.crashes()}
    finally:
        tracker.stop()
        if server:
            pst_run.stop_pluto(server)
        pst_procs.cleanup(tracker, ws, t0)
    if [s for s in status.values() if s["rc"] and not s.get("crashed")]:
        print("  supervisor failed:", status)
    with open(os.path.join(ws, "answers/final.json"), "w") as f:
        json.dump({"jobs": {str(j): pst_build.read_json(os.path.join(ws, f"answers/job_{j}.json"))
                            for j in range(1, e4.JOBS + 1)}}, f)
    return meta, chaos.log


def run5(ws, be, seed, **kw):
    pl = e5.plan(seed, **kw)
    hs = {s: be.start(e5.owner({"stream": s}), "probe", "--stream", str(s)) for s in pl}
    tails, lines = {s: vs.Tail(h) for s, h in hs.items()}, {s: [] for s in hs}
    asks = exp5_baselines._rx(exp5_baselines.wl_probe.ASKS)
    decoys = exp5_baselines._rx(exp5_baselines.wl_probe.DECOYS)
    left = set(hs)
    while left:
        time.sleep(0.7)
        for s in sorted(left):
            rc = be.poll(hs[s])
            new = tails[s].lines()
            lines[s] += new
            for x in new:
                if asks.fullmatch(x) and not decoys.fullmatch(x):
                    with open(os.path.join(ws, f"control/stream_{s}.ack"), "w") as f:
                        f.write(exp5_baselines.CODE.search(x)[0] + "\n")
            if rc is not None:
                assert rc == 0, (s, rc)
                lines[s] += tails[s].lines()
                left.discard(s)
    vs._answer(ws, {"streams": {str(s): exp5_baselines.privileged(pl[s], lines[s])[0]
                                for s in pl}})
    return {"run_end": time.time()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--exp", default="4,5")
    ap.add_argument("--style", default="plain", choices=("plain", "jobs"))
    ap.add_argument("--policy", default="wise", choices=("wise", "naive"))
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--as", dest="me", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.me:
        return supervise(a.dir, a.me, a.style, a.policy, 16.0)
    for e in a.exp.split(","):
        mod = {"4": e4, "5": e5}[e]
        ws = os.path.join(os.path.realpath(a.dir), f"v{e}{a.style}{a.policy}{a.seed}")
        for d in (ws, ws + "_pluto", ws + "_logs"):
            shutil.rmtree(d, ignore_errors=True)
        mod.build(ws, a.seed)
        t = time.time()
        log = []
        if e == "4":
            meta, log = run4(ws, a.style, a.policy, a.seed)
        else:
            be = vs.Plain(ws) if a.style == "plain" else vs.Jobs(ws)
            tracker = pst_procs.Tracker()
            try:
                meta = run5(ws, be, a.seed)
            finally:
                be.close()
                pst_procs.cleanup(tracker, ws, t)
        wall = time.time() - t
        g = mod.grade(ws, mod.truth(a.seed), meta)
        recs = pst_build.launches(ws)
        detail = g.pop("detail", {})
        print(f"exp{e} {a.style} {a.policy} seed={a.seed}: score={g['score']} wall={wall:.0f}s "
              f"launches={len(recs)} peaks="
              f"{pst_build.peak_streams(recs, [], meta, mod.owner)} "
              f"cpu={sum(r['cpu'] for r in recs):.0f}s max_lag={max(r['lag'] for r in recs):.2f}s",
              flush=True)
        print("  " + json.dumps(g))
        for x in log:
            print("  chaos", {k: v if k != "killed" else len(v) for k, v in x.items()})
        for j, d in detail.items():
            if not d["done"] or d["unneeded"] or d["orphans"]:
                print("  job", j, d)


if __name__ == "__main__":
    main()
