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
                case route(Method, Path, Body, Sock) of
                    {skip_response_, already_sent} ->
                        %% Long-poll already sent response directly
                        ok;
                    {Status, RespBody} ->
                        send_http_response(Sock, Status, RespBody)
                end;
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
route('OPTIONS', _Path, _Body, _Sock) ->
    {200, #{<<"status">> => <<"ok">>}};

%% ── Health ──────────────────────────────────────────────────────────
route('GET', <<"/health">>, _Body, _Sock) ->
    {200, #{<<"status">> => <<"ok">>, <<"version">> => list_to_binary(?VERSION)}};

route('GET', <<"/ping">>, _Body, _Sock) ->
    Now = erlang:system_time(millisecond),
    {200, #{<<"status">> => <<"pong">>, <<"ts">> => Now}};

%% ── Agents ──────────────────────────────────────────────────────────
route('GET', <<"/agents">>, _Body, _Sock) ->
    Agents = pluto_msg_hub:list_agents(),
    {200, #{<<"status">> => <<"ok">>, <<"agents">> => Agents}};

%% ── Locks ───────────────────────────────────────────────────────────
route('GET', <<"/locks">>, _Body, _Sock) ->
    Locks = pluto_lock_mgr:list_locks(),
    LockMaps = [format_lock(L) || L <- Locks],
    {200, #{<<"status">> => <<"ok">>, <<"locks">> => LockMaps}};

route('POST', <<"/locks/acquire">>, Body, _Sock) ->
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

route('POST', <<"/locks/release">>, Body, _Sock) ->
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

route('POST', <<"/locks/renew">>, Body, _Sock) ->
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
route('GET', <<"/events">>, _Body, _Sock) ->
    route_events(0, 100);
route('GET', <<"/events?", Query/binary>>, _Body, _Sock) ->
    Params = parse_query(Query),
    SinceSeq = maps:get(<<"since_token">>, Params, 0),
    Limit = maps:get(<<"limit">>, Params, 100),
    route_events(to_int(SinceSeq, 0), to_int(Limit, 100));

%% ── Admin ── Fencing Seq ────────────────────────────────────────────
route('GET', <<"/admin/fencing_seq">>, _Body, _Sock) ->
    FSeq = pluto_lock_mgr:get_fencing_seq(),
    {200, #{<<"status">> => <<"ok">>, <<"fencing_seq">> => FSeq}};

%% ── Admin ── Force Release ──────────────────────────────────────────
route('POST', <<"/admin/force_release">>, Body, _Sock) ->
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
route('GET', <<"/admin/deadlock_graph">>, _Body, _Sock) ->
    Edges = ets:tab2list(?ETS_WAIT_GRAPH),
    EdgeMaps = [#{<<"waiter">> => W, <<"holder">> => H} || {W, H} <- Edges],
    {200, #{<<"status">> => <<"ok">>, <<"edges">> => EdgeMaps}};

%% ── Selftest ────────────────────────────────────────────────────────
route('POST', <<"/selftest">>, _Body, _Sock) ->
    Result = pluto_selftest:run(),
    {200, Result};

%% ── Messaging via HTTP ──────────────────────────────────────────────
%% These endpoints allow HTTP-only clients to send and broadcast messages
%% without maintaining a persistent TCP session.  The sender provides an
%% `agent_id` field in the request body (no session required).
%% Messages to offline agents are queued in the target's inbox.

route('POST', <<"/messages/send">>, Body, _Sock) ->
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

route('POST', <<"/messages/broadcast">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"agent_id">> := From, <<"payload">> := Payload}} ->
            pluto_msg_hub:broadcast(From, Payload),
            {200, #{<<"status">> => <<"ok">>}};
        _ ->
            {400, #{<<"error">> => <<"missing agent_id and payload">>}}
    end;

%% ── Agent discovery ─────────────────────────────────────────────────
%% Query agents by attributes: POST {"filter": {"role": "code-fixer"}}
route('POST', <<"/agents/find">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"filter">> := Filter}} when is_map(Filter) ->
            Agents = pluto_msg_hub:find_agents(Filter),
            {200, #{<<"status">> => <<"ok">>, <<"agents">> => Agents}};
        _ ->
            {400, #{<<"error">> => <<"missing filter map">>}}
    end;

%% ── Detailed agent listing ──────────────────────────────────────────
route('GET', <<"/agents/list/detailed">>, _Body, _Sock) ->
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

route('POST', <<"/agents/register">>, Body, _Sock) ->
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
route('POST', <<"/agents/heartbeat">>, Body, _Sock) ->
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
route('POST', <<"/agents/unregister">>, Body, _Sock) ->
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

%% GET /agents/poll?token=...&timeout=...&ack=true — Poll for queued messages
%% If timeout>0, uses long-poll: blocks until messages arrive or timeout expires.
%% If ack=true, sends delivery_ack receipts back to senders.
route('GET', <<"/agents/poll?", Query/binary>>, _Body, Sock) ->
    Params = parse_query(Query),
    case maps:find(<<"token">>, Params) of
        {ok, Token} ->
            %% Touch session to keep alive
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = AgentId}] ->
                            Timeout = to_int(maps:get(<<"timeout">>, Params, <<"0">>), 0),
                            TimeoutMs = Timeout * 1000, %% timeout is in seconds
                            SendAck = maps:get(<<"ack">>, Params, <<"false">>) =:= <<"true">>,
                            AutoBusy = maps:get(<<"auto_busy">>, Params, <<"false">>) =:= <<"true">>,
                            %% Try immediate poll first
                            {ok, Messages} = pluto_msg_hub:poll_inbox(AgentId),
                            case {Messages, TimeoutMs} of
                                {[], T} when T > 0 ->
                                    %% No messages — long-poll: wait for notification
                                    do_long_poll(Sock, AgentId, Token, T, SendAck, AutoBusy);
                                _ ->
                                    %% Messages available (or timeout=0)
                                    maybe_send_receipts(AgentId, Messages, SendAck),
                                    maybe_set_busy(AgentId, Messages, AutoBusy),
                                    {200, #{<<"status">> => <<"ok">>,
                                            <<"messages">> => Messages,
                                            <<"count">> => length(Messages)}}
                            end;
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
route('POST', <<"/agents/send">>, Body, _Sock) ->
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
route('POST', <<"/agents/broadcast">>, Body, _Sock) ->
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
route('POST', <<"/agents/subscribe">>, Body, _Sock) ->
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

%% POST /agents/update_ttl — Dynamically update session TTL
%% Body: {"token": "PLUTO-...", "ttl_ms": 600000}
route('POST', <<"/agents/update_ttl">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"ttl_ms">> := TtlMs}}
          when is_integer(TtlMs), TtlMs > 0 ->
            case pluto_msg_hub:update_http_ttl(Token, TtlMs) of
                ok ->
                    {200, #{<<"status">> => <<"ok">>, <<"ttl_ms">> => TtlMs}};
                {error, not_found} ->
                    {404, #{<<"status">> => <<"error">>,
                            <<"reason">> => <<"session_not_found">>}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token and ttl_ms">>}}
    end;

%% POST /agents/set_status — Set custom agent status
%% Body: {"token": "PLUTO-...", "custom_status": "busy"}
route('POST', <<"/agents/set_status">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"custom_status">> := CStatus}}
          when is_binary(CStatus) ->
            case pluto_msg_hub:touch_http_agent(Token) of
                ok ->
                    case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                        [#http_session{agent_id = AgentId}] ->
                            pluto_msg_hub:set_agent_status(AgentId, CStatus),
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
            {400, #{<<"error">> => <<"missing token and custom_status">>}}
    end;

%% POST /agents/task_assign — Assign a task via HTTP (token auth)
%% Body: {"token": "PLUTO-...", "assignee": "agent-b", "description": "...", "payload": {...}}
route('POST', <<"/agents/task_assign">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"assignee">> := Assignee} = Msg}
          when is_binary(Assignee) ->
            case auth_token_to_agent(Token) of
                {ok, From} ->
                    TaskId = generate_task_id(),
                    Description = maps:get(<<"description">>, Msg, <<>>),
                    Payload = maps:get(<<"payload">>, Msg, #{}),
                    Now = erlang:system_time(millisecond),
                    Task = #{
                        <<"task_id">>     => TaskId,
                        <<"from">>        => From,
                        <<"assigner">>    => From,
                        <<"assignee">>    => Assignee,
                        <<"description">> => Description,
                        <<"payload">>     => Payload,
                        <<"status">>      => <<"pending">>,
                        <<"created_at">>  => Now,
                        <<"updated_at">>  => Now
                    },
                    ets:insert(?ETS_TASKS, {TaskId, Task}),
                    Event = #{
                        <<"event">>       => ?EVT_TASK_ASSIGNED,
                        <<"task_id">>     => TaskId,
                        <<"from">>        => From,
                        <<"assignee">>    => Assignee,
                        <<"description">> => Description,
                        <<"payload">>     => Payload
                    },
                    pluto_msg_hub:broadcast(From, Event),
                    pluto_event_log:log(task_assigned, #{task_id => TaskId,
                                                         from => From,
                                                         assignee => Assignee}),
                    {200, #{<<"status">> => <<"ok">>, <<"task_id">> => TaskId}};
                {error, Reason} ->
                    {404, #{<<"status">> => <<"error">>, <<"reason">> => Reason}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token and assignee">>}}
    end;

%% POST /agents/task_update — Update task status via HTTP (token auth)
%% Body: {"token": "PLUTO-...", "task_id": "TASK-1", "status": "complete", "result": {...}}
route('POST', <<"/agents/task_update">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token, <<"task_id">> := TaskId,
               <<"status">> := NewStatus} = Msg}
          when is_binary(TaskId), is_binary(NewStatus) ->
            case auth_token_to_agent(Token) of
                {ok, AgentId} ->
                    Result = maps:get(<<"result">>, Msg, #{}),
                    case ets:lookup(?ETS_TASKS, TaskId) of
                        [{TaskId, Task}] ->
                            Now = erlang:system_time(millisecond),
                            Updated = Task#{
                                <<"status">>     => NewStatus,
                                <<"result">>     => Result,
                                <<"updated_at">> => Now
                            },
                            ets:insert(?ETS_TASKS, {TaskId, Updated}),
                            Event = #{
                                <<"event">>    => ?EVT_TASK_UPDATED,
                                <<"task_id">>  => TaskId,
                                <<"agent_id">> => AgentId,
                                <<"status">>   => NewStatus,
                                <<"result">>   => Result
                            },
                            pluto_msg_hub:broadcast(AgentId, Event),
                            pluto_event_log:log(task_updated, #{task_id => TaskId,
                                                                 agent_id => AgentId,
                                                                 status => NewStatus}),
                            {200, #{<<"status">> => <<"ok">>}};
                        [] ->
                            {404, #{<<"status">> => <<"error">>,
                                    <<"reason">> => <<"task_not_found">>}}
                    end;
                {error, Reason} ->
                    {404, #{<<"status">> => <<"error">>, <<"reason">> => Reason}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token, task_id, and status">>}}
    end;

%% POST /agents/task_list — List tasks via HTTP (token auth, optional filters)
%% Body: {"token": "PLUTO-...", "assignee": "agent-b", "status": "pending"}
route('POST', <<"/agents/task_list">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token} = Msg} ->
            case auth_token_to_agent(Token) of
                {ok, _AgentId} ->
                    AllTasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
                    FilterAssignee = maps:get(<<"assignee">>, Msg, undefined),
                    FilterStatus = maps:get(<<"status">>, Msg, undefined),
                    Filtered = lists:filter(fun(T) ->
                        MatchA = (FilterAssignee =:= undefined) orelse
                                 (maps:get(<<"assignee">>, T, undefined) =:= FilterAssignee),
                        MatchS = (FilterStatus =:= undefined) orelse
                                 (maps:get(<<"status">>, T, undefined) =:= FilterStatus),
                        MatchA andalso MatchS
                    end, AllTasks),
                    {200, #{<<"status">> => <<"ok">>, <<"tasks">> => Filtered}};
                {error, Reason} ->
                    {404, #{<<"status">> => <<"error">>, <<"reason">> => Reason}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token">>}}
    end;

%% POST /agents/task_progress — Task progress overview via HTTP (token auth)
%% Body: {"token": "PLUTO-..."}
route('POST', <<"/agents/task_progress">>, Body, _Sock) ->
    case decode_body(Body) of
        {ok, #{<<"token">> := Token}} ->
            case auth_token_to_agent(Token) of
                {ok, _AgentId} ->
                    AllTasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
                    ByStatus = lists:foldl(fun(T, Acc) ->
                        St = maps:get(<<"status">>, T, <<"unknown">>),
                        Acc#{St => maps:get(St, Acc, 0) + 1}
                    end, #{}, AllTasks),
                    ByAgent = lists:foldl(fun(T, Acc) ->
                        Agent = maps:get(<<"assignee">>, T, <<"unassigned">>),
                        St = maps:get(<<"status">>, T, <<"unknown">>),
                        AgentMap = maps:get(Agent, Acc, #{}),
                        Acc#{Agent => AgentMap#{St => maps:get(St, AgentMap, 0) + 1}}
                    end, #{}, AllTasks),
                    {200, #{<<"status">> => <<"ok">>,
                            <<"total">>  => length(AllTasks),
                            <<"by_status">> => ByStatus,
                            <<"by_agent">>  => ByAgent}};
                {error, Reason} ->
                    {404, #{<<"status">> => <<"error">>, <<"reason">> => Reason}}
            end;
        _ ->
            {400, #{<<"error">> => <<"missing token">>}}
    end;

%% ── Agent status query ──────────────────────────────────────────────
%% Must come AFTER more specific /agents/* routes to avoid shadowing.
route('GET', <<"/agents/", AgentId/binary>>, _Body, _Sock)
  when AgentId =/= <<>> ->
    case pluto_msg_hub:agent_status(AgentId) of
        {ok, Info} ->
            {200, #{<<"status">> => <<"ok">>, <<"agent">> => Info}};
        {error, not_found} ->
            {404, #{<<"status">> => <<"error">>,
                    <<"reason">> => <<"not_found">>}}
    end;

%% ── Task management via HTTP ────────────────────────────────────────
route('GET', <<"/tasks">>, _Body, _Sock) ->
    Tasks = [T || {_Id, T} <- ets:tab2list(?ETS_TASKS)],
    {200, #{<<"status">> => <<"ok">>, <<"tasks">> => Tasks}};

route('GET', <<"/tasks/progress">>, _Body, _Sock) ->
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
route('GET', _Path, _Body, _Sock) ->
    {404, #{<<"error">> => <<"not_found">>}};
route('POST', _Path, _Body, _Sock) ->
    {404, #{<<"error">> => <<"not_found">>}};
route(_Method, _Path, _Body, _Sock) ->
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

%% @private Authenticate a token and return the agent_id.
auth_token_to_agent(Token) ->
    case pluto_msg_hub:touch_http_agent(Token) of
        ok ->
            case ets:lookup(?ETS_HTTP_SESSIONS, Token) of
                [#http_session{agent_id = AgentId}] ->
                    {ok, AgentId};
                [] ->
                    {error, <<"session_not_found">>}
            end;
        {error, not_found} ->
            {error, <<"session_not_found">>}
    end.

%% @private Generate a unique task ID (same as pluto_session).
generate_task_id() ->
    N = erlang:unique_integer([monotonic, positive]),
    iolist_to_binary(io_lib:format("TASK-~w", [N])).

%% @private Long-poll: register waiter, block until message or timeout.
%% Sends the HTTP response directly from this process.
do_long_poll(Sock, AgentId, Token, TimeoutMs, SendAck, AutoBusy) ->
    %% Cap timeout at 60 seconds
    CappedTimeout = min(TimeoutMs, 60000),
    pluto_msg_hub:register_long_poll(AgentId),
    receive
        {long_poll_notify, AgentId} ->
            pluto_msg_hub:unregister_long_poll(AgentId),
            %% Touch session again
            pluto_msg_hub:touch_http_agent(Token),
            %% Poll inbox directly via ETS (avoid gen_server ordering issues)
            Messages = poll_with_retry(AgentId, 5),
            maybe_send_receipts(AgentId, Messages, SendAck),
            maybe_set_busy(AgentId, Messages, AutoBusy),
            %% Send response directly
            send_http_response(Sock, 200, #{
                <<"status">> => <<"ok">>,
                <<"messages">> => Messages,
                <<"count">> => length(Messages),
                <<"long_poll">> => true
            }),
            %% Return a sentinel that the caller should NOT send another response
            {skip_response_, already_sent}
    after CappedTimeout ->
        pluto_msg_hub:unregister_long_poll(AgentId),
        %% Check one more time in case a message arrived just before timeout
        {ok, Messages} = pluto_msg_hub:poll_inbox(AgentId),
        maybe_send_receipts(AgentId, Messages, SendAck),
        maybe_set_busy(AgentId, Messages, AutoBusy),
        {200, #{<<"status">> => <<"ok">>,
                <<"messages">> => Messages,
                <<"count">> => length(Messages),
                <<"long_poll">> => true,
                <<"timed_out">> => (Messages =:= [])}}
    end.

%% @private Poll inbox directly via ETS without going through gen_server.
%% Used by long-poll to avoid potential ordering issues.
direct_poll_inbox(AgentId) ->
    Keys = ets:match(?ETS_MSG_INBOX, {{AgentId, '$1'}, '_'}),
    SortedSeqs = lists:sort([S || [S] <- Keys]),
    lists:filtermap(fun(Seq) ->
        Key = {AgentId, Seq},
        case ets:lookup(?ETS_MSG_INBOX, Key) of
            [{_, Event}] ->
                ets:delete(?ETS_MSG_INBOX, Key),
                {true, Event};
            [] ->
                false
        end
    end, SortedSeqs).

%% @private Poll inbox with retry. The notification may arrive slightly before
%% the ETS insert is visible to this process (rare but possible under load).
poll_with_retry(AgentId, 0) ->
    direct_poll_inbox(AgentId);
poll_with_retry(AgentId, Retries) ->
    case direct_poll_inbox(AgentId) of
        [] ->
            timer:sleep(20),
            poll_with_retry(AgentId, Retries - 1);
        Messages ->
            Messages
    end.

%% @private Send delivery receipts for polled messages.
maybe_send_receipts(_AgentId, _Messages, false) -> ok;
maybe_send_receipts(_AgentId, [], _) -> ok;
maybe_send_receipts(AgentId, Messages, true) ->
    lists:foreach(fun(Msg) ->
        case maps:find(<<"from">>, Msg) of
            {ok, Sender} when Sender =/= AgentId ->
                MsgId = maps:get(<<"msg_id">>, Msg, undefined),
                AckPayload = #{
                    <<"event">>    => <<"delivery_ack">>,
                    <<"msg_id">>   => MsgId,
                    <<"to">>       => AgentId,
                    <<"delivered">> => true,
                    <<"acked_at">> => erlang:system_time(millisecond)
                },
                %% Queue the ack to the sender's inbox (don't fail if sender gone)
                catch pluto_msg_hub:send_msg(AgentId, Sender, AckPayload);
            _ -> ok
        end
    end, Messages).

%% @private Auto-set agent status to "processing" when messages are polled.
maybe_set_busy(_AgentId, [], _AutoBusy) -> ok;
maybe_set_busy(_AgentId, _Messages, false) -> ok;
maybe_set_busy(AgentId, _Messages, true) ->
    pluto_msg_hub:set_agent_status(AgentId, <<"processing">>),
    %% Schedule revert to idle after 30 seconds
    spawn(fun() ->
        timer:sleep(30000),
        %% Only revert if still "processing"
        case pluto_msg_hub:agent_status(AgentId) of
            {ok, #{<<"custom_status">> := <<"processing">>}} ->
                pluto_msg_hub:set_agent_status(AgentId, <<"idle">>);
            _ -> ok
        end
    end).
