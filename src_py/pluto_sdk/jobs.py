"""Client for PlutoSmartStreams jobs (``POST /jobs/*``).

Job calls authenticate with the per-user key file rather than a session
token, so they keep working across server restarts. The server creates
the key on first contact; the client reads it lazily.
"""

import getpass
import os
import uuid
from typing import Dict, Optional

from pluto_sdk.errors import PlutoError
from pluto_sdk.http_client import PlutoHttpClient

#: Environment the job needs to behave like the agent's own shell. Values
#: are stored in the package until it starts, so nothing secret goes here.
BASE_ENV = ("PATH", "HOME", "SHELL", "LANG", "LC_ALL", "USER", "LOGNAME",
            "TMPDIR", "VIRTUAL_ENV", "CONDA_PREFIX", "JAVA_HOME", "GOPATH")


def smartstreams_home() -> str:
    return os.environ.get("PLUTO_SMARTSTREAMS_HOME") or os.path.join(
        os.path.expanduser("~"), ".pluto", "smartstreams")


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
        path = os.path.join(smartstreams_home(), "key")
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
            resp = self.http._request(
                "POST", f"/jobs/{op}", body=body, timeout=timeout or 30.0,
                extra_headers={"Authorization": f"Bearer {self._key}"})
            if resp.get("reason") != "bad_key":
                break
            self._key = None    # first contact creates the key; read it again
        if resp.get("status") != "ok":
            reason = resp.get("reason", "no response")
            if reason == "unknown_route":
                reason = ("this Pluto server has no SmartStreams; restart it "
                          "from v0.6.0 or newer")
            raise PlutoError(reason)
        resp.pop("status")
        return resp

    def start(self, **spec) -> dict:
        spec.setdefault("base_env",
                        {k: os.environ[k] for k in BASE_ENV if k in os.environ})
        spec.setdefault("framework", self.framework)
        return self.call("start", **spec)

    def wait(self, *, timeout_ms: int = 50000, **fields) -> dict:
        return self.call("wait", timeout=timeout_ms / 1000 + 15,
                         timeout_ms=timeout_ms, **fields)

    def export(self, job_id: str, path: Optional[str] = None) -> dict:
        return self.call("export", timeout=900, job_id=job_id, path=path)

    def import_(self, path: str) -> dict:
        return self.call("import", timeout=900, path=os.path.abspath(path))
