%%%-------------------------------------------------------------------
%%% @doc pluto_sup — Top-level supervisor for Pluto.
%%%
%%% Supervision tree:
%%%   pluto_sup
%%%   ├── pluto_persistence   (started first — loads snapshots)
%%%   ├── pluto_lock_mgr      (lock management gen_server)
%%%   ├── pluto_msg_hub       (messaging & registry gen_server)
%%%   ├── pluto_heartbeat     (liveness sweeper gen_server)
%%%   └── pluto_listener_sup  (supervisor for TCP listeners)
%%%
%%% All children use `one_for_one` strategy — a crash in one process
%%% does not take down the others.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_sup).
-behaviour(supervisor).

-include("pluto.hrl").

%% API
-export([start_link/0]).

%% Supervisor callback
-export([init/1]).

%%====================================================================
%% API
%%====================================================================

%% @doc Start the top-level supervisor and register it.
start_link() ->
    supervisor:start_link({local, ?MODULE}, ?MODULE, []).

%%====================================================================
%% Supervisor callback
%%====================================================================

%% @doc Define the child specifications.
%% Children are started in the order listed:
%%   1. Persistence — loads state from disk before others need it.
%%   2. Lock manager — needs ETS tables (created by pluto_app).
%%   3. Message hub — agent registry and routing.
%%   4. Heartbeat — periodic liveness sweep.
%%   5. Listener supervisor — starts TCP acceptors.
init([]) ->
    SupFlags = #{
        strategy  => one_for_one,
        intensity => 10,       %% max 10 restarts
        period    => 60        %%   within 60 seconds
    },

    Children = [
        %% 1) Persistence manager — loads saved state before anything else
        #{
            id       => pluto_persistence,
            start    => {pluto_persistence, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_persistence]
        },
        %% 2) Lock manager — coordinates resource locks
        #{
            id       => pluto_lock_mgr,
            start    => {pluto_lock_mgr, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_lock_mgr]
        },
        %% 3) Message hub — agent registration, messaging, broadcast
        #{
            id       => pluto_msg_hub,
            start    => {pluto_msg_hub, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_msg_hub]
        },
        %% 4) Heartbeat sweeper — checks liveness on a timer
        #{
            id       => pluto_heartbeat,
            start    => {pluto_heartbeat, start_link, []},
            restart  => permanent,
            shutdown => 5000,
            type     => worker,
            modules  => [pluto_heartbeat]
        },
        %% 5) Listener supervisor — manages TCP listener children
        #{
            id       => pluto_listener_sup,
            start    => {pluto_listener_sup, start_link, []},
            restart  => permanent,
            shutdown => infinity,
            type     => supervisor,
            modules  => [pluto_listener_sup]
        }
    ],

    {ok, {SupFlags, Children}}.
