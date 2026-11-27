%%%-------------------------------------------------------------------
%%% @doc pluto_job_http — POST /jobs/* for PlutoSmartTasks.
%%%
%%% These routes run commands, so unlike the rest of the HTTP API they
%%% need the per-user key (Authorization: Bearer), accept loopback peers
%%% only unless configured otherwise, and never send CORS headers.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_http).

-include("pluto.hrl").

-export([handle/5]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).
-import(pluto_job_schema, [agent/1, actor/1]).

%% @doc Returns {HttpStatus, JsonMap}.
-spec handle(atom() | binary(), binary(), list(), binary(), gen_tcp:socket()) ->
    {pos_integer(), map()}.
handle('POST', <<"/jobs/", Op/binary>>, Headers, Body, Sock) ->
    case authorize(Headers, Sock) of
        ok ->
            case pluto_protocol_json:decode(Body) of
                {ok, R} when is_map(R) -> dispatch(Op, R);
                _ -> {400, err(<<"bad_json">>)}
            end;
        {error, Code, Why} -> {Code, err(Why)}
    end;
handle(_, _, _, _, _) -> {405, err(<<"method_not_allowed">>)}.

dispatch(Op, R) ->
    try
        case pluto_job_schema:check(R) of
            ok         -> result(op(Op, pluto_job_schema:full_ids(R)));
            {error, W} -> {400, err(W)}
        end
    catch
        throw:{bad, Why}       -> {400, err(Why)};
        throw:{job_error, Why} -> result({error, Why});
        error:{badkey, K} when is_binary(K) ->
            {400, err(<<"missing_field_", K/binary>>)};
        Class:E:St ->
            ?LOG_ERROR("smarttasks ~s failed: ~p ~p", [Op, {Class, E}, St]),
            {500, err(?ERR_INTERNAL_ERROR)}
    end.

result({ok, M})    -> {200, M#{<<"status">> => ?STATUS_OK}};
result({error, W}) -> B = to_bin(W), {code(B), err(B)}.

code(?ERR_UNKNOWN_JOB)      -> 404;
code(?ERR_UNKNOWN_STREAM)   -> 404;
code(?ERR_NOT_OWNER)        -> 403;
code(<<"internal_error">>)  -> 500;
code(<<"disk_error_", _/binary>>)   -> 503;
code(<<"smarttasks_", _/binary>>) -> 503;
code(_) -> 400.

err(W) -> #{<<"status">> => ?STATUS_ERROR, <<"reason">> => W}.

to_bin(B) when is_binary(B) -> B;
to_bin(A) when is_atom(A)   -> atom_to_binary(A);
to_bin(T) -> iolist_to_binary(io_lib:format("~p", [T])).

%%====================================================================
%% Authorization
%%====================================================================

authorize(Headers, Sock) ->
    case peer_allowed(Sock) of
        false -> {error, 403, <<"loopback_only">>};
        true ->
            %% Activation creates the key on first contact.
            case pluto_job_mgr:activate() of
                ok ->
                    case pluto_job_store:key() of
                        {ok, Key}  -> check_key(Headers, Key);
                        {error, W} -> {error, 503, to_bin(W)}
                    end;
                {error, W} -> {error, 503, to_bin(W)}
            end
    end.

peer_allowed(Sock) ->
    case pluto_config:get(smarttasks_allow_remote, false) of
        true -> true;
        _ ->
            case inet:peername(Sock) of
                {ok, {{127, _, _, _}, _}}           -> true;
                {ok, {{0, 0, 0, 0, 0, 0, 0, 1}, _}} -> true;
                _ -> false
            end
    end.

%% Hashing both sides first gives equal sizes for the constant-time compare.
check_key(Headers, Key) ->
    Given = case lists:keyfind(<<"Authorization">>, 1, Headers) of
                {_, <<"Bearer ", K/binary>>} -> string:trim(K);
                _ -> <<>>
            end,
    case crypto:hash_equals(crypto:hash(sha256, Given), crypto:hash(sha256, Key)) of
        true  -> ok;
        false -> {error, 401, ?ERR_BAD_KEY}
    end.

%%====================================================================
%% Operations
%%====================================================================

op(<<"start">>, R)    -> start(R);
op(<<"status">>, R)   -> {ok, pluto_job_view:card(job(R), agent(R))};
op(<<"list">>, R)     -> {ok, pluto_job_view:list(R)};
op(<<"open">>, R)     -> {ok, pluto_job_view:open(job(R), agent(R))};
op(<<"window">>, R)   -> window(R);
op(<<"next">>, R)     -> next(R);
op(<<"grep">>, R)     -> grep(R);
op(<<"analyze">>, R)  -> pluto_job_analyze:analyze(jobs(R), R);
op(<<"watch">>, R)    -> pluto_job_watch:request(jobs(R), actor(R), R);
op(<<"wait">>, R)     -> {ok, pluto_job_wait:wait(jobs(R), R)};
op(<<"control">>, R)  -> _ = job(R), pluto_job_mgr:control(actor(R), R);
op(<<"input">>, R)    -> _ = job(R), pluto_job_mgr:input(actor(R), R);
op(<<"transfer">>, R) -> transfer(R);
op(<<"stream">>, R)   -> stream(R);
op(<<"review">>, R)   -> review(R);
op(<<"manifest">>, R) -> manifest(R);
op(<<"export">>, R)   -> pluto_job_import:export(job(R), opt(<<"path">>, R));
op(<<"import">>, R)   -> import(R);
op(<<"notices">>, R) ->
    case pluto_job_mgr:notices(agent(R), opt(<<"ack">>, R, 0)) of
        {ok, Ns} -> {ok, #{<<"notices">> => Ns}};
        E        -> E
    end;
op(<<"history">>, R) ->
    {ok, #{<<"history">> => pluto_job_notice:read_history(
                              opt(<<"limit">>, R, ?JOB_HISTORY_DEFAULT))}};
op(<<"stats">>, _) -> {ok, pluto_job_view:stats()};
op(<<"prune">>, R) ->
    Before = now_ms() - opt(<<"older_than_ms">>, R, ?JOB_RETENTION_MS),
    case pluto_job_mgr:prune(Before, opt(<<"dry_run">>, R, true)) of
        {ok, Ids} -> {ok, #{<<"jobs">> => Ids}};
        E         -> E
    end;
op(_, _) -> {error, <<"no_such_operation">>}.

job(R) ->
    case pluto_job_registry:pkg(opt(<<"job_id">>, R, <<>>)) of
        {ok, P}    -> P;
        {error, W} -> throw({job_error, W})
    end.

jobs(R) ->
    case opt(<<"job_ids">>, R) of
        [_ | _] = Ids -> [job(#{<<"job_id">> => I}) || I <- Ids];
        _             -> [job(R)]
    end.

source(R) ->
    #{<<"id">> := Id} = P = job(R),
    St = pluto_job_registry:state(P),
    case pluto_job_streams:source(Id, St, opt(<<"stream">>, R)) of
        {ok, Src}  -> {Id, St, Src};
        {error, W} -> throw({job_error, W})
    end.

start(R) ->
    _ = agent(R),
    Earlier = case opt(<<"rerun_of">>, R) of
                  undefined -> none;
                  Id        -> job(#{<<"job_id">> => Id})
              end,
    pluto_job_start:start(R, Earlier).

transfer(R) ->
    _ = job(R),
    case opt(<<"to">>, R, <<>>) of
        To when To =/= <<>>, byte_size(To) =< ?JOB_AGENT_ID_MAX ->
            pluto_job_mgr:transfer(actor(R), R);
        _ -> {error, <<"missing_to">>}
    end.

%% The pattern search of a split may be slow, so it runs here and the
%% manager only records the resulting ranges.
stream(R) ->
    {_, _, Src} = source(R#{<<"stream">> => case opt(<<"action">>, R) of
                                                <<"split">> -> opt(<<"stream">>, R, null);
                                                _ -> null
                                            end}),
    case {opt(<<"action">>, R), opt(<<"pattern">>, R)} of
        {undefined, _} -> {error, <<"bad_action">>};
        {<<"split">>, P} when P =/= undefined ->
            case pluto_job_streams:cut_at(Src, P) of
                {ok, Cut} -> pluto_job_mgr:stream(actor(R), R#{ranges => Cut});
                E         -> E
            end;
        _ -> pluto_job_mgr:stream(actor(R), R)
    end.

review(R) ->
    case opt(<<"action">>, R, <<"add">>) of
        <<"list">> ->
            #{<<"id">> := Id} = job(R),
            {ok, #{<<"reviews">> => pluto_job_review:list(Id)}};
        _ ->
            {Id, _, Src} = source(R#{<<"stream">> => null}),
            case pluto_job_review:build(agent(R), Src, R) of
                {ok, E} -> pluto_job_mgr:review(
                             Id, E#{<<"request_id">> => opt(<<"request_id">>, R, null)});
                Err -> Err
            end
    end.

manifest(R) ->
    {_, St, #{path := Path, man := Man}} = source(R#{<<"stream">> => null}),
    From = opt(<<"from">>, R, 0),
    {N, Recs} = pluto_job_read:records(
                  Man, From, min(opt(<<"limit">>, R, ?JOB_MANIFEST_DEFAULT),
                                 ?JOB_MANIFEST_MAX)),
    Base = #{<<"chunks">> => N, <<"chunk_bytes">> => ?JOB_CHUNK_BYTES,
             <<"from">> => From, <<"records">> => Recs,
             <<"sha256">> => opt(<<"sha256">>, St, null)},
    {ok, case opt(<<"verify">>, R, false) of
             true ->
                 Bad = pluto_job_read:verify(Path, Recs),
                 Base#{<<"verified">> => Bad =:= [], <<"bad_chunks">> => Bad};
             false -> Base
         end}.

import(R) ->
    Agent = agent(R),
    User = opt(<<"user">>, R, <<"unknown">>),
    pluto_job_import:unpack(maps:get(<<"path">>, R), fun(Staging) ->
        case pluto_job_mgr:adopt(Staging, Agent, User) of
            {ok, Id, What} ->
                Card = pluto_job_view:card(job(#{<<"job_id">> => Id}), Agent),
                {ok, Card#{<<"result">> => What}};
            E -> E
        end
    end).

%%====================================================================
%% Reading
%%====================================================================

max_bytes(R) -> max(1, min(opt(<<"max_bytes">>, R, ?JOB_READ_DEFAULT), ?JOB_READ_MAX)).

read_opts(R) ->
    #{cut => true, lines => opt(<<"lines">>, R, infinity),
      encoding => opt(<<"encoding">>, R, <<"utf8">>),
      strip_ansi => opt(<<"strip_ansi">>, R, false)}.

delivered(#{<<"start">> := A, <<"end">> := B} = M, St) ->
    pluto_stats:inc(job_bytes_delivered, B - A),
    {ok, M#{<<"state">> => maps:get(<<"state">>, St)}}.

window(R) ->
    {Id, St, Src} = source(R),
    Max = max_bytes(R),
    At = window_start(Id, Src, R, Max),
    delivered(pluto_job_read:read(Src, At, Max, read_opts(R)), St).

%% The first of offset, line, tail, cursor that is given; else the end.
window_start(Id, #{lo := Lo} = Src, R, Max) ->
    Before = opt(<<"before">>, R, 0),
    case [{K, V} || K <- [<<"offset">>, <<"line">>, <<"tail">>], V <- [opt(K, R)],
                    V =/= undefined] of
        [{<<"offset">>, O} | _] -> max(Lo, O - Before);
        [{<<"line">>, L} | _] -> pluto_job_read:offset_of_line(Src, max(1, L - Before));
        [{<<"tail">>, T} | _] -> pluto_job_read:tail_start(Src, max(1, T), Max);
        [] ->
            case opt(<<"cursor">>, R, false) of
                true ->
                    Key = pluto_job_cursor:key(agent(R), opt(<<"stream">>, R)),
                    pluto_job_cursor:position(Id, Key);
                false -> pluto_job_read:tail_start(Src, Max, Max)
            end
    end.

next(R) ->
    {Id, St, #{lo := Lo, hi := Hi} = Src} = source(R),
    Stream = opt(<<"stream">>, R),
    Key = pluto_job_cursor:key(agent(R), Stream),
    case is_paused(Id, Stream) of
        true -> {ok, #{<<"paused">> => true, <<"text">> => <<>>}};
        false ->
            Cur = pluto_job_cursor:get(Id, Key),
            case pluto_job_cursor:plan(Cur, R, Lo) of
                {replay, A, B} ->
                    delivered(pluto_job_read:read(Src#{hi := min(B, Hi), closed := true},
                                                  A, B - A, read_opts(R)), St);
                {read, From} ->
                    Out = pluto_job_read:read(Src, From, max_bytes(R), read_opts(R)),
                    case pluto_job_mgr:commit(Id, Key, Cur, R, Out) of
                        ok -> delivered(Out#{<<"unread">> =>
                                                 Hi - maps:get(<<"end">>, Out)}, St);
                        conflict -> next(R);     %% same reader, concurrent call
                        E        -> E
                    end
            end
    end.

is_paused(_, undefined) -> false;
is_paused(Id, Stream) ->
    case pluto_job_streams:find(Id, Stream) of
        {ok, #{<<"state">> := ?STREAM_PAUSED}} -> true;
        _ -> false
    end.

grep(R) ->
    {_, St, #{lo := Lo, hi := Hi} = Src} = source(R),
    Pattern = opt(<<"pattern">>, R),
    Pattern =/= undefined orelse throw({bad, <<"missing_pattern">>}),
    From = case opt(<<"from_line">>, R) of
               undefined -> opt(<<"from_offset">>, R, Lo);
               L1 -> pluto_job_read:offset_of_line(Src, L1)
           end,
    To = case opt(<<"to_line">>, R) of
             undefined -> min(Hi, opt(<<"to_offset">>, R, Hi));
             L2 -> pluto_job_read:offset_of_line(Src, L2 + 1)
         end,
    Opts = #{from => max(Lo, min(From, Hi)), to => To,
             before => min(?GREP_MAX_CONTEXT, opt(<<"before">>, R, 0)),
             'after' => min(?GREP_MAX_CONTEXT, opt(<<"after">>, R, 0)),
             max => max(1, min(?GREP_MAX_HITS,
                               opt(<<"max_matches">>, R, ?GREP_DEFAULT_HITS))),
             fixed => opt(<<"fixed">>, R, false),
             ignore_case => opt(<<"ignore_case">>, R, false),
             invert => opt(<<"invert">>, R, false)},
    case pluto_job_grep:grep(Src, Pattern, Opts) of
        {ok, M} ->
            pluto_stats:inc(job_greps),
            {ok, M#{<<"size">> => Hi, <<"state">> => maps:get(<<"state">>, St)}};
        E -> E
    end.
