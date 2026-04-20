%%%-------------------------------------------------------------------
%%% @doc pluto_session — Handles one client TCP connection.
%%%
%%% Each connected agent gets its own session process.  The session:
%%%   1. Generates a unique session_id on creation.
%%%   2. Reads newline-delimited JSON from the socket.
%%%   3. Dispatches requests to pluto_lock_mgr and pluto_msg_hub.
%%%   4. Sends JSON responses back on the socket.
%%%   5. Receives async events from other modules and pushes them
%%%      to the client.
%%%
%%% Socket mode: `{active, once}`.  After each received packet we
%%% re-arm with `inet:setopts/2` to prevent mailbox flooding.
%%%
%%% == Message Push Architecture ==
%%%
%%% Pluto uses a persistent, bidirectional TCP connection per agent.
%%% The same socket carries both request/response traffic and
%%% server-initiated (pushed) events.  The push flow is:
%%%
%%%   1. Agent A sends {"op":"send", "to":"B", "payload":{...}}
%%%      → this session decodes it and calls pluto_msg_hub:send_msg/4.
%%%
%%%   2. pluto_msg_hub looks up Agent B's session_pid in ETS and
%%%      delivers the event via an Erlang message:
%%%        Pid ! {pluto_event, #{"event" => "message", ...}}
%%%      If Agent B is disconnected the event is queued in the
%%%      agent's inbox ETS table and replayed on reconnect.
%%%
%%%   3. Agent B's session process receives {pluto_event, Event} in
%%%      its main receive loop (see below) and writes it directly
%%%      to B's TCP socket as a newline-delimited JSON line via
%%%      gen_tcp:send/2.
%%%
%%% The same push mechanism is used for lock_granted events from
%%% pluto_lock_mgr, broadcast events, delivery acks, task events,
%%% and topic-publish events.  All pushed events contain an
%%% "event" key (e.g. "message", "lock_granted", "broadcast",
%%% "delivery_ack", "task_assigned", "task_updated") so the client
%%% can distinguish them from request responses.
%%%
%%% On the Python client side, a background daemon thread runs a
%%% blocking recv loop on the same socket.  Lines with an "event"
%%% key are dispatched to registered handlers (on_message,
%%% on_lock_granted, etc.); lines without it are placed on a
%%% thread-safe queue for the calling thread's blocking requests.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_session).

-include("pluto.hrl").

%% API
-export([start/1]).

%% Internal entry point (called by proc_lib)
-export([init/1]).

%% Session loop state
-record(sess, {
    socket     :: gen_tcp:socket(),
    session_id :: binary(),
    agent_id   :: binary() | undefined,  %% set after register
    buffer     :: binary()               %% partial line accumulator
}).

%%====================================================================
%% API
%%====================================================================

%% @doc Spawn a new session process for the given client socket.
%% The caller should transfer socket ownership after this returns.
-spec start(gen_tcp:socket()) -> {ok, pid()}.
start(Socket) ->
    Pid = proc_lib:spawn(?MODULE, init, [Socket]),
    {ok, Pid}.

%%====================================================================
%% Session lifecycle
%%====================================================================

%% @doc Initialise the session: generate a session_id and wait for
%% the socket to be transferred to us.
init(Socket) ->
    SessionId = generate_session_id(),
    receive
        socket_ready -> ok
    after 5000 ->
        exit(socket_transfer_timeout)
    end,
    %% Set active-once mode to receive one packet at a time
    inet:setopts(Socket, [{active, once}]),
    loop(#sess{socket = Socket, session_id = SessionId,
               agent_id = undefined, buffer = <<>>}).

%%====================================================================
%% Main receive loop
%%====================================================================

%% @private The main event loop.  Handles three kinds of messages:
%%   - TCP data from the client socket (request/response path)
%%   - Internal events pushed by other Pluto modules (server→client push)
%%   - Socket close / error (cleanup path)
%%
%% Request/response path:  {tcp, Sock, Data} arrives, is buffered,
%% split into complete JSON lines, decoded, and dispatched through
%% handle_request/2 to the appropriate handler.  The handler sends a
%% synchronous JSON response back on the same socket.
%%
%% Server→client push path:  Other Erlang processes (pluto_msg_hub,
%% pluto_lock_mgr) send {pluto_event, Event} to this process.  The
%% event map is JSON-encoded and written to the TCP socket immediately.
%% This is how messages, lock grants, broadcasts, and task events
%% reach the connected agent without the agent polling.
%%
%% Takeover path:  If the same agent_id reconnects in a new session,
%% the old session receives {pluto_takeover, _} and shuts down.
loop(#sess{socket = Sock} = S) ->
    receive
        %% ── Incoming TCP data ───────────────────────────────────
        {tcp, Sock, Data} ->
            NewBuf = <<(S#sess.buffer)/binary, Data/binary>>,
            case check_line_length(NewBuf) of
                ok ->
                    S2 = process_lines(S#sess{buffer = NewBuf}),
                    inet:setopts(Sock, [{active, once}]),
                    loop(S2);
                too_long ->
                    send_error(Sock, <<"line_too_long">>),
                    gen_tcp:close(Sock),
                    cleanup(S)
            end;

        %% ── Socket closed by client ─────────────────────────────
        {tcp_closed, Sock} ->
            cleanup(S);

        %% ── Socket error ────────────────────────────────────────
        {tcp_error, Sock, _Reason} ->
            gen_tcp:close(Sock),
            cleanup(S);

        %% ── Async event from Pluto internals ────────────────────
        %% This is the core push mechanism: pluto_msg_hub, pluto_lock_mgr,
        %% and other modules send {pluto_event, EventMap} directly to this
        %% session process via Erlang messaging (Pid ! {pluto_event, ...}).
        %% The event is serialised as a JSON line and written to the TCP
        %% socket, arriving at the client without any polling or request.
        %% Event types include: "message", "broadcast", "lock_granted",
        %% "delivery_ack", "task_assigned", "task_updated".
        {pluto_event, Event} when is_map(Event) ->
            send_json(Sock, Event),
            loop(S);

        %% ── Takeover: another session claimed our agent_id ──────
        {pluto_takeover, _AgentId} ->
            gen_tcp:close(Sock),
            cleanup(S);

        %% ── Catch-all ───────────────────────────────────────────
        _Other ->
            loop(S)
    end.

%%====================================================================
%% Line processing
%%====================================================================

%% @private Extract complete lines from the buffer and process each one.
%% Returns the session state with the remaining (incomplete) buffer.
process_lines(#sess{buffer = Buf} = S) ->
    case binary:split(Buf, <<"\n">>) of
        [Line, Rest] ->
            S2 = handle_line(string:trim(Line), S),
            process_lines(S2#sess{buffer = Rest});
        [_Incomplete] ->
            %% No complete line yet — keep buffering
            S
    end.

%% @private Handle a single complete JSON line from the client.
handle_line(<<>>, S) ->
    S;  %% Ignore empty lines
handle_line(Line, #sess{socket = Sock, session_id = SessId} = S) ->
    %% Update liveness timestamp on every received message
    pluto_heartbeat:touch(SessId),

    case pluto_protocol_json:decode(Line) of
        {ok, Msg} ->
            handle_request(Msg, S);
        {error, _} ->
            send_error(Sock, ?ERR_BAD_REQUEST),
            S
    end.

%%====================================================================
%% Request dispatch
%%====================================================================

%% @private Route a decoded JSON request to the appropriate handler.
%%
%% Handler categories (grouped by registration requirement):
%%
%%   No registration required:
%%     register    — Registers the agent, sets agent_id on the session.
%%     ping        — Heartbeat / liveness check, returns server timestamp.
%%     selftest    — Runs internal server self-test suite.
%%     stats       — Returns global counters and per-agent statistics.
%%     server_info — Returns server metadata (version, ports, uptime, etc.).
%%     admin_*     — Admin operations (list locks, force release, etc.).
%%
%%   Registration required (agent_id must be set):
%%     acquire      — Request a lock on a resource; may block (returns "wait").
%%     try_acquire  — Non-blocking lock probe; returns "unavailable" if held.
%%     release      — Release a held lock, triggering grant to next waiter.
%%     renew        — Extend TTL of an existing lock.
%%     send         — Direct message to another agent (routed by msg_hub).
%%     broadcast    — Message to all connected agents.
%%     list_agents  — Enumerate connected agents (optionally detailed).
%%     find_agents  — Discover agents by attribute filter.
%%     subscribe    — Subscribe to a named topic channel.
%%     unsubscribe  — Unsubscribe from a topic channel.
%%     publish      — Send a message to all subscribers of a topic.
%%     task_assign  — Create and assign a server-tracked task.
%%     task_update  — Update status of an assigned task.
%%     task_list    — Query tasks with optional filters.
%%     task_batch   — Atomically assign multiple tasks.
%%     task_progress— Get global task progress overview.
%%     ack          — Confirm receipt of a delivered message.
%%     ack_events   — Report highest processed event sequence.
%%     event_history— Query past events by sequence number.
handle_request(#{<<"op">> := ?OP_REGISTER} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_register(Msg, S);
handle_request(#{<<"op">> := ?OP_PING}, S) ->
    pluto_stats:inc(total_requests),
    handle_ping(S);
handle_request(#{<<"op">> := ?OP_SELFTEST}, S) ->
    pluto_stats:inc(total_requests),
    handle_selftest(S);
handle_request(#{<<"op">> := ?OP_STATS}, S) ->
    pluto_stats:inc(total_requests),
    handle_stats(S);
handle_request(#{<<"op">> := ?OP_SERVER_INFO}, S) ->
    pluto_stats:inc(total_requests),
    handle_server_info(S);
%% ── Admin operations (no registration required) ─────────────────
handle_request(#{<<"op">> := ?OP_ADMIN_LIST_LOCKS} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_FORCE_RELEASE} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_LIST_AGENTS} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_DISCONNECT} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_DEADLOCK_GRAPH} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_FENCING_SEQ} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := ?OP_ADMIN_RESET_STATS} = Msg, S) ->
    handle_admin(Msg, S);
handle_request(#{<<"op">> := _Op}, #sess{agent_id = undefined} = S) ->
    %% All operations except register, ping, selftest, stats, and admin require registration
    send_error(S#sess.socket, ?ERR_NOT_REGISTERED),
    S;
handle_request(#{<<"op">> := ?OP_ACQUIRE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_acquire(Msg, S);
handle_request(#{<<"op">> := ?OP_RELEASE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_release(Msg, S);
handle_request(#{<<"op">> := ?OP_RENEW} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_renew(Msg, S);
handle_request(#{<<"op">> := ?OP_SEND} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_send(Msg, S);
handle_request(#{<<"op">> := ?OP_BROADCAST} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_broadcast(Msg, S);
handle_request(#{<<"op">> := ?OP_LIST_AGENTS} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_list_agents(Msg, S);
handle_request(#{<<"op">> := ?OP_EVENT_HISTORY} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_event_history(Msg, S);
%% ── Message delivery confirmation ───────────────────────────────────
handle_request(#{<<"op">> := ?OP_ACK} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_ack(Msg, S);
%% ── Event sequence acknowledgment ───────────────────────────────────
handle_request(#{<<"op">> := ?OP_ACK_EVENTS} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_ack_events(Msg, S);
%% ── Task management (assign / update / list) ────────────────────────
handle_request(#{<<"op">> := ?OP_TASK_ASSIGN} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_task_assign(Msg, S);
handle_request(#{<<"op">> := ?OP_TASK_UPDATE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_task_update(Msg, S);
handle_request(#{<<"op">> := ?OP_TASK_LIST} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_task_list(Msg, S);
%% ── Agent discovery by attributes ───────────────────────────────────
handle_request(#{<<"op">> := ?OP_FIND_AGENTS} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_find_agents(Msg, S);
%% ── Topic-based publish / subscribe ─────────────────────────────────
handle_request(#{<<"op">> := ?OP_SUBSCRIBE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_subscribe(Msg, S);
handle_request(#{<<"op">> := ?OP_UNSUBSCRIBE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_unsubscribe(Msg, S);
handle_request(#{<<"op">> := ?OP_PUBLISH} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_publish(Msg, S);
%% ── Non-blocking lock probe (try-acquire) ───────────────────────────
handle_request(#{<<"op">> := ?OP_TRY_ACQUIRE} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_try_acquire(Msg, S);
%% ── Agent presence & status query ───────────────────────────────────
handle_request(#{<<"op">> := ?OP_AGENT_STATUS} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_agent_status(Msg, S);
%% ── Batch work distribution ─────────────────────────────────────────
handle_request(#{<<"op">> := ?OP_TASK_BATCH} = Msg, S) ->
    pluto_stats:inc(total_requests),
    handle_task_batch(Msg, S);
handle_request(#{<<"op">> := ?OP_TASK_PROGRESS}, S) ->
    pluto_stats:inc(total_requests),
    handle_task_progress(S);
handle_request(#{<<"op">> := _}, S) ->
    send_error(S#sess.socket, ?ERR_UNKNOWN_OP),
    S;
handle_request(_, S) ->
    send_error(S#sess.socket, ?ERR_BAD_REQUEST),
    S.

%%====================================================================
%% Operation handlers
%%====================================================================

%% ── Register ────────────────────────────────────────────────────────
%% Registers the agent with pluto_msg_hub, which stores the agent_id,
%% session_id, and this process's pid in ETS.  From this point on, other
%% agents can send messages to this agent_id and pluto_msg_hub will
%% deliver them by sending {pluto_event, ...} to this session process.
%% Returns: session_id, agent_id, heartbeat_interval_ms.
handle_register(#{<<"agent_id">> := AgentId} = Msg, #sess{socket = Sock,
                                                           session_id = SessId} = S)
  when is_binary(AgentId), AgentId =/= <<>> ->
    Token = maps:get(<<"token">>, Msg, undefined),
    Attrs0 = maps:get(<<"attributes">>, Msg, #{}),
    %% Pass optional session_id from client as resume_session_id hint
    Attrs = case maps:find(<<"session_id">>, Msg) of
        {ok, RSessId} when is_binary(RSessId), RSessId =/= <<>> ->
            Attrs0#{resume_session_id => RSessId};
        _ ->
            Attrs0
    end,
    case pluto_policy:check_auth(AgentId, Token) of
        ok ->
            case pluto_msg_hub:register_agent(AgentId, SessId, self(), Attrs) of
                {ok, RetSessId} ->
                    HbMs = pluto_config:get(heartbeat_interval_ms,
                                            ?DEFAULT_HEARTBEAT_INTERVAL_MS),
                    ReclaimedLocks = reclaim_locks(AgentId),
                    send_json(Sock, #{
                        <<"status">>               => ?STATUS_OK,
                        <<"session_id">>           => RetSessId,
                        <<"agent_id">>             => AgentId,
                        <<"heartbeat_interval_ms">> => HbMs,
                        <<"reclaimed_locks">>       => ReclaimedLocks,
                        <<"guide">>                => tcp_registration_guide(HbMs)
                    }),
                    pluto_event_log:log(agent_registered, #{agent_id => AgentId,
                                                            session_id => RetSessId}),
                    S#sess{agent_id = AgentId, session_id = RetSessId};
                {ok, RetSessId, resumed} ->
                    %% Session resumed — reuse old session_id
                    HbMs = pluto_config:get(heartbeat_interval_ms,
                                            ?DEFAULT_HEARTBEAT_INTERVAL_MS),
                    ReclaimedLocks = reclaim_locks(AgentId),
                    send_json(Sock, #{
                        <<"status">>               => ?STATUS_OK,
                        <<"session_id">>           => RetSessId,
                        <<"agent_id">>             => AgentId,
                        <<"heartbeat_interval_ms">> => HbMs,
                        <<"resumed">>              => true,
                        <<"reclaimed_locks">>       => ReclaimedLocks,
                        <<"guide">>                => tcp_registration_guide(HbMs)
                    }),
                    pluto_event_log:log(agent_registered, #{agent_id => AgentId,
                                                            session_id => RetSessId,
                                                            resumed => true}),
                    S#sess{agent_id = AgentId, session_id = RetSessId};
                {ok, RetSessId, ActualAgentId} ->
                    %% Name was taken — server assigned a unique suffixed name
                    HbMs = pluto_config:get(heartbeat_interval_ms,
                                            ?DEFAULT_HEARTBEAT_INTERVAL_MS),
                    ReclaimedLocks = reclaim_locks(ActualAgentId),
                    send_json(Sock, #{
                        <<"status">>               => ?STATUS_OK,
                        <<"session_id">>           => RetSessId,
                        <<"agent_id">>             => ActualAgentId,
                        <<"heartbeat_interval_ms">> => HbMs,
                        <<"reclaimed_locks">>       => ReclaimedLocks,
                        <<"guide">>                => tcp_registration_guide(HbMs)
                    }),
                    pluto_event_log:log(agent_registered, #{agent_id => ActualAgentId,
                                                            requested_id => AgentId,
                                                            session_id => RetSessId}),
                    S#sess{agent_id = ActualAgentId, session_id = RetSessId}
            end;
        {error, unauthorized} ->
            pluto_event_log:log(auth_failure, #{agent_id => AgentId, reason => bad_token}),
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_UNAUTHORIZED
            }),
            S
    end;
handle_register(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Ping ────────────────────────────────────────────────────────────
handle_ping(#sess{socket = Sock} = S) ->
    HbMs = pluto_config:get(heartbeat_interval_ms, ?DEFAULT_HEARTBEAT_INTERVAL_MS),
    Now  = erlang:system_time(millisecond),
    send_json(Sock, #{
        <<"status">>               => ?STATUS_PONG,
        <<"ts">>                   => Now,
        <<"heartbeat_interval_ms">> => HbMs
    }),
    S.

%% ── Acquire lock ────────────────────────────────────────────────────
%% Requests a lock on a named resource via pluto_lock_mgr.  Three
%% outcomes: (1) granted immediately → returns lock_ref + fencing_token,
%% (2) resource busy → returns "wait" + wait_ref, and a lock_granted
%% event is pushed later via {pluto_event, ...} when the lock becomes
%% available, (3) deadlock detected → returns error with victim flag.
handle_acquire(Msg, #sess{socket = Sock, session_id = SessId,
                          agent_id = AgentId} = S) ->
    case maps:find(<<"resource">>, Msg) of
        {ok, RawResource} ->
            case pluto_resource:normalize(RawResource) of
                {ok, Resource} ->
                    Mode   = parse_mode(maps:get(<<"mode">>, Msg, ?MODE_WRITE)),
                    %% ACL check
                    case pluto_policy:check_acl(AgentId, Resource, Mode) of
                        ok ->
                            TtlMs  = maps:get(<<"ttl_ms">>, Msg, 30000),
                            MaxWait = maps:get(<<"max_wait_ms">>, Msg, undefined),
                            Opts = #{
                                ttl_ms      => TtlMs,
                                max_wait_ms => MaxWait,
                                session_id  => SessId,
                                session_pid => self()
                            },
                            case pluto_lock_mgr:acquire(Resource, Mode, AgentId, Opts) of
                                {ok, LockRef, FToken} ->
                                    send_json(Sock, #{
                                        <<"status">>        => ?STATUS_OK,
                                        <<"lock_ref">>      => LockRef,
                                        <<"fencing_token">> => FToken
                                    });
                                {wait, WaitRef} ->
                                    send_json(Sock, #{
                                        <<"status">>   => ?STATUS_WAIT,
                                        <<"wait_ref">> => WaitRef
                                    });
                                {error, deadlock} ->
                                    send_json(Sock, #{
                                        <<"status">> => ?STATUS_ERROR,
                                        <<"reason">> => ?ERR_DEADLOCK,
                                        <<"victim">> => true
                                    });
                                {error, Reason} ->
                                    send_json(Sock, #{
                                        <<"status">> => ?STATUS_ERROR,
                                        <<"reason">> => to_bin(Reason)
                                    })
                            end;
                        {error, unauthorized} ->
                            pluto_event_log:log(acl_denied, #{agent_id => AgentId,
                                                              resource => Resource,
                                                              mode => Mode}),
                            send_json(Sock, #{
                                <<"status">> => ?STATUS_ERROR,
                                <<"reason">> => ?ERR_UNAUTHORIZED,
                                <<"detail">> => <<"resource not permitted">>
                            })
                    end;
                {error, empty_resource} ->
                    send_error(Sock, ?ERR_BAD_REQUEST)
            end;
        error ->
            send_error(Sock, ?ERR_BAD_REQUEST)
    end,
    S.

%% ── Release lock ────────────────────────────────────────────────────
handle_release(#{<<"lock_ref">> := LockRef}, #sess{socket = Sock,
                                                    agent_id = AgentId} = S)
  when is_binary(LockRef) ->
    case pluto_lock_mgr:release(LockRef, AgentId) of
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, not_found} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_NOT_FOUND
            })
    end,
    S;
handle_release(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Renew lock ──────────────────────────────────────────────────────
handle_renew(#{<<"lock_ref">> := LockRef} = Msg, #sess{socket = Sock} = S)
  when is_binary(LockRef) ->
    TtlMs = maps:get(<<"ttl_ms">>, Msg, 30000),
    case pluto_lock_mgr:renew(LockRef, #{ttl_ms => TtlMs}) of
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, not_found} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_NOT_FOUND
            })
    end,
    S;
handle_renew(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Send direct message ─────────────────────────────────────────────
%% Routes a message from this agent to another via pluto_msg_hub.
%% The hub looks up the target's session_pid in ETS and delivers the
%% message as: TargetPid ! {pluto_event, #{"event" => "message", ...}}.
%% The target's session process then writes it to the TCP socket.
%%
%% If the target agent is disconnected but known (registered before),
%% the message is queued in the agent's inbox ETS table and will be
%% replayed in order when the agent reconnects.
%%
%% Supports optional `request_id` field: when present the server will
%% push a delivery_ack event back to the sender once the target
%% receives the message, giving reliable end-to-end delivery feedback.
handle_send(#{<<"to">> := To, <<"payload">> := Payload} = Msg,
            #sess{socket = Sock, agent_id = From} = S)
  when is_binary(To) ->
    RequestId = maps:get(<<"request_id">>, Msg, undefined),
    case pluto_msg_hub:send_msg(From, To, Payload, RequestId) of
        {ok, MsgId} ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK,
                              <<"msg_id">> => MsgId});
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, unknown_target} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_UNKNOWN_TARGET
            })
    end,
    S;
handle_send(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Broadcast ───────────────────────────────────────────────────────
handle_broadcast(#{<<"payload">> := Payload}, #sess{socket = Sock,
                                                     agent_id = From} = S) ->
    pluto_msg_hub:broadcast(From, Payload),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_broadcast(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── List agents ─────────────────────────────────────────────────────
%% Pass {"detailed": true} to include last_seen timestamps, custom status,
%% agent attributes, and subscriptions for each agent.
handle_list_agents(Msg, #sess{socket = Sock} = S) ->
    Detailed = maps:get(<<"detailed">>, Msg, false),
    IncludeOffline = maps:get(<<"include_offline">>, Msg, false),
    case Detailed orelse IncludeOffline of
        true ->
            AgentMaps = pluto_msg_hub:list_agents_detailed(),
            send_json(Sock, #{<<"status">> => ?STATUS_OK,
                              <<"agents">> => AgentMaps});
        _ ->
            Agents = pluto_msg_hub:list_agents(),
            send_json(Sock, #{<<"status">> => ?STATUS_OK,
                              <<"agents">> => Agents})
    end,
    S.

%% ── Event history ───────────────────────────────────────────────────
handle_event_history(Msg, #sess{socket = Sock} = S) ->
    SinceSeq = maps:get(<<"since_token">>, Msg, 0),
    Limit    = maps:get(<<"limit">>, Msg, 100),
    Events   = pluto_event_log:query(SinceSeq, Limit),
    send_json(Sock, #{
        <<"status">> => ?STATUS_OK,
        <<"events">> => Events
    }),
    S.

%% ── Message delivery acknowledgment ─────────────────────────────────
%% The sender confirms receipt of a message by its msg_id.  This is an
%% application-level ack — the server logs the acknowledgment event.
handle_ack(#{<<"msg_id">> := MsgId}, #sess{socket = Sock, agent_id = AgentId} = S) ->
    pluto_event_log:log(message_acked, #{agent_id => AgentId, msg_id => MsgId}),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_ack(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Event sequence acknowledgment ───────────────────────────────────
%% Agents report the highest event sequence they processed so the server
%% can distinguish new vs. already-handled events.  Currently logged;
%% future versions may use this for server-side cursor management.
handle_ack_events(Msg, #sess{socket = Sock, agent_id = AgentId} = S) ->
    Seq = case maps:find(<<"last_seq">>, Msg) of
              {ok, V} when is_integer(V) -> V;
              _ -> maps:get(<<"up_to_seq">>, Msg, undefined)
          end,
    case Seq of
        undefined ->
            send_error(Sock, ?ERR_BAD_REQUEST),
            S;
        _ when is_integer(Seq) ->
            pluto_event_log:log(events_acked, #{agent_id => AgentId, last_seq => Seq}),
            send_json(Sock, #{<<"status">> => ?STATUS_OK}),
            S;
        _ ->
            send_error(Sock, ?ERR_BAD_REQUEST),
            S
    end.

%% ── Task assignment ─────────────────────────────────────────────────
%% Creates a server-tracked task with an immutable task_id.  The server
%% stores the task, broadcasts a task_assigned event so all agents can
%% observe progress, and tries to deliver it to the assigned agent's inbox
%% if that agent is offline.
handle_task_assign(#{<<"assignee">> := Assignee} = Msg,
                   #sess{socket = Sock, agent_id = From} = S)
  when is_binary(Assignee) ->
    TaskId = generate_task_id(),
    Description = maps:get(<<"description">>, Msg, <<>>),
    Payload = maps:get(<<"payload">>, Msg, #{}),
    Now = erlang:system_time(millisecond),
    Task = #{
        <<"task_id">>     => TaskId,
        <<"from">>        => From,
        <<"assigner">>    => From,
        <<"assignee">>    => Assignee,
        <<"description">> => Description,
        <<"payload">>     => Payload,
        <<"status">>      => <<"pending">>,
        <<"created_at">>  => Now,
        <<"updated_at">>  => Now
    },
    ets:insert(?ETS_TASKS, {TaskId, Task}),
    %% Broadcast task_assigned to all connected agents
    Event = #{
        <<"event">>       => ?EVT_TASK_ASSIGNED,
        <<"task_id">>     => TaskId,
        <<"from">>        => From,
        <<"assignee">>    => Assignee,
        <<"description">> => Description,
        <<"payload">>     => Payload
    },
    pluto_msg_hub:broadcast(From, Event),
    pluto_event_log:log(task_assigned, #{task_id => TaskId, from => From,
                                          assignee => Assignee}),
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"task_id">> => TaskId}),
    S;
handle_task_assign(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Task status update ──────────────────────────────────────────────
%% Agents report progress on a task.  Valid statuses include "in_progress",
%% "complete", "failed".  A task_updated event is broadcast so all agents
%% can track workflow progress in real time.
handle_task_update(#{<<"task_id">> := TaskId, <<"status">> := NewStatus} = Msg,
                   #sess{socket = Sock, agent_id = AgentId} = S)
  when is_binary(TaskId), is_binary(NewStatus) ->
    Result = maps:get(<<"result">>, Msg, #{}),
    case ets:lookup(?ETS_TASKS, TaskId) of
        [{TaskId, Task}] ->
            Now = erlang:system_time(millisecond),
            Updated = Task#{
                <<"status">>     => NewStatus,
                <<"result">>     => Result,
                <<"updated_at">> => Now
            },
            ets:insert(?ETS_TASKS, {TaskId, Updated}),
            %% Broadcast task_updated to all agents
            Event = #{
                <<"event">>   => ?EVT_TASK_UPDATED,
                <<"task_id">> => TaskId,
                <<"agent_id">> => AgentId,
                <<"status">>  => NewStatus,
                <<"result">>  => Result
            },
            pluto_msg_hub:broadcast(AgentId, Event),
            pluto_event_log:log(task_updated, #{task_id => TaskId,
                                                 agent_id => AgentId,
                                                 status => NewStatus}),
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        [] ->
            send_json(Sock, #{<<"status">> => ?STATUS_ERROR,
                              <<"reason">> => ?ERR_NOT_FOUND})
    end,
    S;
handle_task_update(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Task list query ─────────────────────────────────────────────────
%% Returns all server-tracked tasks with their current status, assignee,
%% timestamps, and result payloads.
handle_task_list(Msg, #sess{socket = Sock} = S) ->
    AllTasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    %% Optional filters by assignee and/or status
    FilterAssignee = maps:get(<<"assignee">>, Msg, undefined),
    FilterStatus = maps:get(<<"status">>, Msg, undefined),
    Filtered = lists:filter(fun(T) ->
        MatchAssignee = (FilterAssignee =:= undefined) orelse
                        (maps:get(<<"assignee">>, T, undefined) =:= FilterAssignee),
        MatchStatus = (FilterStatus =:= undefined) orelse
                      (maps:get(<<"status">>, T, undefined) =:= FilterStatus),
        MatchAssignee andalso MatchStatus
    end, AllTasks),
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"tasks">> => Filtered}),
    S.

%% ── Agent discovery by attributes ───────────────────────────────────
%% Find agents whose metadata matches all key-value pairs in the filter.
%% Example: {"op": "find_agents", "filter": {"role": "code-fixer"}}
handle_find_agents(#{<<"filter">> := Filter}, #sess{socket = Sock} = S)
  when is_map(Filter) ->
    AgentMaps = pluto_msg_hub:find_agents(Filter),
    %% Return just agent IDs for simple discovery; use list_agents detailed for full info
    AgentIds = [maps:get(<<"agent_id">>, M) || M <- AgentMaps],
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"agents">> => AgentIds}),
    S;
handle_find_agents(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Topic subscription ──────────────────────────────────────────────
%% Agents subscribe to named channels (e.g. "tasks.code-fix") and only
%% receive messages published on those channels.
handle_subscribe(#{<<"topic">> := Topic}, #sess{socket = Sock,
                                                 agent_id = AgentId} = S)
  when is_binary(Topic) ->
    pluto_msg_hub:subscribe(AgentId, Topic),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_subscribe(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Topic unsubscription ────────────────────────────────────────────
handle_unsubscribe(#{<<"topic">> := Topic}, #sess{socket = Sock,
                                                   agent_id = AgentId} = S)
  when is_binary(Topic) ->
    pluto_msg_hub:unsubscribe(AgentId, Topic),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_unsubscribe(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Publish to topic ────────────────────────────────────────────────
%% Sends a message to all agents subscribed to the given topic.  Only
%% subscribers receive the event — more targeted than the global broadcast.
handle_publish(#{<<"topic">> := Topic, <<"payload">> := Payload},
               #sess{socket = Sock, agent_id = From} = S)
  when is_binary(Topic) ->
    pluto_msg_hub:publish(From, Topic, Payload),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_publish(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Non-blocking lock probe (try-acquire) ───────────────────────────
%% Returns immediately with "ok" + lock_ref if the resource is free, or
%% "unavailable" if it is already locked — never enters the wait queue.
%% Useful for polling or optional coordination.
handle_try_acquire(Msg, #sess{socket = Sock, session_id = SessId,
                               agent_id = AgentId} = S) ->
    case maps:find(<<"resource">>, Msg) of
        {ok, RawResource} ->
            case pluto_resource:normalize(RawResource) of
                {ok, Resource} ->
                    Mode = parse_mode(maps:get(<<"mode">>, Msg, ?MODE_WRITE)),
                    case pluto_policy:check_acl(AgentId, Resource, Mode) of
                        ok ->
                            TtlMs = maps:get(<<"ttl_ms">>, Msg, 30000),
                            Opts = #{
                                ttl_ms      => TtlMs,
                                max_wait_ms => undefined,
                                session_id  => SessId,
                                session_pid => self()
                            },
                            case pluto_lock_mgr:try_acquire(Resource, Mode,
                                                             AgentId, Opts) of
                                {ok, LockRef, FToken} ->
                                    send_json(Sock, #{
                                        <<"status">>        => ?STATUS_OK,
                                        <<"lock_ref">>      => LockRef,
                                        <<"fencing_token">> => FToken
                                    });
                                unavailable ->
                                    send_json(Sock, #{
                                        <<"status">> => ?STATUS_UNAVAILABLE
                                    })
                            end;
                        {error, unauthorized} ->
                            send_json(Sock, #{
                                <<"status">> => ?STATUS_ERROR,
                                <<"reason">> => ?ERR_UNAUTHORIZED
                            })
                    end;
                {error, empty_resource} ->
                    send_error(Sock, ?ERR_BAD_REQUEST)
            end;
        error ->
            send_error(Sock, ?ERR_BAD_REQUEST)
    end,
    S.

%% ── Agent presence & status query ───────────────────────────────────
%% Returns whether a specific agent is online, its last-seen timestamp,
%% custom status, and attributes.  Lets callers choose between send and
%% broadcast intelligently.
handle_agent_status(#{<<"agent_id">> := TargetId}, #sess{socket = Sock} = S)
  when is_binary(TargetId) ->
    case pluto_msg_hub:agent_status(TargetId) of
        {ok, Info} ->
            %% Flatten agent info into the response; rename ets "status" to
            %% "agent_status" to avoid conflict with the JSON response status
            AgentSt = maps:get(<<"status">>, Info, <<"disconnected">>),
            Online = AgentSt =:= <<"connected">>,
            InfoClean = maps:remove(<<"status">>, Info),
            send_json(Sock, maps:merge(
                #{<<"status">> => ?STATUS_OK,
                  <<"online">> => Online},
                InfoClean
            ));
        {error, not_found} ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK,
                              <<"agent_id">> => TargetId,
                              <<"online">> => false,
                              <<"last_seen">> => 0,
                              <<"custom_status">> => <<>>,
                              <<"attributes">> => #{}})
    end,
    S;
%% Set custom status (no agent_id field = set own status)
handle_agent_status(#{<<"custom_status">> := CustomStatus},
                    #sess{socket = Sock, agent_id = AgentId} = S)
  when is_binary(CustomStatus) ->
    pluto_msg_hub:set_agent_status(AgentId, CustomStatus),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_agent_status(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Batch task distribution ─────────────────────────────────────────
%% Atomically assigns a batch of tasks across multiple agents and stores
%% them server-side.  If an assigned agent is disconnected, the task is
%% immediately marked as "orphaned" and a tasks_orphaned event is broadcast.
handle_task_batch(#{<<"tasks">> := TaskDefs},
                  #sess{socket = Sock, agent_id = From} = S)
  when is_list(TaskDefs) ->
    Now = erlang:system_time(millisecond),
    TaskIds = lists:map(fun(TaskDef) ->
        TaskId = generate_task_id(),
        Assignee = maps:get(<<"assignee">>, TaskDef, undefined),
        Desc = maps:get(<<"description">>, TaskDef, <<>>),
        Payload = maps:get(<<"payload">>, TaskDef, #{}),
        Task = #{
            <<"task_id">>     => TaskId,
            <<"from">>        => From,
            <<"assigner">>    => From,
            <<"assignee">>    => Assignee,
            <<"description">> => Desc,
            <<"payload">>     => Payload,
            <<"status">>      => <<"pending">>,
            <<"created_at">>  => Now,
            <<"updated_at">>  => Now
        },
        ets:insert(?ETS_TASKS, {TaskId, Task}),
        pluto_event_log:log(task_assigned, #{task_id => TaskId, from => From,
                                              assignee => Assignee}),
        TaskId
    end, TaskDefs),
    %% Broadcast batch assignment
    Event = #{
        <<"event">>    => ?EVT_TASK_ASSIGNED,
        <<"from">>     => From,
        <<"task_ids">> => TaskIds,
        <<"batch">>    => true
    },
    pluto_msg_hub:broadcast(From, Event),
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"task_ids">> => TaskIds}),
    S;
handle_task_batch(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Task progress overview ──────────────────────────────────────────
%% Returns a global view of all assigned/completed/failed/orphaned tasks
%% grouped by status, enabling coordinators to monitor multi-agent workflows.
handle_task_progress(#sess{socket = Sock} = S) ->
    AllTasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    %% Group by status
    ByStatus = lists:foldl(fun(T, Acc) ->
        St = maps:get(<<"status">>, T, <<"unknown">>),
        Acc#{St => maps:get(St, Acc, 0) + 1}
    end, #{}, AllTasks),
    %% Group by agent
    ByAgent = lists:foldl(fun(T, Acc) ->
        Agent = maps:get(<<"assignee">>, T, <<"unassigned">>),
        St = maps:get(<<"status">>, T, <<"unknown">>),
        AgentMap = maps:get(Agent, Acc, #{}),
        Acc#{Agent => AgentMap#{St => maps:get(St, AgentMap, 0) + 1}}
    end, #{}, AllTasks),
    send_json(Sock, #{
        <<"status">>    => ?STATUS_OK,
        <<"total">>     => length(AllTasks),
        <<"by_status">> => ByStatus,
        <<"by_agent">>  => ByAgent
    }),
    S.

%% ── Self-test ───────────────────────────────────────────────────────
handle_selftest(#sess{socket = Sock} = S) ->
    Result = pluto_selftest:run(),
    send_json(Sock, Result),
    S.

%% ── Stats ───────────────────────────────────────────────────────────
handle_stats(#sess{socket = Sock} = S) ->
    Summary = pluto_stats:get_summary(),
    send_json(Sock, Summary),
    S.

%% ── Server Info ─────────────────────────────────────────────────────
%% Returns comprehensive server metadata: version, OTP version, node name,
%% listen addresses, uptime, OS info, resource counts, and configuration.
handle_server_info(#sess{socket = Sock} = S) ->
    Now       = erlang:system_time(millisecond),
    StartedAt = pluto_stats:get_summary(),
    UptimeMs  = maps:get(<<"uptime_ms">>, StartedAt, 0),
    Live      = maps:get(<<"live">>, StartedAt, #{}),

    %% Erlang / OTP version
    OtpRelease  = list_to_binary(erlang:system_info(otp_release)),
    ErtsVsn     = list_to_binary(erlang:system_info(version)),

    %% Node name
    NodeName = atom_to_binary(node(), utf8),

    %% Configured ports
    TcpPort  = pluto_config:get(tcp_port, ?DEFAULT_TCP_PORT),
    HttpPort = pluto_config:get(http_port, ?DEFAULT_HTTP_PORT),
    HttpPortBin = case HttpPort of
        disabled -> <<"disabled">>;
        P when is_integer(P) -> P
    end,

    %% Collect all local IPs from network interfaces
    IPs = case inet:getifaddrs() of
        {ok, Ifaddrs} ->
            lists:usort(lists:filtermap(fun({_Iface, Props}) ->
                case proplists:get_value(addr, Props) of
                    {A, B, C, D} ->
                        Bin = iolist_to_binary(io_lib:format("~w.~w.~w.~w", [A, B, C, D])),
                        {true, Bin};
                    _ ->
                        false
                end
            end, Ifaddrs));
        _ ->
            []
    end,

    %% Hostname
    Hostname = case inet:gethostname() of
        {ok, H} -> list_to_binary(H);
        _       -> <<"unknown">>
    end,

    %% OS info
    {OsFamily, OsName} = os:type(),
    OsStr = iolist_to_binary(io_lib:format("~w/~w", [OsFamily, OsName])),

    %% Process counts
    ProcessCount = erlang:system_info(process_count),
    ProcessLimit = erlang:system_info(process_limit),

    %% Memory (in bytes)
    MemTotal   = erlang:memory(total),
    MemProcs   = erlang:memory(processes),
    MemEts     = erlang:memory(ets),

    %% Schedulers
    Schedulers = erlang:system_info(schedulers_online),

    Info = #{
        <<"status">>          => ?STATUS_OK,
        <<"server">>          => <<"pluto">>,
        <<"version">>         => list_to_binary(?VERSION),
        <<"otp_release">>     => OtpRelease,
        <<"erts_version">>    => ErtsVsn,
        <<"node">>            => NodeName,
        <<"hostname">>        => Hostname,
        <<"os">>              => OsStr,
        <<"tcp_port">>        => TcpPort,
        <<"http_port">>       => HttpPortBin,
        <<"ips">>             => IPs,
        <<"uptime_ms">>       => UptimeMs,
        <<"server_time">>     => Now,
        <<"schedulers">>      => Schedulers,
        <<"process_count">>   => ProcessCount,
        <<"process_limit">>   => ProcessLimit,
        <<"memory">>          => #{
            <<"total">>     => MemTotal,
            <<"processes">> => MemProcs,
            <<"ets">>       => MemEts
        },
        <<"live">>            => Live
    },
    send_json(Sock, Info),
    S.

%% ── Admin operations ────────────────────────────────────────────────
handle_admin(#{<<"op">> := Op} = Msg, #sess{socket = Sock} = S) ->
    AdminToken = pluto_config:get(admin_token, undefined),
    ProvidedToken = maps:get(<<"admin_token">>, Msg, undefined),
    case AdminToken =:= undefined orelse ProvidedToken =:= AdminToken of
        true ->
            Result = execute_admin(Op, Msg),
            send_json(Sock, Result),
            pluto_event_log:log(admin_action, #{op => Op});
        false ->
            pluto_event_log:log(admin_auth_failure, #{op => Op}),
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_UNAUTHORIZED
            })
    end,
    S.

%% @private Execute an admin operation.
execute_admin(?OP_ADMIN_LIST_LOCKS, _Msg) ->
    Locks = pluto_lock_mgr:list_locks(),
    LockMaps = [#{<<"lock_ref">> => L#lock.lock_ref,
                  <<"resource">> => L#lock.resource,
                  <<"agent_id">> => L#lock.agent_id,
                  <<"mode">> => atom_to_binary(L#lock.mode, utf8),
                  <<"fencing_token">> => L#lock.fencing_token}
                || L <- Locks],
    #{<<"status">> => ?STATUS_OK, <<"locks">> => LockMaps};
execute_admin(?OP_ADMIN_FORCE_RELEASE, #{<<"lock_ref">> := LockRef}) ->
    %% Force-release ignores agent ownership
    case ets:lookup(?ETS_LOCKS, LockRef) of
        [#lock{agent_id = AId}] ->
            pluto_lock_mgr:release(LockRef, AId),
            #{<<"status">> => ?STATUS_OK};
        [] ->
            #{<<"status">> => ?STATUS_ERROR, <<"reason">> => ?ERR_NOT_FOUND}
    end;
execute_admin(?OP_ADMIN_FORCE_RELEASE, _) ->
    #{<<"status">> => ?STATUS_ERROR, <<"reason">> => ?ERR_BAD_REQUEST};
execute_admin(?OP_ADMIN_LIST_AGENTS, _Msg) ->
    AllAgents = ets:tab2list(?ETS_AGENTS),
    AgentMaps = [#{<<"agent_id">> => A#agent.agent_id,
                   <<"status">> => atom_to_binary(A#agent.status, utf8),
                   <<"session_id">> => case A#agent.session_id of
                                           undefined -> null;
                                           SId -> SId
                                       end}
                 || A <- AllAgents],
    #{<<"status">> => ?STATUS_OK, <<"agents">> => AgentMaps};
execute_admin(?OP_ADMIN_DISCONNECT, #{<<"agent_id">> := AgentId}) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{session_pid = Pid}] when is_pid(Pid) ->
            exit(Pid, admin_disconnect),
            pluto_msg_hub:unregister_agent(AgentId),
            #{<<"status">> => ?STATUS_OK};
        _ ->
            #{<<"status">> => ?STATUS_ERROR, <<"reason">> => ?ERR_NOT_FOUND}
    end;
execute_admin(?OP_ADMIN_DISCONNECT, _) ->
    #{<<"status">> => ?STATUS_ERROR, <<"reason">> => ?ERR_BAD_REQUEST};
execute_admin(?OP_ADMIN_DEADLOCK_GRAPH, _Msg) ->
    Edges = ets:tab2list(?ETS_WAIT_GRAPH),
    EdgeMaps = [#{<<"waiter">> => W, <<"holder">> => H} || {W, H} <- Edges],
    #{<<"status">> => ?STATUS_OK, <<"edges">> => EdgeMaps};
execute_admin(?OP_ADMIN_FENCING_SEQ, _Msg) ->
    FSeq = pluto_lock_mgr:get_fencing_seq(),
    #{<<"status">> => ?STATUS_OK, <<"fencing_seq">> => FSeq};
execute_admin(?OP_ADMIN_RESET_STATS, _Msg) ->
    pluto_stats:reset(),
    #{<<"status">> => ?STATUS_OK};
execute_admin(_, _) ->
    #{<<"status">> => ?STATUS_ERROR, <<"reason">> => ?ERR_UNKNOWN_OP}.

%%====================================================================
%% Socket helpers
%%====================================================================

%% @private Send a JSON map as a line on the socket.
send_json(Sock, Map) ->
    gen_tcp:send(Sock, pluto_protocol_json:encode_line(Map)).

%% @private Send a standard error response.
send_error(Sock, Reason) ->
    send_json(Sock, #{<<"status">> => ?STATUS_ERROR, <<"reason">> => Reason}).

%%====================================================================
%% Cleanup
%%====================================================================

%% @private Unregister the agent and clean up when the session ends.
cleanup(#sess{agent_id = undefined}) ->
    ok;
cleanup(#sess{agent_id = AgentId, session_id = SessId}) ->
    ?LOG_INFO("Session ~s (agent ~s) disconnected", [SessId, AgentId]),
    pluto_msg_hub:unregister_agent(AgentId),
    ok.

%%====================================================================
%% Utilities
%%====================================================================

%% @private Generate a unique session ID in the form `sess-<uuid>`.
generate_session_id() ->
    %% Use crypto:strong_rand_bytes for a 128-bit random session ID
    Bytes = crypto:strong_rand_bytes(16),
    Hex = binary:encode_hex(Bytes),
    LowerHex = string:lowercase(Hex),
    %% Format as sess-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    <<A:8/binary, B:4/binary, C:4/binary, D:4/binary, E:12/binary, _/binary>> = LowerHex,
    iolist_to_binary([<<"sess-">>, A, $-, B, $-, C, $-, D, $-, E]).

%% @private Parse a lock mode binary into an atom.
parse_mode(?MODE_WRITE) -> write;
parse_mode(?MODE_READ)  -> read;
parse_mode(_)           -> write.  %% Default to write (exclusive)

%% @private Format reclaimed locks for a register response.
reclaim_locks(AgentId) ->
    Locks = pluto_lock_mgr:locks_for_agent(AgentId),
    [#{<<"lock_ref">>      => L#lock.lock_ref,
       <<"resource">>      => L#lock.resource,
       <<"mode">>          => atom_to_binary(L#lock.mode, utf8),
       <<"fencing_token">> => L#lock.fencing_token} || L <- Locks].

%% @private Generate a unique task ID in the form `TASK-<hex>`.
generate_task_id() ->
    Hex = string:lowercase(binary:encode_hex(crypto:strong_rand_bytes(8))),
    iolist_to_binary([<<"TASK-">>, Hex]).

%% @private Convert a term to binary for JSON error reasons.
to_bin(B) when is_binary(B)  -> B;
to_bin(A) when is_atom(A)    -> atom_to_binary(A, utf8);
to_bin(L) when is_list(L)    -> list_to_binary(L);
to_bin(T)                    -> iolist_to_binary(io_lib:format("~p", [T])).

%% @private Check that the accumulated buffer doesn't exceed the max line length.
check_line_length(Buf) ->
    case byte_size(Buf) > ?MAX_LINE_LENGTH of
        true  -> too_long;
        false -> ok
    end.

%%====================================================================
%% Registration guide — included in TCP register responses
%%====================================================================

tcp_registration_guide(HbMs) ->
    HbSec = HbMs div 1000,
    HbDesc = iolist_to_binary(io_lib:format(
        "Send {\"op\":\"ping\"} every ~w seconds to stay alive. "
        "Sessions expire after ~w seconds of silence.",
        [HbSec, HbSec * 2])),
    [
        #{<<"step">> => 1,
          <<"title">> => <<"Save Registration Details">>,
          <<"description">> => <<"From the response you just received, save: session_id (your session identifier) and agent_id (may differ from requested if the name was taken — server appends a suffix). Always use the returned agent_id for subsequent operations.">>
        },
        #{<<"step">> => 2,
          <<"title">> => <<"Send Heartbeat">>,
          <<"description">> => HbDesc,
          <<"command">> => #{<<"op">> => <<"ping">>}
        },
        #{<<"step">> => 3,
          <<"title">> => <<"Listen for Push Events">>,
          <<"description">> => <<"Your TCP connection is bidirectional. The server pushes events (messages, lock grants, broadcasts) directly to your socket as newline-delimited JSON. Read from the socket continuously in a background thread/loop.">>,
          <<"push_events">> => [
              <<"message — Direct message from another agent">>,
              <<"broadcast — Broadcast from another agent">>,
              <<"lock_granted — A queued lock request has been granted">>,
              <<"lock_expired — A held lock expired (TTL elapsed without renewal)">>,
              <<"agent_joined — A new agent connected">>,
              <<"agent_left — An agent disconnected">>,
              <<"deadlock_detected — Circular wait detected, victim is notified">>,
              <<"task_assigned — A task was assigned to you">>
          ]
        },
        #{<<"step">> => 4,
          <<"title">> => <<"Discovery & Status">>,
          <<"description">> => <<"Find other agents and set your status.">>,
          <<"commands">> => [
              #{<<"op">> => <<"list_agents">>, <<"description">> => <<"List all connected agents">>},
              #{<<"op">> => <<"find_agents">>, <<"filter">> => #{<<"role">> => <<"<role>">>}, <<"description">> => <<"Find agents by attribute">>},
              #{<<"op">> => <<"agent_status">>, <<"agent_id">> => <<"<id>">>, <<"description">> => <<"Get status of a specific agent">>},
              #{<<"op">> => <<"agent_status">>, <<"custom_status">> => <<"ready">>, <<"description">> => <<"Set your own status">>}
          ]
        },
        #{<<"step">> => 5,
          <<"title">> => <<"Send Messages">>,
          <<"description">> => <<"Send direct or broadcast messages to other agents.">>,
          <<"commands">> => [
              #{<<"op">> => <<"send">>, <<"to">> => <<"<agent-id>">>, <<"payload">> => #{<<"type">> => <<"request">>}, <<"description">> => <<"Direct message">>},
              #{<<"op">> => <<"broadcast">>, <<"payload">> => #{<<"type">> => <<"update">>}, <<"description">> => <<"Broadcast to all agents">>}
          ]
        },
        #{<<"step">> => 6,
          <<"title">> => <<"Lock Resources">>,
          <<"description">> => <<"Acquire exclusive (write) or shared (read) locks on resources. If the resource is busy, you get a wait_ref and will receive a lock_granted push event when available.">>,
          <<"commands">> => [
              #{<<"op">> => <<"acquire">>, <<"resource">> => <<"file:/path">>, <<"mode">> => <<"write">>, <<"ttl_ms">> => 30000, <<"description">> => <<"Acquire lock">>},
              #{<<"op">> => <<"release">>, <<"lock_ref">> => <<"LOCK-N">>, <<"description">> => <<"Release lock">>},
              #{<<"op">> => <<"renew">>, <<"lock_ref">> => <<"LOCK-N">>, <<"ttl_ms">> => 30000, <<"description">> => <<"Renew lock TTL">>}
          ]
        },
        #{<<"step">> => 7,
          <<"title">> => <<"Key Rules">>,
          <<"rules">> => [
              <<"Heartbeat: Send ping at the interval shown above or your session will be killed.">>,
              <<"Push events: Read from your socket continuously — messages, lock grants, and broadcasts arrive as push events.">>,
              <<"Always release locks when done, or they expire after the TTL.">>,
              <<"Handle lock_granted events — if a resource is busy, you get a WAIT-* reference and the lock arrives later as a push event.">>,
              <<"Use the agent_id from this response, not the one you requested.">>
          ]
        }
    ].
