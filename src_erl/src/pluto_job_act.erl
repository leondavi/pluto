%%%-------------------------------------------------------------------
%%% @doc pluto_job_act — Carries out what acting watch rules ask for.
%%%
%%% pluto_job_watch keeps the actions of a job, fully expanded, in
%%% watch.json until this server reports them done, so nothing here needs
%%% to survive: after any crash they are simply handed over again. Hence
%%% an action happens at least once, and twice only when a crash falls
%%% between it and the record that it is done.
%%%
%%% Actions of one job happen in order, one at a time; jobs do not wait
%%% for each other. Files are written by a helper process
%%% (priv/pluto_job_act.py), which cannot be led outside the directory of
%%% the job and whose death or hanging costs a retry. Input goes through
%%% pluto_job_mgr, which never calls back.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_act).
-behaviour(gen_server).

-include("pluto.hrl").

-export([start_link/0, perform/2, forget/1]).
-export([init/1, handle_call/3, handle_cast/2, handle_info/2, terminate/2]).

-import(pluto_job_store, [opt/2]).

-type act() :: #{binary() => term()}.   %% <<"a">> numbers a job's actions

-record(task, {act     :: act(),
               tries = 0 :: non_neg_integer(),
               timer = none :: none | reference()}).

-record(state, {
    port  = none :: none | port(),
    refs  = #{}  :: #{integer() => job_id()},   %% asked of the helper
    next  = 1    :: pos_integer(),
    jobs  = #{}  :: #{job_id() => {#task{}, queue:queue(act())}}
}).

%%====================================================================
%% API
%%====================================================================

-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() -> gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Carry out actions, in this order, after those already handed over.
%% Each is answered with pluto_job_watch:acted/3.
-spec perform(job_id(), [act()]) -> ok.
perform(Id, Acts) -> gen_server:cast(?MODULE, {perform, Id, Acts}).

%% @doc The job is gone; drop what has not been done yet.
-spec forget(job_id()) -> ok.
forget(Id) -> gen_server:cast(?MODULE, {forget, Id}).

%%====================================================================
%% gen_server
%%====================================================================

init([]) ->
    process_flag(trap_exit, true),
    {ok, #state{}}.

handle_call(_, _, S) -> {reply, {error, <<"unknown_request">>}, S}.

handle_cast({perform, Id, Acts}, #state{jobs = Jobs} = S) ->
    case Jobs of
        #{Id := {Task, Q}} ->
            {noreply, S#state{jobs = Jobs#{Id := {Task, queue:join(Q, queue:from_list(Acts))}}}};
        _ ->
            {noreply, next(Id, queue:from_list(Acts), S)}
    end;
handle_cast({forget, Id}, #state{jobs = Jobs} = S) ->
    {noreply, S#state{jobs = maps:remove(Id, Jobs)}};
handle_cast(_, S) -> {noreply, S}.

handle_info({Port, {data, Bin}}, #state{port = Port, refs = Refs} = S) ->
    case pluto_protocol_json:decode(Bin) of
        {ok, #{<<"id">> := Ref} = M} ->
            case maps:take(Ref, Refs) of
                {Id, Left} -> {noreply, answered(Id, M, S#state{refs = Left})};
                error      -> {noreply, S}
            end;
        _ -> {noreply, lost_helper(S)}
    end;
handle_info({Port, {exit_status, _}}, #state{port = Port} = S) ->
    {noreply, lost_helper(S)};
handle_info({'EXIT', Port, _}, #state{port = Port} = S) ->
    {noreply, lost_helper(S)};
handle_info({retry, Id, N}, #state{jobs = Jobs} = S) ->
    case Jobs of
        #{Id := {#task{act = #{<<"a">> := N}} = T, Q}} ->
            {noreply, attempt(Id, T#task{timer = none}, Q, S)};
        _ -> {noreply, S}
    end;
handle_info({overdue, Id, N}, #state{jobs = Jobs} = S) ->
    case Jobs of
        #{Id := {#task{act = #{<<"a">> := N}}, _}} -> {noreply, lost_helper(S)};
        _ -> {noreply, S}
    end;
handle_info(_, S) -> {noreply, S}.

terminate(_, _) -> ok.

%%====================================================================
%% One job at a time
%%====================================================================

next(Id, Q, #state{jobs = Jobs} = S) ->
    case queue:out(Q) of
        {empty, _}        -> S#state{jobs = maps:remove(Id, Jobs)};
        {{value, Act}, R} -> attempt(Id, #task{act = Act}, R, S)
    end.

attempt(Id, #task{act = #{<<"kind">> := <<"append">>}} = T, Q, S) ->
    ask_helper(Id, T, Q, S);
attempt(Id, #task{act = #{<<"kind">> := <<"input">>, <<"text">> := Text}} = T, Q, S) ->
    case pluto_job_mgr:input({<<"rules">>, true},
                             #{<<"job_id">> => Id, <<"text">> => <<Text/binary, "\n">>}) of
        {ok, _}    -> settle(Id, T, ok, Q, S);
        {error, W} -> failed(Id, T, W, transient(W), Q, S)
    end;
attempt(Id, T, Q, S) ->
    settle(Id, T, {error, <<"unknown action">>}, Q, S).

%% A job that is only just starting, or has not read its input yet.
transient(<<"job_is_not_running">>)      -> true;
transient(<<"stdin_full">>)              -> true;
transient(<<"control_write_failed">>)    -> true;
transient(<<"smarttasks_", _/binary>>) -> true;
transient(_)                             -> false.

failed(Id, #task{tries = N, act = #{<<"a">> := A}} = T, _Why, true, Q,
       #state{jobs = Jobs} = S) when N < ?ACT_TRIES ->
    Timer = erlang:send_after(?ACT_RETRY_MS bsl N, self(), {retry, Id, A}),
    S#state{jobs = Jobs#{Id => {T#task{tries = N + 1, timer = Timer}, Q}}};
failed(Id, T, Why, _, Q, S) ->
    settle(Id, T, {error, Why}, Q, S).

settle(Id, #task{act = #{<<"a">> := A}}, Result, Q, S) ->
    pluto_job_watch:acted(Id, A, Result),
    next(Id, Q, S).

answered(Id, M, #state{jobs = Jobs} = S) ->
    case Jobs of
        #{Id := {#task{timer = Timer} = T, Q}} ->
            _ = Timer =:= none orelse erlang:cancel_timer(Timer),
            case opt(<<"error">>, M) of
                undefined -> settle(Id, T, ok, Q, S);
                Why       -> settle(Id, T, {error, Why}, Q, S)
            end;
        _ -> S                                %% forgotten meanwhile
    end.

%%====================================================================
%% The helper process
%%====================================================================

ask_helper(Id, #task{act = #{<<"a">> := A, <<"file">> := File, <<"text">> := Text}} = T,
           Q, S0) ->
    case {cwd(Id), helper(S0)} of
        {undefined, S} -> settle(Id, T, {error, <<"the job has no directory">>}, Q, S);
        {_, #state{port = none} = S} -> failed(Id, T, <<"no helper">>, true, Q, S);
        {Cwd, #state{port = Port, refs = Refs, next = Ref, jobs = Jobs} = S} ->
            catch port_command(Port, pluto_protocol_json:encode(
                #{<<"id">> => Ref, <<"root">> => Cwd, <<"file">> => File,
                  <<"text">> => Text})),
            Timer = erlang:send_after(?ACT_OVERDUE_MS, self(), {overdue, Id, A}),
            S#state{refs = Refs#{Ref => Id}, next = Ref + 1,
                    jobs = Jobs#{Id => {T#task{timer = Timer}, Q}}}
    end.

cwd(Id) ->
    case pluto_job_registry:pkg(Id) of
        {ok, P} -> opt(<<"cwd">>, P);
        _       -> undefined
    end.

helper(#state{port = none} = S) ->
    try open_port({spawn_executable, pluto_job_store:python()},
                  [{args, pluto_job_store:script(?JOB_ACT_SCRIPT)},
                   binary, {packet, 4}, exit_status, use_stdio, hide]) of
        Port -> S#state{port = Port}
    catch error:R ->
        ?LOG_ERROR("smarttasks: action helper: ~p", [R]),
        S
    end;
helper(S) -> S.

%% Whatever it was asked may or may not have happened: ask again, a few
%% times. It can only be stuck in the file system.
lost_helper(#state{port = Port, refs = Refs} = S) ->
    Port =:= none orelse pluto_job_store:kill_port(Port),
    maps:fold(fun(_, Id, #state{jobs = Jobs} = Acc) ->
        case Jobs of
            #{Id := {#task{timer = Timer} = T, Q}} ->
                _ = Timer =:= none orelse erlang:cancel_timer(Timer),
                failed(Id, T, <<"the file system did not answer">>, true, Q, Acc);
            _ -> Acc
        end
    end, S#state{port = none, refs = #{}}, Refs).
