%%% Integration tests that exercise the full Pluto TCP protocol.
%%% These tests require the full application to be running, so they
%%% use setup/teardown fixtures to start/stop the app.
-module(pluto_integration_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%%====================================================================
%% Test Fixtures
%%====================================================================

%% Start the full application for integration tests.
app_setup() ->
    %% Use temp dirs so tests don't interfere with production state
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_state"),
    application:set_env(pluto, event_log_dir, "/tmp/pluto/test_events"),
    application:set_env(pluto, tcp_port, 19000),
    application:set_env(pluto, http_port, disabled),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms, 120000),
    application:set_env(pluto, reconnect_grace_ms, 120000),
    %% Open mode — no auth/ACL
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    %% Give processes time to init
    timer:sleep(200),
    19000.

app_teardown(_Port) ->
    application:stop(pluto),
    timer:sleep(100).

%%====================================================================
%% Test generators — grouped under the application fixture
%%====================================================================

integration_test_() ->
    {setup,
     fun app_setup/0,
     fun app_teardown/1,
     fun(Port) ->
         [
          {"ping",                      fun() -> t_ping(Port) end},
          {"register",                  fun() -> t_register(Port) end},
          {"register unique sessions",  fun() -> t_register_unique_sessions(Port) end},
          {"acquire and release",       fun() -> t_acquire_release(Port) end},
          {"fencing token monotonic",   fun() -> t_fencing_monotonic(Port) end},
          {"renew lock",                fun() -> t_renew(Port) end},
          {"lock conflict returns wait",fun() -> t_lock_conflict(Port) end},
          {"release unknown lock",      fun() -> t_release_not_found(Port) end},
          {"list agents",               fun() -> t_list_agents(Port) end},
          {"broadcast",                 fun() -> t_broadcast(Port) end},
          {"direct message",            fun() -> t_direct_message(Port) end},
          {"event history",             fun() -> t_event_history(Port) end},
          {"admin list locks",          fun() -> t_admin_list_locks(Port) end},
          {"admin fencing seq",         fun() -> t_admin_fencing_seq(Port) end},
          {"selftest via protocol",     fun() -> t_selftest(Port) end},
          {"unknown op returns error",  fun() -> t_unknown_op(Port) end},
          {"unregistered op denied",    fun() -> t_unregistered_op(Port) end},
          {"bad json returns error",    fun() -> t_bad_json(Port) end}
         ]
     end}.

%%====================================================================
%% Individual tests
%%====================================================================

t_ping(Port) ->
    {ok, R} = send_recv(Port, #{<<"op">> => <<"ping">>}),
    ?assertEqual(<<"pong">>, maps:get(<<"status">>, R)).

t_register(Port) ->
    AId = rand_agent(),
    {ok, R} = send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => AId}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R)),
    ?assert(is_binary(maps:get(<<"session_id">>, R))).

t_register_unique_sessions(Port) ->
    A1 = rand_agent(),
    A2 = rand_agent(),
    {ok, R1} = send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => A1}),
    {ok, R2} = send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => A2}),
    S1 = maps:get(<<"session_id">>, R1),
    S2 = maps:get(<<"session_id">>, R2),
    ?assertNotEqual(S1, S2).

t_acquire_release(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => <<"test:ar">>,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [RegR, AcqR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RegR)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, AcqR)),
    LockRef = maps:get(<<"lock_ref">>, AcqR),
    ?assert(is_binary(LockRef)),
    ?assert(is_integer(maps:get(<<"fencing_token">>, AcqR))),
    %% Release on same session
    RelCmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"release">>, <<"lock_ref">> => LockRef}
    ],
    {ok, [_, RelR]} = send_multi(Port, RelCmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RelR)).

t_fencing_monotonic(Port) ->
    AId = rand_agent(),
    R1 = <<"test:fm1-", (rand_id())/binary>>,
    R2 = <<"test:fm2-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => R1,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000},
        #{<<"op">> => <<"acquire">>, <<"resource">> => R2,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [_, Acq1, Acq2]} = send_multi(Port, Cmds),
    T1 = maps:get(<<"fencing_token">>, Acq1),
    T2 = maps:get(<<"fencing_token">>, Acq2),
    ?assert(T2 > T1).

t_renew(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => <<"test:renew">>,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [_, AcqR]} = send_multi(Port, Cmds),
    LockRef = maps:get(<<"lock_ref">>, AcqR),
    RenCmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"renew">>, <<"lock_ref">> => LockRef, <<"ttl_ms">> => 10000}
    ],
    {ok, [_, RenR]} = send_multi(Port, RenCmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RenR)).

t_lock_conflict(Port) ->
    A1 = rand_agent(),
    A2 = rand_agent(),
    Res = <<"test:conflict-", (rand_id())/binary>>,
    %% A1 acquires
    Cmds1 = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A1},
        #{<<"op">> => <<"acquire">>, <<"resource">> => Res,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [_, _]} = send_multi(Port, Cmds1),
    %% A2 tries the same resource — should wait
    Cmds2 = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A2},
        #{<<"op">> => <<"acquire">>, <<"resource">> => Res,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000,
          <<"max_wait_ms">> => 100}
    ],
    {ok, [_, Acq2]} = send_multi(Port, Cmds2),
    ?assertEqual(<<"wait">>, maps:get(<<"status">>, Acq2)).

t_release_not_found(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"release">>, <<"lock_ref">> => <<"LOCK-nonexistent">>}
    ],
    {ok, [_, RelR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, RelR)),
    ?assertEqual(<<"not_found">>, maps:get(<<"reason">>, RelR)).

t_list_agents(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"list_agents">>}
    ],
    {ok, [_, ListR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListR)),
    Agents = maps:get(<<"agents">>, ListR),
    ?assert(is_list(Agents)).

t_broadcast(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"broadcast">>, <<"payload">> => #{<<"msg">> => <<"hi">>}}
    ],
    {ok, [_, BcR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, BcR)).

t_direct_message(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    %% Register B first on its own connection
    send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => B}),
    %% Register A and send to B
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"send">>, <<"to">> => B,
          <<"payload">> => #{<<"msg">> => <<"hello">>}}
    ],
    {ok, [_, SendR]} = send_multi(Port, Cmds),
    %% Either ok (B still connected) or unknown_target (B's connection closed)
    Status = maps:get(<<"status">>, SendR),
    ?assert(Status =:= <<"ok">> orelse Status =:= <<"error">>).

t_event_history(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"event_history">>, <<"since_token">> => 0, <<"limit">> => 10}
    ],
    {ok, [_, EhR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, EhR)),
    ?assert(is_list(maps:get(<<"events">>, EhR))).

t_admin_list_locks(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"admin_list_locks">>}
    ],
    {ok, [_, R]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R)),
    ?assert(is_list(maps:get(<<"locks">>, R))).

t_admin_fencing_seq(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"admin_fencing_seq">>}
    ],
    {ok, [_, R]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R)),
    ?assert(is_integer(maps:get(<<"fencing_seq">>, R))).

t_selftest(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"selftest">>}
    ],
    {ok, [_, R]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R)),
    ?assertEqual(0, maps:get(<<"failed">>, R)).

t_unknown_op(Port) ->
    AId = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AId},
        #{<<"op">> => <<"nonexistent_op">>}
    ],
    {ok, [_, R]} = send_multi(Port, Cmds),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, R)),
    ?assertEqual(<<"unknown_op">>, maps:get(<<"reason">>, R)).

t_unregistered_op(Port) ->
    %% Without registering first, acquire should fail
    {ok, R} = send_recv(Port, #{<<"op">> => <<"acquire">>,
                                <<"resource">> => <<"x">>,
                                <<"mode">> => <<"write">>,
                                <<"ttl_ms">> => 1000}),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, R)),
    ?assertEqual(<<"not_registered">>, maps:get(<<"reason">>, R)).

t_bad_json(Port) ->
    case gen_tcp:connect({127, 0, 0, 1}, Port,
                         [binary, {packet, line}, {active, false}], 2000) of
        {ok, Sock} ->
            gen_tcp:send(Sock, <<"this is not json\n">>),
            case gen_tcp:recv(Sock, 0, 2000) of
                {ok, Data} ->
                    {ok, R} = pluto_protocol_json:decode(string:trim(Data)),
                    ?assertEqual(<<"error">>, maps:get(<<"status">>, R)),
                    ?assertEqual(<<"bad_request">>, maps:get(<<"reason">>, R));
                {error, _} ->
                    %% Server may have closed connection — acceptable
                    ok
            end,
            gen_tcp:close(Sock);
        {error, Reason} ->
            ?assert(false, io_lib:format("connect failed: ~p", [Reason]))
    end.

%%====================================================================
%% TCP helpers
%%====================================================================

send_recv(Port, Req) ->
    case gen_tcp:connect({127, 0, 0, 1}, Port,
                         [binary, {packet, line}, {active, false}], 2000) of
        {ok, Sock} ->
            Line = pluto_protocol_json:encode_line(Req),
            gen_tcp:send(Sock, Line),
            Result = case gen_tcp:recv(Sock, 0, 2000) of
                {ok, Data} ->
                    pluto_protocol_json:decode(string:trim(Data));
                {error, Reason} ->
                    {error, Reason}
            end,
            gen_tcp:close(Sock),
            Result;
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

send_multi(Port, Reqs) ->
    case gen_tcp:connect({127, 0, 0, 1}, Port,
                         [binary, {packet, line}, {active, false}], 2000) of
        {ok, Sock} ->
            lists:foreach(fun(Req) ->
                gen_tcp:send(Sock, pluto_protocol_json:encode_line(Req))
            end, Reqs),
            Responses = lists:map(fun(_) ->
                case gen_tcp:recv(Sock, 0, 2000) of
                    {ok, Data} ->
                        case pluto_protocol_json:decode(string:trim(Data)) of
                            {ok, Map} -> Map;
                            {error, _} -> #{<<"status">> => <<"decode_error">>}
                        end;
                    {error, _} ->
                        #{<<"status">> => <<"recv_error">>}
                end
            end, Reqs),
            gen_tcp:close(Sock),
            {ok, Responses};
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

%%====================================================================
%% Utilities
%%====================================================================

rand_agent() ->
    <<"test-", (rand_id())/binary>>.

rand_id() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(4)),
    string:lowercase(Hex).
