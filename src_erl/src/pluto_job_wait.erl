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

%% A pattern is searched from the start; new output means "from now".
mark(_, ?WAIT_PATTERN, R) -> opt(<<"from_offset">>, R, 0);
mark(Id, ?WAIT_OUTPUT, R) ->
    opt(<<"from_offset">>, R,
        filelib:file_size(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT)));
mark(_, _, _) -> 0.

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
                _ when Done     -> {true, #{<<"reason">> => ?WAIT_EXIT}};
                {false, Pos}    -> {false, Pos}
            end
    end.

condition(?WAIT_EXIT, _, _, Mark, _) -> {false, Mark};
condition(?WAIT_IDLE, P, R, Mark, _) ->
    Last = opt(<<"last_output_at">>, pluto_job_registry:state(P)),
    case is_integer(Last) andalso
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
        {ok, #{<<"groups">> := [[E | _] | _]}} ->
            {true, E#{<<"reason">> => ?WAIT_PATTERN}};
        {ok, #{<<"resume_at">> := null}} -> {false, To};
        {ok, #{<<"resume_at">> := Pos}}  -> {false, Pos};
        {error, W} -> {true, #{<<"reason">> => W}}
    end.
