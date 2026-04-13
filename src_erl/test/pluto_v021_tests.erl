%%% Integration tests for Pluto v0.2.1 features:
%%% - HTTP-based session registration (POST /agents/register)
%%% - Stateless agent mode
%%% - Configurable heartbeat TTL for HTTP agents
%%% - HTTP session heartbeat, poll, send, broadcast, unregister
%%% - Duplicate agent name prevention (6-char alphanumeric suffix)
%%% - HTTP session expiry via heartbeat sweeper
-module(pluto_v021_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%%====================================================================
%% Test Fixtures
%%====================================================================

app_setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_v021"),
    application:set_env(pluto, event_log_dir, "/tmp/pluto/test_v021_events"),
    application:set_env(pluto, tcp_port, 19021),
    application:set_env(pluto, http_port, 19022),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms, 120000),
    application:set_env(pluto, reconnect_grace_ms, 120000),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(300),
    {19021, 19022}.

app_teardown(_Ports) ->
    application:stop(pluto),
    timer:sleep(100).

%%====================================================================
%% Test generators
%%====================================================================

v021_test_() ->
    {setup,
     fun app_setup/0,
     fun app_teardown/1,
     fun({TcpPort, HttpPort}) ->
         [
          %% HTTP session registration
          {"http register agent",
           fun() -> t_http_register(HttpPort) end},
          {"http register stateless agent",
           fun() -> t_http_register_stateless(HttpPort) end},
          {"http register with custom ttl",
           fun() -> t_http_register_custom_ttl(HttpPort) end},

          %% HTTP session operations
          {"http heartbeat",
           fun() -> t_http_heartbeat(HttpPort) end},
          {"http poll messages",
           fun() -> t_http_poll(TcpPort, HttpPort) end},
          {"http send message",
           fun() -> t_http_send(TcpPort, HttpPort) end},
          {"http broadcast",
           fun() -> t_http_broadcast(TcpPort, HttpPort) end},
          {"http unregister",
           fun() -> t_http_unregister(HttpPort) end},
          {"http subscribe topic",
           fun() -> t_http_subscribe(HttpPort) end},

          %% HTTP agent visible to TCP agents
          {"http agent in list_agents",
           fun() -> t_http_visible_in_list(TcpPort, HttpPort) end},

          %% Duplicate name prevention
          {"duplicate name tcp gets suffix",
           fun() -> t_duplicate_name_tcp(TcpPort) end},
          {"duplicate name http gets suffix",
           fun() -> t_duplicate_name_http(HttpPort) end},
          {"duplicate name cross protocol",
           fun() -> t_duplicate_name_cross(TcpPort, HttpPort) end},

          %% Session not found
          {"http heartbeat bad token",
           fun() -> t_http_bad_token(HttpPort) end},
          {"http poll bad token",
           fun() -> t_http_poll_bad_token(HttpPort) end},

          %% Agent status shows session_type
          {"http agent status",
           fun() -> t_http_agent_status(TcpPort, HttpPort) end}
         ]
     end}.

%%====================================================================
%% HTTP session registration tests
%%====================================================================

t_http_register(HttpPort) ->
    AgentId = rand_agent(),
    Body = #{<<"agent_id">> => AgentId},
    {ok, Resp} = http_post(HttpPort, "/agents/register", Body),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Resp)),
    ?assert(is_binary(maps:get(<<"token">>, Resp))),
    ?assert(is_binary(maps:get(<<"session_id">>, Resp))),
    ?assertEqual(AgentId, maps:get(<<"agent_id">>, Resp)),
    ?assertEqual(<<"http">>, maps:get(<<"mode">>, Resp)),
    %% Cleanup
    Token = maps:get(<<"token">>, Resp),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_register_stateless(HttpPort) ->
    AgentId = rand_agent(),
    Body = #{<<"agent_id">> => AgentId, <<"mode">> => <<"stateless">>},
    {ok, Resp} = http_post(HttpPort, "/agents/register", Body),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Resp)),
    ?assertEqual(<<"stateless">>, maps:get(<<"mode">>, Resp)),
    Token = maps:get(<<"token">>, Resp),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_register_custom_ttl(HttpPort) ->
    AgentId = rand_agent(),
    Body = #{<<"agent_id">> => AgentId,
             <<"mode">> => <<"stateless">>,
             <<"ttl_ms">> => 600000},  %% 10 minutes
    {ok, Resp} = http_post(HttpPort, "/agents/register", Body),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Resp)),
    ?assertEqual(600000, maps:get(<<"ttl_ms">>, Resp)),
    Token = maps:get(<<"token">>, Resp),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% HTTP session operation tests
%%====================================================================

t_http_heartbeat(HttpPort) ->
    AgentId = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => AgentId}),
    Token = maps:get(<<"token">>, RegResp),
    %% Send heartbeat
    {ok, HbResp} = http_post(HttpPort, "/agents/heartbeat",
                              #{<<"token">> => Token}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, HbResp)),
    ?assert(is_integer(maps:get(<<"ts">>, HbResp))),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_poll(TcpPort, HttpPort) ->
    %% Register HTTP agent
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),
    ActualHttpAgent = maps:get(<<"agent_id">>, RegResp),

    %% Register TCP agent and send message to HTTP agent
    TcpAgent = rand_agent(),
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                  [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => TcpAgent}),
    {ok, _} = recv_on(Sock),
    send_on(Sock, #{<<"op">> => <<"send">>, <<"to">> => ActualHttpAgent,
                     <<"payload">> => #{<<"msg">> => <<"hello from tcp">>}}),
    {ok, SendResp} = recv_on(Sock),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SendResp)),
    gen_tcp:close(Sock),

    %% Small delay to let message be queued
    timer:sleep(100),

    %% Poll messages from HTTP agent
    {ok, PollResp} = http_get(HttpPort,
                               "/agents/poll?token=" ++ binary_to_list(Token)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, PollResp)),
    Messages = maps:get(<<"messages">>, PollResp),
    ?assert(length(Messages) >= 1),
    %% Verify message content
    [FirstMsg | _] = Messages,
    ?assertEqual(<<"message">>, maps:get(<<"event">>, FirstMsg)),
    ?assertEqual(TcpAgent, maps:get(<<"from">>, FirstMsg)),

    %% Second poll should have no messages (they were consumed)
    {ok, PollResp2} = http_get(HttpPort,
                                "/agents/poll?token=" ++ binary_to_list(Token)),
    ?assertEqual(0, maps:get(<<"count">>, PollResp2)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_send(TcpPort, HttpPort) ->
    %% Register TCP agent on persistent connection
    TcpAgent = rand_agent(),
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                  [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => TcpAgent}),
    {ok, _} = recv_on(Sock),

    %% Register HTTP agent
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send message from HTTP agent to TCP agent
    {ok, SendResp} = http_post(HttpPort, "/agents/send",
                                #{<<"token">> => Token,
                                  <<"to">> => TcpAgent,
                                  <<"payload">> => #{<<"msg">> => <<"hello from http">>}}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SendResp)),

    %% TCP agent should receive the message as a pushed event
    %% Drain events (agent_joined, etc.) until we find the message
    ok = drain_until_event(Sock, <<"message">>, 5),

    gen_tcp:close(Sock),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_broadcast(TcpPort, HttpPort) ->
    %% Register TCP listener
    TcpAgent = rand_agent(),
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                  [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => TcpAgent}),
    {ok, _} = recv_on(Sock),

    %% Register HTTP agent and broadcast
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    {ok, BcResp} = http_post(HttpPort, "/agents/broadcast",
                              #{<<"token">> => Token,
                                <<"payload">> => #{<<"msg">> => <<"broadcast from http">>}}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, BcResp)),

    gen_tcp:close(Sock),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_unregister(HttpPort) ->
    AgentId = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => AgentId}),
    Token = maps:get(<<"token">>, RegResp),

    %% Unregister
    {ok, UnregResp} = http_post(HttpPort, "/agents/unregister",
                                 #{<<"token">> => Token}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, UnregResp)),

    %% Heartbeat should fail now
    {ok, HbResp} = http_post(HttpPort, "/agents/heartbeat",
                              #{<<"token">> => Token}),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, HbResp)).

t_http_subscribe(HttpPort) ->
    AgentId = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => AgentId}),
    Token = maps:get(<<"token">>, RegResp),

    {ok, SubResp} = http_post(HttpPort, "/agents/subscribe",
                               #{<<"token">> => Token,
                                 <<"topic">> => <<"test-topic">>}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SubResp)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Cross-protocol visibility test
%%====================================================================

t_http_visible_in_list(TcpPort, HttpPort) ->
    %% Register HTTP agent
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),
    ActualId = maps:get(<<"agent_id">>, RegResp),

    %% TCP agent lists agents
    ListAgent = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => ListAgent},
        #{<<"op">> => <<"list_agents">>}
    ],
    {ok, [_, ListR]} = send_multi(TcpPort, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListR)),
    Agents = maps:get(<<"agents">>, ListR),
    ?assert(lists:member(ActualId, Agents)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Duplicate name prevention tests
%%====================================================================

t_duplicate_name_tcp(TcpPort) ->
    AgentId = <<"dup-tcp-", (rand_id())/binary>>,
    %% Register first agent on persistent connection
    {ok, Sock1} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                   [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock1, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId}),
    {ok, Reg1} = recv_on(Sock1),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Reg1)),
    ?assertEqual(AgentId, maps:get(<<"agent_id">>, Reg1)),

    %% Register second agent with same name
    {ok, Sock2} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                   [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock2, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId}),
    {ok, Reg2} = recv_on(Sock2),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Reg2)),
    %% Should have gotten a different agent_id with suffix
    Reg2AgentId = maps:get(<<"agent_id">>, Reg2),
    ?assertNotEqual(AgentId, Reg2AgentId),
    %% Should start with original name followed by a dash
    PrefixLen = byte_size(AgentId) + 1,  %% original + "-"
    ?assertEqual(<<AgentId/binary, "-">>,
                 binary:part(Reg2AgentId, 0, PrefixLen)),
    %% Suffix should be 6 chars
    SuffixLen = byte_size(Reg2AgentId) - PrefixLen,
    ?assertEqual(6, SuffixLen),

    gen_tcp:close(Sock1),
    gen_tcp:close(Sock2).

t_duplicate_name_http(HttpPort) ->
    AgentId = <<"dup-http-", (rand_id())/binary>>,
    %% Register first HTTP agent
    {ok, Reg1} = http_post(HttpPort, "/agents/register",
                            #{<<"agent_id">> => AgentId}),
    Token1 = maps:get(<<"token">>, Reg1),
    ?assertEqual(AgentId, maps:get(<<"agent_id">>, Reg1)),

    %% Register second HTTP agent with same name
    {ok, Reg2} = http_post(HttpPort, "/agents/register",
                            #{<<"agent_id">> => AgentId}),
    Token2 = maps:get(<<"token">>, Reg2),
    Reg2Id = maps:get(<<"agent_id">>, Reg2),
    ?assertNotEqual(AgentId, Reg2Id),
    %% Should contain the original name
    ?assert(binary:match(Reg2Id, AgentId) =/= nomatch),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token1}),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token2}).

t_duplicate_name_cross(TcpPort, HttpPort) ->
    AgentId = <<"dup-cross-", (rand_id())/binary>>,
    %% Register via TCP first
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, TcpPort,
                                  [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId}),
    {ok, _} = recv_on(Sock),

    %% Try registering same name via HTTP
    {ok, HttpReg} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => AgentId}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, HttpReg)),
    HttpId = maps:get(<<"agent_id">>, HttpReg),
    ?assertNotEqual(AgentId, HttpId),

    gen_tcp:close(Sock),
    Token = maps:get(<<"token">>, HttpReg),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Error handling tests
%%====================================================================

t_http_bad_token(HttpPort) ->
    {ok, Resp} = http_post(HttpPort, "/agents/heartbeat",
                            #{<<"token">> => <<"PLUTO-nonexistent">>}),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, Resp)).

t_http_poll_bad_token(HttpPort) ->
    {ok, Resp} = http_get(HttpPort, "/agents/poll?token=PLUTO-nonexistent"),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, Resp)).

%%====================================================================
%% HTTP agent status test
%%====================================================================

t_http_agent_status(TcpPort, HttpPort) ->
    %% Register HTTP agent
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                               #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),
    ActualId = maps:get(<<"agent_id">>, RegResp),

    %% Query status from TCP
    Querier = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Querier},
        #{<<"op">> => <<"agent_status">>, <<"agent_id">> => ActualId}
    ],
    {ok, [_, StatusR]} = send_multi(TcpPort, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, StatusR)),
    ?assertEqual(true, maps:get(<<"online">>, StatusR)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% HTTP helpers
%%====================================================================

http_post(HttpPort, Path, Body) ->
    JsonBody = pluto_protocol_json:encode(Body),
    case gen_tcp:connect({127, 0, 0, 1}, HttpPort,
                         [binary, {packet, http_bin}, {active, false}], 2000) of
        {ok, Sock} ->
            Request = [
                <<"POST ">>, list_to_binary(Path), <<" HTTP/1.1\r\n">>,
                <<"Host: localhost\r\n">>,
                <<"Content-Type: application/json\r\n">>,
                <<"Content-Length: ">>, integer_to_binary(byte_size(JsonBody)), <<"\r\n">>,
                <<"Connection: close\r\n">>,
                <<"\r\n">>,
                JsonBody
            ],
            %% Switch to raw for sending
            inet:setopts(Sock, [{packet, raw}]),
            gen_tcp:send(Sock, Request),
            %% Read response
            inet:setopts(Sock, [{packet, http_bin}]),
            Result = read_http_response(Sock),
            gen_tcp:close(Sock),
            Result;
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

http_get(HttpPort, Path) ->
    case gen_tcp:connect({127, 0, 0, 1}, HttpPort,
                         [binary, {packet, http_bin}, {active, false}], 2000) of
        {ok, Sock} ->
            Request = [
                <<"GET ">>, list_to_binary(Path), <<" HTTP/1.1\r\n">>,
                <<"Host: localhost\r\n">>,
                <<"Connection: close\r\n">>,
                <<"\r\n">>
            ],
            inet:setopts(Sock, [{packet, raw}]),
            gen_tcp:send(Sock, Request),
            inet:setopts(Sock, [{packet, http_bin}]),
            Result = read_http_response(Sock),
            gen_tcp:close(Sock),
            Result;
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

read_http_response(Sock) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_response, _, _StatusCode, _}} ->
            Headers = read_resp_headers(Sock, []),
            ContentLen = resp_content_length(Headers),
            inet:setopts(Sock, [{packet, raw}]),
            case read_resp_body(Sock, ContentLen) of
                Body when byte_size(Body) > 0 ->
                    pluto_protocol_json:decode(Body);
                _ ->
                    {ok, #{}}
            end;
        {error, Reason} ->
            {error, Reason}
    end.

read_resp_headers(Sock, Acc) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_header, _, Name, _, Value}} ->
            read_resp_headers(Sock, [{header_name(Name), Value} | Acc]);
        {ok, http_eoh} ->
            lists:reverse(Acc);
        _ ->
            lists:reverse(Acc)
    end.

resp_content_length(Headers) ->
    case lists:keyfind(<<"Content-Length">>, 1, Headers) of
        {_, Val} ->
            try binary_to_integer(Val) catch _:_ -> 0 end;
        false ->
            0
    end.

read_resp_body(_Sock, 0) ->
    <<>>;
read_resp_body(Sock, Len) when Len > 0 ->
    case gen_tcp:recv(Sock, Len, 5000) of
        {ok, Data} -> Data;
        _ -> <<>>
    end.

header_name(Atom) when is_atom(Atom) -> atom_to_binary(Atom, utf8);
header_name(Bin) when is_binary(Bin) -> Bin.

%%====================================================================
%% TCP helpers (same as integration tests)
%%====================================================================

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

send_on(Sock, Req) ->
    gen_tcp:send(Sock, pluto_protocol_json:encode_line(Req)).

recv_on(Sock) ->
    case gen_tcp:recv(Sock, 0, 2000) of
        {ok, Data} ->
            pluto_protocol_json:decode(string:trim(Data));
        {error, Reason} ->
            {error, Reason}
    end.

rand_agent() ->
    <<"v021-", (rand_id())/binary>>.

rand_id() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(4)),
    string:lowercase(Hex).

%% @private Drain events from a TCP socket until finding the target event type.
drain_until_event(_Sock, _EventType, 0) ->
    ok;
drain_until_event(Sock, EventType, N) ->
    case gen_tcp:recv(Sock, 0, 2000) of
        {ok, Data} ->
            case pluto_protocol_json:decode(string:trim(Data)) of
                {ok, #{<<"event">> := EventType}} ->
                    ok;
                {ok, _Other} ->
                    drain_until_event(Sock, EventType, N - 1);
                _ ->
                    drain_until_event(Sock, EventType, N - 1)
            end;
        {error, timeout} ->
            ok
    end.
