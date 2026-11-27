%%% Unit tests for the SmartTasks modules that need no running server.
%%% End-to-end behaviour: tests/test_smart_tasks_live.py.
-module(pluto_job_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

-define(DIR, "/tmp/pluto/test_job_unit").

src(Data) -> src(Data, true).
src(Data, Closed) ->
    Path = filename:join(?DIR, "out"),
    ok = filelib:ensure_dir(Path),
    ok = file:write_file(Path, Data),
    #{path => Path, lo => 0, hi => byte_size(Data), man => undefined,
      closed => Closed}.

%%====================================================================
%% read
%%====================================================================

read_lines_test() ->
    S = src(<<"one\ntwo\nthree">>),
    ?assertMatch(#{<<"text">> := <<"two\n">>, <<"first_line">> := 2,
                   <<"last_line">> := 2},
                 pluto_job_read:read(S, 4, 100, #{lines => 1})),
    ?assertMatch(#{<<"eof">> := true, <<"ends_mid_line">> := true,
                   <<"last_line">> := 3}, pluto_job_read:read(S, 0, 100, #{})),
    ?assertEqual(8, pluto_job_read:offset_of_line(S, 3)),
    ?assertEqual(13, pluto_job_read:offset_of_line(S, 99)),
    ?assertEqual(4, pluto_job_read:tail_start(S, 2, 100)),
    %% a tail that starts mid-line skips the partial line
    ?assertEqual(8, pluto_job_read:tail_start(S, 9, 7)),
    ?assertEqual(8, pluto_job_read:last_line_end(S, 0)).

read_empty_and_beyond_test() ->
    ?assertMatch(#{<<"text">> := <<>>, <<"eof">> := true},
                 pluto_job_read:read(src(<<>>), 0, 10, #{})),
    ?assertMatch(#{<<"start">> := 2, <<"text">> := <<>>},
                 pluto_job_read:read(src(<<"a\n">>), 99, 10, #{})).

utf8_is_held_back_only_while_more_may_come_test() ->
    <<Part:4/binary, _/binary>> = <<"ab", "漢"/utf8>>,
    ?assertMatch(#{<<"text">> := <<"ab">>},
                 pluto_job_read:read(src(Part, false), 0, 10, #{})),
    #{<<"text">> := T} = pluto_job_read:read(src(Part, true), 0, 10, #{}),
    ?assertEqual(<<"ab", 16#FFFD/utf8, 16#FFFD/utf8>>, T).

sanitize_test() ->
    ?assertEqual(<<"a", 16#FFFD/utf8, "b">>, pluto_job_read:sanitize(<<"a", 255, "b">>)),
    ?assertEqual(<<"é"/utf8>>, pluto_job_read:sanitize(<<"é"/utf8>>)).

%%====================================================================
%% grep
%%====================================================================

grep(Data, P, Opts) ->
    {ok, R} = pluto_job_grep:grep(src(Data), P, Opts#{from => 0}),
    {[[maps:get(<<"line">>, E) || E <- G] || G <- maps:get(<<"groups">>, R)], R}.

grep_context_merges_test() ->
    D = <<"1\n2\nX\n4\nX\n6\n7\n8\n9\nX\n">>,
    ?assertMatch({[[2, 3, 4, 5, 6], [9, 10]], _},
                 grep(D, <<"X">>, #{before => 1, 'after' => 1})),
    ?assertMatch({[[3], [5], [10]], _}, grep(D, <<"X">>, #{})),
    ?assertMatch({[[3]], #{<<"complete">> := false, <<"resume_at">> := 8}},
                 grep(D, <<"X">>, #{max => 1})).

grep_options_test() ->
    ?assertMatch({[[2]], _}, grep(<<"axb\na.b\n">>, <<"a.b">>, #{fixed => true})),
    ?assertMatch({[[1]], _}, grep(<<"FOO\n">>, <<"foo">>, #{ignore_case => true})),
    ?assertMatch({[[2]], _}, grep(<<"a\nb">>, <<"a">>, #{invert => true})),
    ?assertMatch({error, _}, pluto_job_grep:grep(src(<<>>), <<"(">>, #{from => 0})).

grep_backtracking_is_bounded_test() ->
    L = binary:copy(<<"a">>, 40000),
    {_, R} = grep(<<L/binary, "b\n">>, <<"(a+)+$">>, #{}),
    ?assertEqual(1, maps:get(<<"lines_over_regex_limit">>, R)).

cut_at_test() ->
    S = src(<<"== A\n1\n== B\n2\n">>),
    ?assertEqual({ok, {[{0, 7}, {7, 14}], null}}, pluto_job_streams:cut_at(S, <<"^==">>)),
    ?assertEqual({ok, {[{0, 7}, {7, null}], null}},
                 pluto_job_streams:cut_at(S#{closed := false}, <<"^==">>)),
    ?assertEqual({error, <<"pattern_not_found">>}, pluto_job_streams:cut_at(S, <<"zz">>)).

%%====================================================================
%% store, schema, launcher, cursor, registry
%%====================================================================

ids_test() ->
    ?assert(pluto_job_store:valid_id(pluto_job_store:new_id())),
    [?assertNot(pluto_job_store:valid_id(X)) ||
        X <- [<<"../x">>, <<"J-20260101-0123456789abcdef\n">>, <<>>, 5,
              <<"J-20260101-0123456789ABCDEF">>]].

key_permissions_test() ->
    Home = filename:join(?DIR, "home"),
    _ = file:del_dir_r(Home),
    application:set_env(pluto, smarttasks_home, Home),
    try
        ok = pluto_job_store:ensure_home(),
        {ok, K} = pluto_job_store:key(),
        ?assertEqual(64, byte_size(K)),
        File = filename:join(Home, ?HOME_FILE_KEY),
        ok = file:change_mode(File, 8#644),
        ?assertEqual({error, key_permissions_too_open}, pluto_job_store:key()),
        ok = file:delete(File),
        ok = file:make_symlink("/etc/hosts", File),
        ?assertEqual({error, key_not_a_regular_file}, pluto_job_store:key()),
        %% an empty key is what a power loss leaves; it is replaced
        ok = file:delete(File), ok = file:write_file(File, <<>>),
        ok = file:change_mode(File, 8#600),
        ok = pluto_job_store:ensure_home(),
        ?assertMatch({ok, _}, pluto_job_store:key())
    after application:unset_env(pluto, smarttasks_home)
    end.

script_failures_are_not_death_test() ->
    application:set_env(pluto, smarttasks_python, "/no/such/python"),
    ?assertEqual(unknown, pluto_job_store:ident(1)),
    application:set_env(pluto, smarttasks_python, "/usr/bin/false"),
    ?assertEqual(unknown, pluto_job_store:ident(1)),
    application:unset_env(pluto, smarttasks_python),
    {ok, Me} = pluto_job_store:ident(list_to_binary(os:getpid())),
    ?assertNotEqual(<<>>, Me),
    ?assertEqual({ok, <<>>}, pluto_job_store:ident(4194300)).

schema_test() ->
    ?assertEqual(ok, pluto_job_schema:check(
        #{<<"offset">> => 0, <<"name">> => null, <<"tags">> => [<<"a">>],
          <<"env">> => #{<<"A">> => <<"b">>}, <<"other">> => [1]})),
    [?assertMatch({error, _}, pluto_job_schema:check(M)) ||
        M <- [#{<<"offset">> => -1}, #{<<"offset">> => <<"1">>},
              #{<<"stdin">> => 1}, #{<<"command">> => <<"a", 0>>},
              #{<<"env">> => #{<<"A=">> => <<"b">>}}, #{<<"timeout_s">> => 0},
              #{<<"tags">> => [1]}]].

cursor_plan_test() ->
    C = #{<<"pos">> => 9, <<"last_req">> => <<"r">>, <<"last_start">> => 4,
          <<"last_end">> => 9},
    ?assertEqual({replay, 4, 9}, pluto_job_cursor:plan(C, #{<<"request_id">> => <<"r">>}, 0)),
    ?assertEqual({read, 9}, pluto_job_cursor:plan(C, #{}, 0)),
    ?assertEqual({read, 2}, pluto_job_cursor:plan(C, #{<<"from_offset">> => 2}, 0)),
    ?assertEqual({read, 3}, pluto_job_cursor:plan(#{<<"pos">> => <<"x">>}, #{}, 3)).

%%====================================================================
%% notices of watch rules
%%====================================================================

with_home(Fun) ->
    Home = filename:join(?DIR, "home"),
    _ = file:del_dir_r(Home),
    ok = filelib:ensure_path(Home),
    Old = os:getenv("PLUTO_SMARTTASKS_HOME", ""),
    os:putenv("PLUTO_SMARTTASKS_HOME", Home),
    Tabs = [T || T <- [?ETS_JOBS], ets:info(T) =:= undefined],
    [ets:new(T, [named_table, public]) || T <- Tabs],
    try Fun()
    after
        os:putenv("PLUTO_SMARTTASKS_HOME", Old),
        [ets:delete(T) || T <- Tabs]
    end.

moved_hits_keep_their_order_and_survive_a_restart_test() ->
    with_home(fun() ->
        Id = <<"J-20260101-0123456789abcdef">>,
        ets:insert(?ETS_JOBS, {Id, #{<<"id">> => Id}, undefined}),
        P = #{<<"id">> => Id},
        B1 = lists:foldl(fun(H, B) ->
            pluto_job_notice:notify(<<"a">>, ?EVT_JOB_WATCH, P, <<"running">>,
                                    #{<<"h">> => H, <<"text">> => <<"t">>}, B)
        end, #{}, [1, 2, 3]),
        %% the first hit was seen before the transfer
        {[#{<<"seq">> := S1} | _], _} = pluto_job_notice:page(<<"a">>, 0, B1),
        {_, B2} = pluto_job_notice:page(<<"a">>, S1, B1),
        B3 = pluto_job_notice:move(Id, <<"a">>, <<"b">>, B2),
        ?assertEqual({[], B3}, pluto_job_notice:page(<<"a">>, 0, B3)),
        Hs = fun(B) -> [H || #{<<"h">> := H} <- maps:get(<<"b">>, B)] end,
        ?assertEqual([2, 3], Hs(B3)),
        {B4, Told} = pluto_job_notice:load(),
        ?assertEqual([2, 3], Hs(B4)),
        ?assertEqual(#{Id => 3}, Told),
        %% what was announced outlives acknowledged notices and compaction
        #{<<"b">> := Ns} = B4,
        {[], _} = pluto_job_notice:page(<<"b">>, maps:get(<<"seq">>, lists:last(Ns)), B4),
        {B5, Told} = pluto_job_notice:load(),
        ?assertEqual(#{}, B5),
        ?assertEqual({#{}, Told}, pluto_job_notice:load()),
        %% and goes with the job
        ets:delete(?ETS_JOBS, Id),
        ?assertEqual({#{}, #{}}, pluto_job_notice:load())
    end).

a_flood_of_hits_keeps_the_end_of_a_job_test() ->
    with_home(fun() ->
        P = #{<<"id">> => <<"J-20260101-0123456789abcdef">>},
        B0 = pluto_job_notice:notify(<<"a">>, ?EVT_JOB_FINISHED, P, <<"failed">>, #{}),
        B = lists:foldl(fun(H, Acc) ->
            pluto_job_notice:notify(<<"a">>, ?EVT_JOB_WATCH, P, <<"running">>,
                                    #{<<"h">> => H}, Acc)
        end, B0, lists:seq(1, ?JOB_NOTICE_KEEP + 50)),
        L = maps:get(<<"a">>, B),
        ?assertEqual(?JOB_NOTICE_KEEP, length(L)),
        ?assertMatch([#{<<"event">> := ?EVT_JOB_FINISHED}, #{<<"h">> := 52} | _], L)
    end).

request_schema_of_analyze_and_watch_test() ->
    Ok = fun(M) -> pluto_job_schema:check(M) end,
    ?assertEqual(ok, Ok(#{<<"rows">> => true, <<"where">> => [<<"a > 1">>],
                          <<"idle_s">> => 1.5, <<"once">> => false})),
    ?assertEqual(ok, Ok(#{<<"rows">> => [<<"a">>]})),
    [?assertMatch({error, <<"bad_value_for_", _/binary>>}, Ok(M))
     || M <- [#{<<"rows">> => 1}, #{<<"where">> => <<"a">>}, #{<<"by">> => [1]},
              #{<<"idle_s">> => -1}, #{<<"idle_s">> => <<"x">>}, #{<<"count">> => 1.5},
              #{<<"match">> => <<0>>}]].

%%====================================================================
%% watch.json
%%====================================================================

hit(Rule) -> #{<<"rule">> => Rule, <<"name">> => Rule, <<"line">> => 1,
               <<"then">> => <<"notify">>, <<"text">> => <<"t">>}.
act(Text) -> (hit(<<"R1">>))#{<<"then">> := <<"append">>,
                              <<"act">> => #{<<"kind">> => <<"append">>,
                                             <<"text">> => Text}}.

actions_wait_in_order_and_are_not_told_test() ->
    D = pluto_job_watch_doc:add([act(<<"x">>), hit(<<"R2">>), act(<<"y">>)], 0,
                                pluto_job_watch_doc:empty()),
    ?assertMatch(#{<<"a">> := 2, <<"h">> := 1,
                   <<"acts">> := [#{<<"a">> := 1, <<"text">> := <<"x">>},
                                  #{<<"a">> := 2, <<"text">> := <<"y">>}],
                   <<"hits">> := [#{<<"rule">> := <<"R2">>}]}, D),
    D1 = pluto_job_watch_doc:done(1, ok, 0, D),
    ?assertMatch(#{<<"acts">> := [#{<<"a">> := 2}], <<"h">> := 1}, D1),
    ?assertEqual(unknown, pluto_job_watch_doc:done(1, ok, 0, D1)),
    ?assertMatch(#{<<"acts">> := [], <<"h">> := 2,
                   <<"hits">> := [_, #{<<"text">> := <<"action failed: full">>}]},
                 pluto_job_watch_doc:done(2, {error, <<"full">>}, 0, D1)).

last_action_of_a_rule_is_told_test() ->
    D = pluto_job_watch_doc:add([(act(<<"x">>))#{<<"last">> => true}], 0,
                                pluto_job_watch_doc:empty()),
    ?assertMatch(#{<<"a">> := 1, <<"hits">> := [#{<<"last">> := true}]}, D),
    #{<<"hits">> := [H]} = D,
    ?assertNot(is_map_key(<<"act">>, H)).

hits_are_kept_until_told_test() ->
    Many = [hit(<<"R1">>) || _ <- lists:seq(1, 40)],
    Add = fun(Told, D) -> pluto_job_watch_doc:add(Many, Told, D) end,
    D = Add(0, Add(0, Add(0, pluto_job_watch_doc:empty()))),
    ?assertEqual(120, length(maps:get(<<"hits">>, D))),
    ?assertEqual(50, length(maps:get(<<"hits">>, Add(120, D)))).

parameters_become_placeholders_test() ->
    Ok = fun(P) -> pluto_job_schema:check(#{<<"params">> => P}) end,
    ?assertEqual(ok, Ok(#{<<"k">> => <<"1">>})),
    ?assertMatch({error, _}, Ok(#{<<"_k">> => <<"1">>})),
    ?assertMatch({error, _}, Ok(#{<<"a/b">> => <<"1">>})),
    ?assertMatch({error, _}, Ok(#{<<"k">> => 1})),
    ?assertMatch({error, _}, pluto_job_schema:check(#{<<"watch">> => [#{<<"match">> => 1}]})),
    ?assertEqual(ok, pluto_job_schema:check(#{<<"extract">> => [<<"a">>, <<"b">>]})).
