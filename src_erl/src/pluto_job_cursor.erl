%%%-------------------------------------------------------------------
%%% @doc pluto_job_cursor — Durable per-reader positions.
%%%
%%% cursors.json maps a reader ("agent" or "agent|S1" for a nested
%%% stream) to its position. The read itself runs in the caller; the
%%% manager only commits, and refuses when another call of the same
%%% reader moved the cursor meanwhile. A repeated request_id replays the
%%% previous range, so an HTTP retry can never skip data.
%%%
%%% Cursors are written without fsync: after a power loss a reader may
%%% see some output again, never less.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_cursor).

-include("pluto.hrl").

-export([key/2, load/1, get/2, position/2, plan/3, commit/5, inherit/3]).

-import(pluto_job_store, [opt/2, opt/3]).

-spec key(agent_id(), binary() | undefined) -> binary().
key(Agent, undefined) -> Agent;
key(Agent, Stream)    -> <<Agent/binary, "|", Stream/binary>>.

-spec load(job_id()) -> map().
load(Id) ->
    case pluto_job_store:read_json(pluto_job_store:file(Id, ?JOB_FILE_CURSORS)) of
        {ok, M} when is_map(M) -> M;
        _ -> #{}
    end.

-spec get(job_id(), binary()) -> map().
get(Id, Key) ->
    case maps:get(Key, load(Id), #{}) of
        C when is_map(C) -> C;
        _ -> #{}
    end.

-spec position(job_id(), binary()) -> non_neg_integer().
position(Id, Key) -> pos(get(Id, Key), 0).

pos(C, Default) ->
    case opt(<<"pos">>, C, Default) of
        N when is_integer(N), N >= 0 -> N;
        _ -> Default
    end.

%% @doc What a `next' request should read: an earlier range again, or
%% from a position.
-spec plan(map(), map(), non_neg_integer()) ->
    {replay, non_neg_integer(), non_neg_integer()} | {read, non_neg_integer()}.
plan(C, R, Lo) ->
    Rid = opt(<<"request_id">>, R),
    case C of
        #{<<"last_req">> := Rid, <<"last_start">> := A, <<"last_end">> := B}
          when Rid =/= undefined, is_integer(A), is_integer(B), A =< B ->
            {replay, A, B};
        _ -> {read, opt(<<"from_offset">>, R, pos(C, Lo))}
    end.

%% @doc Manager only. Old is the cursor the read was planned from.
-spec commit(job_id(), binary(), map(), map(), map()) -> ok | conflict.
commit(Id, Key, Old, R, #{<<"start">> := A, <<"end">> := B}) ->
    All = load(Id),
    case maps:get(Key, All, #{}) of
        Old ->
            save(Id, All#{Key => #{
                <<"pos">> => B, <<"last_req">> => opt(<<"request_id">>, R, null),
                <<"last_start">> => A, <<"last_end">> => B,
                <<"at">> => pluto_job_store:now_ms(),
                <<"delivered">> => opt(<<"delivered">>, Old, 0) + B - A}});
        _ -> conflict
    end.

%% @doc Manager only. A new owner continues where the old one stopped,
%% unless it already reads this job. The old request id is dropped: the
%% new owner's ids must never match it.
-spec inherit(job_id(), agent_id(), agent_id()) -> ok.
inherit(Id, From, To) ->
    case load(Id) of
        #{From := C} = All when not is_map_key(To, All) ->
            save(Id, All#{To => maps:without(
                [<<"last_req">>, <<"last_start">>, <<"last_end">>], C)});
        _ -> ok
    end.

save(Id, M) ->
    case pluto_job_store:write_json(pluto_job_store:file(Id, ?JOB_FILE_CURSORS), M) of
        ok -> ok;
        {error, R} -> throw({io, R})
    end.
