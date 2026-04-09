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
    deliver_inbox/1
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
%% Returns `{ok, SessionId}` on success, or `{error, already_registered}`
%% if the agent_id is already active (in strict mode).
-spec register_agent(binary(), binary(), pid()) ->
    {ok, binary()} | {error, already_registered}.
register_agent(AgentId, SessionId, SessionPid) ->
    register_agent(AgentId, SessionId, SessionPid, #{}).

%% @doc Register an agent with its session and attributes.
-spec register_agent(binary(), binary(), pid(), map()) ->
    {ok, binary()} | {error, already_registered}.
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

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    ?LOG_INFO("pluto_msg_hub started"),
    {ok, #state{}}.

%% ── register ────────────────────────────────────────────────────────
handle_call({register, AgentId, SessionId, SessionPid, Attrs}, _From, State) ->
    Policy = pluto_config:get(session_conflict_policy, ?DEFAULT_SESSION_CONFLICT),
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = connected, session_pid = OldPid}] when Policy =:= strict ->
            case is_process_alive(OldPid) of
                true ->
                    {reply, {error, already_registered}, State};
                false ->
                    do_register(AgentId, SessionId, SessionPid, Attrs),
                    {reply, {ok, SessionId}, State}
            end;
        [#agent{status = connected, session_pid = OldPid}] when Policy =:= takeover ->
            case is_process_alive(OldPid) of
                true  -> OldPid ! {pluto_takeover, AgentId};
                false -> ok
            end,
            do_register(AgentId, SessionId, SessionPid, Attrs),
            {reply, {ok, SessionId}, State};
        [#agent{status = disconnected}] ->
            %% Agent was disconnected — reconnect within grace period
            do_register(AgentId, SessionId, SessionPid, Attrs),
            %% Deliver any queued inbox messages
            self() ! {deliver_inbox_sync, AgentId},
            {reply, {ok, SessionId}, State};
        [] ->
            do_register(AgentId, SessionId, SessionPid, Attrs),
            {reply, {ok, SessionId}, State}
    end;

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
        [#agent{status = connected, session_pid = Pid}] when is_pid(Pid) ->
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
                subscriptions = Subs}] ->
            Result = #{
                <<"agent_id">>      => AgentId,
                <<"status">>        => atom_to_binary(Status, utf8),
                <<"last_seen">>     => LastSeen,
                <<"custom_status">> => CStatus,
                <<"attributes">>    => Attrs,
                <<"subscriptions">> => Subs
            },
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
            erlang:send_after(GraceMs, self(), {grace_expired, AgentId}),
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
do_register(AgentId, SessionId, SessionPid, Attrs) ->
    Now = pluto_lease:now_ms(),
    SysNow = erlang:system_time(millisecond),

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
        subscriptions = ExistingSubs
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
    case CurrentSize < MaxInbox of
        true ->
            ets:insert(?ETS_MSG_INBOX, {Key, Event});
        false ->
            %% Inbox full — drop oldest
            case ets:match(?ETS_MSG_INBOX, Pattern) of
                [[]] -> ok;
                Matches when length(Matches) > 0 ->
                    %% ordered_set so first match is oldest
                    OldestKey = {AgentId, lists:min([S || [S] <- ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'})])},
                    ets:delete(?ETS_MSG_INBOX, OldestKey),
                    ets:insert(?ETS_MSG_INBOX, {Key, Event});
                _ -> ok
            end
    end,
    ok.

%% @private Deliver all queued inbox messages to a reconnected agent.
do_deliver_inbox(AgentId) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = connected, session_pid = Pid}] when is_pid(Pid) ->
            %% Get all inbox messages in order
            Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
            SortedSeqs = lists:sort([S || [S] <- Keys]),
            lists:foreach(fun(Seq) ->
                Key = {AgentId, Seq},
                case ets:lookup(?ETS_MSG_INBOX, Key) of
                    [{_, Event}] ->
                        Pid ! {pluto_event, Event},
                        ets:delete(?ETS_MSG_INBOX, Key);
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
