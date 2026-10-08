"""A throw-away Pluto server for one experiment run.

Runs the already-built Pluto beams in a fresh, undistributed `erl` node on
spare ports (default TCP 9300 / HTTP 9302) with its own persistence and
event-log directories, so experiment runs never touch the live server on
the configured ports (config/pluto_config.json) and every run starts with
an empty lock table, fencing counter and task store.

Build the beams first with `./PlutoServer.sh --build` if needed.
"""

import json
import os
import signal
import subprocess
import time
import urllib.request

EBIN = os.environ.get("PLUTO_EBIN", "/tmp/pluto/build/_build/default/lib/pluto/ebin")
HOST = "127.0.0.1"
TCP_PORT = int(os.environ.get("EXP_PLUTO_TCP_PORT", "9300"))
HTTP_PORT = int(os.environ.get("EXP_PLUTO_HTTP_PORT", "9302"))


def _health(timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{HTTP_PORT}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError):
        return None


class PlutoSandbox:
    def __init__(self, workdir):
        self.workdir = os.path.join(workdir, "pluto-server")
        self.proc = None
        self.health = None

    def start(self, timeout=20):
        if _health():
            raise RuntimeError(f"something is already serving on port {HTTP_PORT}; "
                               "set EXP_PLUTO_HTTP_PORT/EXP_PLUTO_TCP_PORT")
        if not os.path.isdir(EBIN):
            raise RuntimeError(f"Pluto beams not found at {EBIN}; run ./PlutoServer.sh --build")
        os.makedirs(self.workdir, exist_ok=True)
        cfg_json = os.path.join(self.workdir, "pluto_config.json")
        with open(cfg_json, "w") as f:
            json.dump({"pluto_server": {"host_ip": HOST, "host_tcp_port": TCP_PORT,
                                        "host_http_port": HTTP_PORT}}, f)
        sys_config = os.path.join(self.workdir, "sys.config")
        with open(sys_config, "w") as f:
            f.write('[{pluto,[{persistence_dir,"%s"},{event_log_dir,"%s"}]}].\n'
                    % (os.path.join(self.workdir, "state"), os.path.join(self.workdir, "events")))
        env = dict(os.environ, PLUTO_CONFIG=cfg_json)
        log = open(os.path.join(self.workdir, "server.log"), "w")
        self.proc = subprocess.Popen(
            ["erl", "-noshell", "-pa", EBIN, "-config", sys_config,
             "-eval", "{ok,_}=application:ensure_all_started(pluto)"],
            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.health = _health()
            if self.health:
                return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"sandbox Pluto failed to start; see {self.workdir}/server.log")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
