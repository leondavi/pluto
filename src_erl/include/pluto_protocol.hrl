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
-define(OP_STATS,               <<"stats">>).
-define(OP_SERVER_INFO,         <<"server_info">>).
-define(OP_ADMIN_RESET_STATS,   <<"admin_reset_stats">>).

%% ── Message delivery confirmation ───────────────────────────────────────────
%% Lets the sender explicitly acknowledge receipt of a message, closing the
%% fire-and-forget gap so agents get reliable end-to-end delivery feedback.
-define(OP_ACK,                 <<"ack">>).

%% ── Event sequence acknowledgment ──────────────────────────────────────────
%% Agents report the highest event sequence number they have processed,
%% enabling the server to distinguish new vs. already-handled events.
-define(OP_ACK_EVENTS,          <<"ack_events">>).

%% ── First-class task management ─────────────────────────────────────────────
%% Structured primitives for assigning work to agents, reporting progress,
%% and querying task status — replaces ad-hoc message conventions.
-define(OP_TASK_ASSIGN,         <<"task_assign">>).
-define(OP_TASK_UPDATE,         <<"task_update">>).
-define(OP_TASK_LIST,           <<"task_list">>).

%% ── Agent discovery by attributes ───────────────────────────────────────────
%% Lets agents discover peers by capability, role, or any custom metadata
%% instead of hardcoding agent IDs.
-define(OP_FIND_AGENTS,         <<"find_agents">>).

%% ── Topic-based publish / subscribe ─────────────────────────────────────────
%% Agents subscribe to named channels and receive only messages published
%% on those channels — more efficient than broadcast-to-all.
-define(OP_SUBSCRIBE,           <<"subscribe">>).
-define(OP_UNSUBSCRIBE,         <<"unsubscribe">>).
-define(OP_PUBLISH,             <<"publish">>).

%% ── Non-blocking lock probe (try-acquire) ───────────────────────────────────
%% Returns immediately with granted/unavailable without entering the wait
%% queue — useful for polling or optional coordination.
-define(OP_TRY_ACQUIRE,         <<"try_acquire">>).

%% ── Agent presence & status query ───────────────────────────────────────────
%% Query whether a specific agent is online, its last-seen timestamp, and
%% custom status so the caller can choose send vs. broadcast intelligently.
-define(OP_AGENT_STATUS,        <<"agent_status">>).

%% ── Batch work distribution ─────────────────────────────────────────────────
%% Atomically assign a batch of tasks across multiple agents and track
%% global progress.  Orphaned tasks are re-emitted if an agent disconnects.
-define(OP_TASK_BATCH,          <<"task_batch">>).
-define(OP_TASK_PROGRESS,       <<"task_progress">>).

%% ── Resource introspection (v0.2.42) ────────────────────────────────────────
%% Query who currently holds a resource, who held it last, and how long the
%% wait queue is.  Read-only; never modifies lock state.
-define(OP_RESOURCE_INFO,       <<"resource_info">>).

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

%% ── Delivery confirmation events ────────────────────────────────────────────
%% Pushed to the message sender when the target agent receives the message.
-define(EVT_DELIVERY_ACK,      <<"delivery_ack">>).

%% ── Task lifecycle events ───────────────────────────────────────────────────
%% Broadcast when a task is assigned to an agent or when its status changes,
%% enabling all participants to observe progress without custom broadcasts.
-define(EVT_TASK_ASSIGNED,     <<"task_assigned">>).
-define(EVT_TASK_UPDATED,      <<"task_updated">>).

%% ── Topic subscription events ───────────────────────────────────────────────
%% Delivered to agents subscribed to a named channel via the publish op.
-define(EVT_TOPIC_MESSAGE,     <<"topic_message">>).

%% ── Orphaned task events ────────────────────────────────────────────────────
%% Broadcast when an agent disconnects with unfinished tasks, allowing
%% another agent to pick up the abandoned work.
-define(EVT_TASKS_ORPHANED,    <<"tasks_orphaned">>).

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

%% ── Non-blocking try-acquire response ───────────────────────────────────────
%% Returned by try_acquire when the resource is already locked and the
%% caller does not want to enter the wait queue.
-define(STATUS_UNAVAILABLE,     <<"unavailable">>).

-endif. %% PLUTO_PROTOCOL_HRL
