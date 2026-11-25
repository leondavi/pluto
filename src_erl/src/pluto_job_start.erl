%%%-------------------------------------------------------------------
%%% @doc pluto_job_start — What a start request asks for, made definite.
%%%
%%% One request starts one job, or a batch: a list of {name, command,
%%% params} that shares everything else. Watch rules come with the request
%%% and are checked once, before any job exists, so a mistake in a rule
%%% starts nothing.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_start).

-include("pluto.hrl").

-export([start/2]).

-import(pluto_job_store, [opt/2, opt/3]).

-define(ITEM_KEYS, [<<"name">>, <<"command">>, <<"params">>]).

%% @doc Start the job or jobs; Earlier is the package named by rerun_of.
-spec start(map(), pkg() | none) -> reply().
start(R0, Earlier) ->
    Given = rerun(R0, Earlier),
    R = Given#{<<"watch">> => rules(Given)},
    case opt(<<"batch">>, R) of
        undefined -> one(R);
        Batch     -> batch(Batch, maps:remove(<<"batch">>, R))
    end.

one(R) ->
    case launch(R) of
        {ok, P}        -> {ok, pluto_job_view:card(P, pluto_job_schema:agent(R))};
        {error, _} = E -> E
    end.

%% Every job is answered for: one that cannot be started does not hide
%% those that were.
batch([], _) -> {error, <<"empty_batch">>};
batch(Items, _) when length(Items) > ?JOB_BATCH_MAX -> {error, <<"batch_too_large">>};
batch(Items, R) ->
    Numbered = lists:zip(lists:seq(1, length(Items)), Items),
    Results = [item(N, I, R) || {N, I} <- Numbered],
    case [W || #{<<"error">> := W} <- Results] of
        [W | _] = Failed when length(Failed) =:= length(Results) -> {error, W};
        _ -> {ok, #{<<"jobs">> => Results}}
    end.

item(N, Item, R) ->
    Own = maps:with(?ITEM_KEYS, Item),
    %% A retried batch finds each of its jobs again.
    Rid = case opt(<<"request_id">>, R) of
              undefined -> #{};
              Q -> #{<<"request_id">> => <<Q/binary, "/", (integer_to_binary(N))/binary>>}
          end,
    Name = opt(<<"name">>, Item, null),
    case launch(maps:merge(maps:merge(R, Own), Rid)) of
        {ok, #{<<"id">> := Id}} -> #{<<"job_id">> => Id, <<"name">> => Name};
        {error, W}              -> #{<<"error">> => W, <<"name">> => Name}
    end.

launch(R) ->
    Given = [K || K <- [<<"command">>, <<"argv">>], V <- [opt(K, R)],
                  V =/= undefined, V =/= <<>>, V =/= []],
    Cwd = opt(<<"cwd">>, R),
    case {Given, Cwd =:= undefined orelse filelib:is_dir(Cwd)} of
        {[_], true} ->
            case pluto_job_mgr:start(R) of
                {ok, _} = Ok -> pluto_stats:inc(jobs_started), Ok;
                E            -> E
            end;
        {[_], false} -> {error, <<"cwd_is_not_a_directory">>};
        _            -> {error, <<"give_exactly_one_of_command_or_argv">>}
    end.

%% The earlier job's spec and rules, overridden by what this request gives.
rerun(R, none) -> R;
rerun(R, #{<<"id">> := Id} = Earlier) ->
    Own = maps:filter(fun(_, V) -> V =/= null end, R),
    Drop = case Own of
               #{<<"command">> := _} -> [<<"argv">>];
               #{<<"argv">> := _}    -> [<<"command">>];
               _ -> []
           end,
    Spec = maps:without(Drop, maps:with(?JOB_RERUN_KEYS, Earlier)),
    maps:merge(Spec#{<<"watch">> => pluto_job_watch:specs(Id)}, Own).

rules(R) ->
    Rules = [maps:filter(fun(_, V) -> V =/= null end, maps:with(?WATCH_KEYS, W))
             || W <- opt(<<"watch">>, R, [])],
    length(Rules) =< ?WATCH_RULES_MAX orelse throw({bad, <<"too_many_rules_on_this_job">>}),
    case pluto_job_analyze:check_rules(Rules, params(R)) of
        {ok, Checked}  -> Checked;
        {error, Why}   -> throw({job_error, Why})
    end.

%% Names every job of the request gives a value to.
params(R) ->
    Own = maps:keys(opt(<<"params">>, R, #{})),
    Items = opt(<<"batch">>, R, []),
    Common = lists:foldl(fun(I, Acc) ->
        Keys = maps:keys(opt(<<"params">>, I, #{})),
        case Acc of
            none -> Keys;
            _    -> [K || K <- Acc, lists:member(K, Keys)]
        end
    end, none, Items),
    case Common of
        none -> Own;
        _    -> lists:usort(Own ++ Common)
    end.
