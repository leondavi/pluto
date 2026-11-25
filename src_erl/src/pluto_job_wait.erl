%%%-------------------------------------------------------------------
%%% @doc pluto_job_wait — Block until something happens to some jobs.
%%%
%%% Runs in the HTTP connection's own process. Waiting for exit only
%%% looks at the registry, so many waiters cost next to nothing; the other
%%% modes look at files, and the polling step grows with the job count.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_wait).

-include("pluto.hrl").

-export([wait/2]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).

%% @doc Hits per job id plus a card per job; throws {bad, Reason}.
-spec wait([pkg()], map()) -> map().
wait(Jobs, R) ->
    length(Jobs) =< ?JOB_WAIT_MAX_IDS orelse throw({bad, <<"too_many_jobs">>}),
    Until = opt(<<"until">>, R, ?WAIT_EXIT),
    lists:member(Until, ?WAIT_MODES) orelse throw({bad, <<"bad_value_for_until">>}),
    Until =:= ?WAIT_PATTERN andalso check_pattern(opt(<<"pattern">>, R)),
    Ids = lists:usort([maps:get(<<"id">>, P) || P <- Jobs]),
    Until =:= ?WAIT_WATCH andalso
        (lists:any(fun pluto_job_watch:watched/1, Ids) orelse
            throw({bad, <<"no_watch_rules_on_these_jobs">>})),
    Marks = maps:from_list([{Id, mark(Id, Until, R)} || Id <- Ids]),
    Deadline = now_ms() + min(opt(<<"timeout_ms">>, R, ?JOB_WAIT_DEFAULT_MS),
                              ?JOB_WAIT_MAX_MS),
    Hits = loop(Ids, Until, R, Marks, opt(<<"all">>, R, false), Deadline),
    Enough = enough(Hits, Ids, opt(<<"all">>, R, false)),
    Agent = pluto_job_schema:agent(R),
    #{<<"timed_out">> => not Enough, <<"hits">> => Hits,
      <<"jobs">> => [pluto_job_view:card(P, Agent)
                     || I <- Ids, {ok, P} <- [pluto_job_registry:pkg(I)]]}.

check_pattern(undefined) -> throw({bad, <<"missing_pattern">>});
check_pattern(P) ->
    case pluto_job_grep:compile(P, #{}) of
        {ok, _}    -> ok;
        {error, W} -> throw({bad, W})
    end.

%% Where to look from. A client following many jobs passes from_offsets
%% ({job id: offset}) so that each wait reports only what is new: pattern
%% and output count from there, and idle needs output beyond it.
mark(Id, Until, R) ->
    case opt(<<"from_offsets">>, R, #{}) of
        #{Id := N} -> N;
        _ -> default_mark(Id, Until, R)
    end.

%% A pattern is searched from the start; new output means "from now".
default_mark(_, ?WAIT_PATTERN, R) -> opt(<<"from_offset">>, R, 0);
default_mark(Id, ?WAIT_OUTPUT, R) ->
    opt(<<"from_offset">>, R,
        filelib:file_size(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT)));
default_mark(_, ?WAIT_WATCH, _) -> 0;
default_mark(_, ?WAIT_FOLLOW, _) -> 0;
default_mark(_, _, _) -> -1.

enough(Hits, Ids, true)   -> map_size(Hits) =:= length(Ids);
enough(Hits, _Ids, false) -> map_size(Hits) > 0.

loop(Ids, Until, R, Marks0, All, Deadline) ->
    {Hits, Marks} = lists:foldl(fun(Id, {H, M}) ->
        case hit(Id, Until, R, maps:get(Id, M)) of
            {true, Why}  -> {H#{Id => Why}, M};
            {false, Pos} -> {H, M#{Id => Pos}}
        end
    end, {#{}, Marks0}, Ids),
    Left = Deadline - now_ms(),
    case enough(Hits, Ids, All) orelse Left =< 0 of
        true -> Hits;
        false ->
            Step = ?JOB_WAIT_STEP_MS * (1 + length(Ids) div 16),
            timer:sleep(min(Step, Left)),
            loop(Ids, Until, R, Marks, All, Deadline)
    end.

%% {true, Why} or {false, WhereToLookNext}.
hit(Id, Until, R, Mark) ->
    case pluto_job_registry:pkg(Id) of
        {error, _} -> {true, #{<<"reason">> => <<"gone">>}};
        {ok, P} ->
            Done = pluto_job_registry:is_done(P),
            case condition(Until, P, R, Mark, Done) of
                {true, _} = Hit -> Hit;
                {false, Pos} when Done, Until =:= ?WAIT_WATCH; Done, Until =:= ?WAIT_FOLLOW ->
                    case last_hits_due(P) of
                        true  -> {false, Pos};
                        false -> {true, #{<<"reason">> => ?WAIT_EXIT}}
                    end;
                _ when Done     -> {true, #{<<"reason">> => ?WAIT_EXIT}};
                {false, Pos}    -> {false, Pos}
            end
    end.

%% The last output of a job that just ended is still being evaluated.
last_hits_due(#{<<"id">> := Id} = P) ->
    not pluto_job_watch:settled(Id) andalso
        now_ms() - opt(<<"ended_at">>, pluto_job_registry:state(P), 0) < ?WATCH_SETTLE_MS.

condition(?WAIT_EXIT, _, _, Mark, _) -> {false, Mark};
condition(?WAIT_WATCH, #{<<"id">> := Id}, _, Mark, _) ->
    hits(pluto_job_watch:hits(Id, Mark), Mark);
condition(?WAIT_FOLLOW, #{<<"id">> := Id}, _, Mark, _) ->
    hits([H || #{<<"then">> := <<"notify">>} = H <- pluto_job_watch:hits(Id, Mark)], Mark);
condition(?WAIT_IDLE, #{<<"id">> := Id} = P, R, Mark, _) ->
    Size = filelib:file_size(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT)),
    Last = opt(<<"last_output_at">>, pluto_job_registry:state(P)),
    case is_integer(Last) andalso Size > Mark andalso
         now_ms() - Last >= opt(<<"idle_ms">>, R, ?JOB_WAIT_IDLE_MS) of
        true  -> {true, #{<<"reason">> => ?WAIT_IDLE}};
        false -> {false, Mark}
    end;
condition(Until, #{<<"id">> := Id} = P, R, Mark, Done) ->
    #{hi := Hi} = Src =
        pluto_job_streams:main_source(Id, pluto_job_registry:state(P)),
    case {Until, Hi > Mark} of
        {_, false} -> {false, Mark};
        {?WAIT_OUTPUT, true} ->
            {true, #{<<"reason">> => ?WAIT_OUTPUT, <<"offset">> => Mark}};
        {?WAIT_PATTERN, true} -> pattern(Src, R, Mark, Done)
    end.

hits([], Mark) -> {false, Mark};
hits(Hits, _) ->
    {true, #{<<"reason">> => ?WAIT_WATCH, <<"total">> => length(Hits),
             <<"next">> => maps:get(<<"h">>, lists:last(Hits)),
             <<"watch">> => lists:nthtail(max(0, length(Hits) - ?WATCH_WAIT_HITS), Hits)}}.

%% Scan only new, complete lines.
pattern(#{hi := Hi} = Src, R, Mark, Done) ->
    To = case Done of
             true  -> Hi;
             false -> pluto_job_read:last_line_end(Src, Mark)
         end,
    case To > Mark andalso
         pluto_job_grep:grep(Src, opt(<<"pattern">>, R),
                             #{from => Mark, to => To, max => 1}) of
        false -> {false, Mark};
        {ok, #{<<"groups">> := [[#{<<"offset">> := Off} = E | _] | _]}} ->
            Next = pluto_job_read:offset_after_line(Src, Off),
            {true, E#{<<"reason">> => ?WAIT_PATTERN, <<"next">> => Next}};
        {ok, #{<<"resume_at">> := null}} -> {false, To};
        {ok, #{<<"resume_at">> := Pos}}  -> {false, Pos};
        {error, W} -> {true, #{<<"reason">> => W}}
    end.
