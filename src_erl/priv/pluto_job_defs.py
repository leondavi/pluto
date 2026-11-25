"""Definitions shared by the job runner and the package helper.

Stdlib only, no side effects. The scripts run with ``python3 -I -B``, which
leaves their own directory off ``sys.path``; each adds it explicitly.
Counterparts: include/pluto_jobs.hrl and src_py/pluto_jobs_def.py, kept in
step by tests/test_job_definitions.py.
"""

import hashlib
import json

SCHEMA = 1
CHUNK = 65536                      # bytes per manifest chunk
RECORD = 256                       # fixed-width manifest line, incl. newline
ZERO = "0" * 64                    # chain value before the first chunk
JOB_ID = r"\AJ-[0-9]{8}-[0-9a-f]{16}\Z"
STREAM_ID = r"\AS[0-9]{1,6}\Z"

FILE_PACKAGE = "package.json"
FILE_STATE = "state.json"
FILE_OUTPUT = "output.log"
FILE_MANIFEST = "manifest.jsonl"
FILE_CURSORS = "cursors.json"
FILE_REVIEWS = "reviews.jsonl"
FILE_STREAMS = "streams.json"
FILE_HB = "hb"
FILE_ABORT = "abort"
FILE_RUNNER_LOG = "runner.log"
FILE_RESERVE = "reserve"           # freed to save state when the disk is full
FILE_SUMS = "SHA256SUMS"
DIR_CONTROL = "control"
DIR_STREAMS = "streams"
MODE_FILE = 0o600
MODE_DIR = 0o700

SIGNALS = ("INT", "TERM", "HUP", "USR1", "USR2", "QUIT", "KILL", "STOP", "CONT")
# States a runner can end in; also what an archive may claim.
FINAL = ("succeeded", "failed", "cancelled", "signaled", "timed_out",
         "output_limit")
LIVE = ("starting", "running", "paused")
STREAM_KINDS = ("split", "push")
STREAM_STATES = ("open", "paused", "halted")
FOLLOWUP_STATUSES = ("open", "in_progress", "done", "dropped")

# ── Runner ──────────────────────────────────────────────────────────────────
TICK_S = 0.1
DRAIN_S = 0.5                      # late output of an exited command
GRACE_S = 5.0                      # TERM, then KILL
KILLED_S = 3.0                     # KILL, then stop waiting for the group
HEARTBEAT_S = 1.0
FEED_PER_TICK = 8 * 1024 * 1024    # bytes hashed per tick
FEED_BLOCK = 1 << 20
STDIN_BUFFER_MAX = 1024 * 1024
RESERVE_BYTES = 65536
FINAL_WRITE_RETRY_S = 600          # keep trying to record the result
DEFAULT_MAX_OUTPUT = 1 << 30
EXIT_CANNOT_START = 127

# ── Archives (untrusted input) ──────────────────────────────────────────────
MEMBER_CAPS = {
    FILE_PACKAGE: 1 << 20, FILE_STATE: 1 << 20, FILE_OUTPUT: 16 << 30,
    FILE_MANIFEST: 128 << 20, FILE_REVIEWS: 16 << 20, FILE_STREAMS: 16 << 20,
    FILE_CURSORS: 1 << 20, FILE_SUMS: 1 << 20,
}
STREAM_LOG_MAX = 64 << 20
STREAM_MAX = 1000
MEMBERS_MAX = STREAM_MAX + len(MEMBER_CAPS)
MIN_FREE = 256 << 20
AGENT_ID_MAX = 200
TEXT_MAX = 1 << 20
INT_MAX = (1 << 63) - 1
LIST_MAX = 10000
# Process identity never leaves the host it belongs to.
LOCAL_STATE = ("runner_pid", "runner_ident", "pid", "pgid", "child_ident")


def encode_record(seq, start, end, first_line, newlines, sha256, chain):
    rec = json.dumps({"seq": seq, "start": start, "end": end,
                      "first_line": first_line, "newlines": newlines,
                      "sha256": sha256, "chain": chain},
                     separators=(",", ":"))
    return rec.ljust(RECORD - 1).encode() + b"\n"


def next_chain(chain, sha256):
    """Each link covers everything before it, so a prefix proves itself."""
    return hashlib.sha256((chain + sha256).encode()).hexdigest()
