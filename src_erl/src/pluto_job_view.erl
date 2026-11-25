%%%-------------------------------------------------------------------
%%% @doc pluto_job_view — What clients see: cards, lists, statistics.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_view).

-include("pluto.hrl").

-export([card/2, list/1, open/2, stats/0]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).

%% @doc Status card of one job for one agent. Never fails: a package that
%% cannot be described becomes an error card instead of breaking lists.
-spec card(pkg(), agent_id()) -> map().
card(#{<<"id">> := Id} = P, Agent) ->
    try card(P, pluto_job_registry:state(P), pluto_job_cursor:position(Id, Agent))
    catch _:R ->
        ?LOG_ERROR("smartstreams: cannot describe ~s: ~p", [Id, R]),
        #{<<"job_id">> => Id, <<"state">> => <<"unreadable">>, <<"size">> => 0,
          <<"cursor">> => 0, <<"unread">> => 0, <<"elapsed_ms">> => 0,
          <<"queued_ms">> => 0}
    end.

card(#{<<"id">> := Id, <<"opened_at">> := Opened} = P, St, Pos) ->
    Size = size(Id, St),
    Now = now_ms(),
    Started = opt(<<"started_at">>, St, null),
    End = opt(<<"ended_at">>, St, Now),
    {Elapsed, Queued} = case is_integer(Started) of
                            true  -> {End - Started, Started - Opened};
                            false -> {0, End - Opened}
                        end,
    Extra = #{<<"job_id">> => Id,
              <<"host">> => maps:get(<<"hostname">>, opt(<<"origin">>, P, #{}), null),
              <<"activity">> => activity(St, Now),
              <<"started_at">> => Started,
              <<"elapsed_ms">> => max(0, Elapsed),
              <<"queued_ms">> => max(0, Queued),
              <<"paused_ms">> => opt(<<"paused_ms">>, St, 0),
              <<"size">> => Size, <<"cursor">> => Pos,
              <<"unread">> => max(0, Size - Pos)},
    FromState = maps:with([<<"state">>, <<"reason">>, <<"exit_code">>, <<"signal">>,
                           <<"ended_at">>, <<"sha256">>, <<"output_truncated">>,
                           <<"leader_exit_code">>], St),
    maps:filter(fun(_, V) -> V =/= null end,
                maps:merge(maps:merge(maps:with(?JOB_CARD_KEYS, P), FromState), Extra)).

size(Id, St) ->
    case St of
        #{<<"final_size">> := N} -> N;
        _ -> filelib:file_size(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT))
    end.

activity(#{<<"state">> := S} = St, Now) when S =:= ?JOB_RUNNING; S =:= ?JOB_ORPHANED ->
    Idle = pluto_config:get(smartstreams_idle_after_ms, ?DEFAULT_JOB_IDLE_AFTER_MS),
    case opt(<<"last_output_at">>, St, 0) of
        T when Now - T =< Idle -> <<"active">>;
        _ -> <<"idle">>
    end;
activity(_, _) -> null.

%% @doc Newest first. Pages before describing, so a long history is cheap.
-spec list(map()) -> map().
list(R) ->
    Agent = pluto_job_schema:agent(R),
    Scope = opt(<<"scope">>, R, <<"mine">>),
    User = opt(<<"user">>, R),
    Parent = opt(<<"parent">>, R),
    Tags = opt(<<"tags">>, R, []),
    States = opt(<<"states">>, R, []),
    Sel = [P || P <- pluto_job_registry:all(), in_scope(Scope, P, Agent, User),
                Parent =:= undefined orelse opt(<<"parent">>, P) =:= Parent,
                Tags -- opt(<<"tags">>, P, []) =:= [],
                States =:= [] orelse
                    lists:member(maps:get(<<"state">>, pluto_job_registry:state(P)),
                                 States)],
    Sorted = lists:sort(fun(A, B) -> maps:get(<<"opened_at">>, A) >=
                                     maps:get(<<"opened_at">>, B) end, Sel),
    #{<<"total">> => length(Sel),
      <<"jobs">> => [card(P, Agent) || P <- lists:sublist(
                        Sorted, opt(<<"limit">>, R, ?JOB_LIST_DEFAULT))]}.

in_scope(<<"mine">>, P, Agent, _) ->
    maps:get(<<"owner">>, P) =:= Agent orelse maps:get(<<"opener">>, P) =:= Agent;
in_scope(<<"user">>, P, _, User) -> maps:get(<<"user">>, P, null) =:= User;
in_scope(_, _, _, _) -> true.

%% @doc Everything needed to take a job over.
-spec open(pkg(), agent_id()) -> map().
open(#{<<"id">> := Id} = P, Agent) ->
    St = pluto_job_registry:state(P),
    #{hi := Hi} = Src = pluto_job_streams:main_source(Id, St),
    Tail = case Hi > ?JOB_HEAD_BYTES of
               true ->
                   At = pluto_job_read:tail_start(Src, ?JOB_TAIL_LINES, ?JOB_TAIL_BYTES),
                   pluto_job_read:read(Src, At, ?JOB_TAIL_BYTES, #{});
               false -> null
           end,
    maps:merge(card(P, Agent), (maps:with(?JOB_OPEN_KEYS, P))#{
        <<"head">> => pluto_job_read:read(Src, 0, ?JOB_HEAD_BYTES, #{cut => true}),
        <<"tail">> => Tail,
        <<"nested">> => pluto_job_streams:describe(Id, St),
        <<"children">> => [maps:get(<<"id">>, C)
                           || C <- pluto_job_registry:children(Id)],
        <<"reviews">> => pluto_job_review:list(Id)}).

-spec stats() -> map().
stats() ->
    Cards = [card(P, <<>>) || P <- pluto_job_registry:all()],
    Count = fun(Key) ->
        lists:foldl(fun(#{Key := V}, A) -> maps:update_with(V, fun(N) -> N + 1 end, 1, A);
                       (_, A) -> A
                    end, #{}, Cards) end,
    Sum = fun(Key) -> lists:sum([maps:get(Key, D) || D <- Cards]) end,
    Delivered = lists:sum([opt(<<"delivered">>, C, 0)
                           || #{<<"job_id">> := Id} <- Cards,
                              C <- maps:values(pluto_job_cursor:load(Id)), is_map(C)]),
    #{<<"jobs">> => length(Cards), <<"by_state">> => Count(<<"state">>),
      <<"by_owner">> => Count(<<"owner">>), <<"by_user">> => Count(<<"user">>),
      <<"by_activity">> => Count(<<"activity">>),
      <<"total_elapsed_ms">> => Sum(<<"elapsed_ms">>),
      <<"total_queued_ms">> => Sum(<<"queued_ms">>),
      <<"bytes_captured">> => Sum(<<"size">>),
      <<"bytes_read_by_cursor">> => Delivered,
      <<"since_server_start">> =>
          #{<<"bytes_delivered">> => pluto_stats:get_counter(job_bytes_delivered),
            <<"greps">> => pluto_stats:get_counter(job_greps),
            <<"jobs_started">> => pluto_stats:get_counter(jobs_started)}}.
