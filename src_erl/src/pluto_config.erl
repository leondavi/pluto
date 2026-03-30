%%%-------------------------------------------------------------------
%%% @doc pluto_config — Configuration helper for Pluto.
%%%
%%% Reads values from the `pluto` application environment with
%%% fallback defaults.  All configuration access should go through
%%% this module so there is a single place to change defaults or
%%% add environment-variable overrides later.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_config).

-include("pluto.hrl").

%% API
-export([get/2]).

%%====================================================================
%% API
%%====================================================================

%% @doc Retrieve a configuration value from the `pluto` application env.
%% Falls back to `Default` if the key is not set.
%%
%% Examples:
%%   pluto_config:get(tcp_port, 9000)           => 9000
%%   pluto_config:get(flush_interval, 60000)    => 60000
-spec get(atom(), term()) -> term().
get(Key, Default) ->
    application:get_env(?APP, Key, Default).
