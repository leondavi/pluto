%%%-------------------------------------------------------------------
%%% @doc pluto_job_admit — Which queued jobs start now.
%%%
%%% Oldest first, a queued job starts when
%%%   - every job of its after_jobs has ended (a barrier); with after_ok,
%%%     one that did not succeed cancels it instead;
%%%   - there is room: max running jobs, and per_agent of one owner;
%%%   - its slot has fewer than slot_max running jobs (a counting
%%%     semaphore, shared by every agent of the server);
%%%   - its Pluto lock is granted, which is tried last, as it is taken.
%%%
%%% Followers (jobs with follows) are counted apart: one only reads the
%%% output of another job, and queueing it behind real work would leave
%%% that job unwatched exactly when the server is busy.
%%%
%%% Pure, apart from the two functions the caller passes.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_admit).

-include("pluto.hrl").

-export([select/2]).

-type ended() :: fun((job_id()) -> running | {ended, binary()}).
-type lock()  :: fun((pkg()) -> {ok, pkg()} | busy).
-type limits() :: #{max := pos_integer(), per_agent := pos_integer(),
                    ended := ended(), lock := lock()}.
-export_type([limits/0]).

%% @doc {jobs to start, jobs to cancel with the reason}. A started job is
%% the package as the lock function returned it.
-spec select([pkg()], limits()) -> {[pkg()], [{pkg(), binary()}]}.
select(Live, Limits) ->
    Running = [P || #{<<"sched">> := S} = P <- Live, S =/= ?SCHED_QUEUED],
    Queued = lists:sort(fun(A, B) -> opened(A) =< opened(B) end,
                        [P || #{<<"sched">> := ?SCHED_QUEUED} = P <- Live]),
    pick(Queued, Running, Limits, [], []).

pick([], _, _, Go, Cancel) ->
    {lists:reverse(Go), lists:reverse(Cancel)};
pick([P | T], Running, #{ended := Ended, lock := Lock} = L, Go, Cancel) ->
    case after_jobs(P, Ended) of
        wait -> pick(T, Running, L, Go, Cancel);
        {cancel, Why} -> pick(T, Running, L, Go, [{P, Why} | Cancel]);
        go ->
            case room(P, Running, L) andalso slot_free(P, Running) andalso Lock(P) of
                {ok, P1} -> pick(T, [P1 | Running], L, [P1 | Go], Cancel);
                _        -> pick(T, Running, L, Go, Cancel)
            end
    end.

after_jobs(P, Ended) ->
    States = [Ended(Id) || Id <- maps:get(<<"after_jobs">>, P, [])],
    Bad = [S || {ended, S} <- States, S =/= ?JOB_SUCCEEDED],
    %% Known to fail already: no need to wait for the others.
    case {maps:get(<<"after_ok">>, P, false), Bad, lists:member(running, States)} of
        {true, [_ | _], _} -> {cancel, <<"an_earlier_job_did_not_succeed">>};
        {_, _, true}       -> wait;
        _                  -> go
    end.

room(P, Running, #{max := Max, per_agent := Per}) ->
    Kind = is_follower(P),
    Owner = maps:get(<<"owner">>, P),
    Alike = [maps:get(<<"owner">>, X) || X <- Running, is_follower(X) =:= Kind],
    length(Alike) < Max andalso length([O || O <- Alike, O =:= Owner]) < Per.

slot_free(#{<<"slot">> := Slot} = P, Running) ->
    length([X || #{<<"slot">> := S} = X <- Running, S =:= Slot])
        < maps:get(<<"slot_max">>, P, 1);
slot_free(_, _) -> true.

is_follower(P) -> is_map_key(<<"follows">>, P).

opened(P) -> maps:get(<<"opened_ns">>, P, 0).
