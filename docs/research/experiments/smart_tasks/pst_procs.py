"""Process launching, descendant tracking and end-of-run cleanup.

Only processes the run started are ever signalled: the agents, descendants
observed while they ran (identified by pid + start time), and tty-less
processes of this user that started after the run began and whose command line
or working directory lies inside the run's unique workspace. The last rule
catches jobs that a Pluto server started on an agent's behalf.
"""

import json
import os
import signal
import subprocess
import threading
import time


def ps_table():
    """{pid: {"ppid", "tty", "start", "cmd"}}; start is the lstart string.
    Zombies are left out: they are already dead, cannot be signalled and hold
    nothing, but stay listed until their parent reaps them."""
    env = dict(os.environ, LC_ALL="C")
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,uid=,stat=,tty=,lstart=,command="],
                         capture_output=True, text=True, errors="replace", env=env).stdout
    tab = {}
    for line in out.splitlines():
        f = line.split(None, 10)
        if len(f) < 11 or not f[0].isdigit() or f[2] != str(os.getuid()) or f[3].startswith("Z"):
            continue
        tab[int(f[0])] = {"ppid": int(f[1]), "tty": f[4], "start": " ".join(f[5:10]), "cmd": f[10]}
    return tab


def cpu_share():
    """Share of all cores in use now, from ps's decaying per-process averages."""
    out = subprocess.run(["ps", "-A", "-o", "%cpu="], capture_output=True, text=True,
                         env=dict(os.environ, LC_ALL="C")).stdout
    return round(sum(float(x) for x in out.split()) / 100 / (os.cpu_count() or 1), 3)


def _epoch(start):
    try:
        return time.mktime(time.strptime(start, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return 0.0


def cwd_table():
    """{pid: cwd} of this user's processes (empty if lsof is missing)."""
    try:
        out = subprocess.run(["lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"],
                             capture_output=True, text=True, errors="replace", timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    tab, pid = {}, None
    for line in out.splitlines():
        if line[:1] == "p":
            pid = int(line[1:])
        elif line[:1] == "n" and pid is not None:
            tab[pid] = line[1:]
    return tab


class Tracker(threading.Thread):
    """Records every descendant of the run's agents, with its start time."""

    def __init__(self, interval=1.5):
        super().__init__(daemon=True)
        self.roots, self.known, self.interval = set(), {}, interval
        self.cpu = []                       # whole-machine CPU share samples, 0..1
        self._halt = threading.Event()
        self.lock = threading.Lock()

    def add(self, pid):
        with self.lock:
            self.roots.add(pid)

    def sample(self):
        tab = ps_table()
        with self.lock:
            live = {p for p in self.roots if p in tab}
            # an already-known descendant stays an ancestor after reparenting
            live |= {p for p, st in self.known.items() if p in tab and tab[p]["start"] == st}
            grew = True
            while grew:
                new = {p for p, r in tab.items() if r["ppid"] in live and p not in live}
                live |= new
                grew = bool(new)
            for p in live:
                self.known.setdefault(p, tab[p]["start"])

    def run(self):
        n = 0
        while not self._halt.wait(self.interval):
            n += 1
            try:
                self.sample()
                if n % 4 == 0:
                    self.cpu.append(cpu_share())
            except OSError:
                pass

    def stop(self):
        self._halt.set()
        self.join(timeout=10)


def own_processes(tracker, ws, t0):
    """Pids (with start times) of live processes that belong to the run."""
    tab, cwds, me = ps_table(), cwd_table(), os.getpid()
    skip, p = set(), me
    while p in tab and p not in skip:                   # never our own ancestors
        skip.add(p)
        p = tab[p]["ppid"]
    out, inside = {}, ws.rstrip("/") + "/"
    for pid, r in tab.items():
        if pid in skip or r["cmd"].startswith(("ps ", "lsof ")):
            continue
        tracked = tracker and tracker.known.get(pid) == r["start"]
        ours = (r["tty"] in ("??", "?", "-") and _epoch(r["start"]) >= int(t0)
                and (inside in r["cmd"] or (cwds.get(pid, "") + "/").startswith(inside)))
        if tracked or ours:
            out[pid] = r["start"]
    return out


def cleanup(tracker, ws, t0, rounds=4):
    """Terminate leftovers; several rounds, as a server may dequeue more jobs."""
    killed = {}
    for i in range(rounds):
        procs = own_processes(tracker, ws, t0)
        if not procs and i:
            break
        for sig in (signal.SIGTERM, signal.SIGKILL):
            tab = ps_table()
            for pid, start in procs.items():
                if pid in tab and tab[pid]["start"] == start:
                    killed.setdefault(pid, tab[pid]["cmd"][-160:])
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
            if procs:
                time.sleep(1.5)
    return list(killed.values())


class Agent:
    """One headless claude process with its transcript pump."""

    def __init__(self, name, cmd, prompt, cwd, log_dir, env, tracker, limit=None):
        self.name, self.limit = name, limit
        self.err = open(os.path.join(log_dir, f"{name}.stderr"), "w")
        self.t0 = time.time()
        self.p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.err,
                                  start_new_session=True)
        tracker.add(self.p.pid)
        self.path = os.path.join(log_dir, f"{name}.jsonl")
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()
        try:
            self.p.stdin.write(prompt.encode())
            self.p.stdin.close()
        except OSError:
            pass

    def _pump(self):
        with open(self.path, "w", encoding="utf-8") as out:
            for raw in self.p.stdout:
                try:
                    ev = json.loads(raw)
                    if not isinstance(ev, dict):
                        raise ValueError
                except ValueError:
                    ev = {"type": "_raw", "text": raw.decode("utf-8", "replace")[:2000]}
                ev["_recv_ts"] = time.time()
                out.write(json.dumps(ev) + "\n")
                out.flush()

    def signal(self, sig):
        try:
            self.p.send_signal(sig)                      # the claude process only, so that
        except OSError:                                  # detached jobs may survive it
            pass


def session_members(root, tab, pgid=None):
    """Pids a crash of ``root`` takes down: its live descendants by parent link
    and the members of its process group. A process that was detached (its
    launching shell has exited, so its parent is init) is in neither set."""
    live = {root} if root in tab else set()
    grew = True
    while grew:
        new = {p for p, r in tab.items() if r["ppid"] in live and p not in live}
        live |= new
        grew = bool(new)
    for p in tab:
        try:
            if os.getpgid(p) == (pgid or root):
                live.add(p)
        except OSError:
            pass
    return live - {os.getpid()}


def crash_tree(agent):
    """kill -9 of the agent's process tree; returns the commands killed.

    Everything is stopped first, so that nothing can be started, reparented or
    reported in between; pid reuse is excluded by comparing start times."""
    done = {}
    for _ in range(4):
        tab = ps_table()
        new = {p: tab[p]["start"] for p in session_members(agent.p.pid, tab) if p in tab
               and p not in done}
        if not new:
            break
        for p in new:
            try:
                os.kill(p, signal.SIGSTOP)
            except OSError:
                pass
        done.update(new)
    tab, cmds = ps_table(), []
    for p, start in done.items():
        if p in tab and tab[p]["start"] == start:
            cmds.append(tab[p]["cmd"][:160])
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    agent.p.wait()
    return cmds


def run_phase(agents, timeout, expected_cutoff=False, chaos=None):
    """Wait for all agents; stop the late ones. Returns {name: status}.

    ``chaos.step(left, status)`` may crash agents and returns replacements."""
    status, left = {}, {a.name: a for a in agents}
    agents = list(agents)
    try:
        while left or (chaos and chaos.pending()):
            for a in chaos.step(left, status) if chaos else ():
                agents.append(a)
                left[a.name] = a
            for name, a in list(left.items()):
                late = time.time() - a.t0 > (a.limit or timeout)
                if a.p.poll() is None and late:
                    a.signal(signal.SIGTERM)
                    try:
                        a.p.wait(8)
                    except subprocess.TimeoutExpired:
                        a.signal(signal.SIGKILL)
                        a.p.wait()
                if a.p.poll() is not None:
                    status[name] = {"rc": a.p.returncode, "timed_out": late,
                                    "expected_cutoff": expected_cutoff,
                                    "secs": round(time.time() - a.t0, 2)}
                    del left[name]
            time.sleep(0.2)
    finally:
        for a in left.values():                          # only if interrupted
            a.signal(signal.SIGKILL)
        for a in agents:
            # a surviving detached child may hold the pipe open, so do not wait for EOF
            a.pump.join(timeout=3)
            a.err.close()
    return status
