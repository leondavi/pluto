%%% Which queued jobs start: limits, followers, slots, after_jobs, locks.
-module(pluto_job_admit_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

job(N, Owner, Sched) -> job(N, Owner, Sched, #{}).
job(N, Owner, Sched, More) ->
    maps:merge(#{<<"id">> => N, <<"owner">> => Owner, <<"sched">> => Sched,
                 <<"opened_ns">> => N}, More).

limits(Max, Per) -> limits(Max, Per, #{}).
limits(Max, Per, Ended) ->
    #{max => Max, per_agent => Per,
      ended => fun(Id) -> maps:get(Id, Ended, running) end,
      lock => fun(P) -> {ok, P} end}.

ids({Go, _}) -> [maps:get(<<"id">>, P) || P <- Go].

respects_limits_and_age_test() ->
    Live = [job(4, b, ?SCHED_QUEUED), job(1, a, ?SCHED_SPAWNED), job(2, a, ?SCHED_QUEUED),
            job(3, a, ?SCHED_QUEUED), job(5, c, ?SCHED_QUEUED)],
    ?assertEqual([2, 3, 4, 5], ids(pluto_job_admit:select(Live, limits(9, 9)))),
    ?assertEqual([2, 3], ids(pluto_job_admit:select(Live, limits(3, 9)))),
    ?assertEqual([2, 4, 5], ids(pluto_job_admit:select(Live, limits(9, 2)))).

followers_are_counted_apart_test() ->
    F = #{<<"follows">> => <<"x">>},
    Live = [job(1, a, ?SCHED_SPAWNED), job(2, a, ?SCHED_SPAWNED),
            job(3, a, ?SCHED_QUEUED), job(4, a, ?SCHED_QUEUED, F),
            job(5, a, ?SCHED_QUEUED, F), job(6, a, ?SCHED_SPAWNED, F)],
    %% Two real jobs fill the agent's share; followers have their own.
    ?assertEqual([4], ids(pluto_job_admit:select(Live, limits(9, 2)))),
    ?assertEqual([3, 4, 5], ids(pluto_job_admit:select(Live, limits(9, 3)))).

a_slot_is_a_semaphore_over_all_agents_test() ->
    G = fun(N) -> #{<<"slot">> => <<"gpu">>, <<"slot_max">> => N} end,
    Live = [job(1, a, ?SCHED_SPAWNED, G(2)), job(2, b, ?SCHED_QUEUED, G(2)),
            job(3, c, ?SCHED_QUEUED, G(2)), job(4, c, ?SCHED_QUEUED),
            job(5, d, ?SCHED_QUEUED, #{<<"slot">> => <<"gpu">>})],
    ?assertEqual([2, 4], ids(pluto_job_admit:select(Live, limits(9, 9)))),
    %% Without slot_max a slot is a mutex: one already runs.
    ?assertEqual([], ids(pluto_job_admit:select([job(1, a, ?SCHED_SPAWNED, G(1)),
                                                 job(5, d, ?SCHED_QUEUED,
                                                     #{<<"slot">> => <<"gpu">>})],
                                                limits(9, 9)))).

after_jobs_is_a_barrier_test() ->
    A = fun(Deps, Ok) -> #{<<"after_jobs">> => Deps, <<"after_ok">> => Ok} end,
    Live = [job(10, a, ?SCHED_QUEUED, A([1, 2], false)),
            job(11, a, ?SCHED_QUEUED, A([1], true)),
            job(12, a, ?SCHED_QUEUED, A([3], true))],
    Ended = #{1 => {ended, ?JOB_SUCCEEDED}, 3 => {ended, <<"failed">>}},
    {Go, Cancel} = pluto_job_admit:select(Live, limits(9, 9, Ended)),
    ?assertEqual([11], [maps:get(<<"id">>, P) || P <- Go]),
    ?assertMatch([{#{<<"id">> := 12}, <<"an_earlier_job_did_not_succeed">>}], Cancel),
    %% Once 2 has ended, failed or not, 10 starts.
    All = Ended#{2 => {ended, <<"failed">>}},
    ?assertEqual([10, 11], ids(pluto_job_admit:select(lists:sublist(Live, 2),
                                                      limits(9, 9, All)))).

a_busy_lock_keeps_the_job_queued_without_using_room_test() ->
    Live = [job(1, a, ?SCHED_QUEUED, #{<<"lock">> => <<"db">>}), job(2, a, ?SCHED_QUEUED)],
    L = (limits(9, 1))#{lock := fun(#{<<"lock">> := _}) -> busy; (P) -> {ok, P} end},
    ?assertEqual([2], ids(pluto_job_admit:select(Live, L))),
    Taken = (limits(9, 9))#{lock := fun(P) -> {ok, P#{<<"lock_ref">> => r}} end},
    {[First | _], []} = pluto_job_admit:select(Live, Taken),
    ?assertEqual(r, maps:get(<<"lock_ref">>, First)).

readers_do_not_starve_an_older_writer_test() ->
    R = fun(M) -> #{<<"lock">> => <<"db">>, <<"lock_mode">> => M} end,
    Live = [job(1, a, ?SCHED_SPAWNED, R(<<"read">>)), job(2, b, ?SCHED_QUEUED, R(<<"write">>)),
            job(3, c, ?SCHED_QUEUED, R(<<"read">>)), job(4, c, ?SCHED_QUEUED)],
    %% A reader runs, so the writer is busy; the younger reader would get in.
    L = (limits(9, 9))#{lock := fun(#{<<"lock_mode">> := <<"write">>}) -> busy;
                                   (P) -> {ok, P} end},
    ?assertEqual([4], ids(pluto_job_admit:select(Live, L))),
    %% A writer still waiting for its after_jobs holds nothing back.
    W = job(2, b, ?SCHED_QUEUED, (R(<<"write">>))#{<<"after_jobs">> => [9]}),
    ?assertEqual([3, 4], ids(pluto_job_admit:select([W | tl(tl(Live))], L))).

the_strictest_slot_max_counts_test() ->
    G = fun(N) -> #{<<"slot">> => <<"cpu">>, <<"slot_max">> => N} end,
    Live = [job(1, a, ?SCHED_SPAWNED, G(8)), job(2, a, ?SCHED_SPAWNED, G(8)),
            job(3, b, ?SCHED_QUEUED, G(2)), job(4, a, ?SCHED_QUEUED, G(8))],
    %% b allows 2 at once: 2 run, so neither b's job nor (in this pass) a's.
    ?assertEqual([], ids(pluto_job_admit:select(Live, limits(9, 9)))),
    ?assertEqual([3], ids(pluto_job_admit:select([job(1, a, ?SCHED_SPAWNED, G(8)),
                                                   job(3, b, ?SCHED_QUEUED, G(2))],
                                                  limits(9, 9)))).

a_job_held_by_its_own_limit_keeps_no_place_test() ->
    G = #{<<"slot">> => <<"cpu">>, <<"slot_max">> => 4},
    Live = [job(1, a, ?SCHED_SPAWNED), job(2, a, ?SCHED_QUEUED, G), job(3, b, ?SCHED_QUEUED, G)],
    ?assertEqual([3], ids(pluto_job_admit:select(Live, limits(9, 1)))).

a_job_waiting_for_its_lock_leaves_its_slot_free_test() ->
    %% b's older job wants a busy lock and the gpu slot; the slot has room, so
    %% a's younger job in the same slot must not wait behind it (that could
    %% deadlock with whoever holds the lock).
    Live = [job(1, b, ?SCHED_QUEUED, #{<<"slot">> => <<"gpu">>, <<"slot_max">> => 2,
                                         <<"lock">> => <<"file:/data">>}),
            job(2, a, ?SCHED_QUEUED, #{<<"slot">> => <<"gpu">>, <<"slot_max">> => 2})],
    L = (limits(9, 9))#{lock := fun(#{<<"lock">> := _}) -> busy; (P) -> {ok, P} end},
    ?assertEqual([2], ids(pluto_job_admit:select(Live, L))).

a_job_held_by_a_full_slot_leaves_its_lock_free_test() ->
    %% b's older job waits for a full slot; a's younger job with the same lock
    %% but no slot may take the lock.
    G = #{<<"slot">> => <<"cpu">>, <<"slot_max">> => 1},
    Live = [job(1, c, ?SCHED_SPAWNED, G),
            job(2, b, ?SCHED_QUEUED, G#{<<"lock">> => <<"db">>}),
            job(3, a, ?SCHED_QUEUED, #{<<"lock">> => <<"db">>})],
    ?assertEqual([3], ids(pluto_job_admit:select(Live, limits(9, 9)))).
