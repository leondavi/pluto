%%%-------------------------------------------------------------------
%%% @doc pluto_persistence — Periodic state snapshot manager.
%%%
%%% Serialises the contents of the main ETS tables to disk on a
%%% configurable timer, and on clean shutdown.  On startup, loads
%%% the most recent snapshot so that locks and agent records survive
%%% restarts.
%%%
%%% Snapshot format: Erlang binary term (`term_to_binary/1`), written
%%% to a single file `<persistence_dir>/pluto.snapshot`.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_persistence).
-behaviour(gen_server).

-include("pluto.hrl").

%% API
-export([start_link/0, flush/0]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {
    dir          :: string(),
    flush_ms     :: non_neg_integer(),
    fencing_seq  :: non_neg_integer()
}).

-define(SNAPSHOT_FILE, "pluto.snapshot").

%%====================================================================
%% API
%%====================================================================

%% @doc Start and register the persistence manager.
-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Trigger an immediate synchronous flush.
-spec flush() -> ok.
flush() ->
    gen_server:call(?MODULE, flush, 30000).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    Dir     = pluto_config:get(persistence_dir, ?DEFAULT_PERSISTENCE_DIR),
    FlushMs = resolve_interval(pluto_config:get(flush_interval, ?DEFAULT_FLUSH_INTERVAL)),

    %% Ensure the persistence directory exists
    filelib:ensure_dir(filename:join(Dir, ?SNAPSHOT_FILE)),

    %% Load existing snapshot if present
    FSeq = load_snapshot(Dir),

    %% Schedule the first periodic flush
    erlang:send_after(FlushMs, self(), periodic_flush),
    ?LOG_INFO("pluto_persistence started (dir=~s, interval=~wms, fencing_seq=~w)",
              [Dir, FlushMs, FSeq]),
    {ok, #state{dir = Dir, flush_ms = FlushMs, fencing_seq = FSeq}}.

handle_call(flush, _From, State) ->
    do_flush(State),
    {reply, ok, State};

handle_call(_Request, _From, State) ->
    {reply, ok, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

%% ── Periodic flush ──────────────────────────────────────────────────
handle_info(periodic_flush, #state{flush_ms = FlushMs} = State) ->
    do_flush(State),
    erlang:send_after(FlushMs, self(), periodic_flush),
    {noreply, State};

handle_info(_Info, State) ->
    {noreply, State}.

%% ── Clean shutdown — final flush ────────────────────────────────────
terminate(_Reason, State) ->
    ?LOG_INFO("pluto_persistence: final flush on shutdown"),
    do_flush(State),
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Write a snapshot of all ETS tables to disk.
do_flush(#state{dir = Dir}) ->
    Snapshot = #{
        locks    => ets:tab2list(?ETS_LOCKS),
        agents   => ets:tab2list(?ETS_AGENTS),
        sessions => ets:tab2list(?ETS_SESSIONS),
        waiters  => ets:tab2list(?ETS_WAITERS)
    },
    Path = filename:join(Dir, ?SNAPSHOT_FILE),
    Data = term_to_binary(Snapshot),
    case file:write_file(Path, Data) of
        ok ->
            ok;
        {error, Reason} ->
            ?LOG_ERROR("pluto_persistence: failed to write snapshot: ~p", [Reason])
    end.

%% @private Load a snapshot from disk and populate ETS tables.
%% Returns the fencing_seq value (0 if no snapshot exists).
load_snapshot(Dir) ->
    Path = filename:join(Dir, ?SNAPSHOT_FILE),
    case file:read_file(Path) of
        {ok, Data} ->
            try
                Snapshot = binary_to_term(Data),
                restore_snapshot(Snapshot),
                ?LOG_INFO("pluto_persistence: snapshot loaded from ~s", [Path]),
                0  %% fencing_seq is managed by pluto_lock_mgr
            catch
                _:Err ->
                    ?LOG_ERROR("pluto_persistence: corrupt snapshot — ~p", [Err]),
                    0
            end;
        {error, enoent} ->
            ?LOG_INFO("pluto_persistence: no snapshot found — starting fresh"),
            0;
        {error, Reason} ->
            ?LOG_ERROR("pluto_persistence: failed to read snapshot — ~p", [Reason]),
            0
    end.

%% @private Restore ETS tables from a snapshot map.
%% Discards expired locks and marks all sessions as disconnected.
restore_snapshot(#{locks := Locks, agents := Agents}) ->
    Now = pluto_lease:now_ms(),

    %% Restore non-expired locks
    lists:foreach(fun(Lock) ->
        case is_record(Lock, lock) andalso Lock#lock.expires_at > Now of
            true  -> ets:insert(?ETS_LOCKS, Lock);
            false -> ok  %% Skip expired locks
        end
    end, Locks),

    %% Restore agents as disconnected (they need to reconnect)
    lists:foreach(fun(Agent) ->
        case is_record(Agent, agent) of
            true ->
                ets:insert(?ETS_AGENTS, Agent#agent{
                    status      = disconnected,
                    session_pid = undefined,
                    session_id  = undefined
                });
            false ->
                ok
        end
    end, Agents),
    ok;
restore_snapshot(_) ->
    %% Unknown snapshot format — ignore
    ok.

%% @private Convert interval config to milliseconds.
%% Accepts atoms `minute` and `hour`, or a raw integer.
resolve_interval(minute) -> 60000;
resolve_interval(hour)   -> 3600000;
resolve_interval(N) when is_integer(N), N > 0 -> N;
resolve_interval(_) -> 60000.  %% fallback
