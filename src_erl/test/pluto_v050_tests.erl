%%% Regression tests for v0.5.0 server fixes.
%%%
%%% 1. Agents restored from a persistence snapshot come back `disconnected`
%%%    and must time out through the normal grace period. Previously no grace
%%%    timer was armed for them, so they stayed `disconnected` for up to the
%%%    7-day stale cutoff, and direct sends to them were silently queued into
%%%    an inbox nobody would drain.
%%% 2. The periodic heartbeat reminder goes to TCP sessions only. Once
%%%    broadcasts started reaching HTTP agents (v0.5.0), a reminder queued
%%%    into every HTTP inbox would wake every idle MCP-backed agent.
-module(pluto_v050_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

-define(DIR, "/tmp/pluto/test_v050").
-define(TCP_PORT, 19051).
-define(HTTP_PORT, 19052).
-define(GRACE_MS, 300).
-define(REMINDER_MS, 300).

%%====================================================================
%% Fixtures
%%====================================================================

setup_env() ->
    application:set_env(pluto, persistence_dir, ?DIR),
    application:set_env(pluto, event_log_dir, ?DIR ++ "_events"),
    application:set_env(pluto, tcp_port, ?TCP_PORT),
    application:set_env(pluto, http_port, ?HTTP_PORT),
    application:set_env(pluto, reconnect_grace_ms, ?GRACE_MS),
    application:set_env(pluto, heartbeat_reminder_ms, ?REMINDER_MS),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms, 120000),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined).

teardown(_) ->
    application:stop(pluto),
    application:unset_env(pluto, reconnect_grace_ms),
    application:unset_env(pluto, heartbeat_reminder_ms),
    timer:sleep(100).

%% Write a snapshot holding one recently-seen agent, then boot the app so
%% pluto_persistence restores it.
restored_agent_setup() ->
    os:cmd("rm -rf " ++ ?DIR),
    ok = filelib:ensure_dir(filename:join(?DIR, "pluto.snapshot")),
    Now = erlang:system_time(millisecond),
    Agent = #agent{agent_id = <<"restored-ghost">>, session_id = <<"S-old">>,
                   status = connected, connected_at = Now - 1000,
                   attributes = #{}, last_seen = Now - 1000,
                   custom_status = <<"online">>, subscriptions = [],
                   session_type = http},
    Snapshot = #{locks => [], agents => [Agent], sessions => [],
                 waiters => [], fencing_seq => 0},
    ok = file:write_file(filename:join(?DIR, "pluto.snapshot"),
                         term_to_binary(Snapshot)),
    setup_env(),
    {ok, _} = application:ensure_all_started(pluto),
    ok.

plain_setup() ->
    os:cmd("rm -rf " ++ ?DIR),
    setup_env(),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(100),
    ok.

restored_agent_test_() ->
    {setup, fun restored_agent_setup/0, fun teardown/1,
     [{"restored agent times out after the grace period",
       fun t_restored_agent_times_out/0}]}.

reminder_test_() ->
    {setup, fun plain_setup/0, fun teardown/1,
     [{timeout, 10, {"heartbeat reminder reaches TCP but not HTTP agents",
                     fun t_reminder_tcp_only/0}}]}.

%%====================================================================
%% Tests
%%====================================================================

t_restored_agent_times_out() ->
    {ok, Before} = status_of(<<"restored-ghost">>),
    ?assertEqual(disconnected, Before),
    timer:sleep(?GRACE_MS * 3),
    {ok, After} = status_of(<<"restored-ghost">>),
    ?assertEqual(disconnected_timeout, After).

t_reminder_tcp_only() ->
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, ?TCP_PORT,
                                 [binary, {packet, line}, {active, false}], 2000),
    ok = gen_tcp:send(Sock, pluto_protocol_json:encode_line(
                              #{<<"op">> => <<"register">>,
                                <<"agent_id">> => <<"v050-tcp">>})),
    {ok, _} = gen_tcp:recv(Sock, 0, 2000),
    {ok, Reg} = http_post("/agents/register", #{<<"agent_id">> => <<"v050-http">>}),
    Token = maps:get(<<"token">>, Reg),

    %% The TCP agent sees a reminder within a few intervals.
    ?assert(saw_reminder(Sock, 15)),

    %% The HTTP agent's inbox holds no reminder.
    {ok, Peek} = http_get("/agents/peek?token=" ++ binary_to_list(Token)
                          ++ "&since_token=0"),
    Reminders = [M || M <- maps:get(<<"messages">>, Peek),
                      is_reminder(M)],
    ?assertEqual([], Reminders),
    gen_tcp:close(Sock).

%%====================================================================
%% Helpers
%%====================================================================

status_of(AgentId) ->
    case ets:lookup(?ETS_AGENTS, AgentId) of
        [#agent{status = S}] -> {ok, S};
        []                   -> {error, not_found}
    end.

is_reminder(#{<<"event">> := <<"broadcast">>,
              <<"payload">> := #{<<"type">> := <<"heartbeat_reminder">>}}) -> true;
is_reminder(_) -> false.

saw_reminder(_Sock, 0) -> false;
saw_reminder(Sock, N) ->
    case gen_tcp:recv(Sock, 0, 500) of
        {ok, Line} ->
            case pluto_protocol_json:decode(string:trim(Line)) of
                {ok, Msg} ->
                    is_reminder(Msg) orelse saw_reminder(Sock, N - 1);
                _ ->
                    saw_reminder(Sock, N - 1)
            end;
        {error, timeout} ->
            saw_reminder(Sock, N - 1);
        {error, _} ->
            false
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
