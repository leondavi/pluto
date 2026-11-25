"""Definitions for PlutoSmartStreams clients (no logic here).

Counterparts: src_erl/include/pluto_jobs.hrl (server) and
src_erl/priv/pluto_job_defs.py (runner, package helper); kept in step by
tests/test_job_definitions.py.
"""

# ── Storage ───────────────────────────────────────────────────────────────────
HOME_ENV = "PLUTO_SMARTSTREAMS_HOME"
CONFIG_ENV = "PLUTO_CONFIG"
CONFIG_SECTION = "smartstreams"
DEFAULT_HOME_PARTS = (".pluto", "smartstreams")
KEY_FILE = "key"

# ── States ────────────────────────────────────────────────────────────────────
FINAL_STATES = ("succeeded", "failed", "cancelled", "signaled", "timed_out",
                "output_limit", "lost")
LIVE_STATES = ("queued", "starting", "running", "paused", "orphaned")
SIGNALS = ("INT", "TERM", "HUP", "USR1", "USR2", "QUIT", "KILL", "STOP", "CONT")

# ── Events and error reasons matched by clients ───────────────────────────────
EVENT_FINISHED = "job_finished"
EVENT_TRANSFERRED = "job_transferred"
JOB_EVENTS = (EVENT_FINISHED, EVENT_TRANSFERRED)
ERR_BAD_KEY = "bad_key"
ERR_UNKNOWN_ROUTE = "unknown_route"
CAPABILITY = "smartstreams"

# ── Limits ────────────────────────────────────────────────────────────────────
READ_DEFAULT = 8192
READ_MAX = 32768
WAIT_MAX_S = 55
WAIT_DEFAULT_S = 50
GREP_MATCHES = 50
LIST_DEFAULT = 20                 # tool results; smaller than the server's
LIST_ALL = 500                    # --manage
HISTORY_ALL = 10000               # --history

# ── Client timeouts (seconds) ─────────────────────────────────────────────────
CALL_TIMEOUT_S = 30.0
WAIT_SLACK_S = 15                 # beyond the server-side wait
PACKAGE_TIMEOUT_S = 900

#: Environment a job needs to behave like the agent's own shell. The values
#: are stored in the package on this host, so nothing secret goes here.
BASE_ENV = ("PATH", "HOME", "SHELL", "LANG", "LC_ALL", "USER", "LOGNAME",
            "TMPDIR", "VIRTUAL_ENV", "CONDA_PREFIX", "JAVA_HOME", "GOPATH")
