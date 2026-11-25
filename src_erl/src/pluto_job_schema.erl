%%%-------------------------------------------------------------------
%%% @doc pluto_job_schema — Type checks for /jobs/* request bodies.
%%%
%%% Checking every known field up front means the operations can rely on
%%% types and only validate meaning. Fields nobody reads are ignored.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_schema).

-include("pluto.hrl").

-export([check/1, full_ids/1, agent/1, actor/1]).

%% @doc ok, or the name of the first badly typed field. null is absent.
-spec check(map()) -> ok | {error, binary()}.
check(R) ->
    case [K || {K, V} <- maps:to_list(R), V =/= null, not valid(K, V)] of
        []      -> ok;
        [K | _] -> {error, <<"bad_value_for_", (pluto_job_read:sanitize(K))/binary>>}
    end.

valid(<<"timeout_s">>, V) -> is_number(V) andalso V > 0 andalso V < 1.0e12;
%% A flag, or the fields to show.
valid(<<"rows">>, V) when is_boolean(V) -> true;
valid(<<"rows">>, V) -> is_list(V) andalso lists:all(fun str/1, V);
valid(<<"from_offsets">>, M) ->
    is_map(M) andalso lists:all(fun(N) -> is_integer(N) andalso N >= 0 end,
                                maps:values(M));
valid(K, V) ->
    Kinds = [{?JOB_STR_KEYS, fun str/1},
             {?JOB_INT_KEYS, fun(X) -> is_integer(X) andalso X >= 0 end},
             {?JOB_NUM_KEYS, fun(X) -> is_number(X) andalso X >= 0 andalso X < 1.0e12 end},
             {?JOB_BOOL_KEYS, fun is_boolean/1},
             {?JOB_STRS_KEYS, fun(X) -> is_list(X) andalso lists:all(fun str/1, X) end},
             {?JOB_ENV_KEYS, fun env/1}],
    case [F || {Keys, F} <- Kinds, lists:member(K, Keys)] of
        [F] -> F(V);
        []  -> true
    end.

%% NUL would truncate paths and arguments.
str(V) -> is_binary(V) andalso binary:match(V, <<0>>) =:= nomatch.

env(M) ->
    is_map(M) andalso lists:all(fun str/1, maps:values(M)) andalso
        lists:all(fun(K) -> str(K) andalso K =/= <<>> andalso
                            binary:match(K, <<"=">>) =:= nomatch end, maps:keys(M)).

%% @doc Replace short job ids by full ones wherever a request names jobs.
-spec full_ids(map()) -> map().
full_ids(R) ->
    Full = fun pluto_job_registry:resolve/1,
    maps:map(fun(K, V) when is_binary(V), K =:= <<"job_id">>;
                            is_binary(V), K =:= <<"parent">>;
                            is_binary(V), K =:= <<"rerun_of">> -> Full(V);
                (<<"job_ids">>, V) when is_list(V) -> lists:map(Full, V);
                (<<"from_offsets">>, V) when is_map(V) ->
                     maps:from_list([{Full(I), N} || {I, N} <- maps:to_list(V)]);
                (_, V) -> V
             end, R).

%% @doc The calling agent's id; throws {bad, Reason}.
-spec agent(map()) -> agent_id().
agent(R) ->
    case pluto_job_store:opt(<<"agent_id">>, R) of
        A when is_binary(A), A =/= <<>>, byte_size(A) =< ?JOB_AGENT_ID_MAX -> A;
        _ -> throw({bad, <<"missing_agent_id">>})
    end.

-spec actor(map()) -> actor().
actor(R) -> {agent(R), pluto_job_store:opt(<<"as_user">>, R, false)}.
