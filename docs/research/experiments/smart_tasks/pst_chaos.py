"""Fault schedule of experiment 4: crashes agent sessions and the Pluto server.

The schedule is a pure function of the seed. Times count from the start of the
agents and are identical in all arms; the server fault exists only in the
smarttasks arm, so it can only count against it. A reboot of the machine is
not simulated: page cache, /tmp, clocks and detached processes all survive.
"""

import threading
import time

import pst_procs


class Chaos:
    def __init__(self, events, t0, respawn, respawn_s, server=None, server_down_s=5.0):
        """``respawn(name, n)`` starts the n-th replacement of agent ``name``."""
        self.events = [e for e in sorted(events, key=lambda e: e[0])
                       if e[1] != "server" or server]
        self.t0, self.respawn, self.respawn_s = t0, respawn, respawn_s
        self.server, self.down_s = server, server_down_s
        self.waiting, self.count = [], {}
        self.log, self.threads = [], []

    def pending(self):
        """Replacements that are still to be started."""
        return bool(self.waiting)

    def _server(self):
        rec = {"kind": "server", "killed_at": time.time()}
        self.log.append(rec)
        try:
            self.server.kill()
            time.sleep(self.down_s)
            self.server.start()
            rec["up_at"] = time.time()
        except Exception as e:  # noqa: BLE001 - must not end the run; the row shows it
            rec["error"] = repr(e)

    def step(self, left, status):
        now, new = time.time(), []
        while self.events and now - self.t0 >= self.events[0][0]:
            _, kind, who = self.events.pop(0)
            if kind == "server":
                th = threading.Thread(target=self._server, daemon=True)
                th.start()
                self.threads.append(th)
                continue
            # the current session of this agent (original or a replacement)
            name = next((n for n in left if n.split(".")[0] == who), None)
            if name is None:
                self.log.append({"kind": "agent", "agent": who, "missed": True, "at": now})
                continue
            a = left.pop(name)
            if a.p.poll() is not None:
                left[name] = a                           # already over: let run_phase reap it
                self.log.append({"kind": "agent", "agent": who, "missed": True, "at": now})
                continue
            cmds = pst_procs.crash_tree(a)
            at = time.time()
            status[name] = {"rc": a.p.returncode, "timed_out": False, "expected_cutoff": False,
                            "crashed": True, "secs": round(at - a.t0, 2)}
            self.log.append({"kind": "agent", "agent": who, "session": name, "at": at,
                             "killed": cmds})
            self.waiting.append((at + self.respawn_s, who))
        for item in [w for w in self.waiting if now >= w[0]]:
            self.waiting.remove(item)
            n = self.count[item[1]] = self.count.get(item[1], 0) + 1
            new.append(self.respawn(item[1], n))
        return new

    def finish(self):
        for th in self.threads:
            th.join(timeout=40)
        for _, kind, who in self.events:                 # everybody had finished before
            self.log.append({"kind": kind, "agent": who, "missed": True})
        self.events = []

    def crashes(self):
        return [(e["at"], e["agent"]) for e in self.log
                if e["kind"] == "agent" and not e.get("missed")]
