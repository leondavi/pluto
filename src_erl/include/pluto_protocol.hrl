%%% ==========================================================================
%%% pluto_protocol.hrl — Protocol constants for the JSON wire protocol.
%%%
%%% Defines operation names, status codes, event types, and error reasons
%%% as binary constants matching the JSON values clients send/receive.
%%% ==========================================================================

-ifndef(PLUTO_PROTOCOL_HRL).
-define(PLUTO_PROTOCOL_HRL, true).

%% ── Operation names (the "op" field in client requests) ─────────────────────
-define(OP_REGISTER,            <<"register">>).
-define(OP_ACQUIRE,             <<"acquire">>).
-define(OP_RELEASE,             <<"release">>).
-define(OP_RENEW,               <<"renew">>).
-define(OP_SEND,                <<"send">>).
-define(OP_BROADCAST,           <<"broadcast">>).
-define(OP_LIST_AGENTS,         <<"list_agents">>).
-define(OP_PING,                <<"ping">>).
-define(OP_EVENT_HISTORY,       <<"event_history">>).
-define(OP_SELFTEST,            <<"selftest">>).
-define(OP_ADMIN_LIST_LOCKS,    <<"admin_list_locks">>).
-define(OP_ADMIN_FORCE_RELEASE, <<"admin_force_release">>).
-define(OP_ADMIN_LIST_AGENTS,   <<"admin_list_agents">>).
-define(OP_ADMIN_DISCONNECT,    <<"admin_disconnect_agent">>).
-define(OP_ADMIN_DEADLOCK_GRAPH,<<"admin_deadlock_graph">>).
-define(OP_ADMIN_FENCING_SEQ,   <<"admin_fencing_seq">>).

%% ── Response status codes ───────────────────────────────────────────────────
-define(STATUS_OK,    <<"ok">>).
-define(STATUS_WAIT,  <<"wait">>).
-define(STATUS_ERROR, <<"error">>).
-define(STATUS_PONG,  <<"pong">>).

%% ── Lock modes ──────────────────────────────────────────────────────────────
-define(MODE_WRITE, <<"write">>).
-define(MODE_READ,  <<"read">>).

%% ── Event types (pushed to clients asynchronously) ──────────────────────────
-define(EVT_MESSAGE,           <<"message">>).
-define(EVT_BROADCAST,         <<"broadcast">>).
-define(EVT_LOCK_GRANTED,      <<"lock_granted">>).
-define(EVT_LOCK_EXPIRED,      <<"lock_expired">>).
-define(EVT_LOCK_RELEASED,     <<"lock_released">>).
-define(EVT_WAIT_TIMEOUT,      <<"wait_timeout">>).
-define(EVT_DEADLOCK_DETECTED, <<"deadlock_detected">>).
-define(EVT_AGENT_JOINED,      <<"agent_joined">>).
-define(EVT_AGENT_LEFT,        <<"agent_left">>).

%% ── Error reason codes ──────────────────────────────────────────────────────
-define(ERR_BAD_REQUEST,        <<"bad_request">>).
-define(ERR_UNKNOWN_OP,         <<"unknown_op">>).
-define(ERR_UNKNOWN_TARGET,     <<"unknown_target">>).
-define(ERR_CONFLICT,           <<"conflict">>).
-define(ERR_NOT_FOUND,          <<"not_found">>).
-define(ERR_EXPIRED,            <<"expired">>).
-define(ERR_WAIT_TIMEOUT,       <<"wait_timeout">>).
-define(ERR_DEADLOCK,           <<"deadlock">>).
-define(ERR_ALREADY_REGISTERED, <<"already_registered">>).
-define(ERR_UNAUTHORIZED,       <<"unauthorized">>).
-define(ERR_INTERNAL_ERROR,     <<"internal_error">>).
-define(ERR_NOT_REGISTERED,     <<"not_registered">>).

-endif. %% PLUTO_PROTOCOL_HRL
