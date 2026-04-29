%%% ==========================================================================
%%% pluto_records.hrl — Record definitions for Pluto data structures.
%%%
%%% All records used across multiple modules are defined here to ensure
%%% consistency and avoid circular includes.
%%% ==========================================================================

-ifndef(PLUTO_RECORDS_HRL).
-define(PLUTO_RECORDS_HRL, true).

%% ── Lock record ─────────────────────────────────────────────────────────────
%% Represents an active lock held by an agent on a resource.
%%
%%   lock_ref     — Unique identifier for this lock (binary, e.g. <<"LOCK-42">>)
%%   resource     — Normalized resource key (binary)
%%   mode         — Lock mode: 'write' (exclusive) or 'read' (shared)
%%   agent_id     — The logical agent identity that owns this lock (binary)
%%   session_id   — The session that acquired the lock (binary)
%%   fencing_token— Monotonically increasing integer issued at grant time
%%   expires_at   — Monotonic time (ms) when this lock expires
%%   inserted_at  — Monotonic time (ms) when this lock was created
%%
-record(lock, {
    lock_ref      :: binary(),
    resource      :: binary(),
    mode          :: write | read,
    agent_id      :: binary(),
    session_id    :: binary(),
    fencing_token :: non_neg_integer(),
    expires_at    :: integer(),
    inserted_at   :: integer(),
    warned        = false :: boolean()  %% true after lock_expiring_soon warning was sent
}).

%% ── Wait entry record ───────────────────────────────────────────────────────
%% Represents a queued lock request waiting for a resource to become available.
%%
%%   wait_ref       — Unique identifier for this wait entry (binary)
%%   resource       — Normalized resource key (binary)
%%   mode           — Requested lock mode: 'write' or 'read'
%%   agent_id       — The agent requesting the lock (binary)
%%   session_id     — The session that made the request (binary)
%%   session_pid    — PID of the session process (for async notification)
%%   requested_at   — Monotonic time (ms) when request was enqueued
%%   max_wait_until — Absolute monotonic time deadline (ms), or 'infinity'
%%
-record(wait_entry, {
    wait_ref       :: binary(),
    resource       :: binary(),
    mode           :: write | read,
    agent_id       :: binary(),
    session_id     :: binary(),
    session_pid    :: pid(),
    requested_at   :: integer(),
    max_wait_until :: integer() | infinity
}).

%% ── Agent record ────────────────────────────────────────────────────────────
%% Stored in the ETS_AGENTS table, keyed by agent_id.
%%
%%   agent_id     — Stable logical identity (binary)
%%   session_id   — Current session ID (binary) or 'undefined' if disconnected
%%   session_pid  — PID of the current session process, or 'undefined'
%%   status       — 'connected' | 'disconnected'
%%   connected_at — Monotonic time (ms) when last connected
%%   attributes   — Agent metadata key-value map (binary keys/values)
%%   last_seen    — System time (ms) of last heartbeat/message
%%   custom_status— Custom agent status (e.g. <<"busy">>, <<"idle">>)
%%   subscriptions— List of topic names this agent subscribes to
%%   session_type — Connection type: 'tcp' | 'http' | 'stateless'
%%
-record(agent, {
    agent_id      :: binary(),
    session_id    :: binary() | undefined,
    session_pid   :: pid() | undefined,
    status        :: connected | disconnected,
    connected_at  :: integer(),
    attributes    :: map(),
    last_seen     :: integer(),
    custom_status :: binary(),
    subscriptions :: [binary()],
    session_type  :: tcp | http | stateless
}).

%% ── Session record ──────────────────────────────────────────────────────────
%% Stored in the ETS_SESSIONS table, keyed by session_id.
%%
%%   session_id  — Server-generated unique session identifier (binary)
%%   agent_id    — The agent_id bound to this session (binary), or 'undefined'
%%   session_pid — PID of the session process
%%
-record(session, {
    session_id  :: binary(),
    agent_id    :: binary() | undefined,
    session_pid :: pid()
}).

%% ── HTTP session record ─────────────────────────────────────────────────────
%% Stored in the ETS_HTTP_SESSIONS table, keyed by token.
%% Represents a stateless/HTTP agent session maintained via periodic heartbeats.
%%
%%   token       — Opaque session token returned at HTTP registration (binary)
%%   agent_id    — The agent_id bound to this HTTP session (binary)
%%   session_id  — Server-generated session identifier (binary)
%%   ttl_ms      — Time-to-live; session expires if no heartbeat within this window
%%   last_seen   — System time (ms) of last HTTP heartbeat or request
%%   mode        — 'http' | 'stateless'
%%
-record(http_session, {
    token      :: binary(),
    agent_id   :: binary(),
    session_id :: binary(),
    ttl_ms     :: non_neg_integer(),
    last_seen  :: integer(),
    mode       :: http | stateless
}).

-endif. %% PLUTO_RECORDS_HRL
