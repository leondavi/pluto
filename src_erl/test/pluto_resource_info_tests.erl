%%% Tests for the resource-introspection API added in v0.2.42:
%%%   pluto_lock_mgr:resource_info/1
%%%   pluto_lock_mgr:last_holder/1
%%%   pluto_lock_mgr:queue_length/1
-module(pluto_resource_info_tests).
-include_lib("eunit/include/eunit.hrl").

%%====================================================================
%% Fixture
%%====================================================================

setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_rinfo"),
    application:set_env(pluto, event_log_dir, "/tmp/pluto/test_rinfo_events"),
    application:set_env(pluto, signal_dir, "/tmp/pluto/test_rinfo_signals"),
    application:set_env(pluto, tcp_port, 19051),
    application:set_env(pluto, http_port, 19052),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(200),
    ok.

teardown(_) ->
    application:stop(pluto),
    timer:sleep(100),
    ok.

resource_info_test_() ->
    {setup, fun setup/0, fun teardown/1,
     [
      {"unknown resource returns empty info",
       fun t_unknown_resource/0},
      {"acquire populates current_holders, last_holder still null",
       fun t_current_holders/0},
      {"release populates last_holder with reason=released",
       fun t_last_holder_after_release/0},
      {"queue_length reflects pending waiters in FIFO order",
       fun t_queue_length/0}
     ]}.

%%====================================================================
%% Tests
%%====================================================================

t_unknown_resource() ->
    R = <<"resinfo/never-locked">>,
    Info = pluto_lock_mgr:resource_info(R),
    ?assertEqual(R, maps:get(resource, Info)),
    ?assertEqual([], maps:get(current_holders, Info)),
    ?assertEqual(null, maps:get(last_holder, Info)),
    ?assertEqual(0, maps:get(queue_length, Info)),
    ?assertEqual([], maps:get(queue, Info)),
    ?assertEqual(null, pluto_lock_mgr:last_holder(R)),
    ?assertEqual(0, pluto_lock_mgr:queue_length(R)).

t_current_holders() ->
    R = <<"resinfo/held">>,
    A = <<"agent-held-1">>,
    {ok, _LockRef, _FT} = pluto_lock_mgr:acquire(R, write, A, #{ttl_ms => 60000}),
    Info = pluto_lock_mgr:resource_info(R),
    Holders = maps:get(current_holders, Info),
    ?assertMatch([_], Holders),
    [H] = Holders,
    ?assertEqual(A, maps:get(agent_id, H)),
    ?assertEqual(<<"write">>, maps:get(mode, H)),
    ?assertEqual(null, maps:get(last_holder, Info)),
    ?assertEqual(0, maps:get(queue_length, Info)).

t_last_holder_after_release() ->
    R = <<"resinfo/released">>,
    A = <<"agent-released-1">>,
    {ok, LockRef, _FT} = pluto_lock_mgr:acquire(R, write, A, #{ttl_ms => 60000}),
    ok = pluto_lock_mgr:release(LockRef, A),
    Info = pluto_lock_mgr:resource_info(R),
    ?assertEqual([], maps:get(current_holders, Info)),
    Last = maps:get(last_holder, Info),
    ?assert(is_map(Last)),
    ?assertEqual(A, maps:get(agent_id, Last)),
    ?assertEqual(LockRef, maps:get(lock_ref, Last)),
    ?assertEqual(released, maps:get(reason, Last)),
    ?assert(is_integer(maps:get(released_at, Last))),
    %% helper API also returns the entry
    Last2 = pluto_lock_mgr:last_holder(R),
    ?assertEqual(A, maps:get(agent_id, Last2)).

t_queue_length() ->
    R = <<"resinfo/queued">>,
    Holder = <<"holder-1">>,
    {ok, _Ref, _FT} =
        pluto_lock_mgr:acquire(R, write, Holder, #{ttl_ms => 60000}),
    %% 3 waiters — use max_wait_ms so they enqueue instead of failing fast.
    WaiterOpts = #{ttl_ms => 60000, max_wait_ms => 60000},
    lists:foreach(
      fun(N) ->
          AId = <<"waiter-", (integer_to_binary(N))/binary>>,
          ?assertMatch({wait, _},
                       pluto_lock_mgr:acquire(R, write, AId, WaiterOpts))
      end,
      [1, 2, 3]),
    ?assertEqual(3, pluto_lock_mgr:queue_length(R)),
    Info = pluto_lock_mgr:resource_info(R),
    ?assertEqual(3, maps:get(queue_length, Info)),
    Q = maps:get(queue, Info),
    ?assertEqual(3, length(Q)),
    Ids = [maps:get(agent_id, W) || W <- Q],
    ?assertEqual([<<"waiter-1">>, <<"waiter-2">>, <<"waiter-3">>], Ids).
