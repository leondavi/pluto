%%% Unit tests for pluto_persistence:drop_stale_agents/3 — the filter
%%% applied at snapshot save and snapshot restore so that long-disconnected
%%% agents do not accumulate forever in the persisted state file (and thus
%%% do not reappear in pluto_msg_hub:list_agents_detailed/0 across restarts).
-module(pluto_stale_agents_tests).
-include_lib("eunit/include/eunit.hrl").
-include("pluto.hrl").

%% Helper: build an #agent{} with just the field we care about.
agent(Id, LastSeen) ->
    #agent{agent_id = Id, last_seen = LastSeen, status = disconnected}.

drop_stale_agents_keeps_fresh_test() ->
    Now = 1_000_000,
    StaleAfter = 1_000,  %% 1 second
    Fresh = agent(<<"a">>, Now - 500),
    Old   = agent(<<"b">>, Now - 5_000),
    Result = pluto_persistence:drop_stale_agents([Fresh, Old], Now, StaleAfter),
    ?assertEqual([Fresh], Result).

drop_stale_agents_at_threshold_kept_test() ->
    %% last_seen exactly StaleAfter ms ago is kept (boundary inclusive).
    Now = 1_000_000,
    StaleAfter = 1_000,
    Boundary = agent(<<"boundary">>, Now - StaleAfter),
    ?assertEqual([Boundary],
                 pluto_persistence:drop_stale_agents([Boundary], Now, StaleAfter)).

drop_stale_agents_just_past_threshold_dropped_test() ->
    Now = 1_000_000,
    StaleAfter = 1_000,
    Stale = agent(<<"stale">>, Now - StaleAfter - 1),
    ?assertEqual([],
                 pluto_persistence:drop_stale_agents([Stale], Now, StaleAfter)).

drop_stale_agents_default_seven_days_test() ->
    %% The default constant is 7 days. Agents older than 7 days must be
    %% dropped; agents younger must be kept.
    Now = erlang:system_time(millisecond),
    SevenDaysMs = ?DEFAULT_AGENT_STALE_AFTER_MS,
    Fresh = agent(<<"yesterday">>, Now - 86_400_000),       %% 1 day ago
    Old   = agent(<<"month-ago">>, Now - 30 * 86_400_000),  %% 30 days ago
    Result = pluto_persistence:drop_stale_agents([Fresh, Old], Now, SevenDaysMs),
    ?assertEqual([Fresh], Result).

drop_stale_agents_passes_through_non_records_test() ->
    %% Defensive: non-#agent{} entries (corrupt or future-format snapshots)
    %% are kept rather than silently discarded.
    Now = 1_000_000,
    StaleAfter = 1_000,
    Junk = some_other_term,
    ?assertEqual([Junk],
                 pluto_persistence:drop_stale_agents([Junk], Now, StaleAfter)).

drop_stale_agents_empty_list_test() ->
    ?assertEqual([], pluto_persistence:drop_stale_agents([], 1, 1)).
