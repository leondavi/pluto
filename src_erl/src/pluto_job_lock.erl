%%%-------------------------------------------------------------------
%%% @doc pluto_job_lock — A Pluto lock that a job holds while it runs.
%%%
%%% The holder is job:<id>, not the job's owner: two jobs of one agent
%%% exclude each other, and an agent holding the resource through Pluto's
%%% lock API excludes the job, and the other way round. The lock is only
%%% tried (never queued), so the scheduler simply tries again on a later
%%% tick and a queued job never shows up in the deadlock graph. It is
%%% renewed while the job runs and released when it ends; a server that
%%% stops leaves it to expire. The command gets the fencing token as
%%% PLUTO_LOCK_TOKEN.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_lock).

-include("pluto.hrl").

-export([take/1, keep/1, drop/1, env/1, check/1]).

%% @doc The package with the lock it now holds; busy if someone else has it.
-spec take(pkg()) -> {ok, pkg()} | busy.
take(#{<<"lock">> := Raw} = P) ->
    {ok, Res} = pluto_resource:normalize(Raw),      %% check/1 let it in
    Opts = #{ttl_ms => ?JOB_LOCK_TTL_MS, session_id => <<"smartstreams">>},
    try pluto_lock_mgr:try_acquire(Res, mode(P), holder(P), Opts) of
        {ok, Ref, Token} -> {ok, P#{<<"lock_ref">> => Ref, <<"lock_token">> => Token}};
        _                -> busy
    catch exit:_ -> busy           %% the lock manager is restarting
    end;
take(P) -> {ok, P}.

%% @doc Renew the lock of a running job. One that was lost (the server
%% restarted, or it expired) is taken again if it is free, and otherwise
%% the package says lock_lost.
-spec keep(pkg()) -> pkg().
keep(#{<<"lock_ref">> := Ref} = P) ->
    case catch pluto_lock_mgr:renew(Ref, #{ttl_ms => ?JOB_LOCK_TTL_MS}) of
        ok -> P;
        _ ->
            case take(maps:without([<<"lock_ref">>, <<"lock_token">>], P)) of
                {ok, P1} -> P1;
                busy     -> (maps:without([<<"lock_ref">>, <<"lock_token">>], P))
                            #{<<"lock_lost">> => true}
            end
    end;
keep(P) -> P.

%% @doc Release whatever the job holds.
-spec drop(pkg()) -> pkg().
drop(#{<<"lock_ref">> := Ref} = P) ->
    catch pluto_lock_mgr:release(Ref, holder(P)),
    maps:without([<<"lock_ref">>, <<"lock_token">>], P);
drop(P) -> P.

%% @doc Environment for the command.
-spec env(pkg()) -> map().
env(#{<<"lock">> := Res, <<"lock_token">> := T}) ->
    #{<<"PLUTO_LOCK_RESOURCE">> => Res, <<"PLUTO_LOCK_TOKEN">> => integer_to_binary(T)};
env(_) -> #{}.

%% @doc ok, or why a start request's lock fields are wrong.
-spec check(map()) -> ok | {error, binary()}.
check(#{<<"lock">> := Res} = R) ->
    case {pluto_resource:normalize(Res), maps:get(<<"lock_mode">>, R, <<"write">>)} of
        {{error, _}, _}        -> {error, <<"bad_lock_resource">>};
        {_, M} when M =:= <<"write">>; M =:= <<"read">> -> ok;
        _                      -> {error, <<"lock_mode_is_read_or_write">>}
    end;
check(#{<<"lock_mode">> := _}) -> {error, <<"lock_mode_without_lock">>};
check(_) -> ok.

holder(#{<<"id">> := Id}) -> <<"job:", Id/binary>>.

mode(P) ->
    case maps:get(<<"lock_mode">>, P, <<"write">>) of
        <<"read">> -> read;
        _          -> write
    end.
