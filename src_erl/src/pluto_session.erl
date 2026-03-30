%%%-------------------------------------------------------------------
%%% @doc pluto_session — Handles one client TCP connection.
%%%
%%% Each connected agent gets its own session process.  The session:
%%%   1. Generates a unique session_id on creation.
%%%   2. Reads newline-delimited JSON from the socket.
%%%   3. Dispatches requests to pluto_lock_mgr and pluto_msg_hub.
%%%   4. Sends JSON responses back on the socket.
%%%   5. Receives async events from other modules and pushes them
%%%      to the client.
%%%
%%% Socket mode: `{active, once}`.  After each received packet we
%%% re-arm with `inet:setopts/2` to prevent mailbox flooding.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_session).

-include("pluto.hrl").

%% API
-export([start/1]).

%% Internal entry point (called by proc_lib)
-export([init/1]).

%% Session loop state
-record(sess, {
    socket     :: gen_tcp:socket(),
    session_id :: binary(),
    agent_id   :: binary() | undefined,  %% set after register
    buffer     :: binary()               %% partial line accumulator
}).

%%====================================================================
%% API
%%====================================================================

%% @doc Spawn a new session process for the given client socket.
%% The caller should transfer socket ownership after this returns.
-spec start(gen_tcp:socket()) -> {ok, pid()}.
start(Socket) ->
    Pid = proc_lib:spawn(?MODULE, init, [Socket]),
    {ok, Pid}.

%%====================================================================
%% Session lifecycle
%%====================================================================

%% @doc Initialise the session: generate a session_id and wait for
%% the socket to be transferred to us.
init(Socket) ->
    SessionId = generate_session_id(),
    receive
        socket_ready -> ok
    after 5000 ->
        exit(socket_transfer_timeout)
    end,
    %% Set active-once mode to receive one packet at a time
    inet:setopts(Socket, [{active, once}]),
    loop(#sess{socket = Socket, session_id = SessionId,
               agent_id = undefined, buffer = <<>>}).

%%====================================================================
%% Main receive loop
%%====================================================================

%% @private The main event loop.  Handles three kinds of messages:
%%   - TCP data from the client socket
%%   - Internal events pushed by other Pluto modules
%%   - Socket close / error
loop(#sess{socket = Sock} = S) ->
    receive
        %% ── Incoming TCP data ───────────────────────────────────
        {tcp, Sock, Data} ->
            NewBuf = <<(S#sess.buffer)/binary, Data/binary>>,
            case check_line_length(NewBuf) of
                ok ->
                    S2 = process_lines(S#sess{buffer = NewBuf}),
                    inet:setopts(Sock, [{active, once}]),
                    loop(S2);
                too_long ->
                    send_error(Sock, <<"line_too_long">>),
                    gen_tcp:close(Sock),
                    cleanup(S)
            end;

        %% ── Socket closed by client ─────────────────────────────
        {tcp_closed, Sock} ->
            cleanup(S);

        %% ── Socket error ────────────────────────────────────────
        {tcp_error, Sock, _Reason} ->
            gen_tcp:close(Sock),
            cleanup(S);

        %% ── Async event from Pluto internals ────────────────────
        {pluto_event, Event} when is_map(Event) ->
            send_json(Sock, Event),
            loop(S);

        %% ── Takeover: another session claimed our agent_id ──────
        {pluto_takeover, _AgentId} ->
            gen_tcp:close(Sock),
            cleanup(S);

        %% ── Catch-all ───────────────────────────────────────────
        _Other ->
            loop(S)
    end.

%%====================================================================
%% Line processing
%%====================================================================

%% @private Extract complete lines from the buffer and process each one.
%% Returns the session state with the remaining (incomplete) buffer.
process_lines(#sess{buffer = Buf} = S) ->
    case binary:split(Buf, <<"\n">>) of
        [Line, Rest] ->
            S2 = handle_line(string:trim(Line), S),
            process_lines(S2#sess{buffer = Rest});
        [_Incomplete] ->
            %% No complete line yet — keep buffering
            S
    end.

%% @private Handle a single complete JSON line from the client.
handle_line(<<>>, S) ->
    S;  %% Ignore empty lines
handle_line(Line, #sess{socket = Sock, session_id = SessId} = S) ->
    %% Update liveness timestamp on every received message
    pluto_heartbeat:touch(SessId),

    case pluto_protocol_json:decode(Line) of
        {ok, Msg} ->
            handle_request(Msg, S);
        {error, _} ->
            send_error(Sock, ?ERR_BAD_REQUEST),
            S
    end.

%%====================================================================
%% Request dispatch
%%====================================================================

%% @private Route a decoded JSON request to the appropriate handler.
handle_request(#{<<"op">> := ?OP_REGISTER} = Msg, S) ->
    handle_register(Msg, S);
handle_request(#{<<"op">> := ?OP_PING}, S) ->
    handle_ping(S);
handle_request(#{<<"op">> := _Op}, #sess{agent_id = undefined} = S) ->
    %% All operations except register and ping require registration
    send_error(S#sess.socket, ?ERR_NOT_REGISTERED),
    S;
handle_request(#{<<"op">> := ?OP_ACQUIRE} = Msg, S) ->
    handle_acquire(Msg, S);
handle_request(#{<<"op">> := ?OP_RELEASE} = Msg, S) ->
    handle_release(Msg, S);
handle_request(#{<<"op">> := ?OP_RENEW} = Msg, S) ->
    handle_renew(Msg, S);
handle_request(#{<<"op">> := ?OP_SEND} = Msg, S) ->
    handle_send(Msg, S);
handle_request(#{<<"op">> := ?OP_BROADCAST} = Msg, S) ->
    handle_broadcast(Msg, S);
handle_request(#{<<"op">> := ?OP_LIST_AGENTS}, S) ->
    handle_list_agents(S);
handle_request(#{<<"op">> := _}, S) ->
    send_error(S#sess.socket, ?ERR_UNKNOWN_OP),
    S;
handle_request(_, S) ->
    send_error(S#sess.socket, ?ERR_BAD_REQUEST),
    S.

%%====================================================================
%% Operation handlers
%%====================================================================

%% ── Register ────────────────────────────────────────────────────────
handle_register(#{<<"agent_id">> := AgentId}, #sess{socket = Sock,
                                                     session_id = SessId} = S)
  when is_binary(AgentId), AgentId =/= <<>> ->
    case pluto_msg_hub:register_agent(AgentId, SessId, self()) of
        {ok, SessId} ->
            HbMs = pluto_config:get(heartbeat_interval_ms,
                                    ?DEFAULT_HEARTBEAT_INTERVAL_MS),
            send_json(Sock, #{
                <<"status">>               => ?STATUS_OK,
                <<"session_id">>           => SessId,
                <<"heartbeat_interval_ms">> => HbMs
            }),
            S#sess{agent_id = AgentId};
        {error, already_registered} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_ALREADY_REGISTERED
            }),
            S
    end;
handle_register(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Ping ────────────────────────────────────────────────────────────
handle_ping(#sess{socket = Sock} = S) ->
    HbMs = pluto_config:get(heartbeat_interval_ms, ?DEFAULT_HEARTBEAT_INTERVAL_MS),
    Now  = erlang:system_time(millisecond),
    send_json(Sock, #{
        <<"status">>               => ?STATUS_PONG,
        <<"ts">>                   => Now,
        <<"heartbeat_interval_ms">> => HbMs
    }),
    S.

%% ── Acquire lock ────────────────────────────────────────────────────
handle_acquire(Msg, #sess{socket = Sock, session_id = SessId,
                          agent_id = AgentId} = S) ->
    case maps:find(<<"resource">>, Msg) of
        {ok, RawResource} ->
            case pluto_resource:normalize(RawResource) of
                {ok, Resource} ->
                    Mode   = parse_mode(maps:get(<<"mode">>, Msg, ?MODE_WRITE)),
                    TtlMs  = maps:get(<<"ttl_ms">>, Msg, 30000),
                    MaxWait = maps:get(<<"max_wait_ms">>, Msg, undefined),
                    Opts = #{
                        ttl_ms      => TtlMs,
                        max_wait_ms => MaxWait,
                        session_id  => SessId,
                        session_pid => self()
                    },
                    case pluto_lock_mgr:acquire(Resource, Mode, AgentId, Opts) of
                        {ok, LockRef, FToken} ->
                            send_json(Sock, #{
                                <<"status">>        => ?STATUS_OK,
                                <<"lock_ref">>      => LockRef,
                                <<"fencing_token">> => FToken
                            });
                        {wait, WaitRef} ->
                            send_json(Sock, #{
                                <<"status">>   => ?STATUS_WAIT,
                                <<"wait_ref">> => WaitRef
                            });
                        {error, deadlock} ->
                            send_json(Sock, #{
                                <<"status">> => ?STATUS_ERROR,
                                <<"reason">> => ?ERR_DEADLOCK,
                                <<"victim">> => true
                            });
                        {error, Reason} ->
                            send_json(Sock, #{
                                <<"status">> => ?STATUS_ERROR,
                                <<"reason">> => to_bin(Reason)
                            })
                    end;
                {error, empty_resource} ->
                    send_error(Sock, ?ERR_BAD_REQUEST)
            end;
        error ->
            send_error(Sock, ?ERR_BAD_REQUEST)
    end,
    S.

%% ── Release lock ────────────────────────────────────────────────────
handle_release(#{<<"lock_ref">> := LockRef}, #sess{socket = Sock,
                                                    agent_id = AgentId} = S)
  when is_binary(LockRef) ->
    case pluto_lock_mgr:release(LockRef, AgentId) of
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, not_found} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_NOT_FOUND
            })
    end,
    S;
handle_release(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Renew lock ──────────────────────────────────────────────────────
handle_renew(#{<<"lock_ref">> := LockRef} = Msg, #sess{socket = Sock} = S)
  when is_binary(LockRef) ->
    TtlMs = maps:get(<<"ttl_ms">>, Msg, 30000),
    case pluto_lock_mgr:renew(LockRef, #{ttl_ms => TtlMs}) of
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, not_found} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_NOT_FOUND
            })
    end,
    S;
handle_renew(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Send direct message ────────────────────────────────────────────
handle_send(#{<<"to">> := To, <<"payload">> := Payload},
            #sess{socket = Sock, agent_id = From} = S)
  when is_binary(To) ->
    case pluto_msg_hub:send_msg(From, To, Payload) of
        ok ->
            send_json(Sock, #{<<"status">> => ?STATUS_OK});
        {error, unknown_target} ->
            send_json(Sock, #{
                <<"status">> => ?STATUS_ERROR,
                <<"reason">> => ?ERR_UNKNOWN_TARGET
            })
    end,
    S;
handle_send(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── Broadcast ───────────────────────────────────────────────────────
handle_broadcast(#{<<"payload">> := Payload}, #sess{socket = Sock,
                                                     agent_id = From} = S) ->
    pluto_msg_hub:broadcast(From, Payload),
    send_json(Sock, #{<<"status">> => ?STATUS_OK}),
    S;
handle_broadcast(_, #sess{socket = Sock} = S) ->
    send_error(Sock, ?ERR_BAD_REQUEST),
    S.

%% ── List agents ─────────────────────────────────────────────────────
handle_list_agents(#sess{socket = Sock} = S) ->
    Agents = pluto_msg_hub:list_agents(),
    send_json(Sock, #{
        <<"status">> => ?STATUS_OK,
        <<"agents">> => Agents
    }),
    S.

%%====================================================================
%% Socket helpers
%%====================================================================

%% @private Send a JSON map as a line on the socket.
send_json(Sock, Map) ->
    gen_tcp:send(Sock, pluto_protocol_json:encode_line(Map)).

%% @private Send a standard error response.
send_error(Sock, Reason) ->
    send_json(Sock, #{<<"status">> => ?STATUS_ERROR, <<"reason">> => Reason}).

%%====================================================================
%% Cleanup
%%====================================================================

%% @private Unregister the agent and clean up when the session ends.
cleanup(#sess{agent_id = undefined}) ->
    ok;
cleanup(#sess{agent_id = AgentId, session_id = SessId}) ->
    ?LOG_INFO("Session ~s (agent ~s) disconnected", [SessId, AgentId]),
    pluto_msg_hub:unregister_agent(AgentId),
    ok.

%%====================================================================
%% Utilities
%%====================================================================

%% @private Generate a unique session ID in the form `sess-<uuid>`.
generate_session_id() ->
    %% Use crypto:strong_rand_bytes for a 128-bit random session ID
    Bytes = crypto:strong_rand_bytes(16),
    Hex = binary:encode_hex(Bytes),
    LowerHex = string:lowercase(Hex),
    %% Format as sess-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    <<A:8/binary, B:4/binary, C:4/binary, D:4/binary, E:12/binary, _/binary>> = LowerHex,
    iolist_to_binary([<<"sess-">>, A, $-, B, $-, C, $-, D, $-, E]).

%% @private Parse a lock mode binary into an atom.
parse_mode(?MODE_WRITE) -> write;
parse_mode(?MODE_READ)  -> read;
parse_mode(_)           -> write.  %% Default to write (exclusive)

%% @private Convert a term to binary for JSON error reasons.
to_bin(B) when is_binary(B)  -> B;
to_bin(A) when is_atom(A)    -> atom_to_binary(A, utf8);
to_bin(L) when is_list(L)    -> list_to_binary(L);
to_bin(T)                    -> iolist_to_binary(io_lib:format("~p", [T])).

%% @private Check that the accumulated buffer doesn't exceed the max line length.
check_line_length(Buf) ->
    case byte_size(Buf) > ?MAX_LINE_LENGTH of
        true  -> too_long;
        false -> ok
    end.
