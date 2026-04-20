%%%-------------------------------------------------------------------
%%% @doc pluto_app — OTP application entry point for Pluto.
%%%
%%% This module is the first code that runs when the Pluto application
%%% starts.  It prints the startup banner, initialises ETS tables, and
%%% launches the top-level supervisor.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_app).
-behaviour(application).

-include("pluto.hrl").

%% Application callbacks
-export([start/2, stop/1]).

%%====================================================================
%% Application callbacks
%%====================================================================

%% @doc Called by the OTP application controller when `application:start(pluto)`
%% is invoked.  We create all shared ETS tables here (before any child
%% process tries to read them) and then hand control to the supervisor.
start(_StartType, _StartArgs) ->
    print_banner(),
    create_ets_tables(),
    case pluto_sup:start_link() of
        {ok, Pid} ->
            print_server_info(),
            {ok, Pid};
        Error ->
            Error
    end.

%% @doc Called on clean shutdown.  Triggers a final persistence flush.
stop(_State) ->
    ?LOG_INFO("Pluto shutting down..."),
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Create all ETS tables used by Pluto.
%% Tables are owned by the calling process (the application master),
%% which means they survive individual gen_server restarts.
create_ets_tables() ->
    ets:new(?ETS_LOCKS,      [named_table, set, public,
                              {keypos, #lock.lock_ref}]),
    ets:new(?ETS_AGENTS,     [named_table, set, public,
                              {keypos, #agent.agent_id}]),
    ets:new(?ETS_SESSIONS,   [named_table, set, public,
                              {keypos, #session.session_id}]),
    ets:new(?ETS_WAITERS,    [named_table, ordered_set, public,
                              {keypos, 1}]),
    ets:new(?ETS_WAIT_GRAPH, [named_table, bag, public]),
    ets:new(?ETS_LIVENESS,   [named_table, set, public]),
    ets:new(?ETS_TASKS,      [named_table, set, public]),
    ets:new(?ETS_MSG_INBOX,  [named_table, ordered_set, public]),
    ets:new(?ETS_HTTP_SESSIONS, [named_table, set, public,
                                 {keypos, #http_session.token}]),
    ets:new(?ETS_LONG_POLL, [named_table, set, public]),
    ets:new(?ETS_GRACE_TIMERS, [named_table, set, public]),
    %% Ensure signal directory exists
    SignalDir = pluto_config:get(signal_dir, ?DEFAULT_SIGNAL_DIR),
    filelib:ensure_dir(filename:join(SignalDir, "dummy")),
    ok.

%% @private Print the Pluto ASCII art banner on startup.
print_banner() ->
    Banner =
        "\n"
        "    +---------------------------------------------------+\n"
        "    |                                                   |\n"
        "    |   PPPPPP  L       U     U TTTTTTT  OOOOO         |\n"
        "    |   P    P  L       U     U    T    O     O        |\n"
        "    |   PPPPPP  L       U     U    T    O     O        |\n"
        "    |   P       L       U     U    T    O     O        |\n"
        "    |   P       LLLLLL   UUUUU     T     OOOOO         |\n"
        "    |                                                   |\n"
        "    |       . . .  Agent Coordination Server  . . .     |\n"
        "    |                                                   |\n"
        "    +---------------------------------------------------+\n"
        "\n"
        "    Pluto is a lightweight coordination server for\n"
        "    multi-agent workflows. It provides distributed\n"
        "    locking, message passing, and resource management\n"
        "    over a simple JSON-over-TCP protocol.\n"
        "\n",
    io:format("~s", [Banner]).

%% @private Print connection details so operators (and agents) know
%% how to reach this server.
print_server_info() ->
    Port = pluto_config:get(tcp_port, ?DEFAULT_TCP_PORT),
    HttpPort = pluto_config:get(http_port, ?DEFAULT_HTTP_PORT),
    Host = get_hostname(),
    IP   = get_local_ip(),
    HbMs = pluto_config:get(heartbeat_interval_ms, ?DEFAULT_HEARTBEAT_INTERVAL_MS),

    HttpStr = case HttpPort of
                  disabled -> "disabled";
                  _        -> integer_to_list(HttpPort)
              end,

    io:format(
        "  +--- Server Details ------------------------------------+~n"
        "  |                                                       |~n"
        "  |  Hostname  : ~-41s|~n"
        "  |  IP        : ~-41s|~n"
        "  |  TCP Port  : ~-41w|~n"
        "  |  HTTP Port : ~-41s|~n"
        "  |  Protocol  : ~-41s|~n"
        "  |  Version   : ~-41s|~n"
        "  |                                                       |~n"
        "  +--- How Agents Connect --------------------------------+~n"
        "  |                                                       |~n"
        "  |  TCP:  Connect to ~s:~w~s|~n"
        "  |         Send newline-delimited JSON requests          |~n"
        "  |  HTTP: REST API on port ~-29s|~n"
        "  |                                                       |~n"
        "  |  1. First TCP message must be:                        |~n"
        "  |     {\"op\":\"register\",\"agent_id\":\"<name>\"}           |~n"
        "  |  2. Send ping every ~w ms to stay alive~s|~n"
        "  |  3. Read the agent_guide.md for full protocol         |~n"
        "  |                                                       |~n"
        "  +-------------------------------------------------------+~n"
        "~n"
        "  Quick Start:~n"
        "    ./PlutoClient.sh ping          Check connectivity~n"
        "    ./PlutoClient.sh stats         View server statistics~n"
        "    ./PlutoServer.sh --status      Check server status~n"
        "    ./PlutoServer.sh --kill        Stop the server~n"
        "~n"
        "  Starting an Agent:~n"
        "    1. Connect via TCP to this server on port 9000~n"
        "    2. Send: {\"op\":\"register\",\"agent_id\":\"<name>\"}~n"
        "    3. Acquire locks before touching shared resources~n"
        "    4. Send/receive messages to coordinate with peers~n"
        "    5. Ping every 15s to stay alive~n"
        "    Run: ./PlutoClient.sh guide --output agent_guide.md~n"
        "~n"
        "  Server is ready. Waiting for agent connections...~n~n",
        [Host, IP, Port, HttpStr,
         "JSON over TCP + REST/HTTP",
         ?VERSION,
         Host, Port, padding(Host, Port, 28),
         HttpStr,
         HbMs, padding_int(HbMs, 24)
        ]),
    ?LOG_NOTICE("Pluto v~s listening on ~s:~w (TCP), HTTP: ~s", [?VERSION, IP, Port, HttpStr]).

%% @private Get the system hostname as a string.
get_hostname() ->
    case inet:gethostname() of
        {ok, Name} -> Name;
        _          -> "localhost"
    end.

%% @private Resolve the first non-loopback IPv4 address, or fall back
%% to 127.0.0.1.
get_local_ip() ->
    case inet:getifaddrs() of
        {ok, Addrs} ->
            find_ipv4(Addrs);
        _ ->
            "127.0.0.1"
    end.

%% @private Walk interface list looking for a non-loopback IPv4 address.
find_ipv4([]) ->
    "127.0.0.1";
find_ipv4([{_If, Props} | Rest]) ->
    case find_addr_in_props(Props) of
        {ok, Addr} -> Addr;
        none       -> find_ipv4(Rest)
    end.

find_addr_in_props([]) ->
    none;
find_addr_in_props([{addr, {A, B, C, D}} | _Rest])
  when not (A =:= 127 andalso B =:= 0 andalso C =:= 0 andalso D =:= 1) ->
    Str = lists:flatten(io_lib:format("~w.~w.~w.~w", [A, B, C, D])),
    {ok, Str};
find_addr_in_props([_ | Rest]) ->
    find_addr_in_props(Rest).

%% @private Generate whitespace padding so the box lines up.
padding(Host, Port, FieldWidth) ->
    Len = length(Host) + length(integer_to_list(Port)) + 1, %% "host:port"
    Pad = FieldWidth - Len,
    lists:duplicate(max(0, Pad), $\s).

padding_int(Val, FieldWidth) ->
    Len = length(integer_to_list(Val)) + 4, %% " ms "
    Pad = FieldWidth - Len,
    lists:duplicate(max(0, Pad), $\s).
