%%%-------------------------------------------------------------------
%%% @doc pluto_job_import — Moving packages between hosts, and pruning.
%%%
%%% The archive work is done by priv/pluto_job_package.py, which
%%% validates and rewrites everything. Nothing here ever starts a job.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_import).

-include("pluto.hrl").

-export([export/2, unpack/2, adopt/3, prune/2, sweep/0]).

-import(pluto_job_store, [opt/3, now_ms/0]).

%% @doc Write a .pjob. Runs in the caller.
-spec export(pkg(), binary() | undefined) -> reply().
export(#{<<"id">> := Id} = P, Path) ->
    Dest = case Path of
               undefined ->
                   Name = [binary_to_list(Id), "-",
                           integer_to_list(now_ms()), "-",
                           integer_to_list(erlang:unique_integer([positive])), ".pjob"],
                   filename:join([pluto_job_store:home(), ?HOME_DIR_EXPORTS,
                                  lists:flatten(Name)]);
               _ -> Path
           end,
    case filename:pathtype(Dest) of
        absolute ->
            Tmp = staging() ++ ".json",
            ok = filelib:ensure_dir(Dest),
            ok = pluto_job_store:write_json(Tmp, maps:without(?JOB_LOCAL_KEYS, P)),
            try helper(["export", pluto_job_store:dir(Id), Tmp, Dest])
            after file:delete(Tmp)
            end;
        _ -> {error, <<"path_must_be_absolute">>}
    end.

%% @doc Validate an archive into a fresh staging directory and pass that
%% directory to Fun; it is removed afterwards unless Fun renamed it.
-spec unpack(binary(), fun((file:filename()) -> Result)) -> Result | {error, binary()}.
unpack(Path, Fun) ->
    Staging = staging(),
    ok = file:make_dir(Staging),
    ok = file:change_mode(Staging, ?MODE_DIR),
    try helper(["import", Path, Staging]) of
        {ok, _} -> Fun(Staging);
        E       -> E
    after file:del_dir_r(Staging)
    end.

staging() ->
    Base = filename:join(pluto_job_store:home(), ?HOME_DIR_STAGING),
    ok = filelib:ensure_path(Base),
    _ = file:change_mode(Base, ?MODE_DIR),
    filename:join(Base, binary_to_list(
                          pluto_job_store:hex(crypto:strong_rand_bytes(8)))).

%% @doc Remove what interrupted imports and exports left behind.
-spec sweep() -> ok.
sweep() ->
    _ = file:del_dir_r(filename:join(pluto_job_store:home(), ?HOME_DIR_STAGING)),
    ok.

helper(Args) ->
    case pluto_job_store:run(?JOB_PACKAGE_SCRIPT, Args, <<>>, ?JOB_PACKAGE_MS) of
        {ok, _, Out} ->
            case pluto_protocol_json:decode(Out) of
                {ok, #{<<"error">> := E}} -> {error, E};
                {ok, M} when is_map(M)    -> {ok, M};
                _ -> {error, <<"package_helper_failed">>}
            end;
        {error, _} -> {error, <<"package_helper_failed">>}
    end.

%% @doc Manager only. Register a validated staging directory.
%%
%%   not here yet      install
%%   same or older     nothing to do
%%   longer, imported  replace; notes made here were merged into staging
%%   longer, ours      refuse: the original is on this host
%%   anything else     refuse
-spec adopt(file:filename(), agent_id(), binary()) ->
    {ok, job_id(), binary()} | {error, binary()}.
adopt(Staging, Agent, User) ->
    {ok, #{<<"id">> := Id} = New} =
        pluto_job_store:read_json(filename:join(Staging, ?JOB_FILE_PACKAGE)),
    Dir = pluto_job_store:dir(Id),
    case pluto_job_registry:pkg(Id) of
        {error, ?ERR_UNKNOWN_JOB} ->
            case filelib:is_file(Dir) of
                true  -> {error, <<"job_directory_already_exists">>};
                false -> install(Staging, Dir, New, Agent, User), {ok, Id, <<"imported">>}
            end;
        {ok, Old} ->
            case helper(["merge", Dir, Staging]) of
                {ok, #{<<"result">> := <<"extends">>}} ->
                    case is_ours(Old) of
                        true -> {error, <<"this_host_originated_the_job">>};
                        false ->
                            replace(Staging, Dir, New, maps:get(<<"owner">>, Old), User),
                            {ok, Id, <<"extended">>}
                    end;
                {ok, #{<<"result">> := <<"diverged">>}} -> {error, <<"diverged">>};
                {ok, #{<<"result">> := Same}}           -> {ok, Id, Same};
                E -> E
            end;
        E -> E
    end.

is_ours(P) ->
    maps:get(<<"host_id">>, opt(<<"origin">>, P, #{}), null) =:= pluto_job_store:host_id().

%% The finished package.json is in place before the directory becomes
%% visible under jobs/, so no crash can expose foreign scheduling data.
install(Staging, Dir, New, Agent, User) ->
    {ok, St} = pluto_job_store:read_json(filename:join(Staging, ?JOB_FILE_STATE)),
    Sched = case is_map_key(<<"final_size">>, St) of
                true  -> ?SCHED_IMPORTED;
                false -> ?SCHED_SNAPSHOT
            end,
    P = New#{<<"sched">> => Sched, <<"sched_reason">> => null, <<"done">> => true,
             <<"owner">> => Agent,
             <<"imported">> => #{<<"at">> => now_ms(), <<"by">> => Agent,
                                 <<"user">> => User,
                                 <<"owner_at_origin">> => maps:get(<<"owner">>, New)}},
    case pluto_job_store:write_json(filename:join(Staging, ?JOB_FILE_PACKAGE), P, sync) of
        ok -> ok;
        {error, R} -> throw({io, R})
    end,
    case file:rename(Staging, Dir) of
        ok -> ok;
        {error, R2} -> throw({io, R2})
    end,
    pluto_job_registry:save(P),
    pluto_job_notice:history(<<"imported">>, P,
        #{<<"state">> => maps:get(<<"state">>, pluto_job_registry:state(P))}),
    pluto_job_notice:event(job_imported, P, <<"imported">>).

replace(Staging, Dir, New, Owner, User) ->
    Old = Dir ++ ?JOB_OLD_SUFFIX,          %% recovered at boot if we die here
    _ = file:del_dir_r(Old),
    case file:rename(Dir, Old) of
        ok -> ok;
        {error, R} -> throw({io, R})
    end,
    try install(Staging, Dir, New, Owner, User)
    catch C:E ->
        _ = file:del_dir_r(Dir),
        _ = file:rename(Old, Dir),
        erlang:raise(C, E, [])
    end,
    _ = file:del_dir_r(Old),
    ok.

%% @doc Manager only. Delete finished jobs that ended before BeforeMs.
-spec prune(integer(), boolean()) -> [job_id()].
prune(BeforeMs, Dry) ->
    Old = [P || P <- pluto_job_registry:all(), pluto_job_registry:is_done(P),
                opt(<<"ended_at">>, pluto_job_registry:state(P),
                    maps:get(<<"opened_at">>, P)) < BeforeMs],
    lists:foreach(fun(#{<<"id">> := Id} = P) when not Dry ->
                          pluto_job_notice:history(<<"pruned">>, P,
                              #{<<"state">> => maps:get(<<"state">>,
                                                        pluto_job_registry:state(P))}),
                          _ = file:del_dir_r(pluto_job_store:dir(Id)),
                          pluto_job_registry:forget(Id);
                     (_) -> ok
                  end, Old),
    [maps:get(<<"id">>, P) || P <- Old].
