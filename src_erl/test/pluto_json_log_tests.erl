-module(pluto_json_log_tests).
-include_lib("eunit/include/eunit.hrl").

format_basic_test() ->
    Event = #{
        level => info,
        msg   => {string, "hello world"},
        meta  => #{time => erlang:system_time(microsecond)}
    },
    Result = pluto_json_log:format(Event, #{}),
    Bin = iolist_to_binary(Result),
    ?assert(byte_size(Bin) > 0),
    %% Should end with a newline
    ?assertEqual($\n, binary:last(Bin)),
    %% Should be valid JSON (minus trailing newline)
    JsonPart = binary:part(Bin, 0, byte_size(Bin) - 1),
    {ok, Map} = pluto_protocol_json:decode(JsonPart),
    ?assertEqual(<<"info">>, maps:get(<<"level">>, Map)),
    ?assertEqual(<<"hello world">>, maps:get(<<"msg">>, Map)).

format_with_mfa_test() ->
    Event = #{
        level => warning,
        msg   => {string, "test"},
        meta  => #{time => erlang:system_time(microsecond),
                   mfa  => {my_mod, my_fun, 2}}
    },
    Result = pluto_json_log:format(Event, #{}),
    Bin = iolist_to_binary(Result),
    JsonPart = binary:part(Bin, 0, byte_size(Bin) - 1),
    {ok, Map} = pluto_protocol_json:decode(JsonPart),
    ?assertEqual(<<"my_mod">>, maps:get(<<"module">>, Map)).

format_with_format_msg_test() ->
    Event = #{
        level => error,
        msg   => {"count: ~w", [42]},
        meta  => #{time => erlang:system_time(microsecond)}
    },
    Result = pluto_json_log:format(Event, #{}),
    Bin = iolist_to_binary(Result),
    JsonPart = binary:part(Bin, 0, byte_size(Bin) - 1),
    {ok, Map} = pluto_protocol_json:decode(JsonPart),
    ?assertEqual(<<"count: 42">>, maps:get(<<"msg">>, Map)).
