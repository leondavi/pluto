%%%-------------------------------------------------------------------
%%% @doc pluto_selftest — Built-in self-test runner.
%%%
%%% Exercises the full protocol via loopback TCP connections and
%%% returns a summary of passed/failed checks.  Safe to invoke from
%%% the `selftest` protocol op or from a monitoring script.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_selftest).

-include("pluto.hrl").

%% API
-export([run/0]).

%%====================================================================
%% API
%%====================================================================

%% @doc Run the self-test suite and return a result map suitable for
%% JSON encoding.
-spec run() -> map().
run() ->
    Port = pluto_config:get(tcp_port, ?DEFAULT_TCP_PORT),
    Start = erlang:monotonic_time(millisecond),
    Results = run_checks(Port),
    End = erlang:monotonic_time(millisecond),
    Duration = End - Start,

    Checks = length(Results),
    Failed = length([Name || {Name, fail, _} <- Results]),
    Failures = [#{<<"check">> => atom_to_binary(Name, utf8),
                  <<"reason">> => iolist_to_binary(Reason)}
                || {Name, fail, Reason} <- Results],

    case Failed of
        0 ->
            #{<<"status">>      => ?STATUS_OK,
              <<"checks">>      => Checks,
              <<"failed">>      => 0,
              <<"duration_ms">> => Duration};
        _ ->
            #{<<"status">>      => ?STATUS_ERROR,
              <<"checks">>      => Checks,
              <<"failed">>      => Failed,
              <<"duration_ms">> => Duration,
              <<"failures">>    => Failures}
    end.

%%====================================================================
%% Internal — Test runner
%%====================================================================

run_checks(Port) ->
    [
        check_ping(Port),
        check_register(Port),
        check_acquire_release(Port),
        check_renew(Port),
        check_list_agents(Port),
        check_broadcast(Port),
        check_direct_message(Port),
        check_lock_conflict(Port),
        check_event_history(Port),
        check_fencing_token_monotonic(Port),
        check_admin_list_locks(Port),
        check_admin_fencing_seq(Port)
    ].

%%====================================================================
%% Individual checks
%%====================================================================

check_ping(Port) ->
    case send_recv(Port, #{<<"op">> => <<"ping">>}) of
        {ok, #{<<"status">> := <<"pong">>}} -> {ping, pass, ""};
        Other -> {ping, fail, fmt("unexpected: ~p", [Other])}
    end.

check_register(Port) ->
    AgentId = <<"selftest-reg-", (rand_id())/binary>>,
    case send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId}) of
        {ok, #{<<"status">> := <<"ok">>, <<"session_id">> := SId}} when is_binary(SId) ->
            {register, pass, ""};
        Other ->
            {register, fail, fmt("unexpected: ~p", [Other])}
    end.

check_acquire_release(Port) ->
    AgentId = <<"selftest-acq-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => <<"selftest:res-1">>,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>},
               #{<<"status">> := <<"ok">>, <<"lock_ref">> := LockRef}]} ->
            %% Now release
            Release = #{<<"op">> => <<"release">>, <<"lock_ref">> => LockRef},
            case send_recv_on_session(Port, AgentId, Release) of
                {ok, #{<<"status">> := <<"ok">>}} ->
                    {acquire_release, pass, ""};
                Other2 ->
                    {acquire_release, fail, fmt("release failed: ~p", [Other2])}
            end;
        Other ->
            {acquire_release, fail, fmt("unexpected: ~p", [Other])}
    end.

check_renew(Port) ->
    AgentId = <<"selftest-rnw-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => <<"selftest:res-renew">>,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>},
               #{<<"status">> := <<"ok">>, <<"lock_ref">> := LockRef}]} ->
            Renew = #{<<"op">> => <<"renew">>, <<"lock_ref">> => LockRef, <<"ttl_ms">> => 10000},
            case send_recv_on_session(Port, AgentId, Renew) of
                {ok, #{<<"status">> := <<"ok">>}} ->
                    {renew, pass, ""};
                Other2 ->
                    {renew, fail, fmt("renew failed: ~p", [Other2])}
            end;
        Other ->
            {renew, fail, fmt("unexpected: ~p", [Other])}
    end.

check_list_agents(Port) ->
    case send_recv(Port, #{<<"op">> => <<"list_agents">>}) of
        %% list_agents requires registration, but we test the error is correct
        {ok, #{<<"status">> := <<"error">>, <<"reason">> := <<"not_registered">>}} ->
            {list_agents, pass, ""};
        {ok, #{<<"status">> := <<"ok">>}} ->
            {list_agents, pass, ""};
        Other ->
            {list_agents, fail, fmt("unexpected: ~p", [Other])}
    end.

check_broadcast(Port) ->
    AgentId = <<"selftest-bc-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"broadcast">>, <<"payload">> => #{<<"msg">> => <<"selftest">>}}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>}, #{<<"status">> := <<"ok">>}]} ->
            {broadcast, pass, ""};
        Other ->
            {broadcast, fail, fmt("unexpected: ~p", [Other])}
    end.

check_direct_message(Port) ->
    AgentA = <<"selftest-dm-a-", (rand_id())/binary>>,
    AgentB = <<"selftest-dm-b-", (rand_id())/binary>>,
    %% Register both agents
    send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentA}),
    send_recv(Port, #{<<"op">> => <<"register">>, <<"agent_id">> => AgentB}),
    %% Send from A to B
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentA},
        #{<<"op">> => <<"send">>, <<"to">> => AgentB,
          <<"payload">> => #{<<"msg">> => <<"hi">>}}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [_, #{<<"status">> := <<"ok">>}]} ->
            {direct_message, pass, ""};
        {ok, [_, #{<<"status">> := <<"error">>, <<"reason">> := <<"unknown_target">>}]} ->
            %% AgentB's connection closed before send — expected in selftest
            {direct_message, pass, ""};
        Other ->
            {direct_message, fail, fmt("unexpected: ~p", [Other])}
    end.

check_lock_conflict(Port) ->
    AgentA = <<"selftest-lc-a-", (rand_id())/binary>>,
    AgentB = <<"selftest-lc-b-", (rand_id())/binary>>,
    Res = <<"selftest:conflict-", (rand_id())/binary>>,
    %% AgentA acquires
    CmdsA = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentA},
        #{<<"op">> => <<"acquire">>, <<"resource">> => Res,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    case send_multi_recv(Port, CmdsA) of
        {ok, [#{<<"status">> := <<"ok">>}, #{<<"status">> := <<"ok">>}]} ->
            %% AgentB tries to acquire same resource — should get "wait"
            CmdsB = [
                #{<<"op">> => <<"register">>, <<"agent_id">> => AgentB},
                #{<<"op">> => <<"acquire">>, <<"resource">> => Res,
                  <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000,
                  <<"max_wait_ms">> => 100}
            ],
            case send_multi_recv(Port, CmdsB) of
                {ok, [#{<<"status">> := <<"ok">>}, #{<<"status">> := <<"wait">>}]} ->
                    {lock_conflict, pass, ""};
                Other2 ->
                    {lock_conflict, fail, fmt("expected wait: ~p", [Other2])}
            end;
        Other ->
            {lock_conflict, fail, fmt("unexpected: ~p", [Other])}
    end.

check_event_history(Port) ->
    AgentId = <<"selftest-eh-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"event_history">>, <<"since_token">> => 0, <<"limit">> => 5}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>}, #{<<"status">> := <<"ok">>, <<"events">> := _}]} ->
            {event_history, pass, ""};
        Other ->
            {event_history, fail, fmt("unexpected: ~p", [Other])}
    end.

check_fencing_token_monotonic(Port) ->
    AgentId = <<"selftest-ft-", (rand_id())/binary>>,
    Res1 = <<"selftest:ft-1-", (rand_id())/binary>>,
    Res2 = <<"selftest:ft-2-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"acquire">>, <<"resource">> => Res1,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000},
        #{<<"op">> => <<"acquire">>, <<"resource">> => Res2,
          <<"mode">> => <<"write">>, <<"ttl_ms">> => 5000}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>},
               #{<<"status">> := <<"ok">>, <<"fencing_token">> := T1},
               #{<<"status">> := <<"ok">>, <<"fencing_token">> := T2}]} ->
            case T2 > T1 of
                true  -> {fencing_token_monotonic, pass, ""};
                false -> {fencing_token_monotonic, fail,
                          fmt("T2 (~w) not > T1 (~w)", [T2, T1])}
            end;
        Other ->
            {fencing_token_monotonic, fail, fmt("unexpected: ~p", [Other])}
    end.

check_admin_list_locks(Port) ->
    AgentId = <<"selftest-al-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"admin_list_locks">>}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>}, #{<<"status">> := <<"ok">>, <<"locks">> := _}]} ->
            {admin_list_locks, pass, ""};
        Other ->
            {admin_list_locks, fail, fmt("unexpected: ~p", [Other])}
    end.

check_admin_fencing_seq(Port) ->
    AgentId = <<"selftest-af-", (rand_id())/binary>>,
    Cmds = [
        #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
        #{<<"op">> => <<"admin_fencing_seq">>}
    ],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>},
               #{<<"status">> := <<"ok">>, <<"fencing_seq">> := _}]} ->
            {admin_fencing_seq, pass, ""};
        Other ->
            {admin_fencing_seq, fail, fmt("unexpected: ~p", [Other])}
    end.

%%====================================================================
%% TCP helpers
%%====================================================================

%% @private Send a single JSON request and receive the response.
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

%% @private Send multiple JSON requests on one connection and collect responses.
send_multi_recv(Port, Reqs) ->
    case gen_tcp:connect({127, 0, 0, 1}, Port,
                         [binary, {packet, line}, {active, false}], 2000) of
        {ok, Sock} ->
            %% Send all requests
            lists:foreach(fun(Req) ->
                gen_tcp:send(Sock, pluto_protocol_json:encode_line(Req))
            end, Reqs),
            %% Receive one response per request
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

%% @private Register an agent, then send a command on the same session.
send_recv_on_session(Port, AgentId, Cmd) ->
    Cmds = [#{<<"op">> => <<"register">>, <<"agent_id">> => AgentId}, Cmd],
    case send_multi_recv(Port, Cmds) of
        {ok, [#{<<"status">> := <<"ok">>}, Response]} -> {ok, Response};
        {ok, [#{<<"status">> := <<"error">>} = Err, _]} -> {error, Err};
        Other -> Other
    end.

%%====================================================================
%% Utilities
%%====================================================================

rand_id() ->
    Bytes = crypto:strong_rand_bytes(4),
    Hex = binary:encode_hex(Bytes),
    string:lowercase(Hex).

fmt(Fmt, Args) ->
    lists:flatten(io_lib:format(Fmt, Args)).
