%%%-------------------------------------------------------------------
%%% @doc pluto_job_sup — Isolates SmartStreams from the rest of Pluto.
%%%
%%% pluto_sup starts this supervisor as `temporary': if jobs fail again
%%% and again it gives up alone, and locks and messaging keep working.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_sup).
-behaviour(supervisor).

-export([start_link/0, init/1]).

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    supervisor:start_link({local, ?MODULE}, ?MODULE, []).

init([]) ->
    {ok, {#{strategy => one_for_one, intensity => 20, period => 60},
          [#{id => pluto_job_mgr, start => {pluto_job_mgr, start_link, []},
             restart => permanent, shutdown => 5000, type => worker,
             modules => [pluto_job_mgr]}]}}.
