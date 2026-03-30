%%%-------------------------------------------------------------------
%%% @doc pluto_json_log — OTP logger formatter that outputs JSON lines.
%%%
%%% Plugs into the OTP logger framework.  When configured as the
%%% formatter for a handler, all log messages are emitted as single-
%%% line JSON objects to the handler's output (stdout, file, etc.).
%%%
%%% Configuration example in sys.config:
%%%   {kernel, [
%%%     {logger, [
%%%       {handler, default, logger_std_h,
%%%         #{formatter => {pluto_json_log, #{}}}}
%%%     ]}
%%%   ]}
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_json_log).

%% Logger formatter callback
-export([format/2]).

%%====================================================================
%% Formatter callback
%%====================================================================

%% @doc Format a log event as a JSON line.
-spec format(logger:log_event(), logger:formatter_config()) -> unicode:chardata().
format(#{level := Level, msg := Msg, meta := Meta}, _Config) ->
    Ts = format_timestamp(Meta),
    MsgBin = format_msg(Msg),
    Base = #{
        <<"level">> => atom_to_binary(Level, utf8),
        <<"ts">>    => Ts,
        <<"msg">>   => MsgBin
    },
    %% Add useful metadata fields if present
    WithMod = case maps:find(mfa, Meta) of
        {ok, {M, F, A}} ->
            Base#{<<"module">> => atom_to_binary(M, utf8),
                  <<"function">> => iolist_to_binary(io_lib:format("~s/~w", [F, A]))};
        _ ->
            Base
    end,
    WithPid = case maps:find(pid, Meta) of
        {ok, Pid} -> WithMod#{<<"pid">> => list_to_binary(pid_to_list(Pid))};
        _         -> WithMod
    end,
    try
        [json:encode(WithPid), $\n]
    catch
        _:_ ->
            %% Fallback if json encode fails
            [io_lib:format("{\"level\":\"~s\",\"msg\":\"~s\"}~n",
                           [Level, MsgBin])]
    end.

%%====================================================================
%% Internal
%%====================================================================

%% @private Format the timestamp from metadata.
format_timestamp(#{time := Ts}) ->
    list_to_binary(calendar:system_time_to_rfc3339(
        Ts div 1000000, [{unit, microsecond}, {offset, "Z"}]));
format_timestamp(_) ->
    list_to_binary(calendar:system_time_to_rfc3339(
        erlang:system_time(microsecond), [{unit, microsecond}, {offset, "Z"}])).

%% @private Format the message part of a log event.
format_msg({string, Msg}) ->
    iolist_to_binary(Msg);
format_msg({report, Report}) when is_map(Report) ->
    iolist_to_binary(io_lib:format("~p", [Report]));
format_msg({report, Report}) ->
    iolist_to_binary(io_lib:format("~p", [Report]));
format_msg({Fmt, Args}) ->
    iolist_to_binary(io_lib:format(Fmt, Args)).
