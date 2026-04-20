%%%-------------------------------------------------------------------
%%% @doc pluto_msg_hub — Agent registry, messaging, and broadcast.
%%%
%%% Manages the two-way mapping between agent_id and session_id,
%%% routes direct messages between agents, and delivers broadcasts
%%% to all connected sessions.
%%%
%%% The message hub owns the logical registry.  Session processes
%%% call `register_agent/3` when a client sends the `register` op
%%% and `unregister_agent/1` when the session terminates.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_msg_hub).
-behaviour(gen_server).

-include("pluto.hrl").

%% Public API
-export([
    start_link/0,
    register_agent/3,
    register_agent/4,
    register_http_agent/5,
    unregister_agent/1,
    send_msg/3,
    send_msg/4,
    broadcast/2,
    list_agents/0,
    list_agents_detailed/0,
    lookup_agent/1,
    find_agents/1,
    subscribe/2,
    unsubscribe/2,
    publish/3,
    agent_status/1,
    set_agent_status/2,
    deliver_inbox/1,
    touch_http_agent/1,
    poll_inbox/1,
    unregister_http_agent/1,
    register_long_poll/1,
    unregister_long_poll/1,
    update_http_ttl/2,
    sweep_inbox/0,
    peek_inbox/1,
    peek_inbox/2
]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {
    msg_seq = 0 :: non_neg_integer()  %% monotonic message sequence counter
}).

%%====================================================================
%% API
%%====================================================================

%% @doc Start and register the message hub.
-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Register an agent with its session.
%% Returns `{ok, SessionId}` when the requested agent_id was used,
%% or `{ok, SessionId, ActualAgentId}` when a unique suffix was appended
%% because the requested name was already active.
-spec register_agent(binary(), binary(), pid()) ->
    {ok, binary()} | {ok, binary(), binary()}.
register_agent(AgentId, SessionId, SessionPid) ->
    register_agent(AgentId, SessionId, SessionPid, #{}).

%% @doc Register an agent with its session and attributes.
-spec register_agent(binary(), binary(), pid(), map()) ->
    {ok, binary()} | {ok, binary(), binary()}.
register_agent(AgentId, SessionId, SessionPid, Attrs) ->
    gen_server:call(?MODULE, {register, AgentId, SessionId, SessionPid, Attrs}).

%% @doc Unregister an agent when its session ends.
%% Marks the agent as disconnected and starts the grace period timer.
-spec unregister_agent(binary()) -> ok.
unregister_agent(AgentId) ->
    gen_server:cast(?MODULE, {unregister, AgentId}).

%% @doc Send a direct message from one agent to another.
%% The payload is forwarded as-is to the target session process.
-spec send_msg(binary(), binary(), map()) -> ok | {error, unknown_target}.
send_msg(From, To, Payload) ->
    send_msg(From, To, Payload, undefined).

%% @doc Send a direct message with optional request_id for ack tracking.
-spec send_msg(binary(), binary(), map(), binary() | undefined) ->
    {ok, binary()} | ok | {error, unknown_target | queued}.
send_msg(From, To, Payload, RequestId) ->
    gen_server:call(?MODULE, {send, From, To, Payload, RequestId}).

%% @doc Broadcast a message from one agent to all other connected agents.
-spec broadcast(binary(), map()) -> ok.
broadcast(From, Payload) ->
    gen_server:cast(?MODULE, {broadcast, From, Payload}).

%% @doc Return the list of currently connected agent IDs.
-spec list_agents() -> [binary()].
list_agents() ->
    gen_server:call(?MODULE, list_agents).

%% @doc Return detailed info for all agents (including status, last_seen, attributes).
-spec list_agents_detailed() -> [map()].
list_agents_detailed() ->
    gen_server:call(?MODULE, list_agents_detailed).

%% @doc Look up an agent by agent_id.  Returns the agent record or not_found.
-spec lookup_agent(binary()) -> {ok, #agent{}} | {error, not_found}.
lookup_agent(AgentId) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent] -> {ok, Agent};
        []      -> {error, not_found}
    end.

%% @doc Find agents matching a set of attribute filters.
-spec find_agents(map()) -> [map()].
find_agents(Filter) ->
    gen_server:call(?MODULE, {find_agents, Filter}).

%% @doc Subscribe an agent to a named topic.
-spec subscribe(binary(), binary()) -> ok.
subscribe(AgentId, Topic) ->
    gen_server:call(?MODULE, {subscribe, AgentId, Topic}).

%% @doc Unsubscribe an agent from a named topic.
-spec unsubscribe(binary(), binary()) -> ok.
unsubscribe(AgentId, Topic) ->
    gen_server:call(?MODULE, {unsubscribe, AgentId, Topic}).

%% @doc Publish a message to a topic (delivered to all subscribers).
-spec publish(binary(), binary(), map()) -> ok.
publish(From, Topic, Payload) ->
    gen_server:cast(?MODULE, {publish, From, Topic, Payload}).

%% @doc Query the status of a specific agent.
-spec agent_status(binary()) -> {ok, map()} | {error, not_found}.
agent_status(AgentId) ->
    gen_server:call(?MODULE, {agent_status, AgentId}).

%% @doc Set a custom status for an agent (e.g. busy, idle).
-spec set_agent_status(binary(), binary()) -> ok.
set_agent_status(AgentId, CustomStatus) ->
    gen_server:call(?MODULE, {set_agent_status, AgentId, CustomStatus}).

%% @doc Deliver queued inbox messages to a reconnected agent.
-spec deliver_inbox(binary()) -> ok.
deliver_inbox(AgentId) ->
    gen_server:cast(?MODULE, {deliver_inbox, AgentId}).

%% @doc Register an agent via HTTP (no persistent TCP session).
%% Returns `{ok, Token, SessionId}`, `{ok, Token, SessionId, resumed}` when
%% the agent reconnects within grace period and provides the matching session_id,
%% or `{ok, Token, SessionId, ActualAgentId}` when the requested name was taken.
-spec register_http_agent(binary(), map(), http | stateless, non_neg_integer(), map()) ->
    {ok, binary(), binary()} | {ok, binary(), binary(), resumed} |
    {ok, binary(), binary(), binary()}.
register_http_agent(AgentId, Attrs, Mode, TtlMs, Opts) ->
    gen_server:call(?MODULE, {register_http, AgentId, Attrs, Mode, TtlMs, Opts}).

%% @doc Touch an HTTP agent's liveness timestamp (heartbeat via HTTP).
-spec touch_http_agent(binary()) -> ok | {error, not_found}.
touch_http_agent(Token) ->
    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
        [HS] ->
            Now = erlang:system_time(millisecond),
            ets:insert(?ETS_HTTP_SESSIONS, HS#http_session{last_seen = Now}),
            %% Also update the agent's last_seen
            case ets:lookup(?ETS_AGENTS, HS#http_session.agent_id) of
                [Agent] ->
                    ets:insert(?ETS_AGENTS, Agent#agent{last_seen = Now});
                [] -> ok
            end,
            ok;
        [] ->
            {error, not_found}
    end.

%% @doc Poll and return queued inbox messages for an HTTP agent.
-spec poll_inbox(binary()) -> {ok, [map()]}.
poll_inbox(AgentId) ->
    gen_server:call(?MODULE, {poll_inbox, AgentId}).

%% @doc Unregister an HTTP agent by token.
-spec unregister_http_agent(binary()) -> ok | {error, not_found}.
unregister_http_agent(Token) ->
    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
        [#http_session{agent_id = AgentId}] ->
            ets:delete(?ETS_HTTP_SESSIONS, Token),
            unregister_agent(AgentId),
            ok;
        [] ->
            {error, not_found}
    end.

%% @doc Register the calling process as a long-poll waiter for an agent.
-spec register_long_poll(binary()) -> ok.
register_long_poll(AgentId) ->
    ets:insert(?ETS_LONG_POLL, {AgentId, self()}),
    ok.

%% @doc Unregister the long-poll waiter for an agent.
-spec unregister_long_poll(binary()) -> ok.
unregister_long_poll(AgentId) ->
    ets:delete(?ETS_LONG_POLL, AgentId),
    ok.

%% @doc Update the TTL for an HTTP session by token.
-spec update_http_ttl(binary(), non_neg_integer()) -> ok | {error, not_found}.
update_http_ttl(Token, NewTtlMs) ->
    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
        [HS] ->
            Now = erlang:system_time(millisecond),
            ets:insert(?ETS_HTTP_SESSIONS, HS#http_session{ttl_ms = NewTtlMs, last_seen = Now}),
            ok;
        [] ->
            {error, not_found}
    end.

%% @doc Sweep expired inbox messages across all agents.
-spec sweep_inbox() -> ok.
sweep_inbox() ->
    gen_server:cast(?MODULE, sweep_inbox).

%% @doc Peek at inbox messages for an agent without consuming them.
-spec peek_inbox(binary()) -> {ok, [map()]}.
peek_inbox(AgentId) ->
    peek_inbox(AgentId, 0).

-spec peek_inbox(binary(), non_neg_integer()) -> {ok, [map()]}.
peek_inbox(AgentId, SinceToken) ->
    gen_server:call(?MODULE, {peek_inbox, AgentId, SinceToken}).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    ?LOG_INFO("pluto_msg_hub started"),
    {ok, #state{}}.

%% ── register ────────────────────────────────────────────────────────
handle_call({register, AgentId, SessionId, SessionPid, Attrs}, _From, State) ->
    %% Extract optional resume_session_id from Attrs (added by session handler)
    ResumeSessionId = maps:get(resume_session_id, Attrs, undefined),
    CleanAttrs = maps:without([resume_session_id], Attrs),
    case pluto_name_registry:reserve_name(AgentId, SessionPid, tcp) of
        {ok, FinalId} ->
            {IsReconnect, ExistingSessId} = case ets:lookup(?ETS_AGENTS, FinalId) of
                [#agent{status = disconnected, session_id = ESessId}] -> {true, ESessId};
                _ -> {false, undefined}
            end,
            %% Determine if this is a session resumption
            IsResumed = IsReconnect andalso
                        ResumeSessionId =/= undefined andalso
                        ResumeSessionId =:= ExistingSessId,
            ActualSessId = case IsResumed of
                true  -> ResumeSessionId;
                false -> SessionId
            end,
            do_register(FinalId, ActualSessId, SessionPid, CleanAttrs, tcp),
            case IsReconnect of
                true  -> self() ! {deliver_inbox_sync, FinalId};
                false -> ok
            end,
            Reply = case {FinalId =:= AgentId, IsResumed} of
                {_, true}  -> {ok, ActualSessId, resumed};
                {true, _}  -> {ok, ActualSessId};
                {false, _} -> {ok, ActualSessId, FinalId}
            end,
            {reply, Reply, State};
        {ok, FinalId, evicted} ->
            evict_http_sessions(FinalId),
            do_register(FinalId, SessionId, SessionPid, Attrs, tcp),
            {reply, {ok, SessionId}, State}
    end;

%% ── register_http — HTTP/stateless session registration ─────────────
handle_call({register_http, AgentId, Attrs, Mode, TtlMs, Opts}, _From, State) ->
    ResumeSessionId = maps:get(resume_session_id, Opts, undefined),
    %% Pre-generate the token so we can use it as the owner reference
    PreToken = generate_http_token(),
    case pluto_name_registry:reserve_name(AgentId, PreToken, Mode) of
        {ok, FinalId} ->
            {IsReconnect, ExistingSessId} = case ets:lookup(?ETS_AGENTS, FinalId) of
                [#agent{status = disconnected, session_id = ESessId}] -> {true, ESessId};
                _ -> {false, undefined}
            end,
            %% Determine if this is a session resumption (same session_id provided)
            IsResumed = IsReconnect andalso
                        ResumeSessionId =/= undefined andalso
                        ResumeSessionId =:= ExistingSessId,
            {Token, SessId} = case IsResumed of
                true  -> do_register_http_with_session(FinalId, PreToken, Attrs, Mode, TtlMs, ResumeSessionId);
                false -> do_register_http_with_token(FinalId, PreToken, Attrs, Mode, TtlMs)
            end,
            case IsReconnect of
                true  -> self() ! {deliver_inbox_sync, FinalId};
                false -> ok
            end,
            Reply = case {FinalId =:= AgentId, IsResumed} of
                {_, true}  -> {ok, Token, SessId, resumed};
                {true, _}  -> {ok, Token, SessId};
                {false, _} -> {ok, Token, SessId, FinalId}
            end,
            {reply, Reply, State};
        {ok, FinalId, evicted} ->
            evict_http_sessions(FinalId),
            {Token, SessId} = do_register_http_with_token(FinalId, PreToken, Attrs, Mode, TtlMs),
            {reply, {ok, Token, SessId}, State}
    end;

%% ── poll_inbox — retrieve and clear queued messages ─────────────────
handle_call({poll_inbox, AgentId}, _From, State) ->
    Messages = do_poll_inbox(AgentId),
    {reply, {ok, Messages}, State};

%% ── send direct message (with optional request_id for ack) ──────────
handle_call({send, From, To, Payload, RequestId}, _From,
            #state{msg_seq = Seq} = State) ->
    NewSeq = Seq + 1,
    MsgId = iolist_to_binary(io_lib:format("MSG-~w", [NewSeq])),
    Event = #{
        <<"event">>   => ?EVT_MESSAGE,
        <<"from">>    => From,
        <<"payload">> => Payload,
        <<"msg_id">>  => MsgId,
        <<"seq">>     => NewSeq
    },
    Event2 = case RequestId of
        undefined -> Event;
        _         -> Event#{<<"request_id">> => RequestId}
    end,
    %% Log message to event log for auditability
    pluto_event_log:log(message_sent, #{from => From, to => To, msg_id => MsgId}),
    case ets:lookup(?ETS_AGENTS, To) of
        [#agent{status = connected, session_pid = Pid, session_type = SType}]
          when is_pid(Pid), SType =:= tcp ->
            Pid ! {pluto_event, Event2},
            pluto_stats:inc(messages_sent),
            pluto_stats:inc(messages_received),
            pluto_stats:inc_agent(From, messages_sent),
            pluto_stats:inc_agent(To, messages_received),
            %% Send delivery ack back to sender if request_id provided
            case RequestId of
                undefined -> ok;
                _ ->
                    case ets:lookup(?ETS_AGENTS, From) of
                        [#agent{status = connected, session_pid = SenderPid}]
                          when is_pid(SenderPid) ->
                            AckEvt = #{
                                <<"event">>      => ?EVT_DELIVERY_ACK,
                                <<"msg_id">>     => MsgId,
                                <<"request_id">> => RequestId,
                                <<"to">>         => To,
                                <<"delivered">>  => true
                            },
                            SenderPid ! {pluto_event, AckEvt};
                        _ -> ok
                    end
            end,
            {reply, {ok, MsgId}, State#state{msg_seq = NewSeq}};
        [#agent{status = connected, session_type = SType}]
          when SType =:= http; SType =:= stateless ->
            %% HTTP/stateless agent — queue message in inbox for polling
            queue_inbox_message(To, Event2),
            pluto_stats:inc(messages_sent),
            pluto_stats:inc_agent(From, messages_sent),
            {reply, {ok, MsgId}, State#state{msg_seq = NewSeq}};
        [#agent{status = disconnected}] ->
            %% Agent is disconnected — queue message in inbox
            queue_inbox_message(To, Event2),
            pluto_stats:inc(messages_sent),
            pluto_stats:inc_agent(From, messages_sent),
            {reply, {ok, MsgId}, State#state{msg_seq = NewSeq}};
        _ ->
            {reply, {error, unknown_target}, State#state{msg_seq = NewSeq}}
    end;

%% ── list agents ─────────────────────────────────────────────────────
handle_call(list_agents, _From, State) ->
    Agents = ets:match(?ETS_AGENTS, #agent{agent_id = '$1',
                                           status = connected,
                                           _ = '_'}),
    Ids = [Id || [Id] <- Agents],
    {reply, Ids, State};

%% ── List agents with full detail (attributes, last-seen, status) ────
handle_call(list_agents_detailed, _From, State) ->
    AllAgents = ets:tab2list(?ETS_AGENTS),
    AgentMaps = [#{
        <<"agent_id">>      => A#agent.agent_id,
        <<"status">>        => atom_to_binary(A#agent.status, utf8),
        <<"last_seen">>     => A#agent.last_seen,
        <<"custom_status">> => A#agent.custom_status,
        <<"attributes">>    => A#agent.attributes,
        <<"subscriptions">> => A#agent.subscriptions
    } || A <- AllAgents],
    {reply, AgentMaps, State};

%% ── Find agents by attribute filter ──────────────────────────────────
%% Matches agents whose attributes contain all key-value pairs in Filter.
%% Optionally filter by connection status or custom_status as well.
handle_call({find_agents, Filter}, _From, State) ->
    AllAgents = ets:tab2list(?ETS_AGENTS),
    Matching = lists:filter(fun(#agent{attributes = Attrs, status = Status,
                                       custom_status = CStatus}) ->
        %% Check status filter if present
        StatusOk = case maps:find(<<"status">>, Filter) of
            {ok, <<"connected">>}    -> Status =:= connected;
            {ok, <<"disconnected">>} -> Status =:= disconnected;
            _ -> true
        end,
        %% Check custom_status filter if present
        CStatusOk = case maps:find(<<"custom_status">>, Filter) of
            {ok, CS} -> CStatus =:= CS;
            _ -> true
        end,
        %% Check all other attribute filters
        AttrFilter = maps:without([<<"status">>, <<"custom_status">>], Filter),
        AttrsOk = maps:fold(fun(K, V, Acc) ->
            Acc andalso maps:get(K, Attrs, undefined) =:= V
        end, true, AttrFilter),
        StatusOk andalso CStatusOk andalso AttrsOk
    end, AllAgents),
    Result = [#{
        <<"agent_id">>      => A#agent.agent_id,
        <<"status">>        => atom_to_binary(A#agent.status, utf8),
        <<"attributes">>    => A#agent.attributes,
        <<"custom_status">> => A#agent.custom_status
    } || A <- Matching],
    {reply, Result, State};

%% ── Subscribe to a named topic channel ──────────────────────────────
handle_call({subscribe, AgentId, Topic}, _From, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent = #agent{subscriptions = Subs}] ->
            case lists:member(Topic, Subs) of
                true  -> ok;
                false ->
                    ets:insert(?ETS_AGENTS,
                               Agent#agent{subscriptions = [Topic | Subs]})
            end,
            {reply, ok, State};
        [] ->
            {reply, {error, not_found}, State}
    end;

%% ── Unsubscribe from a topic channel ───────────────────────────────
handle_call({unsubscribe, AgentId, Topic}, _From, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent = #agent{subscriptions = Subs}] ->
            ets:insert(?ETS_AGENTS,
                       Agent#agent{subscriptions = lists:delete(Topic, Subs)}),
            {reply, ok, State};
        [] ->
            {reply, {error, not_found}, State}
    end;

%% ── Agent presence & status query ───────────────────────────────────
%% Returns detailed status of a specific agent including connection state,
%% last-seen time, custom status, attributes, and topic subscriptions.
handle_call({agent_status, AgentId}, _From, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = Status, last_seen = LastSeen,
                custom_status = CStatus, attributes = Attrs,
                subscriptions = Subs, session_type = SType}] ->
            Result0 = #{
                <<"agent_id">>      => AgentId,
                <<"status">>        => atom_to_binary(Status, utf8),
                <<"last_seen">>     => LastSeen,
                <<"custom_status">> => CStatus,
                <<"attributes">>    => Attrs,
                <<"subscriptions">> => Subs,
                <<"session_type">>  => atom_to_binary(SType, utf8)
            },
            %% Add TTL info for HTTP/stateless agents
            Result = case ets:match_object(?ETS_HTTP_SESSIONS,
                         #http_session{agent_id = AgentId, _ = '_'}) of
                [#http_session{ttl_ms = TtlMs, last_seen = HLastSeen}] ->
                    Now = erlang:system_time(millisecond),
                    ExpiresIn = max(0, TtlMs - (Now - HLastSeen)),
                    Result0#{<<"ttl_ms">> => TtlMs,
                             <<"expires_in_ms">> => ExpiresIn};
                _ ->
                    Result0
            end,
            {reply, {ok, Result}, State};
        [] ->
            {reply, {error, not_found}, State}
    end;

%% ── Set custom agent status (busy, idle, etc.) ──────────────────────
handle_call({set_agent_status, AgentId, CustomStatus}, _From, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent] ->
            ets:insert(?ETS_AGENTS, Agent#agent{custom_status = CustomStatus}),
            {reply, ok, State};
        [] ->
            {reply, {error, not_found}, State}
    end;

handle_call({peek_inbox, AgentId, SinceToken}, _From, State) ->
    InboxTtlMs = pluto_config:get(inbox_msg_ttl_ms, ?DEFAULT_INBOX_MSG_TTL_MS),
    NowMs = erlang:system_time(millisecond),
    Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
    FilteredSeqs = lists:filter(fun(S) -> S > SinceToken end,
                                lists:sort([S || [S] <- Keys])),
    Messages = lists:filtermap(fun(Seq) ->
        Key = {AgentId, Seq},
        case ets:lookup(?ETS_MSG_INBOX, Key) of
            [{_, {Event, InsertedAt}}] when NowMs - InsertedAt =< InboxTtlMs ->
                {true, Event#{<<"seq_token">> => Seq}};
            [{_, {_Event, _InsertedAt}}] ->
                ets:delete(?ETS_MSG_INBOX, Key),
                false;
            [] -> false
        end
    end, FilteredSeqs),
    {reply, {ok, Messages}, State};

handle_call(_Request, _From, State) ->
    {reply, {error, unknown_request}, State}.

%% ── broadcast ───────────────────────────────────────────────────────
handle_cast({broadcast, From, Payload}, State) ->
    Event = #{
        <<"event">>   => ?EVT_BROADCAST,
        <<"from">>    => From,
        <<"payload">> => Payload
    },
    pluto_stats:inc(broadcasts_sent),
    pluto_stats:inc_agent(From, broadcasts_sent),
    %% Send to all connected agents except the sender
    AllAgents = ets:match_object(?ETS_AGENTS, #agent{status = connected, _ = '_'}),
    lists:foreach(fun(#agent{agent_id = AId, session_pid = Pid}) ->
        case AId =/= From andalso is_pid(Pid) of
            true  -> Pid ! {pluto_event, Event};
            false -> ok
        end
    end, AllAgents),
    {noreply, State};

%% ── unregister ──────────────────────────────────────────────────────
handle_cast({unregister, AgentId}, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent = #agent{session_id = SessId}] ->
            %% Mark as disconnected rather than deleting
            Now = erlang:system_time(millisecond),
            ets:insert(?ETS_AGENTS, Agent#agent{
                status      = disconnected,
                session_pid = undefined,
                last_seen   = Now
            }),
            pluto_stats:inc(agents_disconnected),
            pluto_stats:inc_agent(AgentId, disconnections),
            %% Release from centralized name registry
            pluto_name_registry:release_name(AgentId),
            %% Remove the session record
            ets:delete(?ETS_SESSIONS, SessId),
            %% Notify other agents
            broadcast_event(#{
                <<"event">>    => ?EVT_AGENT_LEFT,
                <<"agent_id">> => AgentId
            }, AgentId),
            %% Orphan any tasks assigned to this agent
            orphan_agent_tasks(AgentId),
            %% Start the grace-period timer
            GraceMs = pluto_config:get(reconnect_grace_ms, ?DEFAULT_RECONNECT_GRACE_MS),
            TRef = erlang:send_after(GraceMs, self(), {grace_expired, AgentId}),
            ets:insert(?ETS_GRACE_TIMERS, {AgentId, TRef}),
            ok;
        [] ->
            ok
    end,
    {noreply, State};

%% ── Publish a message to all subscribers of a topic ────────────────
handle_cast({publish, From, Topic, Payload}, State) ->
    Event = #{
        <<"event">>   => ?EVT_TOPIC_MESSAGE,
        <<"from">>    => From,
        <<"topic">>   => Topic,
        <<"payload">> => Payload
    },
    pluto_event_log:log(topic_publish, #{from => From, topic => Topic}),
    %% Deliver to all connected agents subscribed to this topic (except sender)
    AllAgents = ets:match_object(?ETS_AGENTS, #agent{status = connected, _ = '_'}),
    lists:foreach(fun(#agent{agent_id = AId, session_pid = Pid,
                             subscriptions = Subs}) ->
        case AId =/= From andalso is_pid(Pid)
             andalso lists:member(Topic, Subs) of
            true  -> Pid ! {pluto_event, Event};
            false -> ok
        end
    end, AllAgents),
    {noreply, State};

%% ── deliver_inbox (async) ───────────────────────────────────────────
handle_cast({deliver_inbox, AgentId}, State) ->
    do_deliver_inbox(AgentId),
    {noreply, State};

handle_cast(sweep_inbox, State) ->
    do_sweep_inbox(),
    {noreply, State};

handle_cast(_Msg, State) ->
    {noreply, State}.

%% ── Deliver inbox on reconnect (sync from register handler) ─────────
handle_info({deliver_inbox_sync, AgentId}, State) ->
    do_deliver_inbox(AgentId),
    {noreply, State};

%% ── Grace period expiry ─────────────────────────────────────────────
handle_info({grace_expired, AgentId}, State) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = disconnected}] ->
            ?LOG_INFO("Grace period expired for agent ~s -- releasing locks",
                      [AgentId]),
            %% Release all locks held by this agent
            Locks = pluto_lock_mgr:locks_for_agent(AgentId),
            lists:foreach(fun(#lock{lock_ref = Ref}) ->
                pluto_lock_mgr:release(Ref, AgentId)
            end, Locks),
            %% Remove the agent from the registry entirely
            ets:delete(?ETS_AGENTS, AgentId),
            %% Release from centralized name registry (belt and suspenders)
            pluto_name_registry:release_name(AgentId),
            %% Clean up inbox
            clear_inbox(AgentId);
        _ ->
            %% Agent has reconnected in the meantime — nothing to do
            ok
    end,
    {noreply, State};

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Perform the actual registration: insert agent and session records,
%% update liveness, and broadcast the join event.
do_register(AgentId, SessionId, SessionPid, Attrs, SessionType) ->
    Now = pluto_lease:now_ms(),
    SysNow = erlang:system_time(millisecond),

    %% Cancel any pending grace timer for this agent
    case ets:lookup(?ETS_GRACE_TIMERS, AgentId) of
        [{_, TRef}] ->
            erlang:cancel_timer(TRef),
            ets:delete(?ETS_GRACE_TIMERS, AgentId);
        [] -> ok
    end,

    %% Preserve existing attributes/subscriptions on reconnect
    {MergedAttrs, ExistingSubs} = case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{attributes = OldAttrs, subscriptions = OldSubs}] ->
            {maps:merge(OldAttrs, Attrs), OldSubs};
        [] ->
            {Attrs, []}
    end,

    %% Upsert agent record
    Agent = #agent{
        agent_id      = AgentId,
        session_id    = SessionId,
        session_pid   = SessionPid,
        status        = connected,
        connected_at  = Now,
        attributes    = MergedAttrs,
        last_seen     = SysNow,
        custom_status = <<"online">>,
        subscriptions = ExistingSubs,
        session_type  = SessionType
    },
    ets:insert(?ETS_AGENTS, Agent),

    %% Insert session record
    Session = #session{
        session_id  = SessionId,
        agent_id    = AgentId,
        session_pid = SessionPid
    },
    ets:insert(?ETS_SESSIONS, Session),

    %% Initialise liveness timestamp
    ets:insert(?ETS_LIVENESS, {SessionId, Now}),

    %% Monitor the session process so we clean up if it crashes
    erlang:monitor(process, SessionPid),

    %% Track stats
    pluto_stats:inc(agents_registered),
    pluto_stats:inc_agent(AgentId, registrations),

    %% Broadcast agent_joined to all other connected agents
    broadcast_event(#{
        <<"event">>    => ?EVT_AGENT_JOINED,
        <<"agent_id">> => AgentId
    }, AgentId),

    ok.

%% @private Send an event to all connected agents except `ExcludeAgentId`.
broadcast_event(Event, ExcludeAgentId) ->
    AllAgents = ets:match_object(?ETS_AGENTS, #agent{status = connected, _ = '_'}),
    lists:foreach(fun(#agent{agent_id = AId, session_pid = Pid}) ->
        case AId =/= ExcludeAgentId andalso is_pid(Pid) of
            true  -> Pid ! {pluto_event, Event};
            false -> ok
        end
    end, AllAgents).

%% @private Queue a message in an offline agent's inbox (bounded).
queue_inbox_message(AgentId, Event) ->
    MaxInbox = pluto_config:get(max_inbox_size, ?DEFAULT_MAX_INBOX_SIZE),
    Seq = erlang:unique_integer([monotonic, positive]),
    Key = {AgentId, Seq},
    %% Count current inbox size
    Pattern = {{AgentId, '_'}, '_'},
    CurrentSize = length(ets:match(?ETS_MSG_INBOX, Pattern)),
    InsertedAt = erlang:system_time(millisecond),
    case CurrentSize < MaxInbox of
        true ->
            ets:insert(?ETS_MSG_INBOX, {Key, {Event, InsertedAt}});
        false ->
            %% Inbox full — drop oldest
            case ets:match(?ETS_MSG_INBOX, Pattern) of
                [[]] -> ok;
                Matches when length(Matches) > 0 ->
                    %% ordered_set so first match is oldest
                    OldestKey = {AgentId, lists:min([S || [S] <- ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'})])},
                    ets:delete(?ETS_MSG_INBOX, OldestKey),
                    ets:insert(?ETS_MSG_INBOX, {Key, {Event, InsertedAt}});
                _ -> ok
            end
    end,
    %% Notify any long-poll waiter
    notify_long_poll(AgentId),
    %% Write file-based signal
    write_signal_file(AgentId),
    ok.

%% @private Deliver all queued inbox messages to a reconnected agent.
do_deliver_inbox(AgentId) ->
    InboxTtlMs = pluto_config:get(inbox_msg_ttl_ms, ?DEFAULT_INBOX_MSG_TTL_MS),
    NowMs = erlang:system_time(millisecond),
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = connected, session_pid = Pid}] when is_pid(Pid) ->
            Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
            SortedSeqs = lists:sort([S || [S] <- Keys]),
            lists:foreach(fun(Seq) ->
                Key = {AgentId, Seq},
                case ets:lookup(?ETS_MSG_INBOX, Key) of
                    [{_, {Event, InsertedAt}}] ->
                        ets:delete(?ETS_MSG_INBOX, Key),
                        case NowMs - InsertedAt > InboxTtlMs of
                            true  -> ok; %% expired, skip
                            false -> Pid ! {pluto_event, Event}
                        end;
                    [] -> ok
                end
            end, SortedSeqs);
        _ -> ok
    end.

%% @private Clear all inbox messages for an agent.
clear_inbox(AgentId) ->
    Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
    lists:foreach(fun([Seq]) ->
        ets:delete(?ETS_MSG_INBOX, {AgentId, Seq})
    end, Keys).

%% @private Emit tasks_orphaned event when an agent disconnects with active tasks.
orphan_agent_tasks(AgentId) ->
    AllTasks = ets:tab2list(?ETS_TASKS),
    Orphaned = [T || {_TId, T} <- AllTasks,
                     maps:get(<<"assignee">>, T, undefined) =:= AgentId,
                     maps:get(<<"status">>, T, undefined) =/= <<"complete">>,
                     maps:get(<<"status">>, T, undefined) =/= <<"failed">>],
    case Orphaned of
        [] -> ok;
        _ ->
            OrphanIds = [maps:get(<<"task_id">>, T) || T <- Orphaned],
            %% Mark tasks as orphaned
            lists:foreach(fun(T) ->
                TId = maps:get(<<"task_id">>, T),
                ets:insert(?ETS_TASKS, {TId, T#{<<"status">> => <<"orphaned">>}})
            end, Orphaned),
            %% Broadcast orphaned event
            Event = #{
                <<"event">>    => ?EVT_TASKS_ORPHANED,
                <<"agent_id">> => AgentId,
                <<"task_ids">> => OrphanIds
            },
            broadcast_event(Event, AgentId),
            pluto_event_log:log(tasks_orphaned, #{agent_id => AgentId,
                                                   task_ids => OrphanIds})
    end.

%% @private Register an agent via HTTP. Creates agent record and HTTP session.
%% Returns {Token, SessionId}.
%% @private Register an HTTP agent using a pre-generated token.
do_register_http_with_token(AgentId, Token, Attrs, Mode, TtlMs) ->
    SessionId = generate_http_session_id(),
    Now = pluto_lease:now_ms(),
    SysNow = erlang:system_time(millisecond),

    %% Preserve existing attributes/subscriptions on reconnect
    {MergedAttrs, ExistingSubs} = case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{attributes = OldAttrs, subscriptions = OldSubs}] ->
            {maps:merge(OldAttrs, Attrs), OldSubs};
        [] ->
            {Attrs, []}
    end,

    %% Evict any previous HTTP sessions for this agent
    evict_http_sessions(AgentId),

    %% Create agent record (no session_pid for HTTP agents)
    Agent = #agent{
        agent_id      = AgentId,
        session_id    = SessionId,
        session_pid   = undefined,
        status        = connected,
        connected_at  = Now,
        attributes    = MergedAttrs,
        last_seen     = SysNow,
        custom_status = <<"online">>,
        subscriptions = ExistingSubs,
        session_type  = Mode
    },
    ets:insert(?ETS_AGENTS, Agent),

    %% Create HTTP session record
    HttpSession = #http_session{
        token      = Token,
        agent_id   = AgentId,
        session_id = SessionId,
        ttl_ms     = TtlMs,
        last_seen  = SysNow,
        mode       = Mode
    },
    ets:insert(?ETS_HTTP_SESSIONS, HttpSession),

    %% Insert session record
    Session = #session{
        session_id  = SessionId,
        agent_id    = AgentId,
        session_pid = undefined
    },
    ets:insert(?ETS_SESSIONS, Session),

    %% Track stats
    pluto_stats:inc(agents_registered),
    pluto_stats:inc_agent(AgentId, registrations),

    %% Broadcast agent_joined
    broadcast_event(#{
        <<"event">>    => ?EVT_AGENT_JOINED,
        <<"agent_id">> => AgentId
    }, AgentId),

    pluto_event_log:log(agent_registered, #{agent_id => AgentId,
                                            session_id => SessionId,
                                            mode => Mode}),

    {Token, SessionId}.

%% @private Generate a cryptographically random HTTP session token.
generate_http_token() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(24)),
    <<"PLUTO-", Hex/binary>>.

%% @private Generate an HTTP session ID.
generate_http_session_id() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(8)),
    <<"HTTP-SESS-", Hex/binary>>.

%% @private Re-register an HTTP agent preserving the existing session_id (session resumption).
%% Used when the client provides the old session_id and the agent was disconnected.
do_register_http_with_session(AgentId, Token, Attrs, Mode, TtlMs, OldSessionId) ->
    Now = pluto_lease:now_ms(),
    SysNow = erlang:system_time(millisecond),
    {MergedAttrs, ExistingSubs} = case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{attributes = OldAttrs, subscriptions = OldSubs}] ->
            {maps:merge(OldAttrs, Attrs), OldSubs};
        [] ->
            {Attrs, []}
    end,
    evict_http_sessions(AgentId),
    Agent = #agent{
        agent_id      = AgentId,
        session_id    = OldSessionId,
        session_pid   = undefined,
        status        = connected,
        connected_at  = Now,
        attributes    = MergedAttrs,
        last_seen     = SysNow,
        custom_status = <<"online">>,
        subscriptions = ExistingSubs,
        session_type  = Mode
    },
    ets:insert(?ETS_AGENTS, Agent),
    HttpSession = #http_session{
        token      = Token,
        agent_id   = AgentId,
        session_id = OldSessionId,
        ttl_ms     = TtlMs,
        last_seen  = SysNow,
        mode       = Mode
    },
    ets:insert(?ETS_HTTP_SESSIONS, HttpSession),
    Session = #session{
        session_id  = OldSessionId,
        agent_id    = AgentId,
        session_pid = undefined
    },
    ets:insert(?ETS_SESSIONS, Session),
    pluto_stats:inc(agents_registered),
    pluto_stats:inc_agent(AgentId, registrations),
    broadcast_event(#{
        <<"event">>    => ?EVT_AGENT_JOINED,
        <<"agent_id">> => AgentId
    }, AgentId),
    pluto_event_log:log(agent_registered, #{agent_id => AgentId,
                                            session_id => OldSessionId,
                                            mode => Mode,
                                            resumed => true}),
    {Token, OldSessionId}.

%% @private Evict all HTTP sessions for a given agent_id.
evict_http_sessions(AgentId) ->
    Sessions = ets:match_object(?ETS_HTTP_SESSIONS,
                                #http_session{agent_id = AgentId, _ = '_'}),
    lists:foreach(fun(#http_session{token = T}) ->
        ets:delete(?ETS_HTTP_SESSIONS, T)
    end, Sessions).

%% @private Poll inbox: retrieve and delete all queued messages for an agent.
do_poll_inbox(AgentId) ->
    InboxTtlMs = pluto_config:get(inbox_msg_ttl_ms, ?DEFAULT_INBOX_MSG_TTL_MS),
    NowMs = erlang:system_time(millisecond),
    Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
    SortedSeqs = lists:sort([S || [S] <- Keys]),
    Messages = lists:filtermap(fun(Seq) ->
        Key = {AgentId, Seq},
        case ets:lookup(?ETS_MSG_INBOX, Key) of
            [{_, {Event, InsertedAt}}] ->
                ets:delete(?ETS_MSG_INBOX, Key),
                case NowMs - InsertedAt > InboxTtlMs of
                    true  -> false; %% expired
                    false -> {true, Event}
                end;
            [] ->
                false
        end
    end, SortedSeqs),
    %% Delete the signal file since inbox is now drained
    delete_signal_file(AgentId),
    Messages.

%% @private Notify a waiting long-poll process that a message arrived.
notify_long_poll(AgentId) ->
    case ets:lookup(?ETS_LONG_POLL, AgentId) of
        [{_, Pid}] ->
            ?LOG_INFO("notify_long_poll: notifying ~p for agent ~s", [Pid, AgentId]),
            Pid ! {long_poll_notify, AgentId},
            ok;
        [] ->
            ok
    end.

%% @private Write a signal file indicating messages are waiting.
write_signal_file(AgentId) ->
    SignalDir = pluto_config:get(signal_dir, ?DEFAULT_SIGNAL_DIR),
    %% Sanitize agent_id for filesystem safety
    SafeId = sanitize_filename(AgentId),
    FilePath = filename:join(SignalDir, <<SafeId/binary, ".signal">>),
    Now = erlang:system_time(millisecond),
    %% Count pending messages
    Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
    Count = length(Keys),
    Content = pluto_protocol_json:encode(#{
        <<"agent_id">> => AgentId,
        <<"pending_messages">> => Count,
        <<"timestamp">> => Now
    }),
    file:write_file(FilePath, Content).

%% @private Delete the signal file for an agent (inbox drained).
delete_signal_file(AgentId) ->
    SignalDir = pluto_config:get(signal_dir, ?DEFAULT_SIGNAL_DIR),
    SafeId = sanitize_filename(AgentId),
    FilePath = filename:join(SignalDir, <<SafeId/binary, ".signal">>),
    file:delete(FilePath).

%% @private Sanitize a binary for use as a filename (replace unsafe chars).
sanitize_filename(Bin) ->
    << <<(case C of
        C when C >= $a, C =< $z -> C;
        C when C >= $A, C =< $Z -> C;
        C when C >= $0, C =< $9 -> C;
        $- -> C;
        $_ -> C;
        $. -> C;
        _ -> $_
    end)>> || <<C>> <= Bin >>.

%% @private Sweep all expired inbox messages across all agents.
do_sweep_inbox() ->
    InboxTtlMs = pluto_config:get(inbox_msg_ttl_ms, ?DEFAULT_INBOX_MSG_TTL_MS),
    NowMs = erlang:system_time(millisecond),
    AllEntries = ets:tab2list(?ETS_MSG_INBOX),
    Expired = lists:filter(fun({_Key, {_Event, InsertedAt}}) ->
        NowMs - InsertedAt > InboxTtlMs
    end, AllEntries),
    lists:foreach(fun({Key, _}) ->
        ets:delete(?ETS_MSG_INBOX, Key)
    end, Expired),
    case length(Expired) of
        0 -> ok;
        N -> ?LOG_INFO("inbox sweep: deleted ~w expired messages", [N])
    end.
