-module(pluto_protocol_json_tests).
-include_lib("eunit/include/eunit.hrl").

encode_map_test() ->
    Bin = pluto_protocol_json:encode(#{<<"a">> => 1}),
    ?assert(is_binary(Bin)),
    {ok, Decoded} = pluto_protocol_json:decode(Bin),
    ?assertEqual(1, maps:get(<<"a">>, Decoded)).

encode_line_has_newline_test() ->
    Bin = pluto_protocol_json:encode_line(#{<<"x">> => true}),
    ?assertEqual($\n, binary:last(Bin)).

decode_ok_test() ->
    {ok, Map} = pluto_protocol_json:decode(<<"{\"k\":\"v\"}">>),
    ?assertEqual(<<"v">>, maps:get(<<"k">>, Map)).

decode_invalid_test() ->
    ?assertMatch({error, _}, pluto_protocol_json:decode(<<"not json">>)).

roundtrip_test() ->
    Original = #{<<"op">> => <<"ping">>, <<"ts">> => 12345},
    Encoded = pluto_protocol_json:encode(Original),
    {ok, Decoded} = pluto_protocol_json:decode(Encoded),
    ?assertEqual(<<"ping">>, maps:get(<<"op">>, Decoded)),
    ?assertEqual(12345, maps:get(<<"ts">>, Decoded)).
