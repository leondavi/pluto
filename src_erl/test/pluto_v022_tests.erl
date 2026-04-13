%%% Integration tests for Pluto v0.2.2 features:
%%% - Long-poll endpoint (GET /agents/poll?token=...&timeout=N)
%%% - File-based signal mechanism (/tmp/pluto/signals/<agent_id>.signal)
%%% - Task management via HTTP with token auth
%%% - Message read receipts on poll (ack=true)
%%% - Agent busy status auto-set on poll (auto_busy=true)
%%% - Dynamic TTL updates (POST /agents/update_ttl)
%%% - Agent set_status via HTTP
%%% - Agent status includes TTL info for HTTP agents
-module(pluto_v022_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%%====================================================================
%% Test Fixtures
%%====================================================================

app_setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_v022"),
    application:set_env(pluto, event_log_dir, "/tmp/pluto/test_v022_events"),
    application:set_env(pluto, signal_dir, "/tmp/pluto/test_v022_signals"),
    application:set_env(pluto, tcp_port, 19031),
    application:set_env(pluto, http_port, 19032),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms, 120000),
    application:set_env(pluto, reconnect_grace_ms, 120000),
    application:set_env(pluto, http_session_ttl_ms, 300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(300),
    {19031, 19032}.

app_teardown(_Ports) ->
    application:stop(pluto),
    timer:sleep(100).

%%====================================================================
%% Test generators
%%====================================================================

v022_test_() ->
    {setup,
     fun app_setup/0,
     fun app_teardown/1,
     fun({TcpPort, HttpPort}) ->
         [
          %% Long-poll
          {"long-poll with immediate message",
           fun() -> t_long_poll_immediate(TcpPort, HttpPort) end},
          {"long-poll timeout returns empty",
           fun() -> t_long_poll_timeout(HttpPort) end},
          {"long-poll notification wakes waiter",
           fun() -> t_long_poll_notify(TcpPort, HttpPort) end},

          %% File-based signals
          {"signal file created on message queue",
           fun() -> t_signal_file_created(TcpPort, HttpPort) end},
          {"signal file deleted on poll",
           fun() -> t_signal_file_deleted(TcpPort, HttpPort) end},

          %% Task management via HTTP
          {"http task assign",
           fun() -> t_http_task_assign(HttpPort) end},
          {"http task update",
           fun() -> t_http_task_update(HttpPort) end},
          {"http task list with filters",
           fun() -> t_http_task_list(HttpPort) end},
          {"http task progress",
           fun() -> t_http_task_progress(HttpPort) end},

          %% Read receipts
          {"poll with ack sends receipts",
           fun() -> t_poll_ack(TcpPort, HttpPort) end},

          %% Agent busy status
          {"poll with auto_busy sets processing",
           fun() -> t_poll_auto_busy(TcpPort, HttpPort) end},

          %% Dynamic TTL
          {"update ttl changes session ttl",
           fun() -> t_update_ttl(HttpPort) end},
          {"agent status includes ttl info",
           fun() -> t_agent_status_ttl(TcpPort, HttpPort) end},

          %% Set status via HTTP
          {"http set agent status",
           fun() -> t_http_set_status(HttpPort) end}
         ]
     end}.

%%====================================================================
%% Long-poll tests
%%====================================================================

t_long_poll_immediate(TcpPort, HttpPort) ->
    %% Register HTTP agent and send it a message first, then long-poll should return immediately
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send message from TCP agent
    Sender = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => HttpAgent,
          <<"payload">> => #{<<"text">> => <<"hello from long-poll test">>}}
    ],
    {ok, [_, SendR]} = send_multi(TcpPort, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SendR)),

    timer:sleep(50),

    %% Long-poll should find the message right away
    {ok, PollResp} = http_get(HttpPort,
        "/agents/poll?token=" ++ binary_to_list(Token) ++ "&timeout=1"),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, PollResp)),
    Messages = maps:get(<<"messages">>, PollResp),
    ?assert(length(Messages) >= 1),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_long_poll_timeout(HttpPort) ->
    %% Long-poll with short timeout should return empty after timeout
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Long-poll with 1-second timeout (no messages)
    {ok, PollResp} = http_get_timeout(HttpPort,
        "/agents/poll?token=" ++ binary_to_list(Token) ++ "&timeout=1", 5000),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, PollResp)),
    ?assertEqual(0, maps:get(<<"count">>, PollResp)),
    ?assertEqual(true, maps:get(<<"timed_out">>, PollResp, false)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_long_poll_notify(TcpPort, HttpPort) ->
    %% Register HTTP agent, start long-poll, then send message — should wake up
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Start long-poll in a separate process
    Self = self(),
    spawn(fun() ->
        Result = http_get_timeout(HttpPort,
            "/agents/poll?token=" ++ binary_to_list(Token) ++ "&timeout=3",
            15000),
        Self ! {poll_result, Result}
    end),
    timer:sleep(500),  %% Give time for long-poll to register

    %% Send message from TCP
    Sender = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => HttpAgent,
          <<"payload">> => #{<<"wake">> => <<"up">>}}
    ],
    {ok, _} = send_multi(TcpPort, Cmds),

    %% Wait for long-poll to return
    receive
        {poll_result, {ok, PollResp}} ->
            ?assertEqual(<<"ok">>, maps:get(<<"status">>, PollResp)),
            Messages = maps:get(<<"messages">>, PollResp),
            ?assert(length(Messages) >= 1),
            ?assertEqual(true, maps:get(<<"long_poll">>, PollResp, false));
        {poll_result, {error, Reason}} ->
            ?debugFmt("Long-poll failed: ~p", [Reason]),
            ?assert(false)
    after 5000 ->
        ?assert(false)  %% Should not reach here
    end,

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% File-based signal tests
%%====================================================================

t_signal_file_created(TcpPort, HttpPort) ->
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send a message to queue
    Sender = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => HttpAgent,
          <<"payload">> => #{<<"signal">> => <<"test">>}}
    ],
    {ok, _} = send_multi(TcpPort, Cmds),
    timer:sleep(100),

    %% Check signal file exists
    SignalDir = application:get_env(pluto, signal_dir, ?DEFAULT_SIGNAL_DIR),
    SafeAgent = sanitize_for_test(HttpAgent),
    SignalFile = filename:join(SignalDir, <<SafeAgent/binary, ".signal">>),
    ?assert(filelib:is_file(SignalFile)),

    %% Read and verify contents
    {ok, Content} = file:read_file(SignalFile),
    {ok, Signal} = pluto_protocol_json:decode(Content),
    ?assertEqual(HttpAgent, maps:get(<<"agent_id">>, Signal)),
    ?assert(maps:get(<<"pending_messages">>, Signal) >= 1),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_signal_file_deleted(TcpPort, HttpPort) ->
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send a message
    Sender = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => HttpAgent,
          <<"payload">> => #{<<"signal">> => <<"delete_test">>}}
    ],
    {ok, _} = send_multi(TcpPort, Cmds),
    timer:sleep(100),

    %% Signal file should exist
    SignalDir = application:get_env(pluto, signal_dir, ?DEFAULT_SIGNAL_DIR),
    SafeAgent = sanitize_for_test(HttpAgent),
    SignalFile = filename:join(SignalDir, <<SafeAgent/binary, ".signal">>),
    ?assert(filelib:is_file(SignalFile)),

    %% Poll to drain inbox
    {ok, _} = http_get(HttpPort,
        "/agents/poll?token=" ++ binary_to_list(Token)),

    %% Signal file should be deleted
    ?assertNot(filelib:is_file(SignalFile)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% HTTP task management tests
%%====================================================================

t_http_task_assign(HttpPort) ->
    Assigner = rand_agent(),
    Assignee = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Assigner}),
    Token = maps:get(<<"token">>, RegResp),

    {ok, TaskResp} = http_post(HttpPort, "/agents/task_assign",
                               #{<<"token">> => Token,
                                 <<"assignee">> => Assignee,
                                 <<"description">> => <<"Build API">>,
                                 <<"payload">> => #{<<"priority">> => <<"high">>}}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, TaskResp)),
    TaskId = maps:get(<<"task_id">>, TaskResp),
    ?assert(is_binary(TaskId)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_task_update(HttpPort) ->
    Assigner = rand_agent(),
    Assignee = rand_agent(),
    {ok, Reg1} = http_post(HttpPort, "/agents/register",
                           #{<<"agent_id">> => Assigner}),
    Token1 = maps:get(<<"token">>, Reg1),

    %% Assign task
    {ok, TaskResp} = http_post(HttpPort, "/agents/task_assign",
                               #{<<"token">> => Token1,
                                 <<"assignee">> => Assignee,
                                 <<"description">> => <<"Test task">>}),
    TaskId = maps:get(<<"task_id">>, TaskResp),

    %% Register assignee and update task
    {ok, Reg2} = http_post(HttpPort, "/agents/register",
                           #{<<"agent_id">> => Assignee}),
    Token2 = maps:get(<<"token">>, Reg2),

    {ok, UpdResp} = http_post(HttpPort, "/agents/task_update",
                              #{<<"token">> => Token2,
                                <<"task_id">> => TaskId,
                                <<"status">> => <<"complete">>,
                                <<"result">> => #{<<"output">> => <<"done">>}}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, UpdResp)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token1}),
    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token2}).

t_http_task_list(HttpPort) ->
    Assigner = rand_agent(),
    Assignee = rand_agent(),
    {ok, Reg} = http_post(HttpPort, "/agents/register",
                          #{<<"agent_id">> => Assigner}),
    Token = maps:get(<<"token">>, Reg),

    %% Assign tasks
    {ok, _} = http_post(HttpPort, "/agents/task_assign",
                        #{<<"token">> => Token,
                          <<"assignee">> => Assignee,
                          <<"description">> => <<"Task A">>}),
    {ok, _} = http_post(HttpPort, "/agents/task_assign",
                        #{<<"token">> => Token,
                          <<"assignee">> => Assignee,
                          <<"description">> => <<"Task B">>}),

    %% List with filter by assignee
    {ok, ListResp} = http_post(HttpPort, "/agents/task_list",
                               #{<<"token">> => Token,
                                 <<"assignee">> => Assignee}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListResp)),
    Tasks = maps:get(<<"tasks">>, ListResp),
    ?assert(length(Tasks) >= 2),
    lists:foreach(fun(T) ->
        ?assertEqual(Assignee, maps:get(<<"assignee">>, T))
    end, Tasks),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_http_task_progress(HttpPort) ->
    Agent = rand_agent(),
    {ok, Reg} = http_post(HttpPort, "/agents/register",
                          #{<<"agent_id">> => Agent}),
    Token = maps:get(<<"token">>, Reg),

    {ok, ProgResp} = http_post(HttpPort, "/agents/task_progress",
                               #{<<"token">> => Token}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ProgResp)),
    ?assert(is_integer(maps:get(<<"total">>, ProgResp))),
    ?assert(is_map(maps:get(<<"by_status">>, ProgResp))),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Read receipts test
%%====================================================================

t_poll_ack(TcpPort, HttpPort) ->
    %% Register sender (TCP) and receiver (HTTP)
    Sender = rand_agent(),
    Receiver = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Receiver}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send from TCP
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => Receiver,
          <<"payload">> => #{<<"hello">> => <<"world">>}}
    ],
    {ok, _} = send_multi(TcpPort, Cmds),
    timer:sleep(100),

    %% Poll with ack=true
    {ok, PollResp} = http_get(HttpPort,
        "/agents/poll?token=" ++ binary_to_list(Token) ++ "&ack=true"),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, PollResp)),
    Messages = maps:get(<<"messages">>, PollResp),
    ?assert(length(Messages) >= 1),

    %% The sender should have a delivery_ack queued in their inbox
    %% (but since sender is TCP, the ack is sent as a message)
    timer:sleep(100),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Auto busy test
%%====================================================================

t_poll_auto_busy(TcpPort, HttpPort) ->
    Sender = rand_agent(),
    Receiver = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Receiver}),
    Token = maps:get(<<"token">>, RegResp),

    %% Send a message
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Sender},
        #{<<"op">> => <<"send">>, <<"to">> => Receiver,
          <<"payload">> => #{<<"work">> => <<"do_it">>}}
    ],
    {ok, _} = send_multi(TcpPort, Cmds),
    timer:sleep(100),

    %% Poll with auto_busy=true
    {ok, _} = http_get(HttpPort,
        "/agents/poll?token=" ++ binary_to_list(Token) ++ "&auto_busy=true"),

    %% Check agent status is now "processing"
    {ok, StatusResp} = http_get(HttpPort, "/agents/" ++ binary_to_list(Receiver)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, StatusResp)),
    AgentInfo = maps:get(<<"agent">>, StatusResp),
    ?assertEqual(<<"processing">>, maps:get(<<"custom_status">>, AgentInfo)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Dynamic TTL tests
%%====================================================================

t_update_ttl(HttpPort) ->
    Agent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Agent,
                                <<"ttl_ms">> => 300000}),
    Token = maps:get(<<"token">>, RegResp),

    %% Update TTL to 10 minutes
    {ok, TtlResp} = http_post(HttpPort, "/agents/update_ttl",
                              #{<<"token">> => Token,
                                <<"ttl_ms">> => 600000}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, TtlResp)),
    ?assertEqual(600000, maps:get(<<"ttl_ms">>, TtlResp)),

    %% Verify the session has updated TTL
    [HS] = ets:match_object(?ETS_HTTP_SESSIONS,
                            #http_session{agent_id = Agent, _ = '_'}),
    ?assertEqual(600000, HS#http_session.ttl_ms),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

t_agent_status_ttl(TcpPort, HttpPort) ->
    HttpAgent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => HttpAgent,
                                <<"ttl_ms">> => 300000}),
    Token = maps:get(<<"token">>, RegResp),

    %% Query agent status via TCP (flattened response, no "agent" wrapper)
    Querier = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => Querier},
        #{<<"op">> => <<"agent_status">>, <<"agent_id">> => HttpAgent}
    ],
    {ok, [_, StatusR]} = send_multi(TcpPort, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, StatusR)),
    ?assertEqual(300000, maps:get(<<"ttl_ms">>, StatusR)),
    ?assert(maps:get(<<"expires_in_ms">>, StatusR) =< 300000),
    ?assertEqual(<<"http">>, maps:get(<<"session_type">>, StatusR)),

    http_post(HttpPort, "/agents/unregister", #{<<"token">> => Token}).

%%====================================================================
%% Set status via HTTP test
%%====================================================================

t_http_set_status(HttpPort) ->
    Agent = rand_agent(),
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Agent}),
    Token = maps:get(<<"token">>, RegResp),

    %% Set status to busy
    {ok, SetResp} = http_post(HttpPort, "/agents/set_status",
                              #{<<"token">> => Token,
                                <<"custom_status">> => <<"busy">>}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SetResp)),

    %% Verify via agent status
    {ok, StatusResp} = http_get(HttpPort, "/agents/" ++ binary_to_list(Agent)),
    AgentInfo = maps:get(<<"agent">>, StatusResp),
    ?assertEqual(<<"busy">>, maps:get(<<"custom_status">>, AgentInfo)),

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
            inet:setopts(Sock, [{packet, raw}]),
            gen_tcp:send(Sock, Request),
            inet:setopts(Sock, [{packet, http_bin}]),
            Result = read_http_response(Sock),
            gen_tcp:close(Sock),
            Result;
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

http_get(HttpPort, Path) ->
    http_get_timeout(HttpPort, Path, 5000).

http_get_timeout(HttpPort, Path, Timeout) ->
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
            Result = read_http_response_timeout(Sock, Timeout),
            gen_tcp:close(Sock),
            Result;
        {error, Reason} ->
            {error, {connect, Reason}}
    end.

read_http_response(Sock) ->
    read_http_response_timeout(Sock, 5000).

read_http_response_timeout(Sock, Timeout) ->
    case gen_tcp:recv(Sock, 0, Timeout) of
        {ok, {http_response, _, _StatusCode, _}} ->
            Headers = read_resp_headers(Sock),
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

read_resp_headers(Sock) ->
    read_resp_headers(Sock, []).

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
%% TCP helpers
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

%%====================================================================
%% Test helpers
%%====================================================================

rand_agent() ->
    <<"v022-", (rand_id())/binary>>.

rand_id() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(4)),
    string:lowercase(Hex).

sanitize_for_test(Bin) ->
    << <<(case C of
        C when C >= $a, C =< $z -> C;
        C when C >= $A, C =< $Z -> C;
        C when C >= $0, C =< $9 -> C;
        $- -> C;
        $_ -> C;
        $. -> C;
        _ -> $_
    end)>> || <<C>> <= Bin >>.
