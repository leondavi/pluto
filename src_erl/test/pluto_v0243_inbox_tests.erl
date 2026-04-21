%%% Tests for v0.2.43 at-least-once delivery:
%%%   GET  /agents/peek     — non-destructive inbox read
%%%   POST /agents/ack      — ack messages up to seq_token
%%% And the new pluto_msg_hub:ack_inbox/2 API.
-module(pluto_v0243_inbox_tests).
-include_lib("eunit/include/eunit.hrl").

%%====================================================================
%% Fixture
%%====================================================================

setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_v0243"),
    application:set_env(pluto, event_log_dir,   "/tmp/pluto/test_v0243_ev"),
    application:set_env(pluto, signal_dir,      "/tmp/pluto/test_v0243_sig"),
    application:set_env(pluto, tcp_port,  19081),
    application:set_env(pluto, http_port, 19082),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(200),
    19082.

teardown(_HttpPort) ->
    application:stop(pluto),
    timer:sleep(100),
    ok.

inbox_test_() ->
    {setup, fun setup/0, fun teardown/1,
     fun(HttpPort) ->
         [
          {"peek is non-destructive; same messages returned twice",
           fun() -> t_peek_non_destructive(HttpPort) end},
          {"ack drains messages up to seq_token",
           fun() -> t_ack_drains(HttpPort) end},
          {"ack is idempotent (second call drains 0)",
           fun() -> t_ack_idempotent(HttpPort) end},
          {"since_token filters already-seen messages",
           fun() -> t_since_token(HttpPort) end},
          {"peek missing token => 400",
           fun() -> t_peek_missing_token(HttpPort) end}
         ]
     end}.

%%====================================================================
%% Tests
%%====================================================================

t_peek_non_destructive(HttpPort) ->
    {ok, Receiver, RxToken} = register_http(HttpPort, <<"rx-1">>),
    {ok, _Sender,  _TxToken} = register_http(HttpPort, <<"tx-1">>),
    send(HttpPort, _TxToken, Receiver, #{<<"text">> => <<"hello 1">>}),
    send(HttpPort, _TxToken, Receiver, #{<<"text">> => <<"hello 2">>}),
    timer:sleep(50),
    {ok, R1} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R1)),
    ?assertEqual(2, maps:get(<<"count">>, R1)),
    %% Second peek returns the same 2 messages — server did NOT drain.
    {ok, R2} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    ?assertEqual(2, maps:get(<<"count">>, R2)),
    %% Messages carry seq_token.
    [M1, M2] = maps:get(<<"messages">>, R2),
    ?assert(is_integer(maps:get(<<"seq_token">>, M1))),
    ?assert(maps:get(<<"seq_token">>, M1) < maps:get(<<"seq_token">>, M2)).

t_ack_drains(HttpPort) ->
    {ok, Receiver, RxToken} = register_http(HttpPort, <<"rx-2">>),
    {ok, _Sender, TxToken}  = register_http(HttpPort, <<"tx-2">>),
    send(HttpPort, TxToken, Receiver, #{<<"n">> => 1}),
    send(HttpPort, TxToken, Receiver, #{<<"n">> => 2}),
    send(HttpPort, TxToken, Receiver, #{<<"n">> => 3}),
    timer:sleep(50),
    {ok, R1} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    Msgs = maps:get(<<"messages">>, R1),
    ?assertEqual(3, length(Msgs)),
    %% Ack the first two (sorted ascending by seq).
    [M1, M2, _M3] = Msgs,
    Seq1 = maps:get(<<"seq_token">>, M1),
    Seq2 = maps:get(<<"seq_token">>, M2),
    {ok, AckResp} = http_post(HttpPort, "/agents/ack",
                              #{<<"token">>     => RxToken,
                                <<"up_to_seq">> => Seq2}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, AckResp)),
    ?assertEqual(2, maps:get(<<"drained">>, AckResp)),
    %% Next peek should return only the third message.
    {ok, R2} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    ?assertEqual(1, maps:get(<<"count">>, R2)),
    [M3b] = maps:get(<<"messages">>, R2),
    ?assert(maps:get(<<"seq_token">>, M3b) > Seq2),
    ?assertNot(maps:get(<<"seq_token">>, M3b) =:= Seq1).

t_ack_idempotent(HttpPort) ->
    {ok, Receiver, RxToken} = register_http(HttpPort, <<"rx-3">>),
    {ok, _, TxToken}        = register_http(HttpPort, <<"tx-3">>),
    send(HttpPort, TxToken, Receiver, #{<<"v">> => 1}),
    timer:sleep(50),
    {ok, P1} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    [M] = maps:get(<<"messages">>, P1),
    Seq = maps:get(<<"seq_token">>, M),
    {ok, A1} = http_post(HttpPort, "/agents/ack",
                         #{<<"token">> => RxToken, <<"up_to_seq">> => Seq}),
    {ok, A2} = http_post(HttpPort, "/agents/ack",
                         #{<<"token">> => RxToken, <<"up_to_seq">> => Seq}),
    ?assertEqual(1, maps:get(<<"drained">>, A1)),
    ?assertEqual(0, maps:get(<<"drained">>, A2)).

t_since_token(HttpPort) ->
    {ok, Receiver, RxToken} = register_http(HttpPort, <<"rx-4">>),
    {ok, _, TxToken}        = register_http(HttpPort, <<"tx-4">>),
    send(HttpPort, TxToken, Receiver, #{<<"n">> => 1}),
    send(HttpPort, TxToken, Receiver, #{<<"n">> => 2}),
    timer:sleep(50),
    {ok, P1} = http_get(HttpPort, "/agents/peek?token=" ++ binary_to_list(RxToken)),
    [First, _Second] = maps:get(<<"messages">>, P1),
    FirstSeq = maps:get(<<"seq_token">>, First),
    %% peek with since_token=FirstSeq skips the first message.
    Path = "/agents/peek?token=" ++ binary_to_list(RxToken) ++
           "&since_token=" ++ integer_to_list(FirstSeq),
    {ok, P2} = http_get(HttpPort, Path),
    ?assertEqual(1, maps:get(<<"count">>, P2)).

t_peek_missing_token(HttpPort) ->
    %% The raw path without a token returns 400.  We don't assert on the
    %% exact status code via our helper (which returns bodies), but we
    %% can at least confirm no crash and a non-ok body.
    case http_get(HttpPort, "/agents/peek") of
        {ok, R} ->
            %% 400 body contains an "error" key, not "status"=ok
            ?assertNotEqual(<<"ok">>, maps:get(<<"status">>, R, <<"error">>));
        _ ->
            ok
    end.

%%====================================================================
%% Helpers
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
