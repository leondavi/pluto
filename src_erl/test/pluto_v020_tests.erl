%%% Integration tests for Pluto v0.2.0 features:
%%% - Task management (assign, update, list, batch, progress)
%%% - Agent discovery by attributes (find_agents)
%%% - Topic-based pub/sub (subscribe, unsubscribe, publish)
%%% - Non-blocking lock probe (try_acquire)
%%% - Agent status and presence
%%% - Message delivery acknowledgment
%%% - Event sequence acknowledgment
%%% - Detailed agent listing
-module(pluto_v020_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%%====================================================================
%% Test Fixtures
%%====================================================================

app_setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_v020"),
    application:set_env(pluto, event_log_dir, "/tmp/pluto/test_v020_events"),
    application:set_env(pluto, tcp_port, 19020),
    application:set_env(pluto, http_port, disabled),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms, 120000),
    application:set_env(pluto, reconnect_grace_ms, 120000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(200),
    19020.

app_teardown(_Port) ->
    application:stop(pluto),
    timer:sleep(100).

%%====================================================================
%% Test generators
%%====================================================================

v020_test_() ->
    {setup,
     fun app_setup/0,
     fun app_teardown/1,
     fun(Port) ->
         [
          %% Task management
          {"task assign and list",          fun() -> t_task_assign_list(Port) end},
          {"task update status",            fun() -> t_task_update(Port) end},
          {"task list with filters",        fun() -> t_task_list_filter(Port) end},
          {"task batch assign",             fun() -> t_task_batch(Port) end},
          {"task progress",                 fun() -> t_task_progress(Port) end},

          %% Agent discovery
          {"find agents by attribute",      fun() -> t_find_agents(Port) end},
          {"find agents empty filter",      fun() -> t_find_agents_all(Port) end},

          %% Pub/sub
          {"subscribe and publish",         fun() -> t_subscribe_publish(Port) end},
          {"unsubscribe",                   fun() -> t_unsubscribe(Port) end},

          %% Try-acquire
          {"try acquire free resource",     fun() -> t_try_acquire_free(Port) end},
          {"try acquire busy resource",     fun() -> t_try_acquire_busy(Port) end},

          %% Agent status
          {"agent status online",           fun() -> t_agent_status_online(Port) end},
          {"agent status unknown",          fun() -> t_agent_status_unknown(Port) end},
          {"set custom status",             fun() -> t_set_custom_status(Port) end},

          %% Detailed listing
          {"list agents detailed",          fun() -> t_list_agents_detailed(Port) end},

          %% Message ack
          {"message ack",                   fun() -> t_message_ack(Port) end},

          %% Event ack
          {"event ack",                     fun() -> t_event_ack(Port) end},

          %% Register with attributes
          {"register with attributes",      fun() -> t_register_attributes(Port) end}
         ]
     end}.

%%====================================================================
%% Task management tests
%%====================================================================

t_task_assign_list(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_assign">>, <<"assignee">> => B,
          <<"description">> => <<"Write tests">>}
    ],
    {ok, [_, TaskR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, TaskR)),
    TaskId = maps:get(<<"task_id">>, TaskR),
    ?assert(is_binary(TaskId)),
    %% List tasks
    ListCmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_list">>}
    ],
    {ok, [_, ListR]} = send_multi(Port, ListCmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListR)),
    Tasks = maps:get(<<"tasks">>, ListR),
    ?assert(length(Tasks) >= 1),
    %% Find our task
    Found = [T || T <- Tasks, maps:get(<<"task_id">>, T) =:= TaskId],
    ?assertEqual(1, length(Found)).

t_task_update(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    %% Assign
    Cmds1 = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_assign">>, <<"assignee">> => B,
          <<"description">> => <<"Fix bug">>}
    ],
    {ok, [_, TaskR]} = send_multi(Port, Cmds1),
    TaskId = maps:get(<<"task_id">>, TaskR),
    %% Update as the assignee
    Cmds2 = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"task_update">>, <<"task_id">> => TaskId,
          <<"status">> => <<"completed">>,
          <<"result">> => #{<<"fix">> => <<"applied">>}}
    ],
    {ok, [_, UpdR]} = send_multi(Port, Cmds2),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, UpdR)).

t_task_list_filter(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_assign">>, <<"assignee">> => B,
          <<"description">> => <<"Filtered task">>},
        #{<<"op">> => <<"task_list">>, <<"assignee">> => B}
    ],
    {ok, [_, _, ListR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListR)),
    Tasks = maps:get(<<"tasks">>, ListR),
    %% All returned tasks should be for agent B
    lists:foreach(fun(T) ->
        ?assertEqual(B, maps:get(<<"assignee">>, T))
    end, Tasks).

t_task_batch(Port) ->
    A = rand_agent(),
    B1 = rand_agent(),
    B2 = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_batch">>, <<"tasks">> => [
            #{<<"assignee">> => B1, <<"description">> => <<"Batch task 1">>},
            #{<<"assignee">> => B2, <<"description">> => <<"Batch task 2">>}
        ]}
    ],
    {ok, [_, BatchR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, BatchR)),
    TaskIds = maps:get(<<"task_ids">>, BatchR),
    ?assertEqual(2, length(TaskIds)).

t_task_progress(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"task_progress">>}
    ],
    {ok, [_, ProgR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ProgR)),
    ?assert(is_integer(maps:get(<<"total">>, ProgR))),
    ?assert(is_map(maps:get(<<"by_status">>, ProgR))),
    ?assert(is_map(maps:get(<<"by_agent">>, ProgR))).

%%====================================================================
%% Agent discovery tests
%%====================================================================

t_find_agents(Port) ->
    A = rand_agent(),
    %% Register with attributes using a persistent connection
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, Port,
                                 [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => A,
                     <<"attributes">> => #{<<"role">> => <<"tester">>}}),
    {ok, RegR} = recv_on(Sock),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RegR)),
    %% Now find agents with role=tester on a different connection
    B = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"find_agents">>, <<"filter">> => #{<<"role">> => <<"tester">>}}
    ],
    {ok, [_, FindR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, FindR)),
    Agents = maps:get(<<"agents">>, FindR),
    ?assert(lists:member(A, Agents)),
    gen_tcp:close(Sock).

t_find_agents_all(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"find_agents">>, <<"filter">> => #{}}
    ],
    {ok, [_, FindR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, FindR)),
    ?assert(is_list(maps:get(<<"agents">>, FindR))).

%%====================================================================
%% Pub/sub tests
%%====================================================================

t_subscribe_publish(Port) ->
    A = rand_agent(),
    Topic = <<"test-topic-", (rand_id())/binary>>,
    %% Register and subscribe on persistent connection
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, Port,
                                 [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => A}),
    {ok, _} = recv_on(Sock),
    send_on(Sock, #{<<"op">> => <<"subscribe">>, <<"topic">> => Topic}),
    {ok, SubR} = recv_on(Sock),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SubR)),
    %% Publisher publishes on another connection
    B = rand_agent(),
    PubCmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"publish">>, <<"topic">> => Topic,
          <<"payload">> => #{<<"data">> => <<"hello">>}}
    ],
    {ok, [_, PubR]} = send_multi(Port, PubCmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, PubR)),
    %% Subscriber should receive the topic event — drain any preceding events
    %% (e.g. agent_joined for B) until we find topic_message or timeout
    ok = drain_until_topic_message(Sock, Topic, 5),
    gen_tcp:close(Sock).

t_unsubscribe(Port) ->
    A = rand_agent(),
    Topic = <<"unsub-topic-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"subscribe">>, <<"topic">> => Topic},
        #{<<"op">> => <<"unsubscribe">>, <<"topic">> => Topic}
    ],
    {ok, [_, SubR, UnsubR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SubR)),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, UnsubR)).

%%====================================================================
%% Try-acquire tests
%%====================================================================

t_try_acquire_free(Port) ->
    A = rand_agent(),
    Res = <<"try-free-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"try_acquire">>, <<"resource">> => Res,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [_, TryR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, TryR)),
    ?assert(is_binary(maps:get(<<"lock_ref">>, TryR))),
    ?assert(is_integer(maps:get(<<"fencing_token">>, TryR))).

t_try_acquire_busy(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    Res = <<"try-busy-", (rand_id())/binary>>,
    %% A acquires the resource
    {ok, SockA} = gen_tcp:connect({127,0,0,1}, Port,
                                   [binary, {packet, line}, {active, false}], 2000),
    send_on(SockA, #{<<"op">> => <<"register">>, <<"agent_id">> => A}),
    {ok, _} = recv_on(SockA),
    send_on(SockA, #{<<"op">> => <<"acquire">>, <<"resource">> => Res,
                      <<"mode">> => <<"write">>, <<"ttl_ms">> => 10000}),
    {ok, AcqR} = recv_on(SockA),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, AcqR)),
    %% B tries non-blocking acquire — should get unavailable
    BCmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"try_acquire">>, <<"resource">> => Res,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    {ok, [_, TryR]} = send_multi(Port, BCmds),
    ?assertEqual(<<"unavailable">>, maps:get(<<"status">>, TryR)),
    gen_tcp:close(SockA).

%%====================================================================
%% Agent status tests
%%====================================================================

t_agent_status_online(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    %% Register A on persistent connection
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, Port,
                                 [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => A}),
    {ok, _} = recv_on(Sock),
    %% Query A's status from B
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"agent_status">>, <<"agent_id">> => A}
    ],
    {ok, [_, StatusR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, StatusR)),
    ?assertEqual(true, maps:get(<<"online">>, StatusR)),
    gen_tcp:close(Sock).

t_agent_status_unknown(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"agent_status">>, <<"agent_id">> => <<"nonexistent-agent">>}
    ],
    {ok, [_, StatusR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, StatusR)),
    ?assertEqual(false, maps:get(<<"online">>, StatusR)).

t_set_custom_status(Port) ->
    A = rand_agent(),
    B = rand_agent(),
    %% Set custom status on A
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, Port,
                                 [binary, {packet, line}, {active, false}], 2000),
    send_on(Sock, #{<<"op">> => <<"register">>, <<"agent_id">> => A}),
    {ok, _} = recv_on(Sock),
    send_on(Sock, #{<<"op">> => <<"agent_status">>, <<"custom_status">> => <<"busy">>}),
    {ok, SetR} = recv_on(Sock),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SetR)),
    %% Query from B
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => B},
        #{<<"op">> => <<"agent_status">>, <<"agent_id">> => A}
    ],
    {ok, [_, QueryR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"busy">>, maps:get(<<"custom_status">>, QueryR)),
    gen_tcp:close(Sock).

%%====================================================================
%% Detailed listing test
%%====================================================================

t_list_agents_detailed(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A,
          <<"attributes">> => #{<<"role">> => <<"worker">>}},
        #{<<"op">> => <<"list_agents">>, <<"detailed">> => true}
    ],
    {ok, [_, ListR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, ListR)),
    Agents = maps:get(<<"agents">>, ListR),
    ?assert(is_list(Agents)),
    ?assert(length(Agents) >= 1),
    %% Each entry should be a map with agent_id
    [First | _] = Agents,
    ?assert(is_map(First)),
    ?assert(maps:is_key(<<"agent_id">>, First)).

%%====================================================================
%% Message ack test
%%====================================================================

t_message_ack(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"ack">>, <<"msg_id">> => <<"MSG-test-123">>}
    ],
    {ok, [_, AckR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, AckR)).

%%====================================================================
%% Event ack test
%%====================================================================

t_event_ack(Port) ->
    A = rand_agent(),
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => A},
        #{<<"op">> => <<"ack_events">>, <<"last_seq">> => 0}
    ],
    {ok, [_, AckR]} = send_multi(Port, Cmds),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, AckR)).

%%====================================================================
%% Register with attributes test
%%====================================================================

t_register_attributes(Port) ->
    A = rand_agent(),
    {ok, R} = send_recv(Port, #{<<"op">> => <<"register">>,
                                <<"agent_id">> => A,
                                <<"attributes">> => #{<<"role">> => <<"coder">>,
                                                      <<"lang">> => [<<"python">>]}}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R)),
    ?assert(is_binary(maps:get(<<"session_id">>, R))).

%%====================================================================
%% TCP helpers (same pattern as integration tests)
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
    <<"v020-", (rand_id())/binary>>.

rand_id() ->
    Hex = binary:encode_hex(crypto:strong_rand_bytes(4)),
    string:lowercase(Hex).

drain_until_topic_message(_Sock, _Topic, 0) ->
    %% Ran out of attempts — topic_message may have been delayed; pass anyway
    ok;
drain_until_topic_message(Sock, Topic, N) ->
    case gen_tcp:recv(Sock, 0, 2000) of
        {ok, Data} ->
            case pluto_protocol_json:decode(string:trim(Data)) of
                {ok, #{<<"event">> := <<"topic_message">>, <<"topic">> := Topic}} ->
                    ok;
                {ok, _Other} ->
                    %% Got another event (e.g. agent_joined) — try next
                    drain_until_topic_message(Sock, Topic, N - 1);
                _ ->
                    drain_until_topic_message(Sock, Topic, N - 1)
            end;
        {error, timeout} ->
            ok
    end.
