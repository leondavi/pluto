"""Snapshot helpers shared by PlutoMCPFriend and PlutoAgentFriend.

Provides:
- :data:`DEFAULT_SNAPSHOT_DIR` — canonical filesystem location.
- :func:`resolve_resume_path` — find the latest ``.plut`` for an agent_id,
  used by ``--resume``. Warns and returns ``None`` if nothing is found.
- :func:`clean_snapshots` — purge snapshots (one agent or all).
- :class:`AutoSnapshotter` — thread-based timer that calls
  ``client.save_snapshot_files`` on a fixed interval, and again on
  :meth:`stop` if requested (graceful shutdown).

Everything here is transport-agnostic: both ``PlutoHttpClient`` and the
TCP-mode ``PlutoClient`` expose ``save_snapshot_files(output_dir)``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("pluto.snapshot_helper")

DEFAULT_SNAPSHOT_DIR = "/tmp/pluto/snapshots"
DEFAULT_AUTO_SNAPSHOT_INTERVAL_S = 7200  # 2 hours


def snapshot_path_for(agent_id: str, snapshot_dir: str = DEFAULT_SNAPSHOT_DIR) -> str:
    return os.path.join(snapshot_dir, f"{agent_id}.plut")


def resolve_resume_path(
    agent_id: Optional[str],
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
) -> Optional[str]:
    """Find the .plut to restore for --resume.

    - If *agent_id* is given, look for ``<dir>/<agent_id>.plut``.
    - If not, scan *snapshot_dir* for a single .plut; ambiguous → return None
      and log a warning listing the candidates.
    Returns the path or None (warn-and-continue contract).
    """
    if not os.path.isdir(snapshot_dir):
        logger.warning(
            "--resume: snapshot dir %s does not exist; starting fresh", snapshot_dir,
        )
        return None

    if agent_id:
        path = snapshot_path_for(agent_id, snapshot_dir)
        if os.path.isfile(path):
            return path
        logger.warning(
            "--resume: no snapshot at %s; starting fresh", path,
        )
        return None

    candidates = [
        os.path.join(snapshot_dir, f)
        for f in os.listdir(snapshot_dir)
        if f.endswith(".plut")
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        logger.warning(
            "--resume: no .plut files in %s; starting fresh", snapshot_dir,
        )
        return None
    logger.warning(
        "--resume: multiple snapshots in %s; pass --agent-id to disambiguate. "
        "Candidates: %s. Starting fresh.",
        snapshot_dir, ", ".join(sorted(os.path.basename(p) for p in candidates)),
    )
    return None


def clean_snapshots(
    agent_id: Optional[str] = None,
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
) -> list[str]:
    """Delete snapshot files. If *agent_id* is given, only that agent's
    ``.plut`` + ``-recovery.md``; otherwise all .plut/.md in *snapshot_dir*.
    Returns the list of removed file paths.
    """
    if not os.path.isdir(snapshot_dir):
        return []
    removed: list[str] = []
    if agent_id:
        targets = [
            os.path.join(snapshot_dir, f"{agent_id}.plut"),
            os.path.join(snapshot_dir, f"{agent_id}-recovery.md"),
        ]
    else:
        targets = [
            os.path.join(snapshot_dir, f)
            for f in os.listdir(snapshot_dir)
            if f.endswith(".plut") or f.endswith("-recovery.md")
        ]
    for path in targets:
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("clean_snapshots: cannot remove %s: %s", path, exc)
    return removed


class AutoSnapshotter:
    """Background timer that calls ``save_snapshot_files`` every *interval_s*.

    Thread-based (not asyncio) so the same class works inside the MCP
    server's asyncio event loop *and* inside PlutoAgentFriend's
    synchronous wrapper. The callable must be safe to invoke from a
    worker thread — both Pluto client variants are.
    """

    def __init__(
        self,
        save_fn: Callable[[str], object],
        snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
        interval_s: int = DEFAULT_AUTO_SNAPSHOT_INTERVAL_S,
        label: str = "pluto-auto-snapshot",
    ):
        self.save_fn = save_fn
        self.snapshot_dir = snapshot_dir
        self.interval_s = max(60, int(interval_s))
        self.label = label
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_snapshot_at: Optional[float] = None
        self.last_snapshot_path: Optional[str] = None
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=self.label, daemon=True,
        )
        self._thread.start()
        logger.info(
            "auto-snapshot started: every %ds → %s",
            self.interval_s, self.snapshot_dir,
        )

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._take_one()

    def _take_one(self) -> None:
        try:
            result = self.save_fn(self.snapshot_dir)
            plut_path = result[0] if isinstance(result, tuple) else None
            self.last_snapshot_at = time.time()
            self.last_snapshot_path = plut_path
            self.last_error = None
            logger.info("auto-snapshot wrote %s", plut_path)
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("auto-snapshot failed: %s", exc)

    def stop(self, take_final: bool = True) -> None:
        """Stop the timer. If *take_final*, take one last snapshot first."""
        if take_final:
            self._take_one()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
