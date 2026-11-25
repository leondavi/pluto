%%%-------------------------------------------------------------------
%%% @doc pluto_job_analyze — Questions about output, answered by a helper.
%%%
%%% priv/pluto_job_analyze.py does the work in its own confined process:
%%% a hostile pattern can only hurt that process, which is killed at the
%%% deadline. A few run at once; more callers wait for a moment.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_analyze).

-include("pluto.hrl").

-export([analyze/2, check_rules/2]).

-import(pluto_job_store, [opt/2, opt/3]).

%% @doc Answer one request about some jobs; see priv/pluto_job_analyze.py.
-spec analyze([pkg()], map()) -> reply().
analyze(Jobs, R) ->
    length(Jobs) =< ?JOB_WAIT_MAX_IDS orelse throw({bad, <<"too_many_jobs">>}),
    One = [K || K <- [<<"from_offset">>, <<"to_offset">>, <<"from_line">>, <<"to_line">>],
                opt(K, R) =/= undefined, length(Jobs) > 1],
    One =:= [] orelse throw({bad, <<"positions_are_per_job_use_one_job_or_from_offsets">>}),
    Req = (maps:filter(fun(_, V) -> V =/= null end, maps:with(?ANALYZE_KEYS, R)))
          #{<<"jobs">> => [job(P, R) || P <- Jobs]},
    case in_slot("pluto_analyze_", slots(), fun() -> run([], Req) end) of
        {ok, #{<<"out">> := _} = M} ->
            pluto_stats:inc(job_analyses),
            {ok, M};
        {ok, _} -> {error, <<"analyze_failed">>};
        E -> E
    end.

%% @doc Watch rules with defaults filled in, or what is wrong with one.
%% Params: names of the parameters of the jobs they are for.
-spec check_rules([map()], [binary()]) -> {ok, [map()]} | {error, binary()}.
check_rules([], _) -> {ok, []};
check_rules(Specs, Params) ->
    Req = #{<<"rules">> => Specs, <<"params">> => Params},
    case in_slot("pluto_check_", 2, fun() -> run(["check"], Req) end) of
        {ok, #{<<"rules">> := Rules}} -> {ok, Rules};
        {ok, _} -> {error, <<"analyze_failed">>};
        E -> E
    end.

%% A position is per job; a line or offset given once applies to each.
job(#{<<"id">> := Id} = P, R) ->
    St = pluto_job_registry:state(P),
    Src = case pluto_job_streams:source(Id, St, opt(<<"stream">>, R)) of
              {ok, S}    -> S;
              {error, W} -> throw({job_error, W})
          end,
    #{path := Path, lo := Lo, hi := Hi, closed := Closed} = Src,
    From0 = case {opt(<<"from_offsets">>, R, #{}), opt(<<"from_line">>, R)} of
                {#{Id := N}, _}  -> N;
                {_, undefined}   -> opt(<<"from_offset">>, R, Lo);
                {_, L1} -> pluto_job_read:offset_of_line(Src, max(1, L1))
            end,
    To = case opt(<<"to_line">>, R) of
             undefined -> min(Hi, opt(<<"to_offset">>, R, Hi));
             L2 -> pluto_job_read:offset_of_line(Src, L2 + 1)
         end,
    From = max(Lo, min(From0, Hi)),
    #{<<"id">> => short(Id), <<"name">> => opt(<<"name">>, P, null),
      <<"path">> => unicode:characters_to_binary(Path),
      <<"from">> => From, <<"hi">> => max(From, To),
      <<"line">> => pluto_job_read:line_of(Src, From),
      %% An unfinished last line counts only where no more can follow.
      <<"closed">> => Closed orelse To < Hi}.

short(Id) -> binary:part(Id, byte_size(Id), -?JOB_SHORT_ID_LEN).

%%====================================================================
%% Slots and the helper process
%%====================================================================

%% A slot is a registered name, so it is free again whatever happens to
%% the process that held it.
in_slot(Name, Slots, Fun) ->
    Stop = erlang:monotonic_time(millisecond) + ?ANALYZE_QUEUE_MS,
    {Pid, Ref} = spawn_monitor(fun() -> exit({done, take_slot(Name, Slots, Fun, Stop)}) end),
    receive
        {'DOWN', Ref, process, Pid, {done, R}} -> R;
        {'DOWN', Ref, process, Pid, Why} ->
            ?LOG_ERROR("smartstreams analyze: ~p", [Why]),
            {error, <<"analyze_failed">>}
    end.

slots() -> max(1, min(?ANALYZE_SLOTS, erlang:system_info(schedulers_online) div 2)).

take_slot(Name, Slots, Fun, Stop) ->
    case lists:any(fun(I) ->
            try register(list_to_atom(Name ++ integer_to_list(I)), self())
            catch error:badarg -> false
            end
         end, lists:seq(1, Slots)) of
        true -> Fun();
        false ->
            case erlang:monotonic_time(millisecond) > Stop of
                true  -> {error, <<"analyze_busy_try_again">>};
                false -> timer:sleep(50), take_slot(Name, Slots, Fun, Stop)
            end
    end.

run(Args, Req) ->
    case pluto_job_store:python() of
        false -> {error, <<"python3_not_found">>};
        Py ->
            try open_port({spawn_executable, Py},
                          [{args, pluto_job_store:script(?JOB_ANALYZE_SCRIPT) ++ Args},
                           binary, exit_status, use_stdio, hide]) of
                Port ->
                    port_command(Port, pluto_protocol_json:encode_line(Req)),
                    Stop = erlang:monotonic_time(millisecond) +
                        ?ANALYZE_DEADLINE_MS + ?ANALYZE_SLACK_MS,
                    result(collect(Port, [], 0, Stop))
            catch error:R ->
                ?LOG_ERROR("smartstreams analyze: ~p", [R]),
                {error, <<"analyze_failed">>}
            end
    end.

result({ok, Bin}) ->
    case pluto_protocol_json:decode(Bin) of
        {ok, #{<<"error">> := E}} -> {error, E};
        {ok, M} when is_map(M)    -> {ok, M};
        _ -> {error, <<"analyze_aborted_query_too_expensive">>}
    end;
result({error, _} = E) -> E.

collect(Port, Acc, Size, Stop) ->
    receive
        {Port, {data, D}} when Size + byte_size(D) =< ?ANALYZE_MAX_OUT ->
            collect(Port, [Acc, D], Size + byte_size(D), Stop);
        {Port, {data, _}} -> pluto_job_store:kill_port(Port), {error, <<"analyze_failed">>};
        {Port, {exit_status, _}} -> {ok, iolist_to_binary(Acc)}
    after max(0, Stop - erlang:monotonic_time(millisecond)) ->
        pluto_job_store:kill_port(Port),
        {error, <<"analyze_deadline">>}
    end.
