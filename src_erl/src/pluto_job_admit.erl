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
%%% Per lock resource and per slot the order is kept: once an older job
%%% that wants R is held back, younger ones that want R wait too, so readers
%%% coming one after another cannot starve a writer, nor an agent with a big
%%% slot_max one with a small. A slot's limit is the strictest slot_max
%%% among its running jobs and the one to start.
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
    pick(Queued, Running, Limits#{held => #{}}, [], []).

pick([], _, _, Go, Cancel) ->
    {lists:reverse(Go), lists:reverse(Cancel)};
pick([P | T], Running, #{ended := Ended, lock := Lock, held := Held} = L, Go, Cancel) ->
    case after_jobs(P, Ended) of
        {cancel, Why} -> pick(T, Running, L, Go, [{P, Why} | Cancel]);
        wait -> pick(T, Running, L, Go, Cancel);      %% may wait long: holds nothing
        go ->
            Behind = [K || K <- keys(P), is_map_key(K, Held)],
            case room(P, Running, L) of
                false -> pick(T, Running, L, Go, Cancel);   %% its owner's limit
                true when Behind =/= [] ->
                    pick(T, Running, L, Go, Cancel);         %% waits behind an older job
                true ->
                    case slot_free(P, Running) of
                        false -> pick(T, Running, hold(slot_key(P), L), Go, Cancel);
                        true ->
                            case Lock(P) of
                                {ok, P1} -> pick(T, [P1 | Running], L, [P1 | Go], Cancel);
                                busy     -> pick(T, Running, hold(lock_key(P), L), Go, Cancel)
                            end
                    end
            end
    end.

%% A job held back keeps only the resource that held it back for itself in
%% this pass (its full slot, or its busy lock): younger jobs that want that
%% resource wait behind it, so readers cannot starve a writer, while its other
%% resource stays free for others.
hold(Keys, #{held := H} = L) -> L#{held := maps:merge(H, maps:from_keys(Keys, true))}.

slot_key(P) -> [K || {<<"slot">>, _} = K <- keys(P)].
lock_key(P) -> [K || {<<"lock">>, _} = K <- keys(P)].

keys(P) -> [{K, V} || K <- [<<"lock">>, <<"slot">>], {ok, V} <- [maps:find(K, P)]].

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
    In = [X || #{<<"slot">> := S} = X <- Running, S =:= Slot],
    length(In) < lists:min([maps:get(<<"slot_max">>, X, 1) || X <- [P | In]]);
slot_free(_, _) -> true.

is_follower(P) -> is_map_key(<<"follows">>, P).

opened(P) -> maps:get(<<"opened_ns">>, P, 0).
