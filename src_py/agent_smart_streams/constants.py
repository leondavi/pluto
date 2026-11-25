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
SUBMIT_MAX = 64                   # jobs per job_start
GROUP_RE = r"g[0-9a-f]{6}"        # handle of a batch
GROUP_MAX = 500
LAST_LINE_BYTES = 240             # of an exited job, in job_wait
WAIT_TOTAL_MAX_S = 300            # job_wait: repeated server-side waits
WAKE_GRACE_S = 3.0                # a result may still tell the agent
WAKE_POLL_S = 0.5

INSTRUCTIONS = (
    "Run long or noisy commands as jobs: job_start, then job_wait, then read "
    "only what you need with job_grep / job_window / job_next_chunk. Start "
    "many at once with each=, then use the returned group handle wherever a "
    "job id is accepted. To understand output use job_analyze first: alone it "
    "outlines any log without patterns, and it extracts, filters, aggregates "
    "and compares across jobs in one call. To learn of something as it "
    "happens (errors, stalls, thresholds) add a job_watch rule instead of "
    "polling. `output` is the raw log (a shell glob, read only) for scripts "
    "that do anything else. Jobs and cursors survive this session; job_list "
    "finds earlier ones."
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
