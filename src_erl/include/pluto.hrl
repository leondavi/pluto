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
-define(VERSION, "0.1.0").

%% ── Default configuration values ────────────────────────────────────────────
-define(DEFAULT_TCP_PORT,               9000).
-define(DEFAULT_HTTP_PORT,              9001).
-define(DEFAULT_TCP_BACKLOG,            128).
-define(DEFAULT_HEARTBEAT_INTERVAL_MS,  15000).
-define(DEFAULT_HEARTBEAT_SWEEP_MS,     5000).
-define(DEFAULT_HEARTBEAT_TIMEOUT_MS,   30000).
-define(DEFAULT_RECONNECT_GRACE_MS,     30000).
-define(DEFAULT_MAX_WAIT_MS,            60000).
-define(DEFAULT_FLUSH_INTERVAL,         60000).
-define(DEFAULT_PERSISTENCE_DIR,        "/tmp/pluto/state").
-define(DEFAULT_EVENT_LOG_DIR,          "/tmp/pluto/events").
-define(DEFAULT_SESSION_CONFLICT,       strict).   %% strict | takeover

%% ── ETS table names ────────────────────────────────────────────────────────
-define(ETS_LOCKS,      pluto_locks).       %% Active lock entries
-define(ETS_AGENTS,     pluto_agents).      %% agent_id -> session info
-define(ETS_SESSIONS,   pluto_sessions).    %% session_id -> agent_id
-define(ETS_WAITERS,    pluto_waiters).     %% Wait queue entries (ordered)
-define(ETS_WAIT_GRAPH, pluto_wait_graph).  %% Deadlock detection edges
-define(ETS_LIVENESS,   pluto_liveness).    %% session_id -> last_seen_ms

%% ── Maximum line length for TCP reads (1 MB) ───────────────────────────────
-define(MAX_LINE_LENGTH, 1048576).

%% ── Logging helpers ─────────────────────────────────────────────────────────
%% These wrap the OTP logger macros for convenience.
-define(LOG_INFO(Msg),        logger:info(Msg)).
-define(LOG_INFO(Fmt, Args),  logger:info(Fmt, Args)).
-define(LOG_WARN(Msg),        logger:warning(Msg)).
-define(LOG_WARN(Fmt, Args),  logger:warning(Fmt, Args)).
-define(LOG_ERROR(Msg),       logger:error(Msg)).
-define(LOG_ERROR(Fmt, Args), logger:error(Fmt, Args)).

-endif. %% PLUTO_HRL
