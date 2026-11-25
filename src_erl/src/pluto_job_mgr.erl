%%%-------------------------------------------------------------------
%%% @doc pluto_job_mgr — Serialises every change to job packages.
%%%
%%% Life of a job: start/1 writes a queued package; schedule/1 launches a
%%% detached runner per job (priv/pluto_job_runner.py); check/2 follows
%%% the runner's state.json until it is final and then finishes the job.
%%% The runner needs no server, so after a restart the packages on disk
%%% are simply picked up again.
%%%
%%% Robustness rules: a failure concerning one job never takes the
%%% manager down (guard/2, check_all/1); long work (Python, regex,
%%% hashing, reading) runs in other processes; "cannot tell" is never
%%% treated as "dead".
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_mgr).
-behaviour(gen_server).

-include("pluto.hrl").

-export([start_link/0, activate/0, start/1, control/2, input/2, transfer/2,
         review/2, stream/2, commit/5, notices/2, adopt/3, prune/2,
         is_active/0, watch_hits/2]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).
-import(pluto_job_registry, [pkg/1, state/1, is_done/1, save/1]).

-define(ACTIVE, {?MODULE, active}).

-record(state, {
    active  = false :: boolean(),
    env     = #{}   :: #{job_id() => map()},      %% never written to disk
    retry   = #{}   :: #{job_id() => integer()},  %% do not touch before (ms)
    probes  = #{}   :: #{job_id() => integer()},  %% last probe (ms)
    aborted = #{}   :: #{job_id() => integer()},  %% abort written at (ms)
    board   = #{}   :: map(),                     %% see pluto_job_notice
    told    = #{}   :: #{job_id() => integer()},  %% last watch hit announced
    seen    = #{}   :: map(),                     %% request id -> reply
    seen_old = #{}  :: map()                      %% previous generation
}).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() -> gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Make sure the home exists and belongs to this server. Cheap once
%% active: it does not go through the manager.
-spec activate() -> ok | {error, term()}.
activate() ->
    case is_active() of
        true  -> ok;
        false -> call(activate)
    end.

-spec is_active() -> boolean().
is_active() ->
    case persistent_term:get(?ACTIVE, undefined) of
        undefined -> false;
        Pid       -> is_process_alive(Pid)
    end.

-spec start(map()) -> {ok, pkg()} | {error, binary()}.
start(R) -> call({start, R}).
-spec control(actor(), map()) -> reply().
control(Actor, R) -> call({control, Actor, R}).
-spec input(actor(), map()) -> reply().
input(Actor, R) -> call({input, Actor, R}).
-spec transfer(actor(), map()) -> reply().
transfer(Actor, R) -> call({transfer, Actor, R}).
-spec review(job_id(), map()) -> reply().
review(Id, Review) -> call({review, Id, Review}).
%% @doc Apply an action of pluto_job_streams; for a pattern split, R
%% carries the ranges the caller already computed.
-spec stream(actor(), map()) -> reply().
stream(Actor, R) -> call({stream, Actor, R}).
-spec commit(job_id(), binary(), map(), map(), map()) -> ok | conflict | {error, binary()}.
commit(Id, Key, Old, R, Out) -> call({commit, Id, Key, Old, R, Out}).
-spec notices(agent_id(), non_neg_integer()) -> {ok, [map()]} | {error, binary()}.
notices(Agent, Ack) -> call({notices, Agent, Ack}).
%% @doc Act on and announce hits of watch rules; each is announced once,
%% however often it is reported.
-spec watch_hits(job_id(), [map()]) -> ok | {error, binary()}.
watch_hits(Id, Hits) -> call({watch_hits, Id, Hits}).
-spec adopt(file:filename(), agent_id(), binary()) ->
    {ok, job_id(), binary()} | {error, binary()}.
adopt(Staging, Agent, User) -> call({adopt, Staging, Agent, User}, ?JOB_PACKAGE_MS).
-spec prune(integer(), boolean()) -> {ok, [job_id()]} | {error, binary()}.
prune(BeforeMs, Dry) -> call({prune, BeforeMs, Dry}, ?JOB_PACKAGE_MS).

call(Req) -> call(Req, ?JOB_CALL_MS).
call(Req, Timeout) ->
    try gen_server:call(?MODULE, Req, Timeout)
    catch exit:_ -> {error, <<"smartstreams_unavailable">>}
    end.

%%====================================================================
%% gen_server
%%====================================================================

init([]) ->
    %% Python ports report their exit through links; a helper that dies
    %% must not take us along.
    process_flag(trap_exit, true),
    persistent_term:erase(?ACTIVE),
    %% A server nobody runs jobs on never touches the file system.
    S = case filelib:is_dir(pluto_job_store:jobs_dir()) of
            true ->
                {R, S0} = do_activate(#state{}),
                R =:= ok orelse ?LOG_WARN("smartstreams inactive: ~p", [R]),
                S0;
            false -> #state{}
        end,
    erlang:send_after(?JOB_TICK_MS, self(), tick),
    {ok, S}.

handle_call(activate, _From, S) ->
    {R, S1} = do_activate(S),
    {reply, R, S1};
handle_call(_Req, _From, #state{active = false} = S) ->
    {reply, {error, <<"smartstreams_inactive">>}, S};
handle_call(Req, _From, S) ->
    {Reply, S1} = guard(fun() -> handle(Req, S) end, S),
    {reply, Reply, S1}.

handle_cast(_, S) -> {noreply, S}.

handle_info(tick, S) ->
    erlang:send_after(?JOB_TICK_MS, self(), tick),
    case S#state.active of
        true  -> {noreply, schedule(check_all(S))};
        false -> {noreply, S}
    end;
handle_info({spawned, Id, Result}, S) ->
    {_, S1} = guard(fun() -> {ok, spawned(Id, Result, S)} end, S),
    {noreply, S1};
handle_info({probed, Id, Runner, Child}, S) ->
    {_, S1} = guard(fun() -> {ok, probed(Id, Runner, Child, S)} end, S),
    {noreply, S1};
handle_info(_, S) -> {noreply, S}.

terminate(_, _) ->
    persistent_term:erase(?ACTIVE),
    ok.

%% Anything that goes wrong in one request is that request's error.
guard(Fun, S) ->
    try Fun()
    catch
        throw:{io, R} ->
            ?LOG_ERROR("smartstreams: disk error ~p", [R]),
            {{error, <<"disk_error_", (atom_to_binary(R))/binary>>}, S};
        C:R:St ->
            ?LOG_ERROR("smartstreams: ~p:~p ~p", [C, R, St]),
            {{error, <<"internal_error">>}, S}
    end.

handle({start, R}, S)        -> start_job(R, S);
handle({control, A, R}, S)   -> dedupe(R, S, fun() -> control_tree(A, R, S) end);
handle({input, A, R}, S)     -> dedupe(R, S, fun() -> {do_input(A, R), S} end);
handle({transfer, A, R}, S)  -> dedupe(R, S, fun() -> do_transfer(A, R, S) end);
handle({review, Id, E}, S)   -> dedupe(E, S, fun() ->
    {pluto_job_review:append(Id, maps:remove(<<"request_id">>, E)), S} end);
handle({stream, A, R}, S)    -> dedupe(R, S, fun() -> {do_stream(A, R), S} end);
handle({commit, Id, Key, Old, R, Out}, S) ->
    {pluto_job_cursor:commit(Id, Key, Old, R, Out), S};
handle({notices, Agent, Ack}, S) ->
    {Page, B} = pluto_job_notice:page(Agent, Ack, S#state.board),
    {{ok, Page}, S#state{board = B}};
handle({adopt, Staging, Agent, User}, S) ->
    {pluto_job_import:adopt(Staging, Agent, User), S};
handle({watch_hits, Id, Hits}, S) -> {ok, lists:foldl(fun(H, Acc) ->
                                          watch_hit(Id, H, Acc) end, S, Hits)};
handle({prune, BeforeMs, Dry}, S) -> {{ok, pluto_job_import:prune(BeforeMs, Dry)}, S};
handle(_, S) -> {{error, <<"unknown_request">>}, S}.

%% Retried HTTP requests get the first reply again. Two generations bound
%% the memory without ever forgetting the most recent requests.
dedupe(R, #state{seen = New, seen_old = Old} = S, Fun) ->
    case opt(<<"request_id">>, R) of
        undefined -> Fun();
        Rid ->
            case {New, Old} of
                {#{Rid := Reply}, _} -> {Reply, S};
                {_, #{Rid := Reply}} -> {Reply, S};
                _ ->
                    {Reply, S1} = Fun(),
                    S2 = case maps:size(New) >= ?JOB_DEDUPE_MAX of
                             true  -> S1#state{seen = #{}, seen_old = New};
                             false -> S1
                         end,
                    {Reply, S2#state{seen = (S2#state.seen)#{Rid => Reply}}}
            end
    end.

%%====================================================================
%% Activation
%%====================================================================

do_activate(#state{active = true} = S) -> {ok, S};
do_activate(S) ->
    try
        ok = ok(pluto_job_store:ensure_home()),
        ok = ok(pluto_job_store:lock_home()),
        pluto_job_import:sweep(),
        pluto_job_registry:load_all(),
        {Board, Told} = pluto_job_notice:load(),
        persistent_term:put(?ACTIVE, self()),
        pluto_job_watch:activated(),
        {ok, S#state{active = true, board = Board, told = Told}}
    catch
        throw:{inactive, R} -> {{error, R}, S};
        C:R:St ->
            ?LOG_ERROR("smartstreams activation: ~p:~p ~p", [C, R, St]),
            {{error, activation_failed}, S}
    end.

ok(ok) -> ok;
ok({error, R}) -> throw({inactive, R}).

%%====================================================================
%% Start and schedule
%%====================================================================

start_job(R, S) ->
    Rid = opt(<<"request_id">>, R),
    %% Durable, unlike dedupe/3: a job must not start twice even when the
    %% server restarted between the request and its retry.
    Same = [P || Rid =/= undefined, P <- pluto_job_registry:all(),
                 maps:get(<<"request_id">>, P, undefined) =:= Rid],
    Parent = opt(<<"parent">>, R),
    case {Same, Parent =:= undefined orelse pkg(Parent)} of
        {[P | _], _}        -> {{ok, P}, S};
        {_, {error, _} = E} -> {E, S};
        _ ->
            P = new_package(R),
            #{<<"id">> := Id} = P,
            Rules = opt(<<"watch">>, R, []),
            Dir = pluto_job_store:dir(Id),
            ok = io(file:make_dir(Dir)),
            ok = io(file:change_mode(Dir, ?MODE_DIR)),
            ok = io(file:make_dir(filename:join(Dir, ?JOB_DIR_CONTROL))),
            %% Rules first: a package on disk is a job that may run.
            try
                ok = io(pluto_job_watch:prepare(Id, maps:get(<<"owner">>, P), Rules)),
                save(P)
            catch throw:E -> _ = file:del_dir_r(Dir), throw(E)
            end,
            Rules =:= [] orelse pluto_job_watch:prepared(Id),
            pluto_job_notice:event(job_opened, P, ?JOB_QUEUED),
            Env = opt(<<"env">>, R, #{}),
            S1 = schedule(S#state{env = (S#state.env)#{Id => Env}}),
            {pkg(Id), S1}
    end.

io(ok) -> ok;
io({error, R}) -> throw({io, R}).

new_package(R) ->
    Owner = maps:get(<<"agent_id">>, R),
    Now = now_ms(),
    (maps:filter(fun(_, V) -> V =/= null end, maps:with(?JOB_SPEC_KEYS, R)))
    #{<<"schema">> => ?JOB_SCHEMA, <<"id">> => pluto_job_registry:fresh_id(),
      <<"origin">> => pluto_job_store:origin(),
      <<"user">> => opt(<<"user">>, R, <<"unknown">>),
      <<"opener">> => Owner, <<"owner">> => Owner,
      <<"opened_at">> => Now, <<"opened_ns">> => erlang:system_time(nanosecond),
      <<"env_keys">> => maps:keys(opt(<<"env">>, R, #{})),
      <<"ownership">> => [#{<<"agent">> => Owner, <<"at">> => Now, <<"by">> => Owner}],
      <<"sched">> => ?SCHED_QUEUED, <<"done">> => false}.

schedule(S) ->
    Ready = [P || #{<<"id">> := Id} = P <- pluto_job_registry:live(),
                  maps:get(<<"sched">>, P) =/= ?SCHED_QUEUED orelse not backing_off(Id, S)],
    Chosen = pluto_job_launcher:select(
               Ready,
               pluto_config:get(smartstreams_max_running, ?DEFAULT_JOB_MAX_RUNNING),
               pluto_config:get(smartstreams_max_per_agent, ?DEFAULT_JOB_MAX_PER_AGENT)),
    lists:foldl(fun(#{<<"id">> := Id} = P, Acc) ->
        isolate(Id, fun() ->
            Now = now_ms(),
            P1 = P#{<<"sched">> := ?SCHED_SPAWNING, <<"spawn_at">> => Now},
            save(P1),
            pluto_job_launcher:launch(P1, maps:get(Id, Acc#state.env, #{}), self()),
            Acc#state{env = maps:remove(Id, Acc#state.env)}
        end, Acc)
    end, S, Chosen).

spawned(Id, Result, S) ->
    case {pkg(Id), Result} of
        {{ok, #{<<"sched">> := ?SCHED_SPAWNING} = P}, ok} ->
            save(P#{<<"sched">> := ?SCHED_SPAWNED});
        {{ok, #{<<"sched">> := ?SCHED_SPAWNING} = P}, {error, R}} ->
            save(P#{<<"sched">> := ?SCHED_START_FAILED,
                    <<"sched_reason">> => pluto_job_read:sanitize(R)});
        _ -> ok     %% unknown: check/2 decides from state.json
    end,
    S.

%%====================================================================
%% Watching
%%====================================================================

check_all(S) ->
    lists:foldl(fun(#{<<"id">> := Id} = P, Acc) ->
        case backing_off(Id, Acc) of
            true  -> Acc;
            false -> isolate(Id, fun() -> check(P, Acc) end, Acc)
        end
    end, S, pluto_job_registry:live()).

%% One job's trouble pauses work on that job only.
isolate(Id, Fun, S) ->
    case guard(Fun, failed) of
        {{error, _}, failed} -> back_off(Id, S);
        #state{} = S1 -> S1
    end.

back_off(Id, S) -> S#state{retry = (S#state.retry)#{Id => now_ms() + ?JOB_BACKOFF_MS}}.
backing_off(Id, #state{retry = R}) -> maps:get(Id, R, 0) > now_ms().

check(#{<<"id">> := Id, <<"sched">> := Sched} = P, S) ->
    St = state(P),
    Pid = is_map_key(<<"pid">>, St),
    case filelib:is_dir(pluto_job_store:dir(Id)) of
        false -> vanished(P, S);
        true when is_map_key(<<"final_size">>, St) -> finish(P, St, S);
        true ->
            case Sched of
                ?SCHED_QUEUED -> S;
                %% The runner we had given up on is alive after all.
                ?SCHED_START_FAILED when Pid ->
                    save(P#{<<"sched">> := ?SCHED_SPAWNED}), S;
                ?SCHED_START_FAILED -> finish(P, St, S);
                ?SCHED_SPAWNING when Pid ->
                    save(P#{<<"sched">> := ?SCHED_SPAWNED}), S;
                ?SCHED_SPAWNING -> overdue(P, S);
                _ -> maybe_probe(P, St, S)
            end
    end.

%% Nothing heard from the runner for long: forbid the start, wait a
%% moment for one already past that check, then give up.
overdue(#{<<"id">> := Id} = P, #state{aborted = Ab} = S) ->
    Now = now_ms(),
    case Ab of
        #{Id := At} when Now - At > ?JOB_ABORT_MS ->
            save(P#{<<"sched">> := ?SCHED_START_FAILED,
                    <<"sched_reason">> => <<"runner_never_reported">>}),
            S#state{aborted = maps:remove(Id, Ab)};
        #{Id := _} -> S;
        _ ->
            case Now - opt(<<"spawn_at">>, P, 0) > ?JOB_SPAWN_MAX_MS of
                true ->
                    pluto_job_launcher:abort(Id),
                    S#state{aborted = Ab#{Id => Now}};
                false -> S
            end
    end.

%% A silent runner is probed, at most every few seconds.
maybe_probe(#{<<"id">> := Id} = P, St, #state{probes = Pr} = S) ->
    Now = now_ms(),
    %% A runner that just started has not written a heartbeat yet.
    Quiet = min(pluto_job_launcher:hb_age(Id), Now - opt(<<"spawn_at">>, P, 0)),
    case Quiet > ?JOB_HB_STALE_MS andalso is_map_key(<<"pid">>, St) andalso
         Now - maps:get(Id, Pr, 0) > ?JOB_HB_STALE_MS of
        true ->
            pluto_job_launcher:probe(Id, St, self()),
            S#state{probes = Pr#{Id => Now}};
        false -> S
    end.

probed(Id, Runner, Child, S) ->
    case pkg(Id) of
        {ok, P} ->
            %% It may have finished while we were asking; a final
            %% state.json always wins over the probe.
            Final = is_map_key(<<"final_size">>, state(P)),
            case {is_done(P) orelse Final, Runner, Child} of
                {false, no, yes} -> orphan(P), S;
                {false, no, no}  ->
                    P1 = P#{<<"sched">> := ?SCHED_LOST,
                            <<"sched_ended_at">> => now_ms(),
                            <<"sched_reason">> => <<"runner_and_command_are_gone">>},
                    finish(P1, state(P1), S);
                _ -> S              %% alive, or we could not tell
            end;
        _ -> S
    end.

orphan(#{<<"sched">> := ?SCHED_ORPHANED}) -> ok;
orphan(P) ->
    P1 = P#{<<"sched">> := ?SCHED_ORPHANED,
            <<"sched_reason">> => <<"runner_died_command_still_running">>},
    save(P1),
    pluto_job_notice:event(job_orphaned, P1, ?JOB_ORPHANED).

%% Someone removed the directory of an unfinished job.
vanished(#{<<"id">> := Id, <<"owner">> := O} = P, S) ->
    pluto_job_registry:forget(Id),
    pluto_job_notice:history(<<"vanished">>, P, #{<<"state">> => ?JOB_LOST}),
    S#state{board = pluto_job_notice:notify(O, ?EVT_JOB_FINISHED, P, ?JOB_LOST,
                                            S#state.board)}.

finish(#{<<"id">> := Id, <<"owner">> := O} = P, St, S) ->
    P1 = case is_map_key(<<"ended_at">>, St) of
             true  -> P#{<<"done">> => true};
             false -> P#{<<"done">> => true, <<"sched_ended_at">> => now_ms()}
         end,
    save(P1),
    Final = state(P1),
    Name = maps:get(<<"state">>, Final),
    pluto_job_notice:history(<<"finished">>, P1,
        #{<<"state">> => Name,
          <<"exit_code">> => opt(<<"exit_code">>, Final, null),
          <<"started_at">> => opt(<<"started_at">>, Final, null),
          <<"ended_at">> => opt(<<"ended_at">>, Final, null),
          <<"size">> => filelib:file_size(pluto_job_store:file(Id, ?JOB_FILE_OUTPUT))}),
    pluto_job_notice:event(job_finished, P1, Name),
    S#state{board = pluto_job_notice:notify(O, ?EVT_JOB_FINISHED, P1, Name,
                                            S#state.board),
            retry = maps:remove(Id, S#state.retry),
            probes = maps:remove(Id, S#state.probes),
            env = maps:remove(Id, S#state.env)}.

%% The owner of the moment is told, and acts. The action comes first: if
%% we die before the notice is written, both are done again.
watch_hit(Id, #{<<"h">> := N, <<"then">> := Then} = H, #state{told = Told} = S) ->
    case pkg(Id) of
        {ok, #{<<"owner">> := O} = P} ->
            case maps:get(Id, Told, 0) of
                T when T >= N -> S;
                _ ->
                    {Done, S1} = watch_action(P, Then, S),
                    #{<<"state">> := St} = state(P),
                    Extra = (maps:with([<<"h">>, <<"rule">>, <<"line">>, <<"text">>,
                                        <<"also">>, <<"last">>], H))
                            #{<<"watch">> => maps:get(<<"name">>, H), <<"did">> => Done},
                    pluto_job_notice:event(job_watch, P, St),
                    B = pluto_job_notice:notify(O, ?EVT_JOB_WATCH, P, St, Extra,
                                                S1#state.board),
                    S1#state{board = B, told = (S1#state.told)#{Id => N}}
            end;
        _ -> S
    end.

watch_action(_, <<"notify">>, S) -> {null, S};
watch_action(P, Action, S) ->
    case control_one(P, #{<<"action">> => Action}, S) of
        {ok, S1}         -> {Action, S1};
        {{error, W}, S1} -> {<<Action/binary, " failed: ", W/binary>>, S1}
    end.

%%====================================================================
%% Control
%%====================================================================

%% Ownership coordinates agents; the user (--manage) may do everything.
owned(_, {_, true}) -> true;
owned(#{<<"owner">> := O}, {A, false}) -> O =:= A.

control_tree(Actor, #{<<"job_id">> := Id} = R, S) ->
    case with_owned(Actor, Id, fun(P) -> {ok, P} end) of
        {ok, P} ->
            Tree = case opt(<<"tree">>, R, false) of
                       true  -> [D || D <- descendants(Id, [Id]), owned(D, Actor)];
                       false -> []
                   end,
            {Res, S1} = lists:mapfoldl(fun(#{<<"id">> := I} = X, Acc) ->
                {V, Acc1} = control_one(X, R, Acc),
                {{I, V}, Acc1}
            end, S, [P | Tree]),
            case Res of
                [{_, {error, _} = E}] -> {E, S1};
                _ -> {{ok, #{<<"jobs">> => maps:from_list(
                        [{I, case V of ok -> <<"ok">>; {error, W} -> W end}
                         || {I, V} <- Res])}}, S1}
            end;
        E -> {E, S}
    end.

with_owned(Actor, Id, Fun) ->
    case pkg(Id) of
        {ok, P} ->
            case owned(P, Actor) of
                true  -> Fun(P);
                false -> {error, ?ERR_NOT_OWNER}
            end;
        E -> E
    end.

%% Seen guards against cycles, which an imported package could contain.
descendants(Id, Seen) ->
    Kids = [K || #{<<"id">> := I} = K <- pluto_job_registry:children(Id),
                 not lists:member(I, Seen)],
    Ids = [maps:get(<<"id">>, K) || K <- Kids],
    Kids ++ lists:append([descendants(I, Ids ++ Seen) || I <- Ids]).

control_one(P, R, S) ->
    Action = case opt(<<"action">>, R) of
                 ?ACT_HALT -> ?ACT_CANCEL;
                 A         -> A
             end,
    Signal = opt(<<"signal">>, R, null),
    Known = lists:member(Action, ?JOB_ACTIONS),
    BadSignal = Action =:= ?ACT_SIGNAL andalso not lists:member(Signal, ?JOB_SIGNALS),
    case P of
        _ when not Known -> {{error, <<"bad_action">>}, S};
        _ when BadSignal -> {{error, <<"signal_not_allowed">>}, S};
        #{<<"done">> := true} -> {{error, <<"job_already_finished">>}, S};
        #{<<"sched">> := ?SCHED_QUEUED} when Action =:= ?ACT_CANCEL ->
            P1 = P#{<<"sched">> := ?SCHED_CANCELLED},
            {ok, finish(P1, state(P1), S)};
        #{<<"sched">> := ?SCHED_QUEUED} -> {{error, <<"job_is_queued">>}, S};
        #{<<"sched">> := ?SCHED_ORPHANED} when Action =:= ?ACT_CANCEL ->
            {pluto_job_launcher:kill_orphan(state(P)), S};
        #{<<"sched">> := ?SCHED_ORPHANED} ->
            {{error, <<"job_is_orphaned_only_cancel_works">>}, S};
        #{<<"id">> := Id} ->
            {pluto_job_launcher:request(Id, #{<<"action">> => Action,
                                              <<"signal">> => Signal}), S}
    end.

do_input(Actor, #{<<"job_id">> := Id} = R) ->
    with_owned(Actor, Id, fun(P) ->
        Text = opt(<<"text">>, R, <<>>),
        Stdin = opt(<<"stdin">>, P, false),
        Running = maps:get(<<"sched">>, P) =:= ?SCHED_SPAWNED andalso not is_done(P),
        if
            Stdin =/= true -> {error, <<"job_was_started_without_stdin">>};
            not Running    -> {error, <<"job_is_not_running">>};
            byte_size(Text) > ?JOB_INPUT_MAX -> {error, <<"input_too_large">>};
            true ->
                case pluto_job_launcher:request(
                       Id, #{<<"action">> => ?ACT_INPUT,
                             <<"data">> => base64:encode(Text),
                             <<"close">> => opt(<<"close">>, R, false)}) of
                    ok -> {ok, #{<<"queued_bytes">> => byte_size(Text)}};
                    E  -> E
                end
        end
    end).

do_transfer({By, _} = Actor, #{<<"job_id">> := Id, <<"to">> := To}, S) ->
    case with_owned(Actor, Id, fun(P) -> {ok, P} end) of
        {ok, #{<<"owner">> := From} = P} ->
            Log = maps:get(<<"ownership">>, P, []),
            P1 = P#{<<"owner">> := To,
                    <<"ownership">> => Log ++ [#{<<"agent">> => To, <<"at">> => now_ms(),
                                                 <<"by">> => By}]},
            save(P1),
            pluto_job_cursor:inherit(Id, From, To),
            #{<<"state">> := St} = state(P1),
            pluto_job_notice:history(<<"transferred">>, P1,
                                     #{<<"from">> => From, <<"state">> => St}),
            pluto_job_notice:event(job_transferred, P1, St),
            Board = lists:foldl(fun(A, B) ->
                pluto_job_notice:notify(A, ?EVT_JOB_TRANSFERRED, P1, St, B)
            end, pluto_job_notice:move(Id, From, To, S#state.board),
            %% Who did it knows.
            lists:usort([From, To]) -- [By]),
            {{ok, #{<<"job_id">> => Id, <<"from">> => From, <<"to">> => To}},
             S#state{board = Board}};
        E -> {E, S}
    end.

do_stream({A, _} = Actor, #{<<"job_id">> := Id} = R) ->
    case pkg(Id) of
        {ok, P} -> pluto_job_streams:apply_action(Id, state(P), {A, owned(P, Actor)}, R);
        E       -> E
    end.
