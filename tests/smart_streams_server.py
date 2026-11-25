"""Private Pluto server on spare ports for SmartStreams live tests.

Never uses the tracked config, the default ports, or ~/.pluto.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src_py"))

from pluto_sdk import PlutoHttpClient, PlutoJobsClient  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, base, **cfg):
        self.base = str(base)
        self.home = os.path.join(self.base, "home")
        self.port = free_port()
        os.makedirs(self.base, exist_ok=True)
        self.config = os.path.join(self.base, "config.json")
        with open(self.config, "w") as f:
            json.dump({"pluto_server": {"host_ip": "127.0.0.1",
                                        "host_tcp_port": free_port(),
                                        "host_http_port": self.port},
                       "smartstreams": cfg}, f)
        self.proc = None

    def start(self):
        env = dict(os.environ, PLUTO_CONFIG=self.config,
                   PLUTO_SMARTSTREAMS_HOME=self.home)
        ebin = os.path.join(ROOT, "src_erl", "_build", "default", "lib",
                            "pluto", "ebin")
        self.log = open(os.path.join(self.base, "server.log"), "ab")
        self.proc = subprocess.Popen(
            ["erl", "-noshell", "-pa", ebin,
             "-pluto", "persistence_dir", f'"{self.base}/state"',
             "-pluto", "event_log_dir", f'"{self.base}/events"',
             "-pluto", "signal_dir", f'"{self.base}/signals"',
             "-eval", "{ok,_}=application:ensure_all_started(pluto)."],
            env=env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            cwd=self.base)
        end = time.time() + 20
        while time.time() < end:
            try:
                PlutoHttpClient("127.0.0.1", self.port)._get("/health")
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start, see " + self.log.name)

    def kill(self, sig=signal.SIGKILL):
        self.proc.send_signal(sig)
        self.proc.wait()

    def jobs(self, agent="a1", as_user=False):
        os.environ["PLUTO_SMARTSTREAMS_HOME"] = self.home
        c = PlutoJobsClient(PlutoHttpClient("127.0.0.1", self.port,
                                            agent_id=agent), as_user=as_user)
        c.call("list")
        return c

    def path(self, job_id, name):
        return os.path.join(self.home, "jobs", job_id, name)

    def cleanup(self):
        for j in self.jobs(as_user=True).call("list", scope="all", limit=999)["jobs"]:
            if j["state"] in ("queued", "starting", "running", "paused", "orphaned"):
                try:
                    self.jobs(as_user=True).call("control", job_id=j["job_id"],
                                                 action="signal", signal="KILL")
                except Exception:
                    pass
        time.sleep(0.5)
        self.kill()
