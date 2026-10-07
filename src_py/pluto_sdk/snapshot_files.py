"""On-disk layout of a self-snapshot.

Both clients can capture a restorable snapshot of their session
(``snapshot_self``). This module owns how that snapshot is written to
disk so the TCP and HTTP clients produce identical files.
"""

import json
import os
from typing import Tuple


def write_snapshot_files(agent_id: str, snapshot: dict, output_dir: str) -> Tuple[str, str]:
    """Write a snapshot as ``<agent_id>.plut`` plus ``<agent_id>-recovery.md``.

    Args:
        agent_id: Agent the snapshot belongs to; used for both file names.
        snapshot: ``{"plut": dict, "prompt": str}`` as returned by
            ``snapshot_self``.
        output_dir: Destination directory, created if missing.

    Returns:
        ``(plut_path, md_path)``.
    """
    os.makedirs(output_dir, exist_ok=True)
    plut_path = os.path.join(output_dir, f"{agent_id}.plut")
    md_path = os.path.join(output_dir, f"{agent_id}-recovery.md")
    with open(plut_path, "w", encoding="utf-8") as f:
        json.dump(snapshot["plut"], f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(snapshot["prompt"])
    return plut_path, md_path
