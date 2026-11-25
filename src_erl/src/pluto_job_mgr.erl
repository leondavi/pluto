%%%-------------------------------------------------------------------
%%% @doc pluto_job_mgr — Registry, scheduler and lifecycle of jobs.
%%%
%%% Serialises every mutation of a job package. Each job runs under its
%%% own detached runner (priv/pluto_job_runner.py); this process only
%%% launches runners, reads their state.json, and recovers after a
%%% restart by rescanning the packages on disk.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_mgr).
-behaviour(gen_server).

-include("pluto.hrl").
-include_lib("kernel/include/file.hrl").

-export([start_link/0, activate/0, call/1, pkg/1, all/0, state/1, is_done/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-define(TICK_MS, 500).
-define(HB_STALE_MS, 5000).
-define(SPAWN_LIMIT_MS, 20000).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).

-record(state, {
    active = false :: boolean(),
    env    = #{}   :: map(),    %% job id -> env values (never on disk)
    checks = #{}   :: map(),    %% job id -> last liveness probe (ms)
    seen   = #{}   :: map()     %% request id -> reply, for HTTP retries
}).

start_link() -> gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Make sure the package home exists and belongs to this server.
activate() -> gen_server:call(?MODULE, activate, 30000).
call(Req)  -> gen_server:call(?MODULE, Req, 30000).

pkg(Id) ->
    case pluto_job_store:valid_id(Id) of
        false -> {error, <<"bad_job_id">>};
        true ->
            case ets:lookup(?ETS_JOBS, Id) of
                [{_, P}] -> {ok, P};
                []       -> {error, <<"unknown_job">>}
            end
    end.

all() -> [P || {_, P} <- ets:tab2list(?ETS_JOBS)].
is_done(P) -> maps:get(<<"done">>, P, false).

%% @doc Effective state: the runner's state.json, overridden by what only
%% the server can know (queued, orphaned, lost, snapshot, ...).
state(#{<<"id">> := Id, <<"sched">> := Sched} = P) ->
    St = case pluto_job_store:read_json(pluto_job_store:file(Id, "state.json")) of
             {ok, M} -> M;
             _       -> #{}
         end,
    Reason = opt(<<"sched_reason">>, P, null),
    case {Sched, St} of
        {<<"spawned">>, #{<<"state">> := _}}  -> St;
        {<<"spawning">>, #{<<"state">> := _}} -> St;
        {_, #{<<"final_size">> := _}} -> St;
        {<<"spawning">>, _} -> St#{<<"state">> => <<"starting">>};
        {<<"spawned">>, _}  -> St#{<<"state">> => <<"starting">>};
        {<<"start_failed">>, _} ->
            St#{<<"state">> => <<"failed">>, <<"reason">> => Reason};
        _ -> St#{<<"state">> => Sched, <<"reason">> => Reason}
    end.

%%====================================================================
%% gen_server
%%====================================================================

init([]) ->
    %% Only adopt a home that is already in use; a server nobody runs
    %% jobs on never touches the file system.
    State = case filelib:is_dir(pluto_job_store:jobs_dir()) of
                true ->
                    case do_activate(#state{}) of
                        {ok, S}     -> S;
                        {{error, R}, S} ->
                            ?LOG_WARN("smartstreams inactive: ~p", [R]), S
                    end;
                false -> #state{}
            end,
    erlang:send_after(?TICK_MS, self(), tick),
    {ok, State}.

handle_call(activate, _From, S) ->
    {R, S1} = do_activate(S),
    {reply, R, S1};
handle_call(Req, _From, #state{active = false} = S) ->
    _ = Req,
    {reply, {error, <<"smartstreams_inactive">>}, S};
handle_call({start, R}, _From, S) -> start_job(R, S);
handle_call({control, A, R}, _From, S) ->
    dedupe(R, S, fun(S1) -> {control(A, R), S1} end);
handle_call({input, A, R}, _From, S) ->
    dedupe(R, S, fun(S1) -> {input(A, R), S1} end);
handle_call({transfer, A, R}, _From, S) ->
    dedupe(R, S, fun(S1) -> {transfer(A, R), S1} end);
handle_call({review, A, R}, _From, S) ->
    dedupe(R, S, fun(S1) -> {review(A, R), S1} end);
handle_call({stream, A, R}, _From, S) ->
    dedupe(R, S, fun(S1) -> {stream(A, R), S1} end);
handle_call({next, A, R}, _From, S) -> {reply, next(A, R), S};
handle_call({notices, Agent, Ack}, _From, S) -> {reply, notices(Agent, Ack), S};
handle_call({adopt, Staging, Agent, User}, _From, S) ->
    {reply, adopt(Staging, Agent, User), S};
handle_call({prune, BeforeMs, Dry}, _From, S) ->
    {reply, prune(BeforeMs, Dry), S};
handle_call(_, _From, S) -> {reply, {error, <<"unknown_request">>}, S}.

handle_cast(_, S) -> {noreply, S}.

handle_info(tick, S) ->
    erlang:send_after(?TICK_MS, self(), tick),
    case S#state.active of
        true ->
            S1 = lists:foldl(fun check/2, S, [P || P <- all(), not is_done(P)]),
            {noreply, schedule(S1)};
        false -> {noreply, S}
    end;
handle_info({spawned, Id, Result}, S) ->
    case pkg(Id) of
        {ok, P} ->
            save(case Result of
                     ok -> P#{<<"sched">> := <<"spawned">>};
                     {error, R} ->
                         P#{<<"sched">> := <<"start_failed">>,
                            <<"sched_reason">> => pluto_job_read:sanitize(R)}
                 end);
        _ -> ok
    end,
    {noreply, S};
handle_info(_, S) -> {noreply, S}.

terminate(_, _) -> ok.

%%====================================================================
%% Activation and recovery
%%====================================================================

do_activate(#state{active = true} = S) -> {ok, S};
do_activate(S) ->
    case pluto_job_store:ensure_home() of
        ok ->
            case pluto_job_store:lock_home() of
                ok -> ets:delete_all_objects(?ETS_JOBS), load_all(),
                      {ok, S#state{active = true}};
                E  -> {E, S}
            end;
        E -> {E, S}
    end.

load_all() ->
    {ok, Names} = file:list_dir(pluto_job_store:jobs_dir()),
    lists:foreach(
      fun(Name) ->
          Id = unicode:characters_to_binary(Name),
          case pluto_job_store:valid_id(Id) andalso
               pluto_job_store:read_json(pluto_job_store:file(Id, "package.json")) of
              {ok, #{<<"id">> := Id, <<"sched">> := Sched} = P} ->
                  Now = now_ms(),
                  ets:insert(?ETS_JOBS, {Id, P}),
                  case Sched of
                      <<"queued">> ->
                          case opt(<<"env_keys">>, P, []) of
                              [] -> ok;
                              _  -> save(P#{<<"sched">> := <<"start_failed">>,
                                            <<"sched_reason">> =>
                                                <<"env values were lost in a server restart">>})
                          end;
                      <<"spawning">> -> save(P#{<<"spawn_at">> => Now});
                      _ -> ok
                  end;
              _ -> ok
          end
      end, Names).

save(#{<<"id">> := Id} = P) ->
    ok = pluto_job_store:write_json(pluto_job_store:file(Id, "package.json"), P),
    ets:insert(?ETS_JOBS, {Id, P}),
    ok.

%% Retried HTTP requests get the first reply again.
dedupe(R, #state{seen = Seen} = S, Fun) ->
    case opt(<<"request_id">>, R) of
        undefined -> {Reply, S1} = Fun(S), {reply, Reply, S1};
        Rid ->
            case Seen of
                #{Rid := Reply} -> {reply, Reply, S};
                _ ->
                    {Reply, S1} = Fun(S),
                    Kept = case maps:size(Seen) > 2000 of
                               true  -> #{};
                               false -> Seen
                           end,
                    {reply, Reply, S1#state{seen = Kept#{Rid => Reply}}}
            end
    end.

%%====================================================================
%% Start and schedule
%%====================================================================

start_job(R, S) ->
    Rid = opt(<<"request_id">>, R),
    Same = [P || P <- all(), Rid =/= undefined,
                 maps:get(<<"request_id">>, P, undefined) =:= Rid],
    Parent = opt(<<"parent">>, R),
    case {Same, Parent =:= undefined orelse pkg(Parent)} of
        {[P | _], _}       -> {reply, {ok, P}, S};
        {_, {error, _} = E} -> {reply, E, S};
        _ ->
            Id = pluto_job_store:new_id(),
            Dir = pluto_job_store:dir(Id),
            ok = file:make_dir(Dir),
            ok = file:change_mode(Dir, 8#700),
            ok = file:make_dir(filename:join(Dir, "control")),
            Env = opt(<<"env">>, R, #{}),
            Owner = maps:get(<<"agent_id">>, R),
            Now = now_ms(),
            Spec = maps:with([<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                              <<"timeout_s">>, <<"max_output_bytes">>,
                              <<"name">>, <<"tags">>, <<"task_id">>,
                              <<"framework">>, <<"session_id">>, <<"parent">>,
                              <<"rerun_of">>, <<"base_env">>, <<"request_id">>], R),
            P = Spec#{<<"schema">> => 1, <<"id">> => Id,
                      <<"origin">> => origin(),
                      <<"user">> => opt(<<"user">>, R, <<"unknown">>),
                      <<"opener">> => Owner, <<"owner">> => Owner,
                      <<"opened_at">> => Now,
                      <<"opened_ns">> => erlang:system_time(nanosecond),
                      <<"env_keys">> => maps:keys(Env),
                      <<"ownership">> => [#{<<"agent">> => Owner, <<"at">> => Now,
                                            <<"by">> => Owner}],
                      <<"sched">> => <<"queued">>, <<"done">> => false},
            save(P),
            event(job_opened, P, <<"queued">>),
            S1 = schedule(S#state{env = (S#state.env)#{Id => Env}}),
            {ok, P1} = pkg(Id),
            {reply, {ok, P1}, S1}
    end.

origin() ->
    {ok, Host} = inet:gethostname(),
    {_, Os} = os:type(),
    #{<<"host_id">> => pluto_job_store:host_id(),
      <<"hostname">> => list_to_binary(Host),
      <<"os">> => atom_to_binary(Os),
      <<"arch">> => list_to_binary(erlang:system_info(system_architecture))}.

schedule(S) ->
    Jobs = [P || P <- all(), not is_done(P)],
    Running = [maps:get(<<"owner">>, P) || #{<<"sched">> := Sc} = P <- Jobs,
                                           Sc =/= <<"queued">>],
    Queued = lists:sort(fun(A, B) -> maps:get(<<"opened_ns">>, A, 0) =<
                                     maps:get(<<"opened_ns">>, B, 0) end,
                        [P || #{<<"sched">> := <<"queued">>} = P <- Jobs]),
    schedule(Queued, Running, pluto_config:get(smartstreams_max_running, 16),
             pluto_config:get(smartstreams_max_per_agent, 8), S).

schedule([], _, _, _, S) -> S;
schedule(_, Running, Max, _, S) when length(Running) >= Max -> S;
schedule([#{<<"owner">> := O} = P | T], Running, Max, Per, S) ->
    case length([X || X <- Running, X =:= O]) < Per of
        true  -> schedule(T, [O | Running], Max, Per, launch(P, S));
        false -> schedule(T, Running, Max, Per, S)
    end.

launch(#{<<"id">> := Id} = P, #state{env = Envs} = S) ->
    save(P#{<<"sched">> := <<"spawning">>, <<"spawn_at">> => now_ms()}),
    Spec = (maps:with([<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                       <<"timeout_s">>, <<"max_output_bytes">>,
                       <<"base_env">>], P))
           #{<<"env">> => maps:get(Id, Envs, #{}),
             <<"min_free_bytes">> =>
                 pluto_config:get(smartstreams_min_free_bytes, 268435456)},
    Clean = maps:filter(fun(_, V) -> V =/= null end, Spec),
    Mgr = self(),
    spawn(fun() -> Mgr ! {spawned, Id, run_runner(Id, Clean)} end),
    S#state{env = maps:remove(Id, Envs)}.

run_runner(Id, Spec) ->
    case pluto_job_store:python() of
        {error, R} -> {error, atom_to_binary(R)};
        {ok, Py} ->
            Port = open_port({spawn_executable, Py},
                             [{args, ["-I", pluto_job_store:script("pluto_job_runner.py"),
                                      "run", pluto_job_store:dir(Id)]},
                              binary, exit_status, use_stdio, hide]),
            port_command(Port, pluto_protocol_json:encode_line(Spec)),
            await_ready(Port, <<>>)
    end.

await_ready(Port, Acc) ->
    receive
        {Port, {data, D}} -> await_ready(Port, <<Acc/binary, D/binary>>);
        {Port, {exit_status, _}} ->
            case string:trim(Acc) of
                <<"ready">> -> ok;
                <<>>        -> {error, <<"runner did not start">>};
                Other       -> {error, Other}
            end
    after 15000 ->
        catch port_close(Port),
        {error, <<"runner start timed out">>}
    end.

%%====================================================================
%% Watching
%%====================================================================

check(#{<<"sched">> := <<"queued">>}, S) -> S;
check(#{<<"id">> := Id, <<"sched">> := Sched} = P, S) ->
    St = state(P),
    Final = maps:is_key(<<"final_size">>, St),
    Stale = Sched =:= <<"spawning">> andalso not maps:is_key(<<"pid">>, St)
            andalso now_ms() - opt(<<"spawn_at">>, P, 0) > ?SPAWN_LIMIT_MS,
    if
        Final; Sched =:= <<"start_failed">> ->
            finish(P, St),
            S#state{checks = maps:remove(Id, S#state.checks)};
        Stale ->
            save(P#{<<"sched">> := <<"start_failed">>,
                    <<"sched_reason">> => <<"runner never reported">>}),
            S;
        true -> probe(P, St, S)
    end.

%% A silent runner is probed at most every few seconds.
probe(#{<<"id">> := Id} = P, St, #state{checks = C} = S) ->
    Now = now_ms(),
    Quiet = Now - hb_ms(Id) > ?HB_STALE_MS,
    Due = Now - maps:get(Id, C, 0) > ?HB_STALE_MS,
    case Quiet andalso Due andalso maps:is_key(<<"pid">>, St) of
        false -> S;
        true ->
            Runner = alive(<<"runner_pid">>, <<"runner_ident">>, St),
            Child = Runner orelse alive(<<"pid">>, <<"child_ident">>, St),
            %% It may have finished between our two reads.
            Final = maps:is_key(<<"final_size">>, state(P)),
            if
                Runner; Final -> ok;
                Child  -> orphan(P);
                true   ->
                    P1 = P#{<<"sched">> := <<"lost">>,
                            <<"sched_reason">> => <<"runner and command are gone">>},
                    finish(P1, state(P1))
            end,
            S#state{checks = C#{Id => Now}}
    end.

hb_ms(Id) ->
    case file:read_file_info(pluto_job_store:file(Id, "hb"), [{time, posix}]) of
        {ok, #file_info{mtime = T}} -> T * 1000;
        _ -> 0
    end.

alive(PidKey, IdentKey, St) ->
    case St of
        #{PidKey := Pid, IdentKey := Ident} when Ident =/= <<>> ->
            pluto_job_store:ident(Pid) =:= Ident;
        _ -> false
    end.

orphan(#{<<"sched">> := <<"orphaned">>}) -> ok;
orphan(P) ->
    P1 = P#{<<"sched">> := <<"orphaned">>,
            <<"sched_reason">> => <<"runner died, command still running">>},
    save(P1),
    event(job_orphaned, P1, <<"orphaned">>).

finish(#{<<"id">> := Id} = P, St) ->
    Name = maps:get(<<"state">>, St),
    P1 = P#{<<"done">> => true},
    save(P1),
    history(<<"finished">>, P1,
            #{<<"state">> => Name,
              <<"exit_code">> => opt(<<"exit_code">>, St, null),
              <<"started_at">> => opt(<<"started_at">>, St, null),
              <<"ended_at">> => opt(<<"ended_at">>, St, now_ms()),
              <<"size">> => filelib:file_size(pluto_job_store:file(Id, "output.log"))}),
    event(job_finished, P1, Name),
    notify(maps:get(<<"owner">>, P1), <<"job_finished">>, P1, Name).

%%====================================================================
%% Notices, events, history (ids and states only: /events is public)
%%====================================================================

event(Type, #{<<"id">> := Id, <<"owner">> := O}, St) ->
    pluto_event_log:log(Type, #{<<"job_id">> => Id, <<"agent_id">> => O,
                                <<"state">> => St}).

notify(Agent, Event, #{<<"id">> := Id} = P, St) ->
    File = filename:join(pluto_job_store:home(), "notices.jsonl"),
    ok = pluto_job_store:append_jsonl(
           File, #{<<"seq">> => erlang:system_time(microsecond),
                   <<"agent">> => Agent,
                   <<"event">> => Event, <<"job_id">> => Id, <<"state">> => St,
                   <<"name">> => opt(<<"name">>, P, null),
                   <<"at">> => now_ms()}),
    catch pluto_msg_hub:push_event_to_agent(
            Agent, #{<<"event">> => Event, <<"job_id">> => Id,
                     <<"state">> => St, <<"from">> => <<"smartstreams">>}),
    ok.

%% Durable until acknowledged (Ack = highest seq the agent has seen).
notices(Agent, Ack) ->
    File = filename:join(pluto_job_store:home(), "notices.jsonl"),
    All = pluto_job_store:read_jsonl(File),
    Mine = fun(#{<<"agent">> := A}) -> A =:= Agent end,
    Keep = [N || #{<<"seq">> := Q} = N <- All, not (Mine(N) andalso Q =< Ack)],
    case length(Keep) < length(All) of
        true ->
            ok = file:write_file(File ++ ".tmp",
                                 [pluto_protocol_json:encode_line(N) || N <- Keep]),
            ok = file:rename(File ++ ".tmp", File);
        false -> ok
    end,
    {ok, lists:sublist([N || N <- Keep, Mine(N)], 20)}.

history(Kind, P, Extra) ->
    ok = pluto_job_store:append_jsonl(
           filename:join(pluto_job_store:home(), "history.jsonl"),
           maps:merge((maps:with([<<"id">>, <<"name">>, <<"user">>, <<"opener">>,
                                  <<"owner">>, <<"opened_at">>, <<"command">>,
                                  <<"argv">>, <<"origin">>], P))
                      #{<<"kind">> => Kind, <<"at">> => now_ms()}, Extra)).

%%====================================================================
%% Control
%%====================================================================

%% Actor is {AgentId, IsUser}; the user (--manage) may do everything.
owned(_, {_, true}) -> true;
owned(#{<<"owner">> := O}, {A, false}) -> O =:= A.

with_owned(Actor, Id, Fun) ->
    case pkg(Id) of
        {ok, P} ->
            case owned(P, Actor) of
                true  -> Fun(P);
                false -> {error, <<"not_owner">>}
            end;
        E -> E
    end.

control(Actor, #{<<"job_id">> := Id} = R) ->
    with_owned(Actor, Id, fun(P) ->
        Tree = case opt(<<"tree">>, R, false) of
                   true -> [D || D <- descendants(Id), owned(D, Actor)];
                   _    -> []
               end,
        Res = [{maps:get(<<"id">>, X), control_one(X, R)} || X <- [P | Tree]],
        case Res of
            [{_, {error, _} = E}] -> E;
            _ -> {ok, #{<<"jobs">> => maps:from_list(
                    [{I, case V of ok -> <<"ok">>; {error, W} -> W end}
                     || {I, V} <- Res])}}
        end
    end).

descendants(Id) ->
    Kids = [P || P <- all(), maps:get(<<"parent">>, P, null) =:= Id],
    Kids ++ lists:append([descendants(maps:get(<<"id">>, K)) || K <- Kids]).

control_one(P, R) ->
    Action = case opt(<<"action">>, R) of
                 <<"halt">> -> <<"cancel">>;
                 A          -> A
             end,
    Known = lists:member(Action, [<<"cancel">>, <<"pause">>, <<"resume">>,
                                  <<"signal">>]),
    SigOk = Action =/= <<"signal">> orelse
            lists:member(opt(<<"signal">>, R),
                         [<<"INT">>, <<"TERM">>, <<"HUP">>, <<"USR1">>, <<"USR2">>,
                          <<"QUIT">>, <<"KILL">>, <<"STOP">>, <<"CONT">>]),
    case P of
        _ when not Known -> {error, <<"unknown_action">>};
        _ when not SigOk -> {error, <<"signal_not_allowed">>};
        #{<<"done">> := true} -> {error, <<"job_already_finished">>};
        #{<<"sched">> := <<"queued">>} when Action =:= <<"cancel">> ->
            P1 = P#{<<"sched">> := <<"cancelled">>},
            finish(P1, state(P1));
        #{<<"sched">> := <<"queued">>} -> {error, <<"job_is_queued">>};
        #{<<"sched">> := <<"orphaned">>} -> orphan_control(P, Action);
        _ -> request(P, #{<<"action">> => Action,
                          <<"signal">> => opt(<<"signal">>, R, null)})
    end.

orphan_control(P, <<"cancel">>) ->
    #{<<"pid">> := Pid, <<"child_ident">> := Ident} = state(P),
    {_, Out} = pluto_job_store:run("pluto_job_runner.py",
                                   ["kill", integer_to_binary(Pid), Ident, "9"]),
    case Out of
        <<"ok">> -> ok;
        _        -> {error, <<"process_is_gone">>}
    end;
orphan_control(_, _) -> {error, <<"job_is_orphaned_only_cancel_works">>}.

%% The runner consumes numbered files in name order.
request(#{<<"id">> := Id}, Req) ->
    Name = io_lib:format("~20..0w.json", [erlang:system_time(nanosecond)]),
    case pluto_job_store:write_json(
           pluto_job_store:file(Id, filename:join("control", Name)), Req) of
        ok -> ok;
        _  -> {error, <<"control_write_failed">>}
    end.

input(Actor, #{<<"job_id">> := Id} = R) ->
    with_owned(Actor, Id, fun(P) ->
        Text = opt(<<"text">>, R, <<>>),
        Stdin = opt(<<"stdin">>, P, false),
        Running = maps:get(<<"sched">>, P) =:= <<"spawned">> andalso not is_done(P),
        if
            Stdin =/= true -> {error, <<"job_was_started_without_stdin">>};
            not Running    -> {error, <<"job_is_not_running">>};
            byte_size(Text) > 65536 -> {error, <<"input_too_large">>};
            true ->
                case request(P, #{<<"action">> => <<"input">>,
                                  <<"data">> => base64:encode(Text),
                                  <<"close">> => opt(<<"close">>, R, false)}) of
                    ok -> {ok, #{<<"queued_bytes">> => byte_size(Text)}};
                    E  -> E
                end
        end
    end).

transfer({By, _} = Actor, #{<<"job_id">> := Id, <<"to">> := To}) ->
    with_owned(Actor, Id, fun(#{<<"owner">> := From} = P) ->
        Log = maps:get(<<"ownership">>, P, []),
        P1 = P#{<<"owner">> := To,
                <<"ownership">> => Log ++ [#{<<"agent">> => To, <<"at">> => now_ms(),
                                             <<"by">> => By}]},
        save(P1),
        Cur = cursors(Id),
        case Cur of
            #{From := C} when not is_map_key(To, Cur) ->
                save_cursors(Id, Cur#{To => maps:without([<<"last_req">>], C)});
            _ -> ok
        end,
        #{<<"state">> := St} = state(P1),
        history(<<"transferred">>, P1, #{<<"from">> => From, <<"state">> => St}),
        event(job_transferred, P1, St),
        [notify(A, <<"job_transferred">>, P1, St) || A <- lists:usort([From, To])],
        {ok, #{<<"job_id">> => Id, <<"from">> => From, <<"to">> => To}}
    end).

review({By, _}, #{<<"job_id">> := Id} = R) ->
    case pkg(Id) of
        {ok, P} ->
            #{lo := Lo, hi := Hi} = Src = pluto_job_streams:main_source(Id, state(P)),
            A = opt(<<"from_offset">>, R, Lo),
            B = opt(<<"to_offset">>, R, Hi),
            Ok = is_integer(A) andalso is_integer(B) andalso A >= Lo
                 andalso B >= A andalso B =< Hi,
            case Ok of
                false -> {error, <<"bad_range">>};
                true ->
                    E = #{<<"id">> => hex8(), <<"by">> => By, <<"at">> => now_ms(),
                          <<"verdict">> => opt(<<"verdict">>, R, <<"note">>),
                          <<"note">> => opt(<<"note">>, R, <<>>),
                          <<"from_offset">> => A, <<"to_offset">> => B,
                          <<"sha256">> => range_hash(Src, A, B)},
                    ok = pluto_job_store:append_jsonl(
                           pluto_job_store:file(Id, "reviews.jsonl"), E),
                    {ok, E}
            end;
        E -> E
    end.

hex8() -> string:lowercase(binary:encode_hex(crypto:strong_rand_bytes(4))).

%% Hashing is bounded; a larger range is pinned by its manifest instead.
range_hash(_, A, B) when B - A > 67108864 -> null;
range_hash(Src, A, B) ->
    H = fun F(O, Ctx) when O >= B -> Ctx;
            F(O, Ctx) ->
                D = pluto_job_read:bytes(Src, O, min(1048576, B - O)),
                case D of
                    <<>> -> Ctx;
                    _    -> F(O + byte_size(D), crypto:hash_update(Ctx, D))
                end
        end,
    string:lowercase(binary:encode_hex(
        crypto:hash_final(H(A, crypto:hash_init(sha256))))).

stream({A, _} = Actor, #{<<"job_id">> := Id} = R) ->
    case pkg(Id) of
        {ok, P} -> pluto_job_streams:act(Id, state(P), {A, owned(P, Actor)}, R);
        E       -> E
    end.

%%====================================================================
%% Cursors
%%====================================================================

cursors(Id) ->
    case pluto_job_store:read_json(pluto_job_store:file(Id, "cursors.json")) of
        {ok, M} -> M;
        _       -> #{}
    end.

save_cursors(Id, M) ->
    ok = pluto_job_store:write_json(pluto_job_store:file(Id, "cursors.json"), M).

%% Read the next unread bytes and move this reader's cursor. A repeated
%% request_id replays the same range, so a retry cannot skip data.
next({Agent, _}, #{<<"job_id">> := Id} = R) ->
    case pkg(Id) of
        {ok, P} ->
            SId = opt(<<"stream">>, R),
            case pluto_job_streams:source(Id, state(P), SId) of
                {ok, Src} -> next(Id, Agent, SId, Src, R);
                E -> E
            end;
        E -> E
    end.

next(Id, Agent, SId, #{lo := Lo} = Src, R) ->
    Key = case SId of
              undefined -> Agent;
              _         -> <<Agent/binary, "|", SId/binary>>
          end,
    Cur = cursors(Id),
    C = maps:get(Key, Cur, #{}),
    Rid = opt(<<"request_id">>, R, null),
    Paused = SId =/= undefined andalso
             case pluto_job_streams:find(Id, SId) of
                 {ok, #{<<"state">> := <<"paused">>}} -> true;
                 _ -> false
             end,
    Opts = #{cut => true, encoding => opt(<<"encoding">>, R, <<"utf8">>),
             strip_ansi => opt(<<"strip_ansi">>, R, false)},
    case C of
        _ when Paused -> {ok, #{<<"paused">> => true, <<"text">> => <<>>}};
        #{<<"last_req">> := Rid, <<"last_start">> := A, <<"last_end">> := B}
          when Rid =/= null ->
            {ok, pluto_job_read:read(Src#{hi := B, closed := true}, A, B - A, Opts)};
        _ ->
            From = opt(<<"from_offset">>, R, opt(<<"pos">>, C, Lo)),
            Max = opt(<<"max_bytes">>, R, 8192),
            Out = #{<<"start">> := A, <<"end">> := B} =
                pluto_job_read:read(Src, From, Max, Opts),
            save_cursors(Id, Cur#{Key => #{
                <<"pos">> => B, <<"last_req">> => Rid, <<"last_start">> => A,
                <<"last_end">> => B, <<"at">> => now_ms(),
                <<"delivered">> => opt(<<"delivered">>, C, 0) + B - A}}),
            {ok, Out#{<<"unread">> => maps:get(hi, Src) - B}}
    end.

%%====================================================================
%% Imports and pruning
%%====================================================================

%% Staging was fully verified by pluto_job_package.py.
adopt(Staging, Agent, User) ->
    {ok, #{<<"id">> := Id} = New} =
        pluto_job_store:read_json(filename:join(Staging, "package.json")),
    Dir = pluto_job_store:dir(Id),
    case pkg(Id) of
        {error, <<"unknown_job">>} ->
            case filelib:is_file(Dir) of
                true  -> {error, <<"job_directory_already_exists">>};
                false -> install(Staging, Dir, New, Agent, User)
            end;
        {ok, Old} ->
            Mine = maps:get(<<"host_id">>, opt(<<"origin">>, Old, #{}), <<>>)
                   =:= pluto_job_store:host_id(),
            {_, Out} = pluto_job_store:run("pluto_job_package.py",
                                           ["merge", Dir, Staging]),
            case pluto_protocol_json:decode(Out) of
                {ok, #{<<"result">> := Same}}
                  when Same =:= <<"identical">>; Same =:= <<"older">> ->
                    {ok, Id, Same};
                {ok, #{<<"result">> := <<"extends">>}} when not Mine ->
                    Bak = Dir ++ ".old",
                    _ = file:del_dir_r(Bak),
                    ok = file:rename(Dir, Bak),
                    Res = install(Staging, Dir, New, maps:get(<<"owner">>, Old), User),
                    _ = file:del_dir_r(Bak),
                    case Res of {ok, I, _} -> {ok, I, <<"extended">>}; E -> E end;
                {ok, #{<<"result">> := <<"extends">>}} ->
                    {error, <<"this host originated the job">>};
                _ -> {error, <<"diverged: content does not match the existing job">>}
            end
    end.

install(Staging, Dir, New, Agent, User) ->
    #{<<"id">> := Id} = New,
    {ok, St} = pluto_job_store:read_json(filename:join(Staging, "state.json")),
    Final = maps:is_key(<<"final_size">>, St),
    Keep = maps:without([<<"base_env">>, <<"request_id">>, <<"spawn_at">>], New),
    P = Keep#{<<"sched">> => case Final of true -> <<"imported">>;
                                           false -> <<"snapshot">> end,
              <<"sched_reason">> => null,
              <<"done">> => true, <<"owner">> => Agent,
              <<"env_keys">> => opt(<<"env_keys">>, New, []),
              <<"imported">> => #{<<"at">> => now_ms(), <<"by">> => Agent,
                                  <<"user">> => User,
                                  <<"owner_at_origin">> => maps:get(<<"owner">>, New)}},
    ok = file:rename(Staging, Dir),
    _ = file:change_mode(Dir, 8#700),
    save(P),
    history(<<"imported">>, P, #{<<"state">> => maps:get(<<"state">>, state(P))}),
    event(job_imported, P, <<"imported">>),
    {ok, Id, <<"imported">>}.

prune(BeforeMs, Dry) ->
    Old = [P || P <- all(), is_done(P),
                opt(<<"ended_at">>, state(P), maps:get(<<"opened_at">>, P)) < BeforeMs],
    Ids = [maps:get(<<"id">>, P) || P <- Old],
    case Dry of
        true -> ok;
        false ->
            lists:foreach(fun(#{<<"id">> := Id} = P) ->
                history(<<"pruned">>, P, #{<<"state">> => maps:get(<<"state">>, state(P))}),
                _ = file:del_dir_r(pluto_job_store:dir(Id)),
                ets:delete(?ETS_JOBS, Id)
            end, Old)
    end,
    {ok, Ids}.
