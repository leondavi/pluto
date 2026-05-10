%%% Integration tests for Pluto v0.2.9 snapshot/restore feature:
%%%   - `snapshot_self` op captures agent state into a .plut payload
%%%   - `restore_from_snapshot` op overlays the .plut onto a fresh session
%%%   - Status transitions to `recovered_from_file` after restore
%%%   - Lock reclaim/loss accounting in restore response
%%%   - Both TCP and HTTP paths
-module(pluto_snapshot_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%%====================================================================
%% Test Fixtures
%%====================================================================

app_setup() ->
    application:set_env(pluto, persistence_dir, "/tmp/pluto/test_snapshot"),
    application:set_env(pluto, event_log_dir,   "/tmp/pluto/test_snapshot_events"),
    application:set_env(pluto, signal_dir,      "/tmp/pluto/test_snapshot_signals"),
    application:set_env(pluto, tcp_port,  19101),
    application:set_env(pluto, http_port, 19102),
    application:set_env(pluto, heartbeat_interval_ms, 60000),
    application:set_env(pluto, heartbeat_timeout_ms,  120000),
    application:set_env(pluto, reconnect_grace_ms,    120000),
    application:set_env(pluto, http_session_ttl_ms,   300000),
    application:unset_env(pluto, agent_tokens),
    application:unset_env(pluto, admin_token),
    application:set_env(pluto, acl, undefined),
    %% Wipe any stale snapshot from earlier test runs so list_agents starts clean
    file:delete("/tmp/pluto/test_snapshot/pluto.snapshot"),
    {ok, _} = application:ensure_all_started(pluto),
    timer:sleep(300),
    {19101, 19102}.

app_teardown(_Ports) ->
    application:stop(pluto),
    timer:sleep(100).

%%====================================================================
%% Test generator
%%====================================================================

snapshot_test_() ->
    {setup,
     fun app_setup/0,
     fun app_teardown/1,
     fun({TcpPort, HttpPort}) ->
         [
          {"TCP: snapshot_self returns plut + prompt for registered agent",
           fun() -> t_tcp_snapshot_basic(TcpPort) end},
          {"TCP: snapshot captures attributes, custom_status, subscriptions",
           fun() -> t_tcp_snapshot_state(TcpPort) end},
          {"TCP: snapshot captures held locks with fencing tokens",
           fun() -> t_tcp_snapshot_locks(TcpPort) end},
          {"TCP: restore overlays state and sets status=recovered_from_file",
           fun() -> t_tcp_restore_basic(TcpPort) end},
          {"TCP: restore reports reclaimed vs lost locks correctly",
           fun() -> t_tcp_restore_locks(TcpPort) end},
          {"TCP: restore without prior register fails with not_registered",
           fun() -> t_tcp_restore_unregistered(TcpPort) end},
          {"HTTP: /agents/snapshot_self + /agents/restore_from_snapshot round-trip",
           fun() -> t_http_round_trip(HttpPort) end}
         ]
     end}.

%%====================================================================
%% Tests
%%====================================================================

t_tcp_snapshot_basic(Port) ->
    Agent = <<"snap-basic">>,
    Sock  = register_tcp(Port, Agent),
    Resp  = send_recv(Sock, #{<<"op">> => <<"snapshot_self">>}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Resp)),
    Plut   = maps:get(<<"plut">>,   Resp),
    Prompt = maps:get(<<"prompt">>, Resp),
    ?assert(is_map(Plut)),
    ?assert(is_binary(Prompt)),
    ?assertEqual(Agent, maps:get(<<"agent_id">>, Plut)),
    ?assert(maps:get(<<"taken_at">>, Plut) > 0),
    %% Prompt must mention agent_id and the restore op
    ?assertNotEqual(nomatch, binary:match(Prompt, Agent)),
    ?assertNotEqual(nomatch,
                    binary:match(Prompt, <<"restore_from_snapshot">>)),
    gen_tcp:close(Sock).

t_tcp_snapshot_state(Port) ->
    Agent = <<"snap-state">>,
    Sock  = register_tcp(Port, Agent, #{<<"role">> => <<"specialist">>,
                                         <<"team">> => <<"alpha">>}),
    %% Set a custom status, subscribe to a topic, then snapshot
    {ok, _} = ensure_ok(send_recv(Sock,
        #{<<"op">> => <<"agent_status">>, <<"custom_status">> => <<"busy">>})),
    {ok, _} = ensure_ok(send_recv(Sock,
        #{<<"op">> => <<"subscribe">>, <<"topic">> => <<"alerts">>})),
    {ok, _} = ensure_ok(send_recv(Sock,
        #{<<"op">> => <<"subscribe">>, <<"topic">> => <<"news">>})),

    Resp = send_recv(Sock, #{<<"op">> => <<"snapshot_self">>}),
    Plut = maps:get(<<"plut">>, Resp),

    ?assertEqual(<<"busy">>, maps:get(<<"custom_status">>, Plut)),
    Subs = maps:get(<<"subscriptions">>, Plut),
    ?assert(lists:member(<<"alerts">>, Subs)),
    ?assert(lists:member(<<"news">>,   Subs)),
    Attrs = maps:get(<<"attributes">>, Plut),
    ?assertEqual(<<"specialist">>, maps:get(<<"role">>, Attrs)),
    ?assertEqual(<<"alpha">>,      maps:get(<<"team">>, Attrs)),
    gen_tcp:close(Sock).

t_tcp_snapshot_locks(Port) ->
    Agent = <<"snap-locks">>,
    Sock  = register_tcp(Port, Agent),
    R1 = send_recv(Sock, #{<<"op">> => <<"acquire">>,
                            <<"resource">> => <<"snap:res-A">>,
                            <<"mode">> => <<"write">>, <<"ttl_ms">> => 60000}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R1)),
    R2 = send_recv(Sock, #{<<"op">> => <<"acquire">>,
                            <<"resource">> => <<"snap:res-B">>,
                            <<"mode">> => <<"read">>, <<"ttl_ms">> => 60000}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, R2)),

    Resp = send_recv(Sock, #{<<"op">> => <<"snapshot_self">>}),
    Plut = maps:get(<<"plut">>, Resp),
    Locks = maps:get(<<"held_locks">>, Plut),
    ?assertEqual(2, length(Locks)),
    Resources = lists:sort([maps:get(<<"resource">>, L) || L <- Locks]),
    ?assertEqual([<<"snap:res-A">>, <<"snap:res-B">>], Resources),
    %% Each lock has a fencing token > 0
    lists:foreach(fun(L) ->
        ?assert(is_integer(maps:get(<<"fencing_token">>, L))),
        ?assert(maps:get(<<"fencing_token">>, L) > 0)
    end, Locks),
    gen_tcp:close(Sock).

t_tcp_restore_basic(Port) ->
    Agent = <<"snap-restore">>,
    Sock1 = register_tcp(Port, Agent, #{<<"role">> => <<"orchestrator">>}),
    {ok, _} = ensure_ok(send_recv(Sock1,
        #{<<"op">> => <<"agent_status">>, <<"custom_status">> => <<"working">>})),
    {ok, _} = ensure_ok(send_recv(Sock1,
        #{<<"op">> => <<"subscribe">>, <<"topic">> => <<"control">>})),

    SnapResp = send_recv(Sock1, #{<<"op">> => <<"snapshot_self">>}),
    Plut     = maps:get(<<"plut">>, SnapResp),
    gen_tcp:close(Sock1),
    timer:sleep(100),

    %% Reconnect — comes back as `recovered` (within grace period)
    Sock2 = register_tcp(Port, Agent),
    Restore = send_recv(Sock2, #{<<"op">> => <<"restore_from_snapshot">>,
                                  <<"plut">> => Plut}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Restore)),
    ?assertEqual(Agent, maps:get(<<"agent_id">>, Restore)),
    Subs = maps:get(<<"subscriptions">>, Restore),
    ?assert(lists:member(<<"control">>, Subs)),

    %% Verify status is now `recovered_from_file` via list_agents query
    Listing = send_recv(Sock2, #{<<"op">> => <<"list_agents">>,
                                  <<"detailed">> => true}),
    Agents = maps:get(<<"agents">>, Listing, []),
    case [A || A <- Agents, maps:get(<<"agent_id">>, A) =:= Agent] of
        [Found] ->
            ?assertEqual(<<"recovered_from_file">>,
                         maps:get(<<"status">>, Found));
        [] ->
            ?assert(false, "agent not in list_agents result")
    end,
    gen_tcp:close(Sock2).

t_tcp_restore_locks(Port) ->
    Agent = <<"snap-restore-locks">>,
    Sock1 = register_tcp(Port, Agent),

    %% Acquire two locks
    L1 = send_recv(Sock1, #{<<"op">> => <<"acquire">>,
                             <<"resource">> => <<"snap:keep">>,
                             <<"mode">> => <<"write">>, <<"ttl_ms">> => 60000}),
    LockKeep = maps:get(<<"lock_ref">>, L1),
    L2 = send_recv(Sock1, #{<<"op">> => <<"acquire">>,
                             <<"resource">> => <<"snap:lose">>,
                             <<"mode">> => <<"write">>, <<"ttl_ms">> => 60000}),
    LockLose = maps:get(<<"lock_ref">>, L2),

    SnapResp = send_recv(Sock1, #{<<"op">> => <<"snapshot_self">>}),
    Plut     = maps:get(<<"plut">>, SnapResp),

    %% Release one lock — this one should appear in `lost_locks` post-restore
    {ok, _} = ensure_ok(send_recv(Sock1,
        #{<<"op">> => <<"release">>, <<"lock_ref">> => LockLose})),
    gen_tcp:close(Sock1),
    timer:sleep(100),

    %% Reconnect (within grace, so the kept lock is reclaimed automatically by Pluto)
    Sock2 = register_tcp(Port, Agent),
    Restore = send_recv(Sock2, #{<<"op">> => <<"restore_from_snapshot">>,
                                  <<"plut">> => Plut}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Restore)),

    Reclaimed = maps:get(<<"reclaimed_locks">>, Restore),
    Lost      = maps:get(<<"lost_locks">>,      Restore),
    ReclaimedRefs = [maps:get(<<"lock_ref">>, L) || L <- Reclaimed],
    LostRefs      = [maps:get(<<"lock_ref">>, L) || L <- Lost],
    ?assert(lists:member(LockKeep, ReclaimedRefs),
            "lock_keep should be in reclaimed_locks"),
    ?assert(lists:member(LockLose, LostRefs),
            "lock_lose should be in lost_locks"),
    gen_tcp:close(Sock2).

t_tcp_restore_unregistered(Port) ->
    %% Plut payload from a fresh registration we then drop
    A1 = <<"snap-needs-reg">>,
    Sock1 = register_tcp(Port, A1),
    SnapResp = send_recv(Sock1, #{<<"op">> => <<"snapshot_self">>}),
    Plut = maps:get(<<"plut">>, SnapResp),
    gen_tcp:close(Sock1),
    timer:sleep(100),

    %% Open a *new* TCP connection and try to restore WITHOUT registering first
    {ok, RawSock} = gen_tcp:connect({127,0,0,1}, Port,
                                     [binary, {active, false}, {packet, line}],
                                     5000),
    Resp = send_recv(RawSock, #{<<"op">> => <<"restore_from_snapshot">>,
                                 <<"plut">> => Plut}),
    ?assertEqual(<<"error">>, maps:get(<<"status">>, Resp)),
    ?assertEqual(<<"not_registered">>, maps:get(<<"reason">>, Resp)),
    gen_tcp:close(RawSock).

t_http_round_trip(HttpPort) ->
    Agent = <<"snap-http">>,
    %% Register HTTP agent
    {ok, RegResp} = http_post(HttpPort, "/agents/register",
                              #{<<"agent_id">> => Agent,
                                <<"mode">> => <<"http">>}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RegResp)),
    Token = maps:get(<<"token">>, RegResp),

    %% Take snapshot
    {ok, SnapResp} = http_post(HttpPort, "/agents/snapshot_self",
                                #{<<"token">> => Token}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, SnapResp)),
    Plut = maps:get(<<"plut">>, SnapResp),
    Prompt = maps:get(<<"prompt">>, SnapResp),
    ?assertEqual(Agent, maps:get(<<"agent_id">>, Plut)),
    ?assert(byte_size(Prompt) > 100),

    %% Restore on the same registered agent
    {ok, RestResp} = http_post(HttpPort, "/agents/restore_from_snapshot",
                                #{<<"token">> => Token, <<"plut">> => Plut}),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, RestResp)),
    ?assertEqual(Agent, maps:get(<<"agent_id">>, RestResp)),

    %% Cleanup
    {ok, _} = http_post(HttpPort, "/agents/unregister",
                         #{<<"token">> => Token}).

%%====================================================================
%% TCP / HTTP helpers
%%====================================================================

register_tcp(Port, AgentId) ->
    register_tcp(Port, AgentId, #{}).
register_tcp(Port, AgentId, Attrs) ->
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, Port,
                                  [binary, {active, false}, {packet, line}],
                                  5000),
    Msg0 = #{<<"op">> => <<"register">>, <<"agent_id">> => AgentId},
    Msg = case map_size(Attrs) of
        0 -> Msg0;
        _ -> Msg0#{<<"attributes">> => Attrs}
    end,
    Resp = send_recv(Sock, Msg),
    ?assertEqual(<<"ok">>, maps:get(<<"status">>, Resp)),
    Sock.

send_recv(Sock, Req) ->
    Line = pluto_protocol_json:encode_line(Req),
    ok = gen_tcp:send(Sock, Line),
    {ok, Raw} = gen_tcp:recv(Sock, 0, 5000),
    {ok, Resp} = pluto_protocol_json:decode(string:trim(Raw)),
    Resp.

ensure_ok(#{<<"status">> := <<"ok">>} = Resp) -> {ok, Resp};
ensure_ok(Other) -> {error, Other}.

http_post(HttpPort, Path, Body) ->
    JsonBody = pluto_protocol_json:encode(Body),
    {ok, Sock} = gen_tcp:connect({127,0,0,1}, HttpPort,
                                  [binary, {packet, http_bin}, {active, false}],
                                  2000),
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
    Result.

read_http_response(Sock) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_response, _, Code, _}} ->
            CodeNum = case Code of C when is_integer(C) -> C; _ -> 0 end,
            {ok, _Headers, Body} = read_headers_then_body(Sock),
            case pluto_protocol_json:decode(Body) of
                {ok, Decoded} when CodeNum >= 200, CodeNum < 300 ->
                    {ok, Decoded};
                {ok, Decoded} ->
                    {error, {http_status, CodeNum, Decoded}};
                {error, _} ->
                    {error, {decode, Body}}
            end;
        Other ->
            {error, {bad_response, Other}}
    end.

read_headers_then_body(Sock) ->
    {Headers, ContentLength} = read_headers(Sock, [], 0),
    inet:setopts(Sock, [{packet, raw}]),
    Body = case ContentLength of
        0 -> <<>>;
        N ->
            case gen_tcp:recv(Sock, N, 5000) of
                {ok, B} -> B;
                _ -> <<>>
            end
    end,
    {ok, Headers, Body}.

read_headers(Sock, Acc, CL) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, http_eoh} ->
            {lists:reverse(Acc), CL};
        {ok, {http_header, _, 'Content-Length', _, V}} ->
            CL2 = case V of
                Bin when is_binary(Bin) -> binary_to_integer(Bin);
                List when is_list(List) -> list_to_integer(List)
            end,
            read_headers(Sock, [{<<"content-length">>, V} | Acc], CL2);
        {ok, {http_header, _, Name, _, Value}} ->
            read_headers(Sock, [{Name, Value} | Acc], CL);
        Other ->
            {lists:reverse(Acc), CL, Other}
    end.
