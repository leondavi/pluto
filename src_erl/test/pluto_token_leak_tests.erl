%%% Regression tests for the HTTP token-leak fix:
%%%
%%% Before the fix, pluto_msg_hub:unregister_agent/1 released the agent's
%%% NAME but left HTTP session tokens alive in ?ETS_HTTP_SESSIONS. An
%%% orphaned client could keep polling /agents/poll with the old token
%%% and drain inbox messages — including messages destined for whoever
%%% next claimed the same agent_id.
%%%
%%% The fix adds evict_http_sessions(AgentId) to the unregister handler
%%% and to the grace-expired handler, so unregister atomically invalidates
%%% all access tokens for that agent.
-module(pluto_token_leak_tests).
-include_lib("eunit/include/eunit.hrl").

setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_token_leak"),
    application:set_env(pluto, event_log_dir,   "/tmp/pluto/test_token_leak_ev"),
    application:set_env(pluto, signal_dir,      "/tmp/pluto/test_token_leak_sig"),
    application:set_env(pluto, tcp_port,  19091),
    application:set_env(pluto, http_port, 19092),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(200),
    19092.

teardown(_HttpPort) ->
    application:stop(pluto),
    timer:sleep(100),
    ok.

token_leak_test_() ->
    {setup, fun setup/0, fun teardown/1,
     fun(HttpPort) ->
         [
          {"old token cannot peek inbox after unregister_agent",
           fun() -> t_old_token_peek_after_unregister(HttpPort) end},
          {"old token cannot drain messages destined for new owner",
           fun() -> t_old_token_cannot_steal_from_new_owner(HttpPort) end},
          {"old token cannot ack inbox after unregister_agent",
           fun() -> t_old_token_ack_after_unregister(HttpPort) end}
         ]
     end}.

%%====================================================================
%% Tests
%%====================================================================

%% Bug repro: register agent, observe valid token, unregister, the same
%% token must no longer authenticate against /agents/peek.
t_old_token_peek_after_unregister(HttpPort) ->
    {ok, _, OldToken} = register_http(HttpPort, <<"victim-1">>),
    %% Sanity: the token works while the agent is alive.
    {ok, R0} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(OldToken)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R0)),
    %% Force the agent through the leaky unregister path that callers
    %% like admin-disconnect, TCP cleanup, and TCP heartbeat sweep use.
    pluto_msg_hub:unregister_agent(<<"victim-1">>),
    timer:sleep(100),
    %% Old token must now resolve to session_not_found.
    {ok, R1} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(OldToken)),
    ?assertNotEqual(<<"ok">>, maps:get(<<"status">>, R1, <<"error">>)).

%% Critical scenario: old client must not be able to drain messages that
%% arrive for whoever next claims the same agent_id.
t_old_token_cannot_steal_from_new_owner(HttpPort) ->
    %% Original owner registers, then is unregistered.
    {ok, _, OldToken} = register_http(HttpPort, <<"victim-2">>),
    pluto_msg_hub:unregister_agent(<<"victim-2">>),
    timer:sleep(100),
    %% Someone else claims the same name. With strict policy and a
    %% released name this succeeds with the original id.
    {ok, NewId, NewToken} = register_http(HttpPort, <<"victim-2">>),
    ?assertEqual(<<"victim-2">>, NewId),
    %% A third party sends a message addressed to the new owner.
    {ok, _, TxToken} = register_http(HttpPort, <<"sender-2">>),
    send(HttpPort, TxToken, <<"victim-2">>, #{<<"text">> => <<"for-new-owner">>}),
    timer:sleep(50),
    %% Old token must not be able to read it.
    {ok, ROld} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(OldToken)),
    ?assertNotEqual(<<"ok">>, maps:get(<<"status">>, ROld, <<"error">>)),
    %% New token sees the message intact.
    {ok, RNew} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(NewToken)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RNew)),
    ?assertEqual(1, maps:get(<<"count">>, RNew)).

%% Old token must not be able to ack (drain) inbox either.
t_old_token_ack_after_unregister(HttpPort) ->
    {ok, _, OldToken} = register_http(HttpPort, <<"victim-3">>),
    pluto_msg_hub:unregister_agent(<<"victim-3">>),
    timer:sleep(100),
    {ok, R} = http_post(HttpPort, "/agents/ack",
                        #{<<"token">>     => OldToken,
                          <<"up_to_seq">> => 1}),
    ?assertNotEqual(<<"ok">>, maps:get(<<"status">>, R, <<"error">>)).

%%====================================================================
%% Helpers — same shape as pluto_v0243_inbox_tests
%%====================================================================

register_http(HttpPort, AgentId) ->
    Body = #{<<"agent_id">> => AgentId, <<"mode">> => <<"http">>},
    {ok, #{<<"status">> := <<"ok">>,
           <<"agent_id">> := FinalId,
           <<"token">> := Token}} =
        http_post(HttpPort, "/agents/register", Body),
    {ok, FinalId, Token}.

send(HttpPort, Token, To, Payload) ->
    http_post(HttpPort, "/agents/send",
              #{<<"token">> => Token,
                <<"to">>    => To,
                <<"payload">> => Payload}).

http_post(HttpPort, Path, Body) ->
    JsonBody = pluto_protocol_json:encode(Body),
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, HttpPort,
                                 [binary, {packet, http_bin}, {active, false}], 2000),
    Req = [<<"POST ">>, list_to_binary(Path), <<" HTTP/1.1\r\n",
           "Host: localhost\r\n",
           "Content-Type: application/json\r\n",
           "Content-Length: ">>, integer_to_binary(byte_size(JsonBody)),
           <<"\r\nConnection: close\r\n\r\n">>, JsonBody],
    inet:setopts(Sock, [{packet, raw}]),
    gen_tcp:send(Sock, Req),
    inet:setopts(Sock, [{packet, http_bin}]),
    Result = read_response(Sock),
    gen_tcp:close(Sock),
    Result.

http_get(HttpPort, Path) ->
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, HttpPort,
                                 [binary, {packet, http_bin}, {active, false}], 2000),
    Req = [<<"GET ">>, list_to_binary(Path),
           <<" HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n">>],
    inet:setopts(Sock, [{packet, raw}]),
    gen_tcp:send(Sock, Req),
    inet:setopts(Sock, [{packet, http_bin}]),
    Result = read_response(Sock),
    gen_tcp:close(Sock),
    Result.

read_response(Sock) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_response, _, _Status, _}} ->
            Len = drain_headers(Sock, undefined),
            inet:setopts(Sock, [{packet, raw}]),
            read_body(Sock, Len);
        {error, Reason} ->
            {error, Reason}
    end.

drain_headers(Sock, Len) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, http_eoh} -> Len;
        {ok, {http_header, _, 'Content-Length', _, V}} ->
            drain_headers(Sock, binary_to_integer(V));
        {ok, {http_header, _, _, _, _}} ->
            drain_headers(Sock, Len);
        {error, _} -> Len
    end.

read_body(Sock, undefined) ->
    case gen_tcp:recv(Sock, 0, 2000) of
        {ok, Data} -> pluto_protocol_json:decode(Data);
        {error, _} -> {ok, #{}}
    end;
read_body(_Sock, 0) -> {ok, #{}};
read_body(Sock, Len) ->
    case gen_tcp:recv(Sock, Len, 5000) of
        {ok, Data} -> pluto_protocol_json:decode(Data);
        {error, _} -> {ok, #{}}
    end.
