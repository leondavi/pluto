%%%-------------------------------------------------------------------
%%% @doc pluto_job_watch — Standing rules over the output of live jobs.
%%%
%%% priv/pluto_job_watch.py evaluates the rules of all jobs in one confined
%%% process that runs only while there are rules. This server keeps what
%%% it reports. Per job, watch.json holds the rules, their states (with
%%% the position each has read to) and the hits, which have their own
%%% sequence `h'.
%%%
%%% Order of events for a hit: watch.json with the new states and the hit
%%% is on disk; then pluto_job_mgr acts on it and records the notice, once
%%% per `h'. So a crash before the first step evaluates the same lines
%%% again, and a crash after it delivers the same hit again, which the
%%% manager recognises. Only the action of a rule (cancel, pause) can be
%%% requested twice, when the crash falls between it and its notice.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_watch).
-behaviour(gen_server).

-include("pluto.hrl").

-export([start_link/0, activated/0, request/3, hits/2, settled/1, watched/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).

-record(state, {
    port      = none :: none | port(),
    heard     = 0    :: integer(),          %% last frame of the helper (ms)
    fails     = 0    :: non_neg_integer(),
    not_before = 0   :: integer(),          %% restart the helper then (ms)
    told      = #{}  :: #{job_id() => integer()},   %% delivered up to this h
    suspect   = none :: term(),             %% rule at work in the last death
    unsaved   = #{}  :: #{job_id() => true},
    ended     = #{}  :: #{job_id() => true}         %% helper knows it is over
}).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() -> gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc The manager has loaded all jobs.
-spec activated() -> ok.
activated() -> gen_server:cast(?MODULE, activated).

%% @doc add, remove or list rules on some jobs.
-spec request([pkg()], actor(), map()) -> reply().
request(Jobs, Actor, R) ->
    Ids = lists:usort([maps:get(<<"id">>, P) || P <- Jobs]),
    case opt(<<"action">>, R, <<"add">>) of
        <<"list">> ->
            {ok, #{<<"jobs">> => maps:from_list([{I, describe(I)} || I <- Ids])}};
        <<"add">> ->
            Spec = maps:filter(fun(_, V) -> V =/= null end, maps:with(?WATCH_KEYS, R)),
            case pluto_job_analyze:check_rule(Spec) of
                {ok, Rule} -> call({add, Actor, Ids, Rule, opt(<<"request_id">>, R, null)});
                E          -> E
            end;
        <<"remove">> -> call({remove, Actor, Ids, opt(<<"rule">>, R)});
        _ -> {error, <<"bad_action">>}
    end.

call(Req) ->
    try gen_server:call(?MODULE, Req, ?JOB_CALL_MS)
    catch exit:_ -> {error, <<"smartstreams_unavailable">>}
    end.

%% @doc Hits of a job after sequence number Mark, oldest first.
-spec hits(job_id(), integer()) -> [map()].
hits(Id, Mark) ->
    [H || #{<<"h">> := N} = H <- maps:get(<<"hits">>, doc(Id), []), N > Mark].

%% @doc Has everything the job printed been looked at?
-spec settled(job_id()) -> boolean().
settled(Id) ->
    case doc(Id) of
        #{<<"final">> := false} -> false;
        _ -> true
    end.

%% @doc Does the job have rules, or had when it ran here?
-spec watched(job_id()) -> boolean().
watched(Id) -> maps:get(<<"rules">>, doc(Id), []) =/= [].

%% Rules that came with an imported package are data, never active.
doc(Id) ->
    case ets:lookup(?ETS_JOB_WATCH, Id) of
        [{_, D}] -> D;
        [] ->
            case read(Id) of
                #{<<"inert">> := false} = D -> D;
                _ -> #{}
            end
    end.

read(Id) ->
    case pluto_job_store:read_json(path(Id)) of
        {ok, #{<<"rules">> := _} = D} -> D;
        _ -> #{}
    end.

path(Id) -> pluto_job_store:file(Id, ?JOB_FILE_WATCH).

describe(Id) ->
    D = case doc(Id) of
            M when map_size(M) =:= 0 -> read(Id);
            M -> M
        end,
    States = maps:get(<<"states">>, D, #{}),
    [begin
         St = maps:get(Rid, States, #{}),
         maps:merge(
           #{<<"id">> => Rid, <<"spec">> => Spec,
             <<"active">> => maps:get(<<"final">>, D, true) =:= false andalso
                 not opt(<<"done">>, St, false),
             <<"hits">> => opt(<<"n">>, St, 0),
             <<"fired">> => opt(<<"total">>, St, 0),
             <<"inert">> => maps:get(<<"inert">>, D, true)},
           maps:with([<<"why">>], St))
     end || #{<<"id">> := Rid, <<"spec">> := Spec} <- maps:get(<<"rules">>, D, [])].

%%====================================================================
%% gen_server
%%====================================================================

init([]) ->
    process_flag(trap_exit, true),
    ets:delete_all_objects(?ETS_JOB_WATCH),
    %% After a crash of this server alone, nobody else will say so.
    pluto_job_mgr:is_active() andalso activated(),
    erlang:send_after(?WATCH_TICK_MS, self(), tick),
    {ok, #state{}}.

handle_call({add, Actor, Ids, Rule, Rid}, _From, S) ->
    {Res, S1} = lists:mapfoldl(fun(Id, Acc) ->
        {R, Acc1} = add(Id, Actor, Rule, Rid, Acc),
        {{Id, R}, Acc1}
    end, S, Ids),
    reply(Res, S1);
handle_call({remove, Actor, Ids, What}, _From, S) ->
    {Res, S1} = lists:mapfoldl(fun(Id, Acc) ->
        {R, Acc1} = remove(Id, Actor, What, Acc),
        {{Id, R}, Acc1}
    end, S, Ids),
    reply(Res, S1);
handle_call(_, _, S) -> {reply, {error, <<"unknown_request">>}, S}.

handle_cast(activated, S) ->
    ets:delete_all_objects(?ETS_JOB_WATCH),
    lists:foreach(fun load/1, pluto_job_registry:all()),
    {noreply, helper(S#state{told = #{}, unsaved = #{}, ended = #{}})};
handle_cast(_, S) -> {noreply, S}.

handle_info(tick, S) ->
    %% Without watches this is one look at an empty table.
    Busy = ets:info(?ETS_JOB_WATCH, size) > 0 orelse S#state.port =/= none,
    erlang:send_after(?WATCH_TICK_MS, self(), tick),
    case Busy of
        true  -> {noreply, tick(S)};
        false -> {noreply, S}
    end;
handle_info({Port, {data, Bin}}, #state{port = Port} = S) ->
    S1 = S#state{heard = now_ms()},
    case pluto_protocol_json:decode(Bin) of
        {ok, #{<<"job">> := Id} = M} -> {noreply, report(Id, M, S1)};
        _ -> {noreply, S1}
    end;
handle_info({Port, {exit_status, St}}, #state{port = Port} = S) ->
    {noreply, helper_died(St =:= ?WATCH_EXIT_MEMORY, S)};
handle_info({'EXIT', Port, _}, #state{port = Port} = S) ->
    {noreply, helper_died(false, S)};
handle_info(_, S) -> {noreply, S}.

terminate(_, _) -> ok.

%% One job's refusal does not hide the others' success.
reply(Res, S) ->
    Ok = maps:from_list([{I, V} || {I, {ok, V}} <- Res]),
    Errors = maps:from_list([{I, W} || {I, {error, W}} <- Res]),
    case {map_size(Ok), Res} of
        {0, [{_, {error, W}} | _]} -> {reply, {error, W}, S};
        _ -> {reply, {ok, #{<<"jobs">> => Ok, <<"errors">> => Errors}}, helper(S)}
    end.

%%====================================================================
%% Rules
%%====================================================================

owned(Id, {Agent, AsUser}) ->
    case pluto_job_registry:pkg(Id) of
        {ok, #{<<"owner">> := O}} when O =/= Agent, not AsUser -> {error, ?ERR_NOT_OWNER};
        R -> R
    end.

add(Id, {By, _} = Actor, Rule, Rid, S) ->
    D = maps:merge(#{<<"schema">> => ?JOB_SCHEMA, <<"inert">> => false, <<"seq">> => 0,
                     <<"h">> => 0, <<"rules">> => [], <<"states">> => #{},
                     <<"hits">> => [], <<"final">> => false}, doc(Id)),
    #{<<"rules">> := Rules, <<"states">> := States, <<"seq">> := Seq} = D,
    Same = [X || #{<<"id">> := X, <<"request_id">> := Q} <- Rules, Q =:= Rid, Rid =/= null],
    Live = [X || #{<<"id">> := X} <- Rules,
                 not opt(<<"done">>, maps:get(X, States, #{}), false)],
    case owned(Id, Actor) of
        {error, _} = E -> {E, S};
        {ok, _} when Same =/= [] -> {{ok, hd(Same)}, S};
        {ok, #{<<"done">> := true}} -> {{error, <<"job_already_finished">>}, S};
        {ok, _} when length(Live) >= ?WATCH_RULES_MAX ->
            {{error, <<"too_many_rules_on_this_job">>}, S};
        {ok, _} ->
            New = <<"R", (integer_to_binary(Seq + 1))/binary>>,
            St = start_state(Id, opt(<<"from_now">>, Rule, false)),
            %% Finished rules make room; their hits stay.
            Kept = case length(Rules) >= 2 * ?WATCH_RULES_MAX of
                       true  -> [X || #{<<"id">> := I} = X <- Rules, lists:member(I, Live)];
                       false -> Rules
                   end,
            D1 = D#{<<"seq">> := Seq + 1, <<"final">> := false,
                    <<"rules">> := Kept ++ [#{<<"id">> => New, <<"spec">> => Rule,
                                              <<"by">> => By, <<"at">> => now_ms(),
                                              <<"request_id">> => Rid}],
                    <<"states">> := States#{New => St}},
            case write(Id, D1) of
                ok ->
                    %% Without a helper, starting it announces the rule.
                    ets:insert(?ETS_JOB_WATCH, {Id, D1}),
                    tell_set(Id, [New], S),
                    {{ok, New}, S};
                {error, _} -> {{error, <<"disk_error_watch_not_saved">>}, S}
            end
    end.

start_state(_, false) -> #{<<"offset">> => 0, <<"line">> => 1, <<"c">> => 0,
                           <<"n">> => 0, <<"total">> => 0};
start_state(Id, true) ->
    {ok, P} = pluto_job_registry:pkg(Id),
    Src = pluto_job_streams:main_source(Id, pluto_job_registry:state(P)),
    Off = pluto_job_read:last_line_end(Src, 0),
    case pluto_job_read:line_of(Src, Off) of
        null -> #{<<"offset">> => null, <<"line">> => 1, <<"c">> => 0,
                  <<"n">> => 0, <<"total">> => 0};
        Line -> #{<<"offset">> => Off, <<"line">> => Line, <<"c">> => null,
                  <<"n">> => 0, <<"total">> => 0}
    end.

%% What: a rule id, a rule name, or undefined for all.
remove(Id, Actor, What, S) ->
    D = doc(Id),
    Rules = maps:get(<<"rules">>, D, []),
    Gone = [X || #{<<"id">> := X, <<"spec">> := #{<<"name">> := N}} <- Rules,
                 What =:= undefined orelse X =:= What orelse N =:= What],
    case owned(Id, Actor) of
        {error, _} = E -> {E, S};
        {ok, _} when Gone =:= [] -> {{error, <<"no_such_rule">>}, S};
        {ok, _} ->
            Left = [X || #{<<"id">> := I} = X <- Rules, not lists:member(I, Gone)],
            D1 = D#{<<"rules">> := Left,
                    <<"final">> := Left =:= [] orelse maps:get(<<"final">>, D),
                    <<"states">> := maps:without(Gone, maps:get(<<"states">>, D))},
            case write(Id, D1) of
                ok ->
                    {{ok, Gone}, removed(Id, D1, Gone, S)};
                {error, _} -> {{error, <<"disk_error_watch_not_saved">>}, S}
            end
    end.

%% Without rules a job costs nothing.
removed(Id, D, Gone, S) ->
    case ets:member(?ETS_JOB_WATCH, Id) of
        true ->
            ets:insert(?ETS_JOB_WATCH, {Id, D}),
            case D of
                #{<<"rules">> := []} ->
                    tell(#{<<"op">> => <<"del">>, <<"job">> => Id}, S),
                    deliver(Id, S);
                _ ->
                    [tell(#{<<"op">> => <<"drop">>, <<"job">> => Id, <<"rule">> => G}, S)
                     || G <- Gone],
                    S
            end;
        false -> S
    end.

write(Id, D) -> pluto_job_store:write_json(path(Id), D, sync).

%% At boot: what is unfinished, or may hold hits nobody was told.
load(#{<<"id">> := Id, <<"sched">> := Sched})
  when Sched =/= ?SCHED_IMPORTED, Sched =/= ?SCHED_SNAPSHOT ->
    case read(Id) of
        #{<<"inert">> := false} = D -> ets:insert(?ETS_JOB_WATCH, {Id, D});
        _ -> ok
    end;
load(_) -> ok.

%%====================================================================
%% Reports of the helper
%%====================================================================

report(Id, #{<<"states">> := New, <<"hits">> := Hits, <<"final">> := Final}, S) ->
    case ets:lookup(?ETS_JOB_WATCH, Id) of
        [{_, #{<<"rules">> := Rules} = D}] ->
            %% A rule removed meanwhile reports for the last time.
            Ids = [I || #{<<"id">> := I} <- Rules],
            D1 = add_hits([X || #{<<"rule">> := Ru} = X <- Hits, lists:member(Ru, Ids)],
                          D#{<<"states">> := maps:merge(maps:get(<<"states">>, D),
                                                        maps:with(Ids, New)),
                             <<"final">> := Final}),
            save_and_deliver(Id, D1, S);
        [] -> S
    end.

add_hits(New, #{<<"h">> := H0, <<"hits">> := Old} = D) ->
    At = now_ms(),
    {Numbered, H1} = lists:mapfoldl(fun(X, N) ->
        {X#{<<"h">> => N + 1, <<"at">> => At}, N + 1} end, H0, New),
    All = Old ++ Numbered,
    Keep = max(?WATCH_HITS_KEEP, length(Numbered)),
    D#{<<"h">> := H1, <<"hits">> := lists:nthtail(max(0, length(All) - Keep), All)}.

save_and_deliver(Id, D, #state{unsaved = U} = S) ->
    case write(Id, D) of
        ok ->
            ets:insert(?ETS_JOB_WATCH, {Id, D}),
            deliver(Id, S#state{unsaved = maps:remove(Id, U)});
        {error, R} ->
            %% The helper moved on, so keep what it said and try again.
            maps:is_key(Id, U) orelse ?LOG_ERROR("smartstreams: watch not saved: ~p", [R]),
            ets:insert(?ETS_JOB_WATCH, {Id, D}),
            S#state{unsaved = U#{Id => true}}
    end.

%% Act on and announce hits the manager has not seen. Final and delivered
%% documents leave the memory.
deliver(Id, #state{told = Told} = S) ->
    [{_, #{<<"h">> := H, <<"hits">> := Hits, <<"final">> := Final}}] =
        ets:lookup(?ETS_JOB_WATCH, Id),
    Done = maps:get(Id, Told, 0),
    Fresh = [X || #{<<"h">> := N} = X <- Hits, N > Done],
    case Fresh =:= [] orelse pluto_job_mgr:watch_hits(Id, Fresh) of
        R when R =:= true; R =:= ok ->
            case Final of
                true ->
                    ets:delete(?ETS_JOB_WATCH, Id),
                    S#state{told = maps:remove(Id, Told),
                            ended = maps:remove(Id, S#state.ended)};
                false -> S#state{told = Told#{Id => H}}
            end;
        {error, _} -> S                        %% next tick
    end.

%%====================================================================
%% Once a second
%%====================================================================

tick(S0) ->
    S1 = lists:foldl(fun({Id, D}, S) ->
        case pluto_job_registry:pkg(Id) of
            {error, _} ->                      %% pruned or removed
                ets:delete(?ETS_JOB_WATCH, Id),
                tell(#{<<"op">> => <<"del">>, <<"job">> => Id}, S),
                S;
            {ok, P} ->
                Sa = end_of(Id, P, S),
                case {maps:is_key(Id, S#state.unsaved), D} of
                    {true, _} -> save_and_deliver(Id, D, Sa);
                    {false, #{<<"h">> := H}} ->
                        case H > maps:get(Id, S#state.told, 0) orelse
                             maps:get(<<"final">>, D) of
                            true  -> deliver(Id, Sa);
                            false -> Sa
                        end
                end
        end
    end, S0, ets:tab2list(?ETS_JOB_WATCH)),
    S2 = case S1#state.port =/= none andalso now_ms() - S1#state.heard > ?WATCH_SILENT_MS of
             true  -> kill(S1#state.port), helper_died(true, S1);
             false -> S1
         end,
    helper(S2).

%% A job can end without its runner saying so (lost, cancelled in queue).
end_of(Id, P, #state{ended = E, port = Port} = S) ->
    case pluto_job_registry:is_done(P) andalso Port =/= none andalso
         not is_map_key(Id, E) of
        true ->
            tell(#{<<"op">> => <<"end">>, <<"job">> => Id}, S),
            S#state{ended = E#{Id => true}};
        false -> S
    end.

%%====================================================================
%% The helper process
%%====================================================================

%% Running if and only if there is something to evaluate.
helper(#state{port = Port} = S) ->
    Any = open_docs() =/= [],
    case {Any, Port} of
        {true, none} ->
            case now_ms() >= S#state.not_before of
                true  -> start_helper(S);
                false -> S
            end;
        {false, P} when P =/= none ->
            catch port_close(P),
            S#state{port = none, fails = 0, ended = #{}};
        _ -> S
    end.

open_docs() ->
    ets:select(?ETS_JOB_WATCH, [{{'$1', #{<<"final">> => false}}, [], ['$1']}]).

start_helper(S) ->
    Marker = filename:join(pluto_job_store:home(), ?HOME_FILE_MARKER),
    try open_port({spawn_executable, pluto_job_store:python()},
                  [{args, pluto_job_store:script(?JOB_WATCH_SCRIPT) ++ [Marker]},
                   binary, {packet, 4}, exit_status, use_stdio, hide]) of
        Port ->
            S1 = S#state{port = Port, heard = now_ms(), ended = #{}},
            [tell_set(Id, all, S1) || Id <- open_docs()],
            S1
    catch error:R ->
        ?LOG_ERROR("smartstreams: watch helper: ~p", [R]),
        S#state{not_before = now_ms() + ?WATCH_BACKOFF_MS}
    end.

%% The marker names the rule it was busy with, if any. That rule is taken
%% out if it hung the helper, used up its memory, or was there twice: a
%% helper killed from outside says nothing about the rule.
helper_died(Guilty, #state{fails = F, suspect = Last} = S) ->
    Wait = min(?WATCH_BACKOFF_MS, 250 bsl min(F, 10)),
    S1 = S#state{port = none, fails = F + 1, not_before = now_ms() + Wait, ended = #{}},
    case file:read_file(filename:join(pluto_job_store:home(), ?HOME_FILE_MARKER)) of
        {ok, <<Tag:128/binary, _/binary>>} ->
            case string:lexemes(string:trim(Tag), " ") of
                [Id, Rid] when Guilty; Last =:= {Id, Rid} ->
                    ?LOG_WARN("smartstreams: watch helper died in ~s ~s", [Id, Rid]),
                    disable(Id, Rid, S1#state{suspect = none});
                [Id, Rid] -> S1#state{suspect = {Id, Rid}};
                _ ->
                    ?LOG_WARN("smartstreams: watch helper died", []),
                    S1
            end;
        _ -> S1
    end.

disable(Id, Rid, S) ->
    case ets:lookup(?ETS_JOB_WATCH, Id) of
        [{_, #{<<"states">> := #{Rid := St} = States, <<"rules">> := Rules} = D}] ->
            Why = <<"evaluating it stopped the watch helper">>,
            Name = hd([N || #{<<"id">> := I, <<"spec">> := #{<<"name">> := N}} <- Rules,
                            I =:= Rid] ++ [Rid]),
            Hit = #{<<"rule">> => Rid, <<"name">> => Name, <<"line">> => null,
                    <<"n">> => opt(<<"n">>, St, 0), <<"then">> => <<"notify">>,
                    <<"text">> => <<"rule disabled: ", Why/binary>>},
            D1 = D#{<<"states">> := States#{Rid := St#{<<"done">> => true,
                                                       <<"why">> => Why}}},
            save_and_deliver(Id, add_hits([Hit], D1), S);
        _ -> S
    end.

kill(Port) ->
    case erlang:port_info(Port, os_pid) of
        {os_pid, Pid} -> os:cmd("kill -9 " ++ integer_to_list(Pid));
        undefined     -> ok
    end,
    catch port_close(Port),
    ok.

tell(_, #state{port = none}) -> ok;
tell(M, #state{port = Port}) ->
    catch port_command(Port, pluto_protocol_json:encode(M)),
    ok.

tell_set(Id, Which, S) ->
    [{_, #{<<"rules">> := Rules, <<"states">> := States}}] =
        ets:lookup(?ETS_JOB_WATCH, Id),
    tell(#{<<"op">> => <<"set">>, <<"job">> => Id,
           <<"name">> => name(Id),
           <<"path">> => bin(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT)),
           <<"state_path">> => bin(pluto_job_store:file(Id, ?JOB_FILE_STATE)),
           <<"rules">> => [#{<<"id">> => I, <<"spec">> => Sp,
                             <<"st">> => maps:get(I, States)}
                           || #{<<"id">> := I, <<"spec">> := Sp} <- Rules,
                              Which =:= all orelse lists:member(I, Which)]}, S).

name(Id) ->
    case pluto_job_registry:pkg(Id) of
        {ok, P} -> opt(<<"name">>, P, null);
        _       -> null
    end.

bin(Path) -> unicode:characters_to_binary(Path).
