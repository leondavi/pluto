%%%-------------------------------------------------------------------
%%% @doc pluto_stats — Runtime statistics for the Pluto server.
%%%
%%% Maintains atomic counters in an ETS table for low-overhead stats.
%%% Counters are incremented from the hot path (lock_mgr, msg_hub,
%%% deadlock) using ets:update_counter/3 for lock-free updates.
%%%
%%% Tracked metrics:
%%%   - locks_acquired / locks_released / locks_expired
%%%   - deadlocks_detected / deadlock_victims
%%%   - messages_sent / messages_received / broadcasts_sent
%%%   - agents_registered / agents_disconnected
%%%   - Per-agent message counters (sent / received)
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_stats).
-behaviour(gen_server).

-include("pluto.hrl").

%% Public API
-export([
    start_link/0,
    inc/1,
    inc/2,
    inc_agent/2,
    inc_agent/3,
    get_all/0,
    get_counter/1,
    get_agent_stats/0,
    get_agent_stats/1,
    get_summary/0,
    reset/0
]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

%% ETS table for global counters
-define(ETS_STATS, pluto_stats).
%% ETS table for per-agent counters
-define(ETS_AGENT_STATS, pluto_agent_stats).

%% Global counter keys
-define(STAT_KEYS, [
    locks_acquired,
    locks_released,
    locks_expired,
    locks_renewed,
    lock_waits,
    deadlocks_detected,
    deadlock_victims,
    messages_sent,
    messages_received,
    broadcasts_sent,
    agents_registered,
    agents_disconnected,
    total_requests
]).

-record(state, {
    started_at :: integer()  %% system time (ms) when stats module started
}).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Increment a global counter by 1.
-spec inc(atom()) -> ok.
inc(Key) ->
    inc(Key, 1).

%% @doc Increment a global counter by N.
-spec inc(atom(), non_neg_integer()) -> ok.
inc(Key, N) when is_atom(Key), is_integer(N), N >= 0 ->
    try
        ets:update_counter(?ETS_STATS, Key, N)
    catch
        error:badarg ->
            %% Key doesn't exist yet — insert it
            try
                ets:insert_new(?ETS_STATS, {Key, N}),
                ok
            catch _:_ -> ok
            end
    end,
    ok.

%% @doc Increment a per-agent counter by 1.
-spec inc_agent(binary(), atom()) -> ok.
inc_agent(AgentId, Key) ->
    inc_agent(AgentId, Key, 1).

%% @doc Increment a per-agent counter by N.
-spec inc_agent(binary(), atom(), non_neg_integer()) -> ok.
inc_agent(AgentId, Key, N) when is_binary(AgentId), is_atom(Key), is_integer(N), N >= 0 ->
    CompositeKey = {AgentId, Key},
    try
        ets:update_counter(?ETS_AGENT_STATS, CompositeKey, N)
    catch
        error:badarg ->
            try
                ets:insert_new(?ETS_AGENT_STATS, {CompositeKey, N}),
                ok
            catch _:_ -> ok
            end
    end,
    ok.

%% @doc Get all global counters as a map.
-spec get_all() -> map().
get_all() ->
    Entries = ets:tab2list(?ETS_STATS),
    maps:from_list([{atom_to_binary(K, utf8), V} || {K, V} <- Entries]).

%% @doc Get the value of a single counter.
-spec get_counter(atom()) -> non_neg_integer().
get_counter(Key) ->
    case ets:lookup(?ETS_STATS, Key) of
        [{_, V}] -> V;
        []       -> 0
    end.

%% @doc Get per-agent stats as a map of agent_id => #{counter => value}.
-spec get_agent_stats() -> map().
get_agent_stats() ->
    Entries = ets:tab2list(?ETS_AGENT_STATS),
    lists:foldl(fun({{AgentId, Key}, Val}, Acc) ->
        AgentMap = maps:get(AgentId, Acc, #{}),
        KeyBin = atom_to_binary(Key, utf8),
        maps:put(AgentId, maps:put(KeyBin, Val, AgentMap), Acc)
    end, #{}, Entries).

%% @doc Get stats for a specific agent.
-spec get_agent_stats(binary()) -> map().
get_agent_stats(AgentId) ->
    Pattern = {{AgentId, '_'}, '_'},
    Entries = ets:match_object(?ETS_AGENT_STATS, Pattern),
    maps:from_list([{atom_to_binary(K, utf8), V} || {{_, K}, V} <- Entries]).

%% @doc Get a full summary including global counters, per-agent stats,
%% and live snapshot data (active locks, connected agents, waiters).
-spec get_summary() -> map().
get_summary() ->
    gen_server:call(?MODULE, get_summary).

%% @doc Reset all counters to zero.
-spec reset() -> ok.
reset() ->
    gen_server:call(?MODULE, reset).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    %% Create ETS tables for stats counters
    ets:new(?ETS_STATS, [named_table, set, public, {write_concurrency, true}]),
    ets:new(?ETS_AGENT_STATS, [named_table, set, public, {write_concurrency, true}]),

    %% Initialise all known global counters to 0
    lists:foreach(fun(Key) ->
        ets:insert(?ETS_STATS, {Key, 0})
    end, ?STAT_KEYS),

    StartedAt = erlang:system_time(millisecond),
    ?LOG_INFO("pluto_stats started"),
    {ok, #state{started_at = StartedAt}}.

handle_call(get_summary, _From, #state{started_at = StartedAt} = State) ->
    Now = erlang:system_time(millisecond),
    UptimeMs = Now - StartedAt,

    %% Global counters
    GlobalCounters = get_all(),

    %% Per-agent stats
    AgentStats = get_agent_stats(),

    %% Live snapshot
    ActiveLocks = ets:info(?ETS_LOCKS, size),
    ConnectedAgents = length(ets:match(?ETS_AGENTS,
        #agent{status = connected, _ = '_'})),
    TotalAgents = ets:info(?ETS_AGENTS, size),
    PendingWaiters = ets:info(?ETS_WAITERS, size),
    WaitGraphEdges = ets:info(?ETS_WAIT_GRAPH, size),

    Summary = #{
        <<"status">> => <<"ok">>,
        <<"uptime_ms">> => UptimeMs,
        <<"server_time">> => Now,
        <<"counters">> => GlobalCounters,
        <<"agent_stats">> => AgentStats,
        <<"live">> => #{
            <<"active_locks">> => ActiveLocks,
            <<"connected_agents">> => ConnectedAgents,
            <<"total_agents">> => TotalAgents,
            <<"pending_waiters">> => PendingWaiters,
            <<"wait_graph_edges">> => WaitGraphEdges
        }
    },
    {reply, Summary, State};

handle_call(reset, _From, State) ->
    ets:delete_all_objects(?ETS_STATS),
    ets:delete_all_objects(?ETS_AGENT_STATS),
    lists:foreach(fun(Key) ->
        ets:insert(?ETS_STATS, {Key, 0})
    end, ?STAT_KEYS),
    {reply, ok, State};

handle_call(_Request, _From, State) ->
    {reply, {error, unknown_request}, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.
