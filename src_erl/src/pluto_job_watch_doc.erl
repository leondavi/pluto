%%%-------------------------------------------------------------------
%%% @doc pluto_job_watch_doc — The contents of watch.json, without effects.
%%%
%%%   rules, states   what is looked for, and where each rule has got to
%%%   hits, h         what was or will be told, numbered by `h'
%%%   acts, a         actions not carried out yet, numbered by `a'
%%%   final           the job is over and all its output was looked at
%%%   inert           came with an imported package; never evaluated
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_watch_doc).

-include("pluto.hrl").

-export([empty/0, rule_id/1, rule/4, add/3, done/4, describe/1]).

-import(pluto_job_store, [opt/3, now_ms/0]).

-type doc() :: #{binary() => term()}.

-spec empty() -> doc().
empty() ->
    #{<<"schema">> => ?JOB_SCHEMA, <<"inert">> => false, <<"seq">> => 0, <<"h">> => 0,
      <<"rules">> => [], <<"states">> => #{}, <<"hits">> => [], <<"a">> => 0,
      <<"acts">> => [], <<"final">> => false}.

-spec rule_id(pos_integer()) -> binary().
rule_id(N) -> <<"R", (integer_to_binary(N))/binary>>.

-spec rule(binary(), map(), agent_id(), binary() | null) -> map().
rule(Rid, Spec, By, Request) ->
    #{<<"id">> => Rid, <<"spec">> => Spec, <<"by">> => By, <<"at">> => now_ms(),
      <<"request_id">> => Request}.

%% @doc Take in hits: those of acting rules become actions, the others are
%% kept to be told. Told: the last hit that has been.
-spec add([map()], non_neg_integer(), doc()) -> doc().
add(New, Told, D) ->
    {Acting, Telling} = lists:partition(fun(X) -> is_map_key(<<"act">>, X) end, New),
    %% The last hit of an acting rule is told as well: from here on lines
    %% go unanswered.
    Last = [maps:remove(<<"act">>, X#{<<"then">> := <<"notify">>})
            || #{<<"last">> := true} = X <- Acting],
    add_hits(Telling ++ Last, Told, add_acts(Acting, D)).

add_acts(New, #{<<"a">> := A0, <<"acts">> := Old} = D) ->
    {Numbered, A1} = lists:mapfoldl(fun(#{<<"act">> := Act, <<"rule">> := Rule,
                                          <<"name">> := Name, <<"line">> := Line}, N) ->
        {Act#{<<"a">> => N + 1, <<"rule">> => Rule, <<"name">> => Name,
              <<"line">> => Line}, N + 1} end, A0, New),
    D#{<<"a">> := A1, <<"acts">> := Old ++ Numbered}.

%% Keeps the newest, and whatever nobody was told about yet.
add_hits(New, Told, #{<<"h">> := H0, <<"hits">> := Old} = D) ->
    At = now_ms(),
    {Numbered, H1} = lists:mapfoldl(fun(X, N) ->
        {X#{<<"h">> => N + 1, <<"at">> => At}, N + 1} end, H0, New),
    Low = min(Told, H1 - ?WATCH_HITS_KEEP),
    D#{<<"h">> := H1, <<"hits">> := [X || #{<<"h">> := N} = X <- Old ++ Numbered, N > Low]}.

%% @doc An action is over. Nobody is told unless it failed.
-spec done(pos_integer(), ok | {error, binary()}, non_neg_integer(), doc()) ->
    doc() | unknown.
done(A, Result, Told, #{<<"acts">> := Acts} = D) ->
    case lists:partition(fun(#{<<"a">> := N}) -> N =:= A end, Acts) of
        {[], _}       -> unknown;
        {[Act], Left} ->
            add_hits([failure(Act, Why) || {error, Why} <- [Result]], Told,
                     D#{<<"acts">> := Left})
    end.

failure(#{<<"rule">> := Rule, <<"name">> := Name, <<"line">> := Line}, Why) ->
    #{<<"rule">> => Rule, <<"name">> => Name, <<"line">> => Line,
      <<"then">> => <<"notify">>, <<"text">> => <<"action failed: ", Why/binary>>}.

%% @doc The rules, for those who ask.
-spec describe(doc()) -> [map()].
describe(D) ->
    States = maps:get(<<"states">>, D, #{}),
    Acts = maps:get(<<"acts">>, D, []),
    [begin
         St = maps:get(Rid, States, #{}),
         maps:merge(
           #{<<"id">> => Rid, <<"spec">> => Spec,
             <<"active">> => maps:get(<<"final">>, D, true) =:= false andalso
                 not opt(<<"done">>, St, false),
             <<"hits">> => opt(<<"n">>, St, 0),
             <<"fired">> => opt(<<"total">>, St, 0),
             <<"waiting">> => length([A || #{<<"rule">> := R} = A <- Acts, R =:= Rid]),
             <<"inert">> => maps:get(<<"inert">>, D, true)},
           maps:with([<<"why">>], St))
     end || #{<<"id">> := Rid, <<"spec">> := Spec} <- maps:get(<<"rules">>, D, [])].
