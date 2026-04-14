%%%-------------------------------------------------------------------
%%% @doc pluto_name_registry — Centralized agent name authority.
%%%
%%% This gen_server is the SINGLE source of truth for agent name
%%% ownership. All registration paths (TCP, HTTP, stateless) must
%%% acquire a name through this module before proceeding.
%%%
%%% Design:
%%%   - A dedicated ETS table (pluto_name_registry) maps agent_id
%%%     to {OwnerRef, SessionType, RegisteredAt}.
%%%   - OwnerRef is either a pid (TCP) or a token binary (HTTP).
%%%   - Names are reserved atomically inside a gen_server:call.
%%%   - When an agent disconnects or its session expires, the name
%%%     is released via release_name/1.
%%%   - All callers must release names — the heartbeat sweeper,
%%%     unregister handlers, and grace period expiry all call
%%%     release_name/1.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_name_registry).
-behaviour(gen_server).

-include("pluto.hrl").

%% Public API
-export([
    start_link/0,
    reserve_name/3,
    release_name/1,
    is_registered/1,
    owner_of/1,
    list_names/0
]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-define(NAME_TABLE, pluto_name_registry).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Reserve an agent name. Returns the final name to use.
%%
%% Policy `strict`:  If the name is taken by a live owner, a unique
%%                   suffixed name is returned instead.
%% Policy `takeover`: The old owner is evicted and the name is granted.
%%
%% OwnerRef: pid() for TCP sessions, binary() token for HTTP sessions.
%% SessionType: tcp | http | stateless
%%
%% Returns:
%%   {ok, FinalAgentId}           — name reserved (original or suffixed)
%%   {ok, FinalAgentId, evicted}  — takeover policy, old owner was evicted
-spec reserve_name(binary(), pid() | binary(), tcp | http | stateless) ->
    {ok, binary()} | {ok, binary(), evicted}.
reserve_name(AgentId, OwnerRef, SessionType) ->
    gen_server:call(?MODULE, {reserve, AgentId, OwnerRef, SessionType}).

%% @doc Release a name, making it available for others.
%% Called on unregister, grace period expiry, HTTP session TTL expiry.
-spec release_name(binary()) -> ok.
release_name(AgentId) ->
    gen_server:call(?MODULE, {release, AgentId}).

%% @doc Check if a name is currently reserved.
-spec is_registered(binary()) -> boolean().
is_registered(AgentId) ->
    ets:member(?NAME_TABLE, AgentId).

%% @doc Get the owner of a name. Returns {ok, OwnerRef, SessionType} or error.
-spec owner_of(binary()) -> {ok, pid() | binary(), tcp | http | stateless} | {error, not_found}.
owner_of(AgentId) ->
    case ets:lookup(?NAME_TABLE, AgentId) of
        [{AgentId, OwnerRef, SType, _Ts}] -> {ok, OwnerRef, SType};
        [] -> {error, not_found}
    end.

%% @doc List all registered names.
-spec list_names() -> [binary()].
list_names() ->
    [Name || {Name, _, _, _} <- ets:tab2list(?NAME_TABLE)].

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    ets:new(?NAME_TABLE, [named_table, set, protected, {read_concurrency, true}]),
    ?LOG_INFO("pluto_name_registry started"),
    {ok, #{}}.

handle_call({reserve, AgentId, OwnerRef, SessionType}, _From, State) ->
    Policy = pluto_config:get(session_conflict_policy, ?DEFAULT_SESSION_CONFLICT),
    Now = erlang:system_time(millisecond),
    case ets:lookup(?NAME_TABLE, AgentId) of
        [] ->
            %% Name is free — reserve it
            ets:insert(?NAME_TABLE, {AgentId, OwnerRef, SessionType, Now}),
            maybe_monitor(OwnerRef),
            {reply, {ok, AgentId}, State};
        [{AgentId, ExistingOwner, ExistingSType, _Ts}] ->
            case is_owner_alive(ExistingOwner, ExistingSType) of
                false ->
                    %% Owner is dead — safe to take the name
                    ets:insert(?NAME_TABLE, {AgentId, OwnerRef, SessionType, Now}),
                    maybe_monitor(OwnerRef),
                    {reply, {ok, AgentId}, State};
                true when Policy =:= strict ->
                    %% Name taken by a live owner — assign unique suffix
                    UniqueId = make_unique_name(AgentId),
                    ets:insert(?NAME_TABLE, {UniqueId, OwnerRef, SessionType, Now}),
                    maybe_monitor(OwnerRef),
                    {reply, {ok, UniqueId}, State};
                true when Policy =:= takeover ->
                    %% Evict the old owner
                    notify_eviction(ExistingOwner, ExistingSType, AgentId),
                    ets:insert(?NAME_TABLE, {AgentId, OwnerRef, SessionType, Now}),
                    maybe_monitor(OwnerRef),
                    {reply, {ok, AgentId, evicted}, State}
            end
    end;

handle_call({release, AgentId}, _From, State) ->
    ets:delete(?NAME_TABLE, AgentId),
    {reply, ok, State};

handle_call(_Request, _From, State) ->
    {reply, {error, unknown}, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info({'DOWN', _Ref, process, Pid, _Reason}, State) ->
    %% A TCP session process died — clean up its name reservation
    Pattern = [{{'$1', Pid, '$2', '$3'}, [], ['$1']}],
    Names = ets:select(?NAME_TABLE, Pattern),
    lists:foreach(fun(Name) ->
        ets:delete(?NAME_TABLE, Name),
        ?LOG_INFO("Name registry: auto-released ~s (process died)", [Name])
    end, Names),
    {noreply, State};

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Check if an owner reference is still alive.
%% TCP owners: check process liveness.
%% HTTP owners: check if the token still exists in ETS_HTTP_SESSIONS.
is_owner_alive(OwnerRef, tcp) when is_pid(OwnerRef) ->
    is_process_alive(OwnerRef);
is_owner_alive(OwnerRef, SType) when SType =:= http; SType =:= stateless ->
    case is_binary(OwnerRef) of
        true ->
            %% OwnerRef is an HTTP token — check if session exists
            case ets:lookup(?ETS_HTTP_SESSIONS, OwnerRef) of
                [_] -> true;
                []  -> false
            end;
        false ->
            false
    end;
is_owner_alive(_, _) ->
    false.

%% @private Notify an existing owner that they are being evicted.
notify_eviction(OwnerRef, tcp, AgentId) when is_pid(OwnerRef) ->
    case is_process_alive(OwnerRef) of
        true  -> OwnerRef ! {pluto_takeover, AgentId};
        false -> ok
    end;
notify_eviction(_OwnerRef, _SType, _AgentId) ->
    %% HTTP agents don't have a process to notify — they'll discover
    %% on next poll that their session is gone
    ok.

%% @private Generate a unique name by appending a random suffix.
make_unique_name(BaseId) ->
    Suffix = list_to_binary([random_alphanum() || _ <- lists:seq(1, 6)]),
    <<BaseId/binary, "-", Suffix/binary>>.

%% @private Generate a random alphanumeric character.
random_alphanum() ->
    Chars = "abcdefghijklmnopqrstuvwxyz0123456789",
    lists:nth(rand:uniform(length(Chars)), Chars).

%% @private Monitor a TCP session pid so we auto-release the name if it dies.
maybe_monitor(OwnerRef) when is_pid(OwnerRef) ->
    erlang:monitor(process, OwnerRef);
maybe_monitor(_) ->
    ok.
