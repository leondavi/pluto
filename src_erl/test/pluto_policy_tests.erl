-module(pluto_policy_tests).
-include_lib("eunit/include/eunit.hrl").

%%====================================================================
%% check_auth tests
%%====================================================================

%% When no agent_tokens configured, everything passes.
auth_open_mode_test() ->
    application:unset_env(pluto, agent_tokens),
    ?assertEqual(ok, pluto_policy:check_auth(<<"agent-1">>, undefined)).

auth_valid_token_test() ->
    application:set_env(pluto, agent_tokens, #{<<"agent-1">> => <<"secret123">>}),
    ?assertEqual(ok, pluto_policy:check_auth(<<"agent-1">>, <<"secret123">>)),
    application:unset_env(pluto, agent_tokens).

auth_invalid_token_test() ->
    application:set_env(pluto, agent_tokens, #{<<"agent-1">> => <<"secret123">>}),
    ?assertEqual({error, unauthorized}, pluto_policy:check_auth(<<"agent-1">>, <<"wrong">>)),
    application:unset_env(pluto, agent_tokens).

auth_unknown_agent_no_wildcard_test() ->
    application:set_env(pluto, agent_tokens, #{<<"agent-1">> => <<"t1">>}),
    ?assertEqual({error, unauthorized}, pluto_policy:check_auth(<<"unknown">>, <<"t1">>)),
    application:unset_env(pluto, agent_tokens).

auth_wildcard_token_test() ->
    application:set_env(pluto, agent_tokens, #{<<"*">> => <<"global">>}),
    ?assertEqual(ok, pluto_policy:check_auth(<<"any-agent">>, <<"global">>)),
    ?assertEqual({error, unauthorized}, pluto_policy:check_auth(<<"any">>, <<"bad">>)),
    application:unset_env(pluto, agent_tokens).

%%====================================================================
%% check_acl tests
%%====================================================================

acl_open_mode_test() ->
    application:unset_env(pluto, acl),
    ?assertEqual(ok, pluto_policy:check_acl(<<"a">>, <<"res:1">>, write)).

acl_empty_rules_test() ->
    application:set_env(pluto, acl, []),
    ?assertEqual(ok, pluto_policy:check_acl(<<"a">>, <<"res:1">>, write)),
    application:unset_env(pluto, acl).

acl_exact_match_test() ->
    Rules = [{<<"agent-1">>, <<"res:a">>, [write, read]}],
    application:set_env(pluto, acl, Rules),
    ?assertEqual(ok, pluto_policy:check_acl(<<"agent-1">>, <<"res:a">>, write)),
    application:unset_env(pluto, acl).

acl_deny_wrong_agent_test() ->
    Rules = [{<<"agent-1">>, <<"res:a">>, [write]}],
    application:set_env(pluto, acl, Rules),
    ?assertEqual({error, unauthorized}, pluto_policy:check_acl(<<"agent-2">>, <<"res:a">>, write)),
    application:unset_env(pluto, acl).

acl_deny_wrong_mode_test() ->
    Rules = [{<<"agent-1">>, <<"res:a">>, [read]}],
    application:set_env(pluto, acl, Rules),
    ?assertEqual({error, unauthorized}, pluto_policy:check_acl(<<"agent-1">>, <<"res:a">>, write)),
    application:unset_env(pluto, acl).

acl_wildcard_agent_test() ->
    Rules = [{<<"*">>, <<"res:*">>, [read, write]}],
    application:set_env(pluto, acl, Rules),
    ?assertEqual(ok, pluto_policy:check_acl(<<"any">>, <<"res:foo">>, read)),
    application:unset_env(pluto, acl).

acl_agent_prefix_wildcard_test() ->
    Rules = [{<<"coder-*">>, <<"project:*">>, [write, read]}],
    application:set_env(pluto, acl, Rules),
    ?assertEqual(ok, pluto_policy:check_acl(<<"coder-1">>, <<"project:abc">>, write)),
    ?assertEqual({error, unauthorized}, pluto_policy:check_acl(<<"reader-1">>, <<"project:abc">>, write)),
    application:unset_env(pluto, acl).
