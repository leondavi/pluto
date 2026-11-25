%%%-------------------------------------------------------------------
%%% @doc pluto_job_launcher — Choosing, starting and probing runners.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_launcher).

-include("pluto.hrl").
-include_lib("kernel/include/file.hrl").

-export([select/3, launch/3, probe/3, request/2, kill_orphan/1, abort/1, hb_age/1]).

%% @doc Queued jobs to start now, oldest first, within both limits.
%% Pure: takes and returns packages.
-spec select([pkg()], pos_integer(), pos_integer()) -> [pkg()].
select(Live, Max, PerAgent) ->
    Running = [maps:get(<<"owner">>, P) || #{<<"sched">> := S} = P <- Live,
                                           S =/= ?SCHED_QUEUED],
    Queued = lists:sort(
               fun(A, B) -> maps:get(<<"opened_ns">>, A, 0) =<
                            maps:get(<<"opened_ns">>, B, 0) end,
               [P || #{<<"sched">> := ?SCHED_QUEUED} = P <- Live]),
    pick(Queued, Running, Max, PerAgent).

pick([], _, _, _) -> [];
pick(_, Running, Max, _) when length(Running) >= Max -> [];
pick([#{<<"owner">> := O} = P | T], Running, Max, Per) ->
    case length([X || X <- Running, X =:= O]) < Per of
        true  -> [P | pick(T, [O | Running], Max, Per)];
        false -> pick(T, Running, Max, Per)
    end.

%% @doc Start a runner; sends {spawned, Id, ok | {error, Why} | unknown}.
%% A separate process does it because starting Python takes a while and
%% the manager must stay responsive.
-spec launch(pkg(), map(), pid()) -> ok.
launch(#{<<"id">> := Id} = P, Env, ReplyTo) ->
    Spec = (maps:with(?JOB_LAUNCH_KEYS, P))
           #{<<"env">> => Env,
             <<"min_free_bytes">> => pluto_config:get(smartstreams_min_free_bytes,
                                                      ?DEFAULT_JOB_MIN_FREE_BYTES)},
    Line = pluto_protocol_json:encode_line(
             maps:filter(fun(_, V) -> V =/= null end, Spec)),
    spawn(fun() -> ReplyTo ! {spawned, Id, run_runner(Id, Line)} end),
    ok.

run_runner(Id, Line) ->
    case pluto_job_store:run(?JOB_RUNNER_SCRIPT, ["run", pluto_job_store:dir(Id)],
                             Line, ?JOB_SPAWN_MAX_MS) of
        {ok, _, <<"ready">>}  -> ok;
        {ok, _, <<"error ", Why/binary>>} -> {error, Why};
        %% No verdict (crash, timeout): the runner may still come up, so
        %% the manager decides later from state.json.
        _ -> unknown
    end.

%% @doc Ask whether runner and command are alive; sends
%% {probed, Id, Runner, Child} with yes | no | unknown for each.
-spec probe(job_id(), jstate(), pid()) -> ok.
probe(Id, St, ReplyTo) ->
    spawn(fun() ->
        Runner = alive(<<"runner_pid">>, <<"runner_ident">>, St),
        Child = case Runner of
                    yes -> yes;
                    _   -> alive(<<"pid">>, <<"child_ident">>, St)
                end,
        ReplyTo ! {probed, Id, Runner, Child}
    end),
    ok.

alive(PidKey, IdentKey, St) ->
    case St of
        #{PidKey := Pid, IdentKey := Ident} when is_integer(Pid), Ident =/= <<>> ->
            case pluto_job_store:ident(Pid) of
                {ok, Ident}  -> yes;
                {ok, _Other} -> no;
                unknown      -> unknown
            end;
        _ -> no
    end.

%% @doc Queue a request for the runner, which reads files in name order.
-spec request(job_id(), map()) -> ok | {error, binary()}.
request(Id, Req) ->
    Dir = pluto_job_store:file(Id, ?JOB_DIR_CONTROL),
    %% The clock may stand still between two requests.
    Name = io_lib:format("~20..0w-~12..0w.json",
                         [erlang:system_time(nanosecond),
                          erlang:unique_integer([monotonic, positive])]),
    Pending = case file:list_dir(Dir) of
                  {ok, L} -> length(L);
                  _       -> 0
              end,
    IsInput = maps:get(<<"action">>, Req) =:= ?ACT_INPUT,
    case IsInput andalso Pending >= ?JOB_INPUT_PENDING of
        true -> {error, <<"stdin_full">>};
        false ->
            case pluto_job_store:write_json(filename:join(Dir, Name), Req) of
                ok -> ok;
                _  -> {error, <<"control_write_failed">>}
            end
    end.

%% @doc Kill the group of a command whose runner is gone. The helper
%% refuses unless pid and start time still match.
-spec kill_orphan(jstate()) -> ok | {error, binary()}.
kill_orphan(#{<<"pid">> := Pid, <<"child_ident">> := Ident}) ->
    case pluto_job_store:run(?JOB_RUNNER_SCRIPT,
                             ["kill", integer_to_binary(Pid), Ident, "9"]) of
        {ok, 0, <<"ok">>} -> ok;
        {ok, _, <<"gone">>} -> {error, <<"process_is_gone">>};
        _ -> {error, <<"kill_helper_failed">>}
    end;
kill_orphan(_) -> {error, <<"process_is_gone">>}.

%% @doc Tell a runner that has not shown up not to start the command.
-spec abort(job_id()) -> ok.
abort(Id) ->
    _ = file:write_file(pluto_job_store:file(Id, ?JOB_FILE_ABORT), <<>>),
    ok.

%% @doc Milliseconds since the runner's last heartbeat.
-spec hb_age(job_id()) -> integer().
hb_age(Id) ->
    case file:read_file_info(pluto_job_store:file(Id, ?JOB_FILE_HB), [{time, posix}]) of
        {ok, #file_info{mtime = T}} -> pluto_job_store:now_ms() - T * 1000;
        _ -> pluto_job_store:now_ms()
    end.
