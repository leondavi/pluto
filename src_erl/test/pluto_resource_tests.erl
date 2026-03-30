-module(pluto_resource_tests).
-include_lib("eunit/include/eunit.hrl").

normalize_binary_test() ->
    ?assertEqual({ok, <<"file:/a.txt">>}, pluto_resource:normalize(<<"file:/a.txt">>)).

normalize_list_test() ->
    ?assertEqual({ok, <<"file:/b.txt">>}, pluto_resource:normalize("file:/b.txt")).

normalize_atom_test() ->
    ?assertEqual({ok, <<"hello">>}, pluto_resource:normalize(hello)).

normalize_trims_whitespace_test() ->
    ?assertEqual({ok, <<"x">>}, pluto_resource:normalize(<<"  x  ">>)).

normalize_empty_binary_test() ->
    ?assertEqual({error, empty_resource}, pluto_resource:normalize(<<>>)).

normalize_whitespace_only_test() ->
    ?assertEqual({error, empty_resource}, pluto_resource:normalize(<<"   ">>)).

normalize_integer_test() ->
    ?assertEqual({error, empty_resource}, pluto_resource:normalize(42)).

validate_ok_test() ->
    ?assertEqual(ok, pluto_resource:validate(<<"res:1">>)).

validate_empty_test() ->
    ?assertEqual({error, empty_resource}, pluto_resource:validate(<<>>)).
