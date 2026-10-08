%%% Regression tests for v0.5.1 server fixes.
%%%
%%% 1. A queued lock request from an HTTP agent (no session process) was
%%%    silently dropped when the lock freed: the waiter was deleted, no lock
%%%    was created and no lock_granted event was sent. The grant must now be
%%%    created and queued in the agent's inbox.
%%% 2. A queued grant always got a 30 s lease, ignoring the waiter's ttl_ms.
%%% 3. wait_timeout must reach HTTP waiters too.
-module(pluto_v051_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

-define(DIR, "/tmp/pluto/test_v051").
-define(TCP_PORT, 19061).
-define(HTTP_PORT, 19062).

setup() ->
    os:cmd("rm -rf " ++ ?DIR),
    application:set_env(pluto, persistence_dir, ?DIR),
    application:set_env(pluto, event_log_dir, ?DIR ++ "_events"),
    application:set_env(pluto, tcp_port, ?TCP_PORT),
    application:set_env(pluto, http_port, ?HTTP_PORT),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(100),
    ok.

teardown(_) ->
    application:stop(pluto),
    timer:sleep(100).

v051_test_() ->
    {setup, fun setup/0, fun teardown/1,
     [{timeout, 15, {"queued HTTP grant is created and delivered",
                     fun t_http_queued_grant_delivered/0}},
      {timeout, 15, {"queued grant uses the waiter's ttl_ms",
                     fun t_queued_grant_keeps_ttl/0}},
      {timeout, 15, {"wait_timeout reaches HTTP waiters",
                     fun t_http_wait_timeout_delivered/0}}]}.

%%====================================================================
%% Tests
%%====================================================================

t_http_queued_grant_delivered() ->
    _ = register_http(<<"v051-a">>),
    TokB = register_http(<<"v051-b">>),
    {ok, #{<<"status">> := <<"ok">>, <<"lock_ref">> := RefA,
           <<"fencing_token">> := FA}} =
        acquire(<<"v051-a">>, <<"res-grant">>, 30000, undefined),
    {ok, #{<<"status">> := <<"wait">>, <<"wait_ref">> := WaitRef}} =
        acquire(<<"v051-b">>, <<"res-grant">>, 30000, undefined),
    {ok, _} = http_post("/locks/release", #{<<"lock_ref">> => RefA,
                                            <<"agent_id">> => <<"v051-a">>}),
    Grant = await_event(TokB, <<"lock_granted">>, 20),
    ?assertEqual(WaitRef, maps:get(<<"wait_ref">>, Grant)),
    ?assert(maps:get(<<"fencing_token">>, Grant) > FA),
    %% The lock really exists and belongs to B.
    [#lock{agent_id = Holder}] = ets:lookup(?ETS_LOCKS, maps:get(<<"lock_ref">>, Grant)),
    ?assertEqual(<<"v051-b">>, Holder).

t_queued_grant_keeps_ttl() ->
    _ = register_http(<<"v051-c">>),
    TokD = register_http(<<"v051-d">>),
    {ok, #{<<"lock_ref">> := RefC}} = acquire(<<"v051-c">>, <<"res-ttl">>, 30000, undefined),
    {ok, #{<<"status">> := <<"wait">>}} = acquire(<<"v051-d">>, <<"res-ttl">>, 4000, undefined),
    {ok, _} = http_post("/locks/release", #{<<"lock_ref">> => RefC,
                                            <<"agent_id">> => <<"v051-c">>}),
    Grant = await_event(TokD, <<"lock_granted">>, 20),
    [#lock{expires_at = Exp, inserted_at = Ins}] =
        ets:lookup(?ETS_LOCKS, maps:get(<<"lock_ref">>, Grant)),
    ?assert(Exp - Ins =< 4000),
    ?assert(Exp - Ins > 3000).

t_http_wait_timeout_delivered() ->
    _ = register_http(<<"v051-e">>),
    TokF = register_http(<<"v051-f">>),
    {ok, #{<<"status">> := <<"ok">>}} = acquire(<<"v051-e">>, <<"res-timeout">>, 30000, undefined),
    {ok, #{<<"status">> := <<"wait">>, <<"wait_ref">> := WaitRef}} =
        acquire(<<"v051-f">>, <<"res-timeout">>, 30000, 200),
    %% The waiter sweep runs every 5 s.
    Timeout = await_event(TokF, <<"wait_timeout">>, 40),
    ?assertEqual(WaitRef, maps:get(<<"wait_ref">>, Timeout)).

%%====================================================================
%% Helpers
%%====================================================================

register_http(AgentId) ->
    {ok, Reg} = http_post("/agents/register", #{<<"agent_id">> => AgentId}),
    maps:get(<<"token">>, Reg).

acquire(AgentId, Resource, TtlMs, MaxWait) ->
    Base = #{<<"agent_id">> => AgentId, <<"resource">> => Resource,
             <<"mode">> => <<"write">>, <<"ttl_ms">> => TtlMs},
    Body = case MaxWait of
               undefined -> Base;
               _ -> Base#{<<"max_wait_ms">> => MaxWait}
           end,
    http_post("/locks/acquire", Body).

%% Peek the agent's inbox until an event of the given type shows up.
await_event(_Token, Event, 0) ->
    error({event_not_delivered, Event});
await_event(Token, Event, N) ->
    {ok, Peek} = http_get("/agents/peek?token=" ++ binary_to_list(Token)
                          ++ "&since_token=0"),
    case [M || M <- maps:get(<<"messages">>, Peek, []),
               maps:get(<<"event">>, M, undefined) =:= Event] of
        [M | _] -> M;
        []      -> timer:sleep(250), await_event(Token, Event, N - 1)
    end.

http_post(Path, Body) ->
    http_request("POST", Path, pluto_protocol_json:encode(Body)).

http_get(Path) ->
    http_request("GET", Path, <<>>).

%% Minimal HTTP/1.1 client over gen_tcp (no inets dependency in tests).
http_request(Method, Path, Body) ->
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, ?HTTP_PORT,
                                 [binary, {packet, raw}, {active, false}], 2000),
    Req = [Method, " ", Path, " HTTP/1.1\r\nHost: localhost\r\n",
           "Content-Type: application/json\r\n",
           "Content-Length: ", integer_to_list(byte_size(Body)), "\r\n",
           "Connection: close\r\n\r\n", Body],
    ok = gen_tcp:send(Sock, Req),
    Resp = recv_all(Sock, <<>>),
    gen_tcp:close(Sock),
    [_Headers, RespBody] = binary:split(Resp, <<"\r\n\r\n">>),
    pluto_protocol_json:decode(RespBody).

recv_all(Sock, Acc) ->
    case gen_tcp:recv(Sock, 0, 2000) of
        {ok, Data}       -> recv_all(Sock, <<Acc/binary, Data/binary>>);
        {error, closed}  -> Acc;
        {error, timeout} -> Acc
    end.
