-module(pluto_lease_tests).
-include_lib("eunit/include/eunit.hrl").

now_ms_positive_test() ->
    N = pluto_lease:now_ms(),
    ?assert(is_integer(N)).

make_expires_at_future_test() ->
    Exp = pluto_lease:make_expires_at(5000),
    Now = pluto_lease:now_ms(),
    ?assert(Exp > Now).

is_expired_false_test() ->
    Exp = pluto_lease:make_expires_at(60000),
    ?assertNot(pluto_lease:is_expired(Exp)).

is_expired_true_test() ->
    Past = pluto_lease:now_ms() - 1,
    ?assert(pluto_lease:is_expired(Past)).

remaining_ms_positive_test() ->
    Exp = pluto_lease:make_expires_at(5000),
    Rem = pluto_lease:remaining_ms(Exp),
    ?assert(Rem > 0),
    ?assert(Rem =< 5000).

remaining_ms_negative_test() ->
    Past = pluto_lease:now_ms() - 100,
    ?assert(pluto_lease:remaining_ms(Past) < 0).
