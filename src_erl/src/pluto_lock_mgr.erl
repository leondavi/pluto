%%%-------------------------------------------------------------------
%%% @doc pluto_lock_mgr — Lock and lease management gen_server.
%%%
%%% This is the heart of Pluto's coordination.  All lock operations
%%% (acquire, release, renew) pass through this single gen_server,
%%% which provides serialised access to the lock ETS table.
%%%
%%% Concurrency model:
%%%   All lock reads and writes happen inside gen_server callbacks.
%%%   No other module should write to ETS_LOCKS or ETS_WAITERS directly.
%%%   This eliminates race conditions by design.
%%%
%%% The lock manager also runs a periodic sweep to expire stale locks
%%% and timed-out wait entries.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_lock_mgr).
-behaviour(gen_server).

-include("pluto.hrl").

%% Public API
-export([
    start_link/0,
    acquire/4,
    try_acquire/4,
    release/2,
    renew/2,
    list_locks/0,
    locks_for_agent/1,
    get_fencing_seq/0,
    set_fencing_seq/1
]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

%% Internal state: monotonic fencing counter and ref counters.
-record(state, {
    fencing_seq  = 0 :: non_neg_integer(),   %% monotonically increasing
    lock_counter = 0 :: non_neg_integer(),    %% for generating LOCK-N refs
    wait_counter = 0 :: non_neg_integer()     %% for generating WAIT-N refs
}).

%% Sweep interval for expired locks (5 seconds)
-define(SWEEP_INTERVAL_MS, 5000).

%%====================================================================
%% API
%%====================================================================

%% @doc Start and register the lock manager.
-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Attempt to acquire a lock on a resource.
%%
%% `Opts' is a map that may contain:
%%   - `ttl_ms'      => integer()       (required)
%%   - `max_wait_ms' => integer() | undefined
%%   - `session_pid' => pid()           (for async notifications)
%%
%% Returns:
%%   {ok, LockRef, FencingToken}
%%   {wait, WaitRef}
%%   {error, Reason}
-spec acquire(binary(), write | read, binary(), map()) ->
    {ok, binary(), non_neg_integer()} | {wait, binary()} | {error, term()}.
acquire(Resource, Mode, AgentId, Opts) ->
    gen_server:call(?MODULE, {acquire, Resource, Mode, AgentId, Opts}).

%% @doc Non-blocking lock probe: returns immediately with `{ok, LockRef, FToken}`
%% if the lock can be granted, or `unavailable` if the resource is already
%% locked.  Never enters the wait queue — useful for polling or optional
%% coordination patterns where an agent wants to check if a resource is free
%% without committing to wait for it.
-spec try_acquire(binary(), write | read, binary(), map()) ->
    {ok, binary(), non_neg_integer()} | unavailable.
try_acquire(Resource, Mode, AgentId, Opts) ->
    gen_server:call(?MODULE, {try_acquire, Resource, Mode, AgentId, Opts}).

%% @doc Release a lock by its reference.
-spec release(binary(), binary()) -> ok | {error, term()}.
release(LockRef, AgentId) ->
    gen_server:call(?MODULE, {release, LockRef, AgentId}).

%% @doc Renew an active lock's TTL.
-spec renew(binary(), map()) -> ok | {error, term()}.
renew(LockRef, Opts) ->
    gen_server:call(?MODULE, {renew, LockRef, Opts}).

%% @doc List all currently active locks.
-spec list_locks() -> [#lock{}].
list_locks() ->
    ets:tab2list(?ETS_LOCKS).

%% @doc List locks held by a specific agent.
-spec locks_for_agent(binary()) -> [#lock{}].
locks_for_agent(AgentId) ->
    ets:match_object(?ETS_LOCKS, #lock{agent_id = AgentId, _ = '_'}).

%% @doc Get the current fencing sequence number.
-spec get_fencing_seq() -> non_neg_integer().
get_fencing_seq() ->
    gen_server:call(?MODULE, get_fencing_seq).

%% @doc Set the fencing sequence (used during persistence restore).
-spec set_fencing_seq(non_neg_integer()) -> ok.
set_fencing_seq(Seq) ->
    gen_server:call(?MODULE, {set_fencing_seq, Seq}).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    %% Start the periodic expiry sweep timer
    erlang:send_after(?SWEEP_INTERVAL_MS, self(), sweep_expired),
    ?LOG_INFO("pluto_lock_mgr started"),
    {ok, #state{}}.

%% ── acquire ─────────────────────────────────────────────────────────
handle_call({acquire, Resource, Mode, AgentId, Opts}, _From, State) ->
    case check_conflict(Resource, Mode, AgentId) of
        no_conflict ->
            %% Grant the lock immediately
            {LockRef, FToken, NewState} = grant_lock(Resource, Mode, AgentId, Opts, State),
            pluto_stats:inc(locks_acquired),
            pluto_stats:inc_agent(AgentId, locks_acquired),
            {reply, {ok, LockRef, FToken}, NewState};
        conflict ->
            %% Enqueue the request in the wait queue
            {WaitRef, NewState} = enqueue_waiter(Resource, Mode, AgentId, Opts, State),
            pluto_stats:inc(lock_waits),
            %% Check for deadlock after adding the wait edge
            case pluto_deadlock:check_cycle(AgentId) of
                no_cycle ->
                    {reply, {wait, WaitRef}, NewState};
                {cycle, Agents} ->
                    %% This agent is the victim (youngest waiter)
                    remove_waiter(WaitRef),
                    pluto_deadlock:remove_edge(AgentId),
                    pluto_stats:inc(deadlocks_detected),
                    pluto_stats:inc(deadlock_victims),
                    pluto_stats:inc_agent(AgentId, deadlock_victim),
                    notify_deadlock(Agents, AgentId),
                    {reply, {error, deadlock}, NewState}
            end
    end;

%% ── try_acquire (non-blocking) ──────────────────────────────────────
%% Checks for conflict and either grants immediately or returns
%% `unavailable` — never queues the request.
handle_call({try_acquire, Resource, Mode, AgentId, Opts}, _From, State) ->
    case check_conflict(Resource, Mode, AgentId) of
        no_conflict ->
            {LockRef, FToken, NewState} = grant_lock(Resource, Mode, AgentId, Opts, State),
            pluto_stats:inc(locks_acquired),
            pluto_stats:inc_agent(AgentId, locks_acquired),
            {reply, {ok, LockRef, FToken}, NewState};
        conflict ->
            {reply, unavailable, State}
    end;

%% ── release ─────────────────────────────────────────────────────────
handle_call({release, LockRef, AgentId}, _From, State) ->
    case ets:lookup(?ETS_LOCKS, LockRef) of
        [#lock{agent_id = AgentId, resource = Resource}] ->
            ets:delete(?ETS_LOCKS, LockRef),
            pluto_stats:inc(locks_released),
            pluto_stats:inc_agent(AgentId, locks_released),
            %% Advance the wait queue for this resource
            NewState = advance_queue(Resource, State),
            {reply, ok, NewState};
        [#lock{}] ->
            %% Lock exists but owned by a different agent
            {reply, {error, not_found}, State};
        [] ->
            {reply, {error, not_found}, State}
    end;

%% ── renew ───────────────────────────────────────────────────────────
handle_call({renew, LockRef, Opts}, _From, State) ->
    case ets:lookup(?ETS_LOCKS, LockRef) of
        [Lock = #lock{}] ->
            TtlMs = maps:get(ttl_ms, Opts, 30000),
            NewExpiry = pluto_lease:make_expires_at(TtlMs),
            ets:insert(?ETS_LOCKS, Lock#lock{expires_at = NewExpiry}),
            pluto_stats:inc(locks_renewed),
            {reply, ok, State};
        [] ->
            {reply, {error, not_found}, State}
    end;
%% ── fencing_seq accessors ───────────────────────────────────────
handle_call(get_fencing_seq, _From, #state{fencing_seq = FSeq} = State) ->
    {reply, FSeq, State};

handle_call({set_fencing_seq, Seq}, _From, State) when is_integer(Seq), Seq >= 0 ->
    {reply, ok, State#state{fencing_seq = max(Seq, State#state.fencing_seq)}};
handle_call(_Request, _From, State) ->
    {reply, {error, unknown_request}, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

%% ── Periodic expiry sweep ───────────────────────────────────────────
handle_info(sweep_expired, State) ->
    NewState = sweep_expired_locks(State),
    sweep_expired_waiters(),
    erlang:send_after(?SWEEP_INTERVAL_MS, self(), sweep_expired),
    {noreply, NewState};

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Check whether a new lock request conflicts with existing locks.
%%
%% Conflict rules:
%%   - write vs anything = conflict
%%   - read  vs write   = conflict
%%   - read  vs read    = no conflict (shared readers allowed)
check_conflict(Resource, Mode, _AgentId) ->
    Existing = ets:match_object(?ETS_LOCKS, #lock{resource = Resource, _ = '_'}),
    case has_conflict(Mode, Existing) of
        true  -> conflict;
        false -> no_conflict
    end.

%% @private Determine if Mode conflicts with any of the existing locks.
has_conflict(_Mode, []) ->
    false;
has_conflict(write, [_ | _]) ->
    %% Write lock conflicts with any existing lock
    true;
has_conflict(read, Locks) ->
    %% Read lock conflicts only if a write lock exists
    lists:any(fun(#lock{mode = M}) -> M =:= write end, Locks).

%% @private Create a lock record and insert it into ETS.
grant_lock(Resource, Mode, AgentId, Opts, State) ->
    #state{fencing_seq = FSeq, lock_counter = LC} = State,
    NewFSeq = FSeq + 1,
    NewLC   = LC + 1,
    LockRef = iolist_to_binary(io_lib:format("LOCK-~w", [NewLC])),
    TtlMs   = maps:get(ttl_ms, Opts, 30000),
    SessId  = maps:get(session_id, Opts, <<>>),

    Lock = #lock{
        lock_ref      = LockRef,
        resource      = Resource,
        mode          = Mode,
        agent_id      = AgentId,
        session_id    = SessId,
        fencing_token = NewFSeq,
        expires_at    = pluto_lease:make_expires_at(TtlMs),
        inserted_at   = pluto_lease:now_ms()
    },
    ets:insert(?ETS_LOCKS, Lock),
    NewState = State#state{fencing_seq = NewFSeq, lock_counter = NewLC},
    %% Log lock acquisition event
    pluto_event_log:log(lock_acquired, #{agent_id => AgentId, resource => Resource,
                                         lock_ref => LockRef, fencing_token => NewFSeq}),
    {LockRef, NewFSeq, NewState}.

%% @private Add a request to the wait queue for a resource.
enqueue_waiter(Resource, Mode, AgentId, Opts, State) ->
    #state{wait_counter = WC} = State,
    NewWC   = WC + 1,
    WaitRef = iolist_to_binary(io_lib:format("WAIT-~w", [NewWC])),
    Now     = pluto_lease:now_ms(),
    SessId  = maps:get(session_id, Opts, <<>>),
    SessPid = maps:get(session_pid, Opts, undefined),

    MaxWaitUntil = case maps:get(max_wait_ms, Opts, undefined) of
                       undefined -> infinity;
                       MaxMs     -> Now + MaxMs
                   end,

    Entry = #wait_entry{
        wait_ref       = WaitRef,
        resource       = Resource,
        mode           = Mode,
        agent_id       = AgentId,
        session_id     = SessId,
        session_pid    = SessPid,
        requested_at   = Now,
        max_wait_until = MaxWaitUntil
    },

    %% Key for ordered_set: {Resource, RequestedAt, WaitRef} ensures FIFO
    ets:insert(?ETS_WAITERS, {{Resource, Now, WaitRef}, Entry}),

    %% Add edge to wait-for graph: this agent waits for whoever holds the resource
    Holders = [L#lock.agent_id ||
               L <- ets:match_object(?ETS_LOCKS, #lock{resource = Resource, _ = '_'})],
    lists:foreach(fun(HolderId) ->
        pluto_deadlock:add_edge(AgentId, HolderId)
    end, Holders),

    NewState = State#state{wait_counter = NewWC},
    {WaitRef, NewState}.

%% @private Remove a waiter entry by its WaitRef.
remove_waiter(WaitRef) ->
    %% Scan for the matching entry (wait_ref is inside the value)
    Pattern = {{'_', '_', WaitRef}, '_'},
    case ets:match_object(?ETS_WAITERS, Pattern) of
        [{Key, _Entry} | _] ->
            ets:delete(?ETS_WAITERS, Key);
        [] ->
            ok
    end.

%% @private After a lock is released, check if the next waiter can be granted.
advance_queue(Resource, State) ->
    %% Find all waiters for this resource, ordered by requested_at (FIFO)
    Pattern = {{Resource, '_', '_'}, '_'},
    Waiters = ets:match_object(?ETS_WAITERS, Pattern),
    advance_waiters(Waiters, Resource, State).

advance_waiters([], _Resource, State) ->
    State;
advance_waiters([{Key, Entry} | Rest], Resource, State) ->
    #wait_entry{mode = Mode, agent_id = AgentId} = Entry,
    case check_conflict(Resource, Mode, AgentId) of
        no_conflict ->
            %% Grant this waiter
            ets:delete(?ETS_WAITERS, Key),
            pluto_deadlock:remove_edge(AgentId),
            NewState = notify_lock_granted(Entry, State),
            pluto_stats:inc(locks_acquired),
            pluto_stats:inc_agent(AgentId, locks_acquired),
            %% If this was a read lock, continue granting consecutive readers
            case Mode of
                read  -> advance_waiters(Rest, Resource, NewState);
                write -> NewState  %% Write is exclusive, stop here
            end;
        conflict ->
            %% Can't grant yet; stop advancing
            State
    end.

%% @private Send a lock_granted event to the waiting session.
notify_lock_granted(#wait_entry{session_pid = Pid, wait_ref = WaitRef,
                                resource = Resource, agent_id = AgentId,
                                mode = WaitMode, session_id = WSessId},
                    State) when is_pid(Pid) ->
    #state{fencing_seq = FSeq, lock_counter = LC} = State,
    NewFSeq = FSeq + 1,
    NewLC   = LC + 1,
    LockRef = iolist_to_binary(io_lib:format("LOCK-~w", [NewLC])),

    %% Create the lock using the mode from the wait entry
    Lock = #lock{
        lock_ref      = LockRef,
        resource      = Resource,
        mode          = WaitMode,
        agent_id      = AgentId,
        session_id    = WSessId,
        fencing_token = NewFSeq,
        expires_at    = pluto_lease:make_expires_at(30000),
        inserted_at   = pluto_lease:now_ms()
    },
    ets:insert(?ETS_LOCKS, Lock),

    %% Push the lock_granted event to the session process
    Event = #{
        <<"event">>         => ?EVT_LOCK_GRANTED,
        <<"wait_ref">>      => WaitRef,
        <<"lock_ref">>      => LockRef,
        <<"fencing_token">> => NewFSeq,
        <<"resource">>      => Resource
    },
    Pid ! {pluto_event, Event},
    %% Log the event
    pluto_event_log:log(lock_granted, #{agent_id => AgentId, resource => Resource,
                                        lock_ref => LockRef, fencing_token => NewFSeq}),
    State#state{fencing_seq = NewFSeq, lock_counter = NewLC};
notify_lock_granted(_, State) ->
    %% No session PID — can't notify
    State.

%% @private Sweep and remove expired locks, advancing queues where needed.
sweep_expired_locks(State) ->
    Now = pluto_lease:now_ms(),
    AllLocks = ets:tab2list(?ETS_LOCKS),
    lists:foldl(fun(#lock{lock_ref = Ref, resource = Res,
                          agent_id = AId, expires_at = Exp}, AccState) ->
        case Now >= Exp of
            true ->
                ?LOG_INFO("Lock ~s expired for agent ~s on ~s",
                          [Ref, AId, Res]),
                ets:delete(?ETS_LOCKS, Ref),
                pluto_stats:inc(locks_expired),
                pluto_stats:inc_agent(AId, locks_expired),
                advance_queue(Res, AccState);
            false ->
                AccState
        end
    end, State, AllLocks).

%% @private Remove wait entries that have exceeded their max_wait_until deadline.
sweep_expired_waiters() ->
    Now = pluto_lease:now_ms(),
    AllWaiters = ets:tab2list(?ETS_WAITERS),
    lists:foreach(fun({Key, #wait_entry{max_wait_until = MaxUntil,
                                        session_pid = Pid,
                                        wait_ref = WRef,
                                        agent_id = AId,
                                        resource = Res}}) ->
        case MaxUntil =/= infinity andalso Now >= MaxUntil of
            true ->
                ?LOG_INFO("Wait ~s timed out for agent ~s on ~s",
                          [WRef, AId, Res]),
                ets:delete(?ETS_WAITERS, Key),
                pluto_deadlock:remove_edge(AId),
                %% Notify the session about the timeout
                case is_pid(Pid) of
                    true ->
                        Event = #{
                            <<"event">>    => ?EVT_WAIT_TIMEOUT,
                            <<"wait_ref">> => WRef,
                            <<"resource">> => Res
                        },
                        Pid ! {pluto_event, Event};
                    false ->
                        ok
                end;
            false ->
                ok
        end
    end, AllWaiters).

%% @private Notify all agents in a deadlock cycle (except the victim).
notify_deadlock(Agents, VictimId) ->
    Event = #{
        <<"event">>   => ?EVT_DEADLOCK_DETECTED,
        <<"agents">>  => Agents,
        <<"victim">>  => VictimId
    },
    %% Send to all agents in the cycle except the victim
    lists:foreach(fun(AId) ->
        case AId =/= VictimId of
            true ->
                case ets:lookup(?ETS_AGENTS, AId) of
                    [#agent{session_pid = Pid}] when is_pid(Pid) ->
                        Pid ! {pluto_event, Event};
                    _ ->
                        ok
                end;
            false ->
                ok
        end
    end, Agents).
