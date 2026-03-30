%%%-------------------------------------------------------------------
%%% @doc pluto_listener_sup — Supervisor for TCP listeners.
%%%
%%% Manages one or more TCP listener processes.  Currently starts
%%% a single `pluto_tcp_listener` on the configured port.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_listener_sup).
-behaviour(supervisor).

-include("pluto.hrl").

%% API
-export([start_link/0]).

%% Supervisor callback
-export([init/1]).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    supervisor:start_link({local, ?MODULE}, ?MODULE, []).

%%====================================================================
%% Supervisor callback
%%====================================================================

init([]) ->
    SupFlags = #{
        strategy  => one_for_one,
        intensity => 5,
        period    => 10
    },
    Children = [
        #{
            id       => pluto_tcp_listener,
            start    => {pluto_tcp_listener, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_tcp_listener]
        },
        #{
            id       => pluto_http_listener,
            start    => {pluto_http_listener, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_http_listener]
        }
    ],
    {ok, {SupFlags, Children}}.
