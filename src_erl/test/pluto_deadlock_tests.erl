-module(pluto_deadlock_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%% Each test creates/destroys the ETS table to avoid leaking state.

setup() ->
    ets:new(?ETS_WAIT_GRAPH, [named_table, bag, public]).

teardown(_) ->
    catch ets:delete(?ETS_WAIT_GRAPH).

no_cycle_test_() ->
    {setup, fun setup/0, fun teardown/1, fun() ->
        pluto_deadlock:add_edge(<<"A">>, <<"B">>),
        pluto_deadlock:add_edge(<<"B">>, <<"C">>),
        ?assertEqual(no_cycle, pluto_deadlock:check_cycle(<<"A">>))
    end}.

simple_cycle_test_() ->
    {setup, fun setup/0, fun teardown/1, fun() ->
        pluto_deadlock:add_edge(<<"A">>, <<"B">>),
        pluto_deadlock:add_edge(<<"B">>, <<"A">>),
        ?assertMatch({cycle, _}, pluto_deadlock:check_cycle(<<"A">>))
    end}.

three_node_cycle_test_() ->
    {setup, fun setup/0, fun teardown/1, fun() ->
        pluto_deadlock:add_edge(<<"A">>, <<"B">>),
        pluto_deadlock:add_edge(<<"B">>, <<"C">>),
        pluto_deadlock:add_edge(<<"C">>, <<"A">>),
        {cycle, Agents} = pluto_deadlock:check_cycle(<<"A">>),
        ?assert(length(Agents) >= 2)
    end}.

remove_edge_breaks_cycle_test_() ->
    {setup, fun setup/0, fun teardown/1, fun() ->
        pluto_deadlock:add_edge(<<"A">>, <<"B">>),
        pluto_deadlock:add_edge(<<"B">>, <<"A">>),
        pluto_deadlock:remove_edge(<<"B">>),
        ?assertEqual(no_cycle, pluto_deadlock:check_cycle(<<"A">>))
    end}.

empty_graph_test_() ->
    {setup, fun setup/0, fun teardown/1, fun() ->
        ?assertEqual(no_cycle, pluto_deadlock:check_cycle(<<"X">>))
    end}.
