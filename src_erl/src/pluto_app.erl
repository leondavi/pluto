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
        "\n",
    io:format("~s", [Banner]).

%% @private Print connection details so operators (and agents) know
%% how to reach this server.
print_server_info() ->
    Port = pluto_config:get(tcp_port, ?DEFAULT_TCP_PORT),
    Host = get_hostname(),
    IP   = get_local_ip(),
    HbMs = pluto_config:get(heartbeat_interval_ms, ?DEFAULT_HEARTBEAT_INTERVAL_MS),

    io:format(
        "  +--- Server Details ------------------------------------+~n"
        "  |                                                       |~n"
        "  |  Hostname : ~-42s|~n"
        "  |  IP       : ~-42s|~n"
        "  |  Port     : ~-42w|~n"
        "  |  Protocol : ~-42s|~n"
        "  |  Version  : ~-42s|~n"
        "  |                                                       |~n"
        "  +--- How Agents Connect --------------------------------+~n"
        "  |                                                       |~n"
        "  |  1. Open a TCP connection to ~s:~w~s|~n"
        "  |  2. Send newline-delimited JSON requests              |~n"
        "  |  3. First message must be:                            |~n"
        "  |     {\"op\":\"register\",\"agent_id\":\"<name>\"}           |~n"
        "  |  4. Send ping every ~w ms to stay alive~s|~n"
        "  |  5. Read the agent_guide.md for full protocol         |~n"
        "  |                                                       |~n"
        "  +-------------------------------------------------------+~n~n",
        [Host, IP, Port, "Newline-delimited JSON over TCP",
         ?VERSION,
         Host, Port, padding(Host, Port, 19),
         HbMs, padding_int(HbMs, 24)
        ]),
    ?LOG_INFO("Pluto v~s listening on ~s:~w", [?VERSION, IP, Port]).

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
