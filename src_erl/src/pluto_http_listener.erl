%%%-------------------------------------------------------------------
%%% @doc pluto_http_listener — Lightweight HTTP API listener.
%%%
%%% Accepts HTTP connections on the configured port and dispatches
%%% JSON requests to the same handlers used by the TCP protocol.
%%% Implements a minimal HTTP/1.1 subset — just enough for REST.
%%%
%%% Uses gen_server with a synchronous accept loop (same pattern
%%% as pluto_tcp_listener).  Each request is handled inline since
%%% HTTP is request/response (no persistent sessions).
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_http_listener).
-behaviour(gen_server).

-include("pluto.hrl").

%% API
-export([start_link/0]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {
    listen_sock :: gen_tcp:socket(),
    port        :: non_neg_integer()
}).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    Port = pluto_config:get(http_port, ?DEFAULT_HTTP_PORT),
    case Port of
        disabled ->
            ?LOG_INFO("pluto_http_listener disabled"),
            ignore;
        _ ->
            Opts = [
                binary,
                {packet, http_bin},
                {active, false},
                {reuseaddr, true},
                {backlog, 128}
            ],
            case gen_tcp:listen(Port, Opts) of
                {ok, LSock} ->
                    ?LOG_INFO("pluto_http_listener listening on port ~w", [Port]),
                    self() ! accept,
                    {ok, #state{listen_sock = LSock, port = Port}};
                {error, Reason} ->
                    ?LOG_ERROR("pluto_http_listener failed on port ~w: ~p",
                               [Port, Reason]),
                    {stop, {listen_failed, Reason}}
            end
    end.

handle_call(_Req, _From, State) ->
    {reply, ok, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(accept, #state{listen_sock = LSock} = State) ->
    case gen_tcp:accept(LSock, 1000) of
        {ok, Sock} ->
            spawn(fun() -> handle_connection(Sock) end),
            self() ! accept,
            {noreply, State};
        {error, timeout} ->
            self() ! accept,
            {noreply, State};
        {error, closed} ->
            {stop, normal, State};
        {error, _Reason} ->
            erlang:send_after(100, self(), accept),
            {noreply, State}
    end;
handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, #state{listen_sock = LSock}) ->
    gen_tcp:close(LSock),
    ok.

%%====================================================================
%% HTTP connection handler
%%====================================================================

handle_connection(Sock) ->
    try
        case read_http_request(Sock) of
            {ok, Method, Path, _Headers, Body} ->
                {Status, RespBody} = route(Method, Path, Body),
                send_http_response(Sock, Status, RespBody);
            {error, _} ->
                send_http_response(Sock, 400, #{<<"error">> => <<"bad_request">>})
        end
    catch
        _:_ ->
            send_http_response(Sock, 500, #{<<"error">> => <<"internal_error">>})
    after
        gen_tcp:close(Sock)
    end.

%%====================================================================
%% HTTP request parsing (minimal HTTP/1.1)
%%====================================================================

read_http_request(Sock) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_request, Method, {abs_path, Path}, _Vsn}} ->
            Headers = read_headers(Sock, []),
            ContentLen = content_length(Headers),
            Body = read_body(Sock, ContentLen),
            {ok, Method, Path, Headers, Body};
        {ok, {http_error, _}} ->
            {error, bad_request};
        {error, Reason} ->
            {error, Reason}
    end.

read_headers(Sock, Acc) ->
    case gen_tcp:recv(Sock, 0, 5000) of
        {ok, {http_header, _, Name, _, Value}} ->
            read_headers(Sock, [{header_name(Name), Value} | Acc]);
        {ok, http_eoh} ->
            lists:reverse(Acc);
        _ ->
            lists:reverse(Acc)
    end.

read_body(_Sock, 0) ->
    <<>>;
read_body(Sock, Len) when Len > 0 ->
    %% Switch to raw mode to read the body bytes
    inet:setopts(Sock, [{packet, raw}]),
    case gen_tcp:recv(Sock, Len, 5000) of
        {ok, Data} -> Data;
        _ -> <<>>
    end.

content_length(Headers) ->
    case lists:keyfind(<<"Content-Length">>, 1, Headers) of
        {_, Val} ->
            try binary_to_integer(Val) catch _:_ -> 0 end;
        false ->
            0
    end.

header_name(Atom) when is_atom(Atom) -> atom_to_binary(Atom, utf8);
header_name(Bin) when is_binary(Bin) -> Bin.

%%====================================================================
%% HTTP response
%%====================================================================

send_http_response(Sock, StatusCode, Body) ->
    JsonBody = pluto_protocol_json:encode(Body),
    StatusLine = status_line(StatusCode),
    Headers = [
        <<"HTTP/1.1 ">>, StatusLine, <<"\r\n">>,
        <<"Content-Type: application/json\r\n">>,
        <<"Content-Length: ">>, integer_to_binary(byte_size(JsonBody)), <<"\r\n">>,
        <<"Access-Control-Allow-Origin: *\r\n">>,
        <<"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n">>,
        <<"Access-Control-Allow-Headers: Content-Type, Authorization\r\n">>,
        <<"Connection: close\r\n">>,
        <<"\r\n">>
    ],
    %% Ensure socket is in raw mode for writing
    inet:setopts(Sock, [{packet, raw}]),
    gen_tcp:send(Sock, [Headers, JsonBody]).

status_line(200) -> <<"200 OK">>;
status_line(400) -> <<"400 Bad Request">>;
status_line(401) -> <<"401 Unauthorized">>;
status_line(404) -> <<"404 Not Found">>;
status_line(405) -> <<"405 Method Not Allowed">>;
status_line(500) -> <<"500 Internal Server Error">>;
status_line(N)   -> iolist_to_binary([integer_to_binary(N), <<" ">>]).

%%====================================================================
%% Routing
%%====================================================================

%% CORS preflight
route('OPTIONS', _Path, _Body) ->
    {200, #{<<"status">> => <<"ok">>}};

%% ── Health ──────────────────────────────────────────────────────────
route('GET', <<"/health">>, _Body) ->
    {200, #{<<"status">> => <<"ok">>, <<"version">> => list_to_binary(?VERSION)}};

route('GET', <<"/ping">>, _Body) ->
    Now = erlang:system_time(millisecond),
    {200, #{<<"status">> => <<"pong">>, <<"ts">> => Now}};

%% ── Agents ──────────────────────────────────────────────────────────
route('GET', <<"/agents">>, _Body) ->
    Agents = pluto_msg_hub:list_agents(),
    {200, #{<<"status">> => <<"ok">>, <<"agents">> => Agents}};

%% ── Locks ───────────────────────────────────────────────────────────
route('GET', <<"/locks">>, _Body) ->
    Locks = pluto_lock_mgr:list_locks(),
    LockMaps = [format_lock(L) || L <- Locks],
    {200, #{<<"status">> => <<"ok">>, <<"locks">> => LockMaps}};

route('POST', <<"/locks/acquire">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"agent_id">> := AgentId, <<"resource">> := RawRes} = Msg} ->
            case pluto_resource:normalize(RawRes) of
                {ok, Resource} ->
                    Mode = parse_mode(maps:get(<<"mode">>, Msg, <<"write">>)),
                    TtlMs = maps:get(<<"ttl_ms">>, Msg, 30000),
                    MaxWait = maps:get(<<"max_wait_ms">>, Msg, undefined),
                    Opts = #{ttl_ms => TtlMs, max_wait_ms => MaxWait,
                             session_id => <<"http">>, session_pid => undefined},
                    case pluto_lock_mgr:acquire(Resource, Mode, AgentId, Opts) of
                        {ok, LockRef, FToken} ->
                            {200, #{<<"status">> => <<"ok">>,
                                    <<"lock_ref">> => LockRef,
                                    <<"fencing_token">> => FToken}};
                        {wait, WaitRef} ->
                            {200, #{<<"status">> => <<"wait">>,
                                    <<"wait_ref">> => WaitRef}};
                        {error, deadlock} ->
                            {200, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"deadlock">>}};
                        {error, Reason} ->
                            {200, #{<<"status">> => <<"error">>,
                                    <<"reason">> => to_bin(Reason)}}
                    end;
                {error, empty_resource} ->
                    {400, #{<<"error">> => <<"empty_resource">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing agent_id and resource">>}}
    end;

route('POST', <<"/locks/release">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"lock_ref">> := LockRef, <<"agent_id">> := AgentId}} ->
            case pluto_lock_mgr:release(LockRef, AgentId) of
                ok ->
                    {200, #{<<"status">> => <<"ok">>}};
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing lock_ref and agent_id">>}}
    end;

route('POST', <<"/locks/renew">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"lock_ref">> := LockRef} = Msg} ->
            TtlMs = maps:get(<<"ttl_ms">>, Msg, 30000),
            case pluto_lock_mgr:renew(LockRef, #{ttl_ms => TtlMs}) of
                ok ->
                    {200, #{<<"status">> => <<"ok">>}};
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing lock_ref">>}}
    end;

%% ── Events ──────────────────────────────────────────────────────────
route('GET', <<"/events">>, _Body) ->
    route_events(0, 100);
route('GET', <<"/events?", Query/binary>>, _Body) ->
    Params = parse_query(Query),
    SinceSeq = maps:get(<<"since_token">>, Params, 0),
    Limit = maps:get(<<"limit">>, Params, 100),
    route_events(to_int(SinceSeq, 0), to_int(Limit, 100));

%% ── Admin ── Fencing Seq ────────────────────────────────────────────
route('GET', <<"/admin/fencing_seq">>, _Body) ->
    FSeq = pluto_lock_mgr:get_fencing_seq(),
    {200, #{<<"status">> => <<"ok">>, <<"fencing_seq">> => FSeq}};

%% ── Admin ── Force Release ──────────────────────────────────────────
route('POST', <<"/admin/force_release">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"lock_ref">> := LockRef}} ->
            case ets:lookup(?ETS_LOCKS, LockRef) of
                [#lock{agent_id = AId}] ->
                    pluto_lock_mgr:release(LockRef, AId),
                    {200, #{<<"status">> => <<"ok">>}};
                [] ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing lock_ref">>}}
    end;

%% ── Admin ── Wait Graph ─────────────────────────────────────────────
route('GET', <<"/admin/deadlock_graph">>, _Body) ->
    Edges = ets:tab2list(?ETS_WAIT_GRAPH),
    EdgeMaps = [#{<<"waiter">> => W, <<"holder">> => H} || {W, H} <- Edges],
    {200, #{<<"status">> => <<"ok">>, <<"edges">> => EdgeMaps}};

%% ── Selftest ────────────────────────────────────────────────────────
route('POST', <<"/selftest">>, _Body) ->
    Result = pluto_selftest:run(),
    {200, Result};

%% ── Messaging via HTTP ──────────────────────────────────────────────
%% These endpoints allow HTTP-only clients to send and broadcast messages
%% without maintaining a persistent TCP session.  The sender provides an
%% `agent_id` field in the request body (no session required).
%% Messages to offline agents are queued in the target's inbox.

route('POST', <<"/messages/send">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"agent_id">> := From, <<"to">> := To,
               <<"payload">> := Payload} = Msg} ->
            RequestId = maps:get(<<"request_id">>, Msg, undefined),
            case pluto_msg_hub:send_msg(From, To, Payload, RequestId) of
                {ok, MsgId} ->
                    {200, #{<<"status">> => <<"ok">>, <<"msg_id">> => MsgId}};
                ok ->
                    {200, #{<<"status">> => <<"ok">>}};
                {error, unknown_target} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"unknown_target">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing agent_id, to, and payload">>}}
    end;

route('POST', <<"/messages/broadcast">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"agent_id">> := From, <<"payload">> := Payload}} ->
            pluto_msg_hub:broadcast(From, Payload),
            {200, #{<<"status">> => <<"ok">>}};
        _ ->
            {400, #{<<"error">> => <<"missing agent_id and payload">>}}
    end;

%% ── Agent discovery ─────────────────────────────────────────────────
%% Query agents by attributes: POST {"filter": {"role": "code-fixer"}}
route('POST', <<"/agents/find">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"filter">> := Filter}} when is_map(Filter) ->
            Agents = pluto_msg_hub:find_agents(Filter),
            {200, #{<<"status">> => <<"ok">>, <<"agents">> => Agents}};
        _ ->
            {400, #{<<"error">> => <<"missing filter map">>}}
    end;

%% ── Detailed agent listing ──────────────────────────────────────────
route('GET', <<"/agents/list/detailed">>, _Body) ->
    AgentMaps = pluto_msg_hub:list_agents_detailed(),
    {200, #{<<"status">> => <<"ok">>, <<"agents">> => AgentMaps}};

%%====================================================================
%% HTTP Session Registration (Solutions 1, 2, 4)
%%====================================================================
%%
%% POST /agents/register — Register an agent via HTTP, returns a session
%% token. The session stays alive as long as HTTP heartbeats arrive
%% within the TTL window.
%%
%% Body: {"agent_id": "my-agent", "attributes": {...},
%%         "mode": "http"|"stateless", "ttl_ms": 300000}
%%
%% Returns: {"status":"ok", "token":"PLUTO-...", "session_id":"...",
%%           "agent_id":"...", "ttl_ms": 300000}

route('POST', <<"/agents/register">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"agent_id">> := AgentId} = Msg}
          when is_binary(AgentId), AgentId =/= <<>> ->
            Token = maps:get(<<"token">>, Msg, undefined),
            Attrs = maps:get(<<"attributes">>, Msg, #{}),
            ModeStr = maps:get(<<"mode">>, Msg, <<"http">>),
            Mode = case ModeStr of
                <<"stateless">> -> stateless;
                _               -> http
            end,
            DefaultTtl = pluto_config:get(http_session_ttl_ms,
                                          ?DEFAULT_HTTP_SESSION_TTL_MS),
            TtlMs = maps:get(<<"ttl_ms">>, Msg, DefaultTtl),
            %% Auth check
            case pluto_policy:check_auth(AgentId, Token) of
                ok ->
                    case pluto_msg_hub:register_http_agent(
                             AgentId, Attrs, Mode, TtlMs, #{}) of
                        {ok, SessToken, SessId} ->
                            {200, #{<<"status">>     => <<"ok">>,
                                    <<"token">>      => SessToken,
                                    <<"session_id">> => SessId,
                                    <<"agent_id">>   => AgentId,
                                    <<"mode">>       => ModeStr,
                                    <<"ttl_ms">>     => TtlMs}};
                        {ok, SessToken, SessId, ActualAgentId} ->
                            %% Name was taken — got a unique suffix
                            {200, #{<<"status">>     => <<"ok">>,
                                    <<"token">>      => SessToken,
                                    <<"session_id">> => SessId,
                                    <<"agent_id">>   => ActualAgentId,
                                    <<"requested_id">> => AgentId,
                                    <<"mode">>       => ModeStr,
                                    <<"ttl_ms">>     => TtlMs}}
                    end;
                {error, unauthorized} ->
                    {401, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"unauthorized">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing agent_id">>}}
    end;

%% POST /agents/heartbeat — Keep HTTP session alive
%% Body: {"token": "PLUTO-..."}
route('POST', <<"/agents/heartbeat">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token}} when is_binary(Token) ->
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    Now = erlang:system_time(millisecond),
                    {200, #{<<"status">> => <<"ok">>, <<"ts">> => Now}};
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token">>}}
    end;

%% POST /agents/unregister — Remove HTTP session
%% Body: {"token": "PLUTO-..."}
route('POST', <<"/agents/unregister">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token}} when is_binary(Token) ->
            case pluto_msg_hub:unregister_http_agent(Token) of
                ok ->
                    {200, #{<<"status">> => <<"ok">>}};
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token">>}}
    end;

%% GET /agents/poll?token=... — Poll for queued messages
route('GET', <<"/agents/poll?", Query/binary>>, _Body) ->
    Params = parse_query(Query),
    case maps:find(<<"token">>, Params) of
        {ok, Token} ->
            %% Touch session to keep alive
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    %% Look up agent_id from token
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = AgentId}] ->
                            {ok, Messages} = pluto_msg_hub:poll_inbox(AgentId),
                            {200, #{<<"status">> => <<"ok">>,
                                    <<"messages">> => Messages,
                                    <<"count">> => length(Messages)}};
                        [] ->
                            {404, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"session_not_found">>}}
                    end;
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        error ->
            {400, #{<<"error">> => <<"missing token query parameter">>}}
    end;

%% POST /agents/send — Send message as HTTP agent (with token auth)
%% Body: {"token": "PLUTO-...", "to": "agent-b", "payload": {...}}
route('POST', <<"/agents/send">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"to">> := To,
               <<"payload">> := Payload} = Msg} ->
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = From}] ->
                            RequestId = maps:get(<<"request_id">>, Msg, undefined),
                            case pluto_msg_hub:send_msg(From, To, Payload, RequestId) of
                                {ok, MsgId} ->
                                    {200, #{<<"status">> => <<"ok">>,
                                            <<"msg_id">> => MsgId}};
                                ok ->
                                    {200, #{<<"status">> => <<"ok">>}};
                                {error, unknown_target} ->
                                    {404, #{<<"status">> => <<"error">>,
                                            <<"reason">> => <<"unknown_target">>}}
                            end;
                        [] ->
                            {404, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"session_not_found">>}}
                    end;
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token, to, and payload">>}}
    end;

%% POST /agents/broadcast — Broadcast as HTTP agent (with token)
%% Body: {"token": "PLUTO-...", "payload": {...}}
route('POST', <<"/agents/broadcast">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"payload">> := Payload}} ->
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = From}] ->
                            pluto_msg_hub:broadcast(From, Payload),
                            {200, #{<<"status">> => <<"ok">>}};
                        [] ->
                            {404, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"session_not_found">>}}
                    end;
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token and payload">>}}
    end;

%% POST /agents/subscribe — Subscribe to topic as HTTP agent
route('POST', <<"/agents/subscribe">>, Body) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"topic">> := Topic}} ->
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = AgentId}] ->
                            pluto_msg_hub:subscribe(AgentId, Topic),
                            {200, #{<<"status">> => <<"ok">>}};
                        [] ->
                            {404, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"session_not_found">>}}
                    end;
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token and topic">>}}
    end;

%% ── Agent status query ──────────────────────────────────────────────
%% Must come AFTER more specific /agents/* routes to avoid shadowing.
route('GET', <<"/agents/", AgentId/binary>>, _Body)
  when AgentId =/= <<>> ->
    case pluto_msg_hub:agent_status(AgentId) of
        {ok, Info} ->
            {200, #{<<"status">> => <<"ok">>, <<"agent">> => Info}};
        {error, not_found} ->
            {404, #{<<"status">> => <<"error">>,
                    <<"reason">> => <<"not_found">>}}
    end;

%% ── Task management via HTTP ────────────────────────────────────────
route('GET', <<"/tasks">>, _Body) ->
    Tasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    {200, #{<<"status">> => <<"ok">>, <<"tasks">> => Tasks}};

route('GET', <<"/tasks/progress">>, _Body) ->
    AllTasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    StatusCounts = lists:foldl(fun(T, Acc) ->
        St = maps:get(<<"status">>, T, <<"unknown">>),
        Acc#{St => maps:get(St, Acc, 0) + 1}
    end, #{}, AllTasks),
    {200, #{<<"status">> => <<"ok">>,
            <<"total">>  => length(AllTasks),
            <<"by_status">> => StatusCounts,
            <<"tasks">> => AllTasks}};

%% ── 404 ─────────────────────────────────────────────────────────────
route('GET', _Path, _Body) ->
    {404, #{<<"error">> => <<"not_found">>}};
route('POST', _Path, _Body) ->
    {404, #{<<"error">> => <<"not_found">>}};
route(_Method, _Path, _Body) ->
    {405, #{<<"error">> => <<"method_not_allowed">>}}.

%%====================================================================
%% Internal helpers
%%====================================================================

route_events(SinceSeq, Limit) ->
    Events = pluto_event_log:query(SinceSeq, Limit),
    {200, #{<<"status">> => <<"ok">>, <<"events">> => Events}}.

decode_body(<<>>) ->
    {ok, #{}};
decode_body(Body) ->
    pluto_protocol_json:decode(Body).

format_lock(#lock{lock_ref = Ref, resource = Res, agent_id = AId,
                  mode = Mode, fencing_token = FT}) ->
    #{<<"lock_ref">> => Ref, <<"resource">> => Res,
      <<"agent_id">> => AId, <<"mode">> => atom_to_binary(Mode, utf8),
      <<"fencing_token">> => FT}.

parse_mode(<<"read">>)  -> read;
parse_mode(<<"write">>) -> write;
parse_mode(_)           -> write.

to_bin(B) when is_binary(B) -> B;
to_bin(A) when is_atom(A)   -> atom_to_binary(A, utf8);
to_bin(T)                   -> iolist_to_binary(io_lib:format("~p", [T])).

to_int(V, _Default) when is_integer(V) -> V;
to_int(V, Default) when is_binary(V) ->
    try binary_to_integer(V) catch _:_ -> Default end;
to_int(_, Default) -> Default.

parse_query(Query) ->
    Parts = binary:split(Query, <<"&">>, [global]),
    lists:foldl(fun(Part, Acc) ->
        case binary:split(Part, <<"=">>) of
            [Key, Val] -> Acc#{Key => Val};
            _          -> Acc
        end
    end, #{}, Parts).
