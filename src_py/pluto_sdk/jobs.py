"""Client for PlutoSmartTasks jobs (``POST /jobs/*``).

Job calls authenticate with the per-user key file rather than a session
token, so they keep working across server restarts. The server creates
the key on first contact; the client reads it lazily.
"""

import getpass
import json
import os
import time
import uuid
from typing import Dict, Optional

import pluto_jobs_def as defs
from pluto_sdk.errors import PlutoError
from pluto_sdk.http_client import PlutoHttpClient

_REPO_CONFIG = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config",
    "pluto_config.json"))


def smarttasks_home() -> str:
    """Resolved as the server does: environment, config file, default."""
    env = os.environ.get(defs.HOME_ENV)
    if env:
        return env
    try:
        with open(os.environ.get(defs.CONFIG_ENV) or _REPO_CONFIG,
                  encoding="utf-8") as f:
            home = json.load(f)[defs.CONFIG_SECTION]["home"]
            if home:
                return home
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return os.path.join(os.path.expanduser("~"), *defs.DEFAULT_HOME_PARTS)


class PlutoJobsClient:
    """Thin wrapper: one method per operation, dicts in and out."""

    def __init__(self, http: PlutoHttpClient, *, agent_id: Optional[str] = None,
                 as_user: bool = False, framework: Optional[str] = None):
        self.http = http
        self.agent_id = agent_id or http.agent_id
        self.as_user = as_user
        self.framework = framework
        self.user = getpass.getuser()
        self._key: Optional[str] = None

    def _read_key(self) -> str:
        path = os.path.join(smarttasks_home(), defs.KEY_FILE)
        try:
            with open(path, encoding="ascii") as f:
                return f.read().strip()
        except OSError:
            return ""

    def call(self, op: str, *, timeout: Optional[float] = None, **fields) -> dict:
        """POST /jobs/<op>. None-valued fields are dropped. Raises
        :class:`PlutoError` with the server's reason on failure."""
        body: Dict = {k: v for k, v in fields.items() if v is not None}
        body.setdefault("agent_id", self.agent_id)
        body.setdefault("user", self.user)
        body.setdefault("request_id", uuid.uuid4().hex)
        if self.as_user:
            body["as_user"] = True
        resp: dict = {}
        for _ in range(2):
            self._key = self._key or self._read_key()
            resp = self.http.request(
                "POST", f"/jobs/{op}", body=body, timeout=timeout or defs.CALL_TIMEOUT_S,
                extra_headers={"Authorization": f"Bearer {self._key}"})
            if resp.get("reason") != defs.ERR_BAD_KEY:
                break
            self._key = None    # first contact creates the key; read it again
        if resp.get("status") != "ok":
            reason = resp.get("reason", "no response")
            if reason == defs.ERR_UNKNOWN_ROUTE:
                reason = ("this Pluto server has no SmartTasks; restart it "
                          "from v0.6.0 or newer")
            raise PlutoError(reason)
        resp.pop("status")
        return resp

    # One method per operation that needs more than call().

    def start(self, **spec) -> dict:
        spec.setdefault("base_env",
                        {k: os.environ[k] for k in defs.BASE_ENV if k in os.environ})
        spec.setdefault("framework", self.framework)
        return self.call("start", **spec)

    def wait(self, *, timeout_ms: int = defs.WAIT_DEFAULT_S * 1000, **fields) -> dict:
        return self.call("wait", timeout=timeout_ms / 1000 + defs.WAIT_SLACK_S,
                         timeout_ms=timeout_ms, **fields)

    def analyze(self, **query) -> dict:
        """{"out": text}; with "resume" (positions per job, to be passed as
        from_offsets) when the time limit cut the scan."""
        return self.call("analyze", timeout=defs.ANALYZE_TIMEOUT_S, **query)

    def watch(self, action: str = "add", **fields) -> dict:
        """Add, remove or list standing rules on the output of live jobs."""
        return self.call("watch", action=action, **fields)

    def input(self, job_id: str, text: str, close: bool = False) -> dict:
        """Write to stdin; a job that is only just starting is waited for."""
        end = time.monotonic() + defs.INPUT_PATIENCE_S
        while True:
            try:
                return self.call("input", job_id=job_id, text=text, close=close or None)
            except PlutoError as exc:
                if str(exc) != defs.ERR_NOT_RUNNING or time.monotonic() > end \
                        or self.call("status", job_id=job_id)["state"] != "starting":
                    raise
            time.sleep(0.1)

    def export(self, job_id: str, path: Optional[str] = None) -> dict:
        return self.call("export", timeout=defs.PACKAGE_TIMEOUT_S, job_id=job_id, path=path)

    def import_(self, path: str) -> dict:
        return self.call("import", timeout=defs.PACKAGE_TIMEOUT_S, path=os.path.abspath(path))
