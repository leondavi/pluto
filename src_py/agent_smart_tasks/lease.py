"""One live adapter per agent id on this machine.

Pluto renames an agent whose id is still registered. After a crash the dead
session holds the id until it expires, and the replacement would not own its
predecessor's jobs. The lock on the lease file dies with its process, so
whoever obtains it may end the session written there.
"""

from __future__ import annotations

import fcntl
import logging
import os

from agent_smart_tasks.constants import CLI_STATE_DIR
from pluto_sdk import PlutoHttpClient
from pluto_sdk.jobs import smarttasks_home

logger = logging.getLogger("pluto_smart_tasks")


def state_path(agent: str, suffix: str) -> str:
    """A private per-agent file; agent ids may hold any character."""
    d = os.path.join(smarttasks_home(), CLI_STATE_DIR)
    os.makedirs(d, mode=0o700, exist_ok=True)
    safe = "".join(c if c.isalnum() and c.isascii() or c in "-_" else "%%%02x" % ord(c)
                   for c in agent)
    return os.path.join(d, safe + suffix)


class Lease:
    def __init__(self, agent: str, server: str):
        self.path = state_path(agent, f".{server.replace(':', '-')}.lease")
        self._fd: int | None = None

    def take_over(self, client: PlutoHttpClient) -> bool:
        """True when no live adapter holds the id; ends a dead one's session."""
        if self._fd is None:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            self._fd = fd
        token = os.pread(self._fd, 4096, 0).decode("ascii", "ignore").strip()
        if token:
            try:
                client.request("POST", "/agents/unregister", {"token": token})
            except Exception as exc:
                logger.debug("cannot end the earlier session: %s", exc)
        return True

    def record(self, token: str | None) -> None:
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, (token or "").encode(), 0)
