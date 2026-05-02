%%%-------------------------------------------------------------------
%%% @doc pluto_tcp_listener — TCP accept loop.
%%%
%%% Opens a listening socket on the configured port and spawns a
%%% new `pluto_session` process for each incoming connection.
%%% Runs as a gen_server that loops on `gen_tcp:accept/1` in a
%%% non-blocking fashion by using `prim_inet:async_accept/2`.
%%%
%%% For simplicity in V1, we use a synchronous accept loop in a
%%% dedicated process.  Each accepted connection spawns a session
%%% process that owns the client socket.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_tcp_listener).
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
    Port    = pluto_config:get(tcp_port,    ?DEFAULT_TCP_PORT),
    Backlog = pluto_config:get(tcp_backlog, ?DEFAULT_TCP_BACKLOG),

    Ip = pluto_config:get_bind_ip(),
    Opts = [
        binary,
        {packet, line},          %% line-delimited framing
        {active, false},         %% we control when to read
        {reuseaddr, true},
        {backlog, Backlog},
        {buffer, 65536},
        {recbuf, 65536},
        {ip, Ip}
    ],

    case gen_tcp:listen(Port, Opts) of
        {ok, ListenSock} ->
            ?LOG_INFO("pluto_tcp_listener listening on ~s:~w",
                      [inet:ntoa(Ip), Port]),
            warn_if_exposed(Ip, tcp, Port),
            %% Kick off the accept loop
            self() ! accept,
            {ok, #state{listen_sock = ListenSock, port = Port}};
        {error, Reason} ->
            ?LOG_ERROR("pluto_tcp_listener failed to listen on port ~w: ~p",
                       [Port, Reason]),
            {stop, {listen_failed, Reason}}
    end.

handle_call(_Request, _From, State) ->
    {reply, ok, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

%% ── Accept loop ─────────────────────────────────────────────────────
%% We accept one connection at a time, spawn a session, then immediately
%% re-enter the accept loop.  gen_tcp:accept/2 with a timeout prevents
%% blocking forever, allowing the process to handle other messages.
handle_info(accept, #state{listen_sock = LSock} = State) ->
    case gen_tcp:accept(LSock, 1000) of
        {ok, ClientSock} ->
            %% Spawn a session process for this connection
            {ok, Pid} = pluto_session:start(ClientSock),
            %% Transfer socket ownership to the session process
            gen_tcp:controlling_process(ClientSock, Pid),
            %% Tell the session it now owns the socket
            Pid ! socket_ready,
            self() ! accept,
            {noreply, State};
        {error, timeout} ->
            %% No pending connection — loop again
            self() ! accept,
            {noreply, State};
        {error, closed} ->
            ?LOG_WARN("Listener socket closed"),
            {stop, normal, State};
        {error, Reason} ->
            ?LOG_ERROR("Accept error: ~p", [Reason]),
            %% Brief pause before retrying
            erlang:send_after(100, self(), accept),
            {noreply, State}
    end;

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, #state{listen_sock = LSock}) ->
    gen_tcp:close(LSock),
    ok.

%% @private If the listener is bound to anything other than loopback, the
%% server is reachable from the LAN / world. Make that visible in the log.
warn_if_exposed({127, _, _, _}, _Proto, _Port) -> ok;
warn_if_exposed({0, 0, 0, 0, 0, 0, 0, 1}, _Proto, _Port) -> ok;
warn_if_exposed(Ip, Proto, Port) ->
    ?LOG_WARN("⚠ Pluto ~p listener bound to ~s:~w — Pluto is EXPOSED to the network. "
              "Set pluto_server.host_ip to 127.0.0.1 in config/pluto_config.json to "
              "restrict access to this host.",
              [Proto, inet:ntoa(Ip), Port]).
