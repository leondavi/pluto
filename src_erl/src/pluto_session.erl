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
%%   - TCP data from the client socket
%%   - Internal events pushed by other Pluto modules
%%   - Socket close / error
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
handle_request(#{<<"op">> := ?OP_TASK_LIST}, S) ->
    pluto_stats:inc(total_requests),
    handle_task_list(S);
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
handle_register(#{<<"agent_id">> := AgentId} = Msg, #sess{socket = Sock,
                                                           session_id = SessId} = S)
  when is_binary(AgentId), AgentId =/= <<>> ->
    Token = maps:get(<<"token">>, Msg, undefined),
    Attrs = maps:get(<<"attributes">>, Msg, #{}),
    case pluto_policy:check_auth(AgentId, Token) of
        ok ->
            case pluto_msg_hub:register_agent(AgentId, SessId, self(), Attrs) of
                {ok, SessId} ->
                    HbMs = pluto_config:get(heartbeat_interval_ms,
                                            ?DEFAULT_HEARTBEAT_INTERVAL_MS),
                    send_json(Sock, #{
                        <<"status">>               => ?STATUS_OK,
                        <<"session_id">>           => SessId,
                        <<"heartbeat_interval_ms">> => HbMs
                    }),
                    pluto_event_log:log(agent_registered, #{agent_id => AgentId,
                                                            session_id => SessId}),
                    S#sess{agent_id = AgentId};
                {error, already_registered} ->
                    send_json(Sock, #{
                        <<"status">> => ?STATUS_ERROR,
                        <<"reason">> => ?ERR_ALREADY_REGISTERED
                    }),
                    S
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
%% Supports optional `request_id` field: when present the server will push
%% a delivery_ack event back to the sender once the target receives the
%% message, giving reliable end-to-end delivery feedback.
%% Messages to disconnected-but-known agents are queued in their inbox.
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
    case Detailed of
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
handle_ack_events(#{<<"up_to_seq">> := Seq}, #sess{socket = Sock,
                                                     agent_id = AgentId} = S)
  when is_integer(Seq) ->
    pluto_event_log:log(events_acked, #{agent_id => AgentId, up_to_seq => Seq}),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_ack_events(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Task assignment ─────────────────────────────────────────────────
%% Creates a server-tracked task with an immutable task_id.  The server
%% stores the task, broadcasts a task_assigned event so all agents can
%% observe progress, and tries to deliver it to the assigned agent's inbox
%% if that agent is offline.
handle_task_assign(#{<<"task_id">> := TaskId, <<"to">> := Assignee} = Msg,
                   #sess{socket = Sock, agent_id = From} = S)
  when is_binary(TaskId), is_binary(Assignee) ->
    Payload = maps:get(<<"payload">>, Msg, #{}),
    Now = erlang:system_time(millisecond),
    Task = #{
        <<"task_id">>    => TaskId,
        <<"from">>       => From,
        <<"assignee">>   => Assignee,
        <<"payload">>    => Payload,
        <<"status">>     => <<"assigned">>,
        <<"created_at">> => Now,
        <<"updated_at">> => Now
    },
    ets:insert(?ETS_TASKS, {TaskId, Task}),
    %% Broadcast task_assigned to all connected agents
    Event = #{
        <<"event">>   => ?EVT_TASK_ASSIGNED,
        <<"task_id">> => TaskId,
        <<"from">>    => From,
        <<"to">>      => Assignee,
        <<"payload">> => Payload
    },
    pluto_msg_hub:broadcast(From, Event),
    pluto_event_log:log(task_assigned, #{task_id => TaskId, from => From,
                                          to => Assignee}),
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
handle_task_list(#sess{socket = Sock} = S) ->
    Tasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"tasks">> => Tasks}),
    S.

%% ── Agent discovery by attributes ───────────────────────────────────
%% Find agents whose metadata matches all key-value pairs in the filter.
%% Example: {"op": "find_agents", "filter": {"role": "code-fixer"}}
handle_find_agents(#{<<"filter">> := Filter}, #sess{socket = Sock} = S)
  when is_map(Filter) ->
    Agents = pluto_msg_hub:find_agents(Filter),
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"agents">> => Agents}),
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
            send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"agent">> => Info});
        {error, not_found} ->
            send_json(Sock, #{<<"status">> => ?STATUS_ERROR,
                              <<"reason">> => ?ERR_NOT_FOUND})
    end,
    S;
handle_agent_status(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Batch task distribution ─────────────────────────────────────────
%% Atomically assigns a batch of tasks across multiple agents and stores
%% them server-side.  If an assigned agent is disconnected, the task is
%% immediately marked as "orphaned" and a tasks_orphaned event is broadcast.
handle_task_batch(#{<<"assignments">> := Assignments},
                  #sess{socket = Sock, agent_id = From} = S)
  when is_list(Assignments) ->
    Now = erlang:system_time(millisecond),
    TaskIds = lists:map(fun(Assignment) ->
        Assignee = maps:get(<<"agent">>, Assignment, undefined),
        Tasks    = maps:get(<<"tasks">>, Assignment, []),
        lists:map(fun(TaskDef) ->
            TaskId = maps:get(<<"task_id">>, TaskDef, generate_task_id()),
            Task = #{
                <<"task_id">>    => TaskId,
                <<"from">>       => From,
                <<"assignee">>   => Assignee,
                <<"payload">>    => TaskDef,
                <<"status">>     => <<"assigned">>,
                <<"created_at">> => Now,
                <<"updated_at">> => Now
            },
            ets:insert(?ETS_TASKS, {TaskId, Task}),
            pluto_event_log:log(task_assigned, #{task_id => TaskId,
                                                  from => From,
                                                  to => Assignee}),
            TaskId
        end, Tasks)
    end, Assignments),
    FlatIds = lists:flatten(TaskIds),
    %% Broadcast batch assignment
    Event = #{
        <<"event">>    => ?EVT_TASK_ASSIGNED,
        <<"from">>     => From,
        <<"task_ids">> => FlatIds,
        <<"batch">>    => true
    },
    pluto_msg_hub:broadcast(From, Event),
    send_json(Sock, #{<<"status">> => ?STATUS_OK, <<"task_ids">> => FlatIds}),
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
    StatusCounts = lists:foldl(fun(T, Acc) ->
        St = maps:get(<<"status">>, T, <<"unknown">>),
        Acc#{St => maps:get(St, Acc, 0) + 1}
    end, #{}, AllTasks),
    send_json(Sock, #{
        <<"status">> => ?STATUS_OK,
        <<"total">>  => length(AllTasks),
        <<"by_status">> => StatusCounts,
        <<"tasks">> => AllTasks
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
