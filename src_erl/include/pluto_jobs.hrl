%%% ==========================================================================
%%% pluto_jobs.hrl — Definitions for PlutoSmartStreams job packages.
%%%
%%% Enumerated values, file names, limits and field lists only; plain JSON
%%% map keys stay literal, as elsewhere in Pluto. Included by pluto.hrl.
%%%
%%% Values shared with the Python side also live in
%%%   src_erl/priv/pluto_job_defs.py   (runner and package helper)
%%%   src_py/pluto_jobs_def.py         (SDK, MCP adapter, reports)
%%% tests/test_job_definitions.py fails when the three disagree.
%%% ==========================================================================

-ifndef(PLUTO_JOBS_HRL).
-define(PLUTO_JOBS_HRL, true).

%% ── Types ───────────────────────────────────────────────────────────────────
-type job_id()   :: binary().
-type pkg()      :: map().                      %% package.json contents
-type jstate()   :: map().                      %% state.json contents
-type agent_id() :: binary().
-type actor()    :: {agent_id(), IsUser :: boolean()}.
-type src()      :: #{path := file:filename_all(), lo := non_neg_integer(),
                      hi := non_neg_integer(), closed := boolean(),
                      man := file:filename_all() | undefined}.
-type reply()    :: {ok, map()} | {error, binary()}.

%% ── Job states, as shown to clients ─────────────────────────────────────────
-define(JOB_QUEUED,       <<"queued">>).
-define(JOB_STARTING,     <<"starting">>).
-define(JOB_RUNNING,      <<"running">>).
-define(JOB_PAUSED,       <<"paused">>).
-define(JOB_SUCCEEDED,    <<"succeeded">>).
-define(JOB_FAILED,       <<"failed">>).
-define(JOB_CANCELLED,    <<"cancelled">>).
-define(JOB_SIGNALED,     <<"signaled">>).
-define(JOB_TIMED_OUT,    <<"timed_out">>).
-define(JOB_OUTPUT_LIMIT, <<"output_limit">>).
-define(JOB_ORPHANED,     <<"orphaned">>).      %% runner died, command lives
-define(JOB_LOST,         <<"lost">>).          %% both gone, no exit status
-define(JOB_SNAPSHOT,     <<"snapshot">>).      %% import of a running job

%% ── Scheduler states (package.json "sched"; see pluto_job_registry) ─────────
-define(SCHED_QUEUED,       <<"queued">>).
-define(SCHED_SPAWNING,     <<"spawning">>).
-define(SCHED_SPAWNED,      <<"spawned">>).
-define(SCHED_START_FAILED, <<"start_failed">>).
-define(SCHED_CANCELLED,    <<"cancelled">>).
-define(SCHED_ORPHANED,     <<"orphaned">>).
-define(SCHED_LOST,         <<"lost">>).
-define(SCHED_IMPORTED,     <<"imported">>).
-define(SCHED_SNAPSHOT,     <<"snapshot">>).

%% ── Control actions, signals, waits ─────────────────────────────────────────
-define(ACT_CANCEL, <<"cancel">>).
-define(ACT_HALT,   <<"halt">>).                %% synonym of cancel
-define(ACT_PAUSE,  <<"pause">>).
-define(ACT_RESUME, <<"resume">>).
-define(ACT_SIGNAL, <<"signal">>).
-define(ACT_INPUT,  <<"input">>).
-define(JOB_ACTIONS, [?ACT_CANCEL, ?ACT_PAUSE, ?ACT_RESUME, ?ACT_SIGNAL]).
-define(JOB_SIGNALS, [<<"INT">>, <<"TERM">>, <<"HUP">>, <<"USR1">>, <<"USR2">>,
                      <<"QUIT">>, <<"KILL">>, <<"STOP">>, <<"CONT">>]).
-define(WAIT_EXIT,    <<"exit">>).
-define(WAIT_OUTPUT,  <<"output">>).
-define(WAIT_IDLE,    <<"idle">>).
-define(WAIT_PATTERN, <<"pattern">>).
-define(WAIT_MODES, [?WAIT_EXIT, ?WAIT_OUTPUT, ?WAIT_IDLE, ?WAIT_PATTERN]).

%% ── Nested streams ──────────────────────────────────────────────────────────
-define(STREAM_SPLIT,  <<"split">>).
-define(STREAM_PUSH,   <<"push">>).
-define(STREAM_OPEN,   <<"open">>).
-define(STREAM_PAUSED, <<"paused">>).
-define(STREAM_HALTED, <<"halted">>).
-define(FOLLOWUP_STATUSES, [<<"open">>, <<"in_progress">>, <<"done">>, <<"dropped">>]).

%% ── Package files ───────────────────────────────────────────────────────────
-define(JOB_FILE_PACKAGE,  "package.json").     %% server; what the job is
-define(JOB_FILE_STATE,    "state.json").       %% runner; what it is doing
-define(JOB_FILE_OUTPUT,   "output.log").       %% the command itself
-define(JOB_FILE_MANIFEST, "manifest.jsonl").   %% runner
-define(JOB_FILE_CURSORS,  "cursors.json").     %% server
-define(JOB_FILE_REVIEWS,  "reviews.jsonl").    %% server
-define(JOB_FILE_STREAMS,  "streams.json").     %% server
-define(JOB_FILE_HB,       "hb").               %% runner heartbeat (mtime)
-define(JOB_FILE_ABORT,    "abort").            %% server: do not start any more
-define(JOB_DIR_CONTROL,   "control").
-define(JOB_DIR_STREAMS,   "streams").
%% Beside jobs/ in the SmartStreams home:
-define(HOME_DIR_JOBS,     "jobs").
-define(HOME_DIR_STAGING,  "staging").
-define(HOME_DIR_EXPORTS,  "exports").
-define(HOME_FILE_KEY,     "key").
-define(HOME_FILE_HOST_ID, "host_id").
-define(HOME_FILE_LOCK,    "server.lock").
-define(HOME_FILE_NOTICES, "notices.jsonl").
-define(HOME_FILE_HISTORY, "history.jsonl").
-define(JOB_OLD_SUFFIX,    ".old").             %% package being replaced
-define(JOB_RUNNER_SCRIPT,  "pluto_job_runner.py").
-define(JOB_PACKAGE_SCRIPT, "pluto_job_package.py").
-define(MODE_DIR,  8#700).
-define(MODE_FILE, 8#600).

%% ── Formats ─────────────────────────────────────────────────────────────────
-define(JOB_SCHEMA,       1).
-define(JOB_ID_RE,        "\\AJ-[0-9]{8}-[0-9a-f]{16}\\z").
-define(STREAM_ID_RE,     "\\AS[0-9]{1,6}\\z").
-define(JOB_CHUNK_BYTES,  65536).
-define(JOB_RECORD_BYTES, 256).                 %% fixed-width manifest line
-define(JOB_KEY_BYTES,    32).
-define(JOB_KEY_MIN,      32).                  %% shortest key accepted

%% ── Configurable defaults (pluto_config.json "smartstreams") ────────────────
-define(DEFAULT_JOB_MAX_RUNNING,    16).
-define(DEFAULT_JOB_MAX_PER_AGENT,  8).
-define(DEFAULT_JOB_IDLE_AFTER_MS,  30000).
-define(DEFAULT_JOB_MIN_FREE_BYTES, 268435456).

%% ── Reads ───────────────────────────────────────────────────────────────────
-define(JOB_READ_DEFAULT,  8192).
-define(JOB_READ_MAX,      32768).
-define(JOB_READ_BLOCK,    262144).
-define(JOB_LINE_SCAN_MAX, 8388608).  %% beyond this, line numbers are null
-define(JOB_HEAD_BYTES,    1024).     %% job_open card
-define(JOB_TAIL_BYTES,    2048).
-define(JOB_TAIL_LINES,    20).

%% ── Grep ────────────────────────────────────────────────────────────────────
-define(GREP_MAX_LINE,    1048576).   %% longer lines are matched in pieces
-define(GREP_CLIP,        4096).
-define(GREP_BUDGET,      268435456). %% bytes scanned per call
-define(GREP_DEADLINE_MS, 10000).
-define(GREP_SLACK_MS,    5000).      %% before the scanner is killed
-define(GREP_MAX_OUT,     65536).     %% text returned
-define(GREP_RE_LIMIT,    100000).    %% backtracking steps per line
-define(GREP_HEAP_WORDS,  16#1000000).
-define(GREP_DEFAULT_HITS, 50).
-define(GREP_MAX_HITS,    500).
-define(GREP_MAX_CONTEXT, 20).

%% ── Waits, lists, misc limits ───────────────────────────────────────────────
-define(JOB_WAIT_MAX_MS,      55000).
-define(JOB_WAIT_DEFAULT_MS,  50000).
-define(JOB_WAIT_STEP_MS,     200).
-define(JOB_WAIT_IDLE_MS,     5000).
-define(JOB_WAIT_MAX_IDS,     200).
-define(JOB_LIST_DEFAULT,     50).
-define(JOB_HISTORY_DEFAULT,  200).
-define(JOB_MANIFEST_DEFAULT, 50).
-define(JOB_MANIFEST_MAX,     1000).
-define(JOB_RETENTION_MS,     2592000000).  %% 30 days
-define(JOB_AGENT_ID_MAX,     200).
-define(JOB_INPUT_MAX,        65536).
-define(JOB_INPUT_PENDING,    64).          %% unread control files
-define(JOB_NOTICE_PAGE,      20).
-define(JOB_NOTICE_KEEP,      200).         %% unacknowledged, per agent
-define(JOB_NOTICE_TTL_MS,    1209600000).  %% 14 days
-define(JOB_DEDUPE_MAX,       2000).        %% per generation
-define(REVIEW_HASH_MAX,      67108864).    %% larger ranges: see the manifest
-define(STREAM_MAX,           1000).
-define(STREAM_PUSH_MAX,      1048576).
-define(STREAM_LOG_MAX,       67108864).
-define(STREAM_SPLITS_MAX,    50).
-define(STREAM_NAME_MAX,      200).
-define(FOLLOWUP_MAX,         1000).
-define(FOLLOWUP_TEXT_MAX,    8192).

%% ── Timing ──────────────────────────────────────────────────────────────────
-define(JOB_TICK_MS,       500).
-define(JOB_HB_STALE_MS,   5000).   %% silent runner: start probing
-define(JOB_SPAWN_MAX_MS,  60000).  %% no sign of the runner: write abort
-define(JOB_ABORT_MS,      5000).   %% then: give up on it
-define(JOB_BACKOFF_MS,    5000).   %% after a failed write for one job
-define(JOB_CALL_MS,       30000).
-define(JOB_SCRIPT_MS,     10000).  %% ident/kill helpers
-define(JOB_PACKAGE_MS,    900000). %% export/import

%% ── Field lists ─────────────────────────────────────────────────────────────
%% Copied from a start request into the package.
-define(JOB_SPEC_KEYS, [<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                        <<"timeout_s">>, <<"max_output_bytes">>, <<"name">>,
                        <<"tags">>, <<"task_id">>, <<"framework">>,
                        <<"session_id">>, <<"parent">>, <<"rerun_of">>,
                        <<"base_env">>, <<"request_id">>]).
%% Handed to the runner.
-define(JOB_LAUNCH_KEYS, [<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                          <<"timeout_s">>, <<"max_output_bytes">>, <<"base_env">>]).
%% Repeated by rerun_of.
-define(JOB_RERUN_KEYS, [<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                         <<"timeout_s">>, <<"max_output_bytes">>, <<"name">>]).
%% Meaningful on this host only; stripped from exports.
-define(JOB_LOCAL_KEYS, [<<"base_env">>, <<"request_id">>, <<"spawn_at">>,
                         <<"sched_ended_at">>]).
-define(JOB_CARD_KEYS, [<<"name">>, <<"user">>, <<"opener">>, <<"owner">>,
                        <<"opened_at">>, <<"tags">>, <<"task_id">>, <<"parent">>,
                        <<"rerun_of">>, <<"framework">>]).
-define(JOB_OPEN_KEYS, [<<"command">>, <<"argv">>, <<"cwd">>, <<"origin">>,
                        <<"ownership">>, <<"imported">>, <<"env_keys">>,
                        <<"timeout_s">>]).
-define(JOB_HISTORY_KEYS, [<<"id">>, <<"name">>, <<"user">>, <<"opener">>,
                           <<"owner">>, <<"opened_at">>, <<"command">>,
                           <<"argv">>, <<"origin">>]).

%% ── Request validation (pluto_job_schema) ───────────────────────────────────
-define(JOB_STR_KEYS, [<<"job_id">>, <<"agent_id">>, <<"command">>, <<"cwd">>,
                       <<"name">>, <<"to">>, <<"text">>, <<"pattern">>,
                       <<"user">>, <<"request_id">>, <<"stream">>, <<"action">>,
                       <<"encoding">>, <<"verdict">>, <<"note">>, <<"parent">>,
                       <<"rerun_of">>, <<"path">>, <<"task_id">>, <<"signal">>,
                       <<"scope">>, <<"until">>, <<"status">>, <<"assignee">>,
                       <<"followup_id">>, <<"framework">>, <<"session_id">>]).
-define(JOB_INT_KEYS, [<<"max_bytes">>, <<"from_offset">>, <<"to_offset">>,
                       <<"offset">>, <<"line">>, <<"lines">>, <<"tail">>,
                       <<"before">>, <<"after">>, <<"limit">>, <<"from">>,
                       <<"ack">>, <<"from_line">>, <<"to_line">>,
                       <<"max_matches">>, <<"timeout_ms">>, <<"idle_ms">>,
                       <<"max_output_bytes">>, <<"older_than_ms">>]).
-define(JOB_BOOL_KEYS, [<<"stdin">>, <<"tree">>, <<"verify">>, <<"strip_ansi">>,
                        <<"fixed">>, <<"invert">>, <<"ignore_case">>,
                        <<"close">>, <<"all">>, <<"as_user">>, <<"dry_run">>,
                        <<"cursor">>]).
-define(JOB_STRS_KEYS, [<<"argv">>, <<"tags">>, <<"job_ids">>, <<"states">>]).
-define(JOB_ENV_KEYS,  [<<"env">>, <<"base_env">>]).

-endif. %% PLUTO_JOBS_HRL
