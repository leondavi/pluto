"""Constants of the SmartTasks MCP adapter and reports."""

SERVER_NAME = "smarttasks"
SESSION_TTL_MS = 600_000
EPOCH_CHECK_S = 15.0              # how often a server restart is noticed
DRAIN_HINT = "any job_* tool"     # named in wakeups
NOTICE_KEY = "_job_notices"
INBOX_KEY = "_pluto_inbox"
MANIFEST_PAGE = 20
FLEET_THREADS = 4                 # matches the HTTP connection pool
GREP_MATCHES_MANY = 10            # default per job when grepping several
GREP_MATCHES_CALL = 600           # ceiling of one call
SUBMIT_MAX = 64                   # jobs per job_start
GROUP_RE = r"g[0-9a-f]{6}"        # handle of a batch
GROUP_MAX = 500
SHORT_RE = r"[0-9a-f]{6}"
NAME_RE = r"[A-Za-z][A-Za-z0-9_.\-]{0,59}"
NAME_TAG = "="                    # tag of a named batch: "=soak"
BRIEF_JOBS = 50                   # whose rules are counted
BRIEF_SHAPES = 5
BRIEF_TEXT = 2000
FOLLOW_POLL_S = 0.2              # pst cat -f
FOLLOW_END_MS = 10_000            # a replaced follower ends before the new one starts
FOLLOW_CHECK_S = 1.0              # how often it asks whether the job ended
EACH_WORKERS = 4                  # pst each, programs at once
EACH_TEXT = 1500                  # characters shown of one program's output
EACH_TOTAL = 12_000               # and of all of them together
EACH_BYTES = 16 << 20             # of one program's output kept for a file
LAST_LINE_BYTES = 240             # of an exited job, in job_wait
WAIT_TOTAL_MAX_S = 300            # job_wait: repeated server-side waits
WAIT_FOREVER_S = 86_400           # pst wait
CLI_WAIT_S = 100                  # below the usual shell tool limit
CLI_AGENT_ENV, CLI_HOST_ENV, CLI_PORT_ENV = "PST_AGENT", "PST_HOST", "PST_PORT"
CLI_STATE_DIR = "cli"             # under the SmartTasks home
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

#: Tools every session sees; the others are reached through job_more.
CORE_TOOLS = ("job_start", "job_wait", "job_analyze", "job_watch", "job_grep",
              "job_window", "job_control", "job_list", "job_transfer")
TOOLSETS = ("core", "all", "none")     # none: pst only, wakeups stay

CLI_INSTRUCTIONS = (
    "The shell command pst runs and supervises durable jobs; the server keeps "
    "their rules, so no watcher scripts, ids or log paths are needed: "
    "pst run -n t -e k=1-12 'prog {k}' --on 'need {x}' --append 'ack/{k}' '{x}' "
    "--idle 60 --stop; pst wait t -t 540; "
    "pst each t 'python3 parse.py {log}' -o 'out/{k}.json'. See pst help."
)

#: Words agents try for an action, and what they mean.
ACTION_ALIASES = {"kill": "cancel", "stop": "cancel", "terminate": "cancel",
                  "continue": "resume"}

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
