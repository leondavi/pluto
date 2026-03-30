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
    unregister_agent/1,
    send_msg/3,
    broadcast/2,
    list_agents/0,
    lookup_agent/1
]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {}).

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
    gen_server:call(?MODULE, {register, AgentId, SessionId, SessionPid}).

%% @doc Unregister an agent when its session ends.
%% Marks the agent as disconnected and starts the grace period timer.
-spec unregister_agent(binary()) -> ok.
unregister_agent(AgentId) ->
    gen_server:cast(?MODULE, {unregister, AgentId}).

%% @doc Send a direct message from one agent to another.
%% The payload is forwarded as-is to the target session process.
-spec send_msg(binary(), binary(), map()) -> ok | {error, unknown_target}.
send_msg(From, To, Payload) ->
    gen_server:call(?MODULE, {send, From, To, Payload}).

%% @doc Broadcast a message from one agent to all other connected agents.
-spec broadcast(binary(), map()) -> ok.
broadcast(From, Payload) ->
    gen_server:cast(?MODULE, {broadcast, From, Payload}).

%% @doc Return the list of currently connected agent IDs.
-spec list_agents() -> [binary()].
list_agents() ->
    gen_server:call(?MODULE, list_agents).

%% @doc Look up an agent by agent_id.  Returns the agent record or not_found.
-spec lookup_agent(binary()) -> {ok, #agent{}} | {error, not_found}.
lookup_agent(AgentId) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [Agent] -> {ok, Agent};
        []      -> {error, not_found}
    end.

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    ?LOG_INFO("pluto_msg_hub started"),
    {ok, #state{}}.

%% ── register ────────────────────────────────────────────────────────
handle_call({register, AgentId, SessionId, SessionPid}, _From, State) ->
    Policy = pluto_config:get(session_conflict_policy, ?DEFAULT_SESSION_CONFLICT),
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = connected, session_pid = OldPid}] when Policy =:= strict ->
            %% Agent is already connected — strict mode rejects the new one
            case is_process_alive(OldPid) of
                true ->
                    {reply, {error, already_registered}, State};
                false ->
                    %% Old session is dead; allow re-registration
                    do_register(AgentId, SessionId, SessionPid),
                    {reply, {ok, SessionId}, State}
            end;
        [#agent{status = connected, session_pid = OldPid}] when Policy =:= takeover ->
            %% Takeover mode — kill the old session and register the new one
            case is_process_alive(OldPid) of
                true  -> OldPid ! {pluto_takeover, AgentId};
                false -> ok
            end,
            do_register(AgentId, SessionId, SessionPid),
            {reply, {ok, SessionId}, State};
        [#agent{status = disconnected}] ->
            %% Agent was disconnected — reconnect within grace period
            do_register(AgentId, SessionId, SessionPid),
            {reply, {ok, SessionId}, State};
        [] ->
            %% Brand new agent
            do_register(AgentId, SessionId, SessionPid),
            {reply, {ok, SessionId}, State}
    end;

%% ── send direct message ────────────────────────────────────────────
handle_call({send, From, To, Payload}, _From, State) ->
    case ets:lookup(?ETS_AGENTS, To) of
        [#agent{status = connected, session_pid = Pid}] when is_pid(Pid) ->
            Event = #{
                <<"event">>   => ?EVT_MESSAGE,
                <<"from">>    => From,
                <<"payload">> => Payload
            },
            Pid ! {pluto_event, Event},
            pluto_stats:inc(messages_sent),
            pluto_stats:inc(messages_received),
            pluto_stats:inc_agent(From, messages_sent),
            pluto_stats:inc_agent(To, messages_received),
            {reply, ok, State};
        _ ->
            {reply, {error, unknown_target}, State}
    end;

%% ── list agents ─────────────────────────────────────────────────────
handle_call(list_agents, _From, State) ->
    Agents = ets:match(?ETS_AGENTS, #agent{agent_id = '$1',
                                           status = connected,
                                           _ = '_'}),
    Ids = [Id || [Id] <- Agents],
    {reply, Ids, State};

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
            ets:insert(?ETS_AGENTS, Agent#agent{
                status      = disconnected,
                session_pid = undefined
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
            %% Start the grace-period timer
            GraceMs = pluto_config:get(reconnect_grace_ms, ?DEFAULT_RECONNECT_GRACE_MS),
            erlang:send_after(GraceMs, self(), {grace_expired, AgentId}),
            ok;
        [] ->
            ok
    end,
    {noreply, State};

handle_cast(_Msg, State) ->
    {noreply, State}.

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
            ets:delete(?ETS_AGENTS, AgentId);
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
do_register(AgentId, SessionId, SessionPid) ->
    Now = pluto_lease:now_ms(),

    %% Upsert agent record
    Agent = #agent{
        agent_id     = AgentId,
        session_id   = SessionId,
        session_pid  = SessionPid,
        status       = connected,
        connected_at = Now
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
