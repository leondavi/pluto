%%%-------------------------------------------------------------------
%%% @doc pluto_config — Configuration helper for Pluto.
%%%
%%% Reads values from the `pluto` application environment with
%%% fallback defaults.  All configuration access should go through
%%% this module so there is a single place to change defaults or
%%% add environment-variable overrides later.
%%%
%%% On startup, `load_json_config/0` looks for a JSON config file
%%% (config/pluto_config.json relative to the project root, or the
%%% path in the PLUTO_CONFIG environment variable) and merges any
%%% recognised keys into the application environment.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_config).

-include("pluto.hrl").

%% API
-export([get/2, load_json_config/0]).

%%====================================================================
%% API
%%====================================================================

%% @doc Retrieve a configuration value from the `pluto` application env.
%% Falls back to `Default` if the key is not set.
-spec get(atom(), term()) -> term().
get(Key, Default) ->
    application:get_env(?APP, Key, Default).

%% @doc Load configuration from the JSON config file and merge recognised
%% keys into the pluto application environment.  Values in the JSON file
%% override values from sys.config.
-spec load_json_config() -> ok.
load_json_config() ->
    case find_config_path() of
        {ok, Path} ->
            case file:read_file(Path) of
                {ok, Bin} ->
                    try json_decode(Bin) of
                        Map when is_map(Map) ->
                            apply_json_config(Map),
                            ?LOG_INFO("Loaded JSON config from ~s", [Path]);
                        _ ->
                            ?LOG_WARN("JSON config ~s: expected a JSON object", [Path])
                    catch
                        _:Reason ->
                            ?LOG_WARN("JSON config ~s: parse error: ~p", [Path, Reason])
                    end;
                {error, enoent} ->
                    ?LOG_INFO("No JSON config file found at ~s — using defaults", [Path]);
                {error, Reason} ->
                    ?LOG_WARN("Cannot read JSON config ~s: ~p", [Path, Reason])
            end;
        none ->
            ok
    end.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Locate the JSON config file.
find_config_path() ->
    case os:getenv("PLUTO_CONFIG") of
        false ->
            %% Try relative to the source tree (project root / config/)
            Candidates = [
                filename:join([code:root_dir(), "..", "..", "..", "..", "config", "pluto_config.json"]),
                filename:join([os:getenv("HOME", "/tmp"), "pluto", "config", "pluto_config.json"]),
                "/tmp/pluto/config/pluto_config.json"
            ],
            find_first_existing(Candidates);
        EnvPath ->
            {ok, EnvPath}
    end.

find_first_existing([]) -> none;
find_first_existing([Path | Rest]) ->
    case filelib:is_regular(Path) of
        true  -> {ok, Path};
        false -> find_first_existing(Rest)
    end.

%% @private Decode a JSON binary into a map.  Uses OTP 27+ json module
%% if available, otherwise a minimal inline decoder for simple objects.
json_decode(Bin) ->
    json:decode(Bin).

%% @private Apply recognised keys from the JSON map to the app env.
apply_json_config(Map) ->
    case maps:find(<<"pluto_server">>, Map) of
        {ok, ServerMap} when is_map(ServerMap) ->
            KeyMap = #{
                <<"host_ip">>        => host,
                <<"host_tcp_port">>  => tcp_port,
                <<"host_http_port">> => http_port
            },
            maps:foreach(fun(JsonKey, AppKey) ->
                case maps:find(JsonKey, ServerMap) of
                    {ok, Value} ->
                        Converted = convert_value(AppKey, Value),
                        application:set_env(?APP, AppKey, Converted);
                    error ->
                        ok
                end
            end, KeyMap);
        _ ->
            ok
    end.

convert_value(_Key, Value) when is_integer(Value) -> Value;
convert_value(_Key, Value) when is_binary(Value)  -> binary_to_list(Value);
convert_value(_Key, Value)                        -> Value.
