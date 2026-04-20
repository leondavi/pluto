%%% ==========================================================================
%%% pluto.hrl — Master header for the Pluto coordination server.
%%%
%%% Includes all sub-headers and defines application-wide constants.
%%% Every Pluto module should include this file.
%%% ==========================================================================

-ifndef(PLUTO_HRL).
-define(PLUTO_HRL, true).

%% ── Sub-headers ─────────────────────────────────────────────────────────────
-include("pluto_records.hrl").
-include("pluto_protocol.hrl").

%% ── Application name ────────────────────────────────────────────────────────
-define(APP, pluto).

%% ── Version ─────────────────────────────────────────────────────────────────
-define(VERSION, "0.2.3").

%% ── Default configuration values ────────────────────────────────────────────
-define(DEFAULT_TCP_PORT,               9000).
-define(DEFAULT_HTTP_PORT,              9001).
-define(DEFAULT_TCP_BACKLOG,            128).
-define(DEFAULT_HEARTBEAT_INTERVAL_MS,  15000).
-define(DEFAULT_HEARTBEAT_SWEEP_MS,     5000).
-define(DEFAULT_HEARTBEAT_TIMEOUT_MS,   30000).
-define(DEFAULT_HEARTBEAT_REMINDER_MS,  600000). %% broadcast reminder every 10 min
-define(DEFAULT_RECONNECT_GRACE_MS,     30000).
-define(DEFAULT_MAX_WAIT_MS,            60000).
-define(DEFAULT_HTTP_SESSION_TTL_MS,    300000).  %% 5 minutes for HTTP agents
-define(DEFAULT_HTTP_SESSION_SWEEP_MS,  10000).   %% sweep HTTP sessions every 10s
-define(DEFAULT_FLUSH_INTERVAL,         60000).
-define(DEFAULT_PERSISTENCE_DIR,        "/tmp/pluto/state").
-define(DEFAULT_EVENT_LOG_DIR,          "/tmp/pluto/events").
-define(DEFAULT_SESSION_CONFLICT,       strict).   %% strict | takeover
-define(DEFAULT_MAX_INBOX_SIZE,         1000).     %% max offline messages per agent
-define(DEFAULT_INBOX_MSG_TTL_MS,    86400000).  %% 24 hours
-define(DEFAULT_INBOX_SWEEP_MS,      3600000).   %% sweep inbox hourly

%% ── ETS table names ────────────────────────────────────────────────────────
-define(ETS_LOCKS,      pluto_locks).       %% Active lock entries
-define(ETS_AGENTS,     pluto_agents).      %% agent_id -> session info
-define(ETS_SESSIONS,   pluto_sessions).    %% session_id -> agent_id
-define(ETS_WAITERS,    pluto_waiters).     %% Wait queue entries (ordered)
-define(ETS_WAIT_GRAPH, pluto_wait_graph).  %% Deadlock detection edges
-define(ETS_LIVENESS,   pluto_liveness).    %% session_id -> last_seen_ms
-define(ETS_TASKS,      pluto_tasks).       %% task_id -> task record
-define(ETS_MSG_INBOX,  pluto_msg_inbox).   %% {agent_id, seq} -> message map
-define(ETS_HTTP_SESSIONS, pluto_http_sessions). %% token -> #http_session{}
-define(ETS_LONG_POLL,    pluto_long_poll).     %% agent_id -> waiting pid
-define(ETS_GRACE_TIMERS, pluto_grace_timers). %% agent_id -> grace timer ref

%% ── Signal file directory ───────────────────────────────────────────────────
-define(DEFAULT_SIGNAL_DIR, "/tmp/pluto/signals").

%% ── Maximum line length for TCP reads (1 MB) ───────────────────────────────
-define(MAX_LINE_LENGTH, 1048576).

%% ── Logging helpers ─────────────────────────────────────────────────────────
%% These wrap the OTP logger macros for convenience.
-define(LOG_INFO(Msg),        logger:info(Msg)).
-define(LOG_INFO(Fmt, Args),  logger:info(Fmt, Args)).
-define(LOG_NOTICE(Msg),      logger:notice(Msg)).
-define(LOG_NOTICE(Fmt, Args),logger:notice(Fmt, Args)).
-define(LOG_WARN(Msg),        logger:warning(Msg)).
-define(LOG_WARN(Fmt, Args),  logger:warning(Fmt, Args)).
-define(LOG_ERROR(Msg),       logger:error(Msg)).
-define(LOG_ERROR(Fmt, Args), logger:error(Fmt, Args)).

-endif. %% PLUTO_HRL
