"""Constants of the SmartStreams MCP adapter and reports."""

SERVER_NAME = "smartstreams"
SESSION_TTL_MS = 600_000
EPOCH_CHECK_S = 15.0              # how often a server restart is noticed
DRAIN_HINT = "any job_* tool"     # named in wakeups
NOTICE_KEY = "_job_notices"
INBOX_KEY = "_pluto_inbox"
MANIFEST_PAGE = 20
FLEET_THREADS = 4                 # matches the HTTP connection pool
MIN_MATCHES_PER_JOB = 3

INSTRUCTIONS = (
    "Run long or noisy commands as jobs: job_start, then job_wait, then read "
    "only what you need with job_grep / job_window / job_next_chunk. Jobs and "
    "cursors survive this session; job_list finds earlier ones."
)

#: Host CLIs whose inbox socket may be written to wake the agent.
WAKEABLE_FRAMEWORKS = (None, "claude")

# ── Reports ───────────────────────────────────────────────────────────────────
COLUMNS = (("job_id", 27), ("name", 14), ("user", 9), ("opener", 12),
           ("owner", 12), ("host", 12), ("opened", 16), ("elapsed", 9),
           ("state", 11), ("activity", 8), ("size", 8))
VIEW_LINES = 2000                 # --manage pager
VIEW_MATCHES = 100
VIEW_CONTEXT = 2
REFRESH_MS = 1000
