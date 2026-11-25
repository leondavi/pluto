%%%-------------------------------------------------------------------
%%% @doc pluto_job_review — Verdicts pinned to hashed ranges of output.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_review).

-include("pluto.hrl").

-export([build/3, append/2, list/1]).

-import(pluto_job_store, [opt/3]).

%% @doc Validate and hash; runs in the caller since hashing may be long.
-spec build(agent_id(), src(), map()) -> reply().
build(By, #{lo := Lo, hi := Hi} = Src, R) ->
    A = opt(<<"from_offset">>, R, Lo),
    B = opt(<<"to_offset">>, R, Hi),
    case is_integer(A) andalso is_integer(B) andalso A >= Lo andalso B >= A
         andalso B =< Hi of
        false -> {error, <<"bad_range">>};
        true ->
            {ok, #{<<"id">> => pluto_job_store:hex(crypto:strong_rand_bytes(4)),
                   <<"by">> => By, <<"at">> => pluto_job_store:now_ms(),
                   <<"verdict">> => opt(<<"verdict">>, R, <<"note">>),
                   <<"note">> => opt(<<"note">>, R, <<>>),
                   <<"from_offset">> => A, <<"to_offset">> => B,
                   <<"sha256">> => range_hash(Src, A, B)}}
    end.

%% @doc Manager only.
-spec append(job_id(), map()) -> reply().
append(Id, Review) ->
    case pluto_job_store:append_jsonl(path(Id), Review) of
        ok -> {ok, Review};
        {error, R} -> throw({io, R})
    end.

-spec list(job_id()) -> [map()].
list(Id) -> pluto_job_store:read_jsonl(path(Id)).

path(Id) -> pluto_job_store:file(Id, ?JOB_FILE_REVIEWS).

%% A range too large to hash here is already pinned by its manifest.
range_hash(_, A, B) when B - A > ?REVIEW_HASH_MAX -> null;
range_hash(Src, A, B) ->
    pluto_job_store:hex(crypto:hash_final(hash(Src, A, B, crypto:hash_init(sha256)))).

hash(_, A, B, Ctx) when A >= B -> Ctx;
hash(Src, A, B, Ctx) ->
    case pluto_job_read:bytes(Src, A, min(?JOB_READ_BLOCK, B - A)) of
        <<>> -> Ctx;
        D    -> hash(Src, A + byte_size(D), B, crypto:hash_update(Ctx, D))
    end.
