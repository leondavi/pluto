%%%-------------------------------------------------------------------
%%% @doc pluto_job_registry — The table of known job packages.
%%%
%%% Anyone may read; only the pluto_job_mgr process calls the functions
%%% that write. Two files describe a job:
%%%
%%%   package.json  written by the server. Its `sched' field is what the
%%%                 server knows: queued, spawning, spawned, start_failed,
%%%                 cancelled, orphaned, lost, imported, snapshot.
%%%   state.json    written by the runner: starting, running, paused, then
%%%                 one final state together with `final_size'.
%%%
%%% state/1 combines them, strongest first:
%%%   1. a final state.json always wins: the command really ended;
%%%   2. spawning/spawned (and start_failed, when a slow runner turned out
%%%      to be alive) show whatever state.json says;
%%%   3. otherwise `sched' itself is the state.
%%%
%%% ?ETS_JOBS holds {Id, Package, FinalState | undefined}. The final state
%%% of a finished job never changes, so it is cached. ?ETS_JOBS_LIVE lists
%%% unfinished ids, so periodic work does not grow with history.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_registry).

-include("pluto.hrl").

-export([pkg/1, all/0, live/0, children/1, state/1, is_done/1,
         save/1, forget/1, load_all/0]).

%% @doc Package of a job id. Validates the id, which becomes a path.
-spec pkg(term()) -> {ok, pkg()} | {error, binary()}.
pkg(Id) ->
    case pluto_job_store:valid_id(Id) of
        false -> {error, ?ERR_BAD_JOB_ID};
        true ->
            case ets:lookup(?ETS_JOBS, Id) of
                [{_, P, _}] -> {ok, P};
                []          -> {error, ?ERR_UNKNOWN_JOB}
            end
    end.

-spec all() -> [pkg()].
all() -> ets:select(?ETS_JOBS, [{{'_', '$1', '_'}, [], ['$1']}]).

%% @doc Packages that are not finished.
-spec live() -> [pkg()].
live() ->
    [P || {Id} <- ets:tab2list(?ETS_JOBS_LIVE), {_, P, _} <- ets:lookup(?ETS_JOBS, Id)].

-spec children(job_id()) -> [pkg()].
children(Id) -> [P || P <- all(), maps:get(<<"parent">>, P, null) =:= Id].

-spec is_done(pkg()) -> boolean().
is_done(P) -> maps:get(<<"done">>, P, false).

%% @doc Effective state of a job; see the module header.
-spec state(pkg()) -> jstate().
state(#{<<"id">> := Id} = P) ->
    case ets:lookup(?ETS_JOBS, Id) of
        [{_, _, Final}] when is_map(Final) -> Final;
        _ -> combine(P, read_state(Id))
    end.

read_state(Id) ->
    case pluto_job_store:read_json(pluto_job_store:file(Id, ?JOB_FILE_STATE)) of
        {ok, M} when is_map(M) -> M;
        _ -> #{}
    end.

combine(#{<<"sched">> := Sched} = P, St) ->
    Reason = maps:get(<<"sched_reason">>, P, null),
    Ended = case P of
                #{<<"sched_ended_at">> := E} when not is_map_key(<<"ended_at">>, St) ->
                    St#{<<"ended_at">> => E};
                _ -> St
            end,
    Started = is_map_key(<<"pid">>, St),
    case {Sched, St} of
        {_, #{<<"final_size">> := _, <<"state">> := _}} -> St;
        {S, #{<<"state">> := _}} when S =:= ?SCHED_SPAWNED; S =:= ?SCHED_SPAWNING;
                                      S =:= ?SCHED_START_FAILED, Started -> St;
        {S, _} when S =:= ?SCHED_SPAWNING; S =:= ?SCHED_SPAWNED ->
            St#{<<"state">> => ?JOB_STARTING};
        {?SCHED_START_FAILED, _} ->
            Ended#{<<"state">> => ?JOB_FAILED, <<"reason">> => Reason};
        {?SCHED_IMPORTED, _} -> St#{<<"state">> => ?JOB_LOST};
        _ -> Ended#{<<"state">> => Sched, <<"reason">> => Reason}
    end.

%%====================================================================
%% Writes (pluto_job_mgr only)
%%====================================================================

%% @doc Persist, then publish. Throws {io, Reason} and changes nothing
%% when the disk refuses.
-spec save(pkg()) -> ok.
save(#{<<"id">> := Id} = P) ->
    case pluto_job_store:write_json(
           pluto_job_store:file(Id, ?JOB_FILE_PACKAGE), P, sync) of
        ok -> insert(P);
        {error, R} -> throw({io, R})
    end.

insert(#{<<"id">> := Id} = P) ->
    case is_done(P) of
        true ->
            ets:insert(?ETS_JOBS, {Id, P, combine(P, read_state(Id))}),
            ets:delete(?ETS_JOBS_LIVE, Id);
        false ->
            ets:insert(?ETS_JOBS, {Id, P, undefined}),
            ets:insert(?ETS_JOBS_LIVE, {Id})
    end,
    ok.

-spec forget(job_id()) -> ok.
forget(Id) ->
    ets:delete(?ETS_JOBS, Id),
    ets:delete(?ETS_JOBS_LIVE, Id),
    ok.

%% @doc Rebuild the table from disk after a restart.
-spec load_all() -> ok.
load_all() ->
    ets:delete_all_objects(?ETS_JOBS),
    ets:delete_all_objects(?ETS_JOBS_LIVE),
    recover_replaced(),
    Host = pluto_job_store:host_id(),
    {ok, Names} = file:list_dir(pluto_job_store:jobs_dir()),
    lists:foreach(fun(Name) ->
        Id = unicode:characters_to_binary(Name),
        case pluto_job_store:valid_id(Id) of
            true ->
                try load_one(Id, Host)
                catch _:R -> ?LOG_ERROR("smartstreams: cannot load ~s: ~p", [Id, R])
                end;
            false -> ok
        end
    end, Names).

load_one(Id, Host) ->
    {ok, #{<<"id">> := Id, <<"sched">> := Sched, <<"owner">> := _,
           <<"opened_at">> := _} = P} =
        pluto_job_store:read_json(pluto_job_store:file(Id, ?JOB_FILE_PACKAGE)),
    Local = maps:get(<<"host_id">>, maps:get(<<"origin">>, P, #{}), null) =:= Host,
    case {is_done(P), Local, Sched} of
        {true, _, _} -> insert(P);
        %% Only jobs started here may ever run here.
        {false, false, _} ->
            save(P#{<<"sched">> := ?SCHED_LOST, <<"done">> => true,
                    <<"sched_reason">> => <<"not_started_on_this_host">>});
        %% Env values exist only in memory; running without them could do
        %% something else than what was asked.
        {false, true, ?SCHED_QUEUED} ->
            case pluto_job_store:opt(<<"env_keys">>, P, []) of
                [] -> insert(P);
                _  -> save(P#{<<"sched">> := ?SCHED_START_FAILED,
                              <<"sched_reason">> => <<"env_lost_in_server_restart">>})
            end;
        %% The launcher died with the old server; give the runner a full
        %% grace period from now to show up.
        {false, true, ?SCHED_SPAWNING} ->
            insert(P#{<<"spawn_at">> => pluto_job_store:now_ms()});
        _ -> insert(P)
    end.

%% An import that replaced a package was interrupted between two renames.
recover_replaced() ->
    Dir = pluto_job_store:jobs_dir(),
    lists:foreach(fun(Old) ->
        Target = filename:rootname(Old, ?JOB_OLD_SUFFIX),
        case filelib:is_dir(Target) of
            true  -> file:del_dir_r(Old);
            false -> file:rename(Old, Target)
        end
    end, filelib:wildcard(filename:join(Dir, "*" ++ ?JOB_OLD_SUFFIX))).
