%%%-------------------------------------------------------------------
%%% @doc pluto_job_http — POST /jobs/* for PlutoSmartStreams.
%%%
%%% These routes run commands, so unlike the rest of the HTTP API they
%%% need the per-user key (Authorization: Bearer), accept loopback peers
%%% only unless configured otherwise, and never send CORS headers.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_http).

-include("pluto.hrl").

-export([handle/5, describe/3]).

-import(pluto_job_store, [opt/2, opt/3, now_ms/0]).

-define(MAX_WINDOW, 32768).
-define(WAIT_STEP_MS, 200).
-define(MAX_WAIT_MS, 55000).

%% @doc Returns {Status, JsonMap}.
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
        check_types(R),
        case op(Op, R) of
            {ok, M}                        -> {200, M#{<<"status">> => <<"ok">>}};
            {error, <<"bad_job_id">> = W}  -> {400, err(W)};
            {error, <<"unknown_", _/binary>> = W} -> {404, err(W)};
            {error, <<"not_owner">> = W}   -> {403, err(W)};
            {error, W}                     -> {400, err(to_bin(W))}
        end
    catch
        throw:{bad, Why}  -> {400, err(Why)};
        error:{badkey, K} -> {400, err(<<"missing field ", (to_bin(K))/binary>>)};
        Class:E:St ->
            ?LOG_ERROR("smartstreams ~s failed: ~p ~p", [Op, {Class, E}, St]),
            {500, err(<<"internal_error">>)}
    end.

err(W) -> #{<<"status">> => <<"error">>, <<"reason">> => W}.

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
    case pluto_config:get(smartstreams_allow_remote, false) of
        true -> true;
        _ ->
            case inet:peername(Sock) of
                {ok, {{127, _, _, _}, _}}          -> true;
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
    case crypto:hash_equals(crypto:hash(sha256, Given),
                            crypto:hash(sha256, Key)) of
        true  -> ok;
        false -> {error, 401, <<"bad_key">>}
    end.

%%====================================================================
%% Validation
%%====================================================================

check_types(R) ->
    maps:foreach(fun(K, V) ->
        case V =:= null orelse valid(K, V) of
            true  -> ok;
            false -> throw({bad, <<"bad value for ", (pluto_job_read:sanitize(K))/binary>>})
        end
    end, R).

valid(K, V) when K =:= <<"job_id">>; K =:= <<"agent_id">>; K =:= <<"command">>;
                 K =:= <<"cwd">>; K =:= <<"name">>; K =:= <<"to">>;
                 K =:= <<"text">>; K =:= <<"pattern">>; K =:= <<"user">>;
                 K =:= <<"request_id">>; K =:= <<"stream">>; K =:= <<"action">>;
                 K =:= <<"encoding">>; K =:= <<"verdict">>; K =:= <<"note">>;
                 K =:= <<"parent">>; K =:= <<"rerun_of">>; K =:= <<"path">>;
                 K =:= <<"task_id">>; K =:= <<"signal">>; K =:= <<"scope">> ->
    is_binary(V) andalso binary:match(V, <<0>>) =:= nomatch;
valid(K, V) when K =:= <<"max_bytes">>; K =:= <<"from_offset">>;
                 K =:= <<"to_offset">>; K =:= <<"offset">>; K =:= <<"line">>;
                 K =:= <<"lines">>; K =:= <<"tail">>; K =:= <<"before">>;
                 K =:= <<"after">>; K =:= <<"limit">>; K =:= <<"from">>;
                 K =:= <<"ack">>; K =:= <<"from_line">>; K =:= <<"to_line">>;
                 K =:= <<"max_matches">>; K =:= <<"timeout_ms">>;
                 K =:= <<"max_output_bytes">>; K =:= <<"older_than_ms">>;
                 K =:= <<"idle_ms">> ->
    is_integer(V) andalso V >= 0;
valid(K, V) when K =:= <<"stdin">>; K =:= <<"tree">>; K =:= <<"verify">>;
                 K =:= <<"strip_ansi">>; K =:= <<"fixed">>; K =:= <<"invert">>;
                 K =:= <<"ignore_case">>; K =:= <<"close">>; K =:= <<"all">>;
                 K =:= <<"as_user">>; K =:= <<"dry_run">>; K =:= <<"cursor">> ->
    is_boolean(V);
valid(<<"timeout_s">>, V) -> is_number(V) andalso V > 0;
valid(K, V) when K =:= <<"argv">>; K =:= <<"tags">>; K =:= <<"job_ids">>;
                 K =:= <<"states">> ->
    is_list(V) andalso lists:all(fun is_binary/1, V);
valid(K, V) when K =:= <<"env">>; K =:= <<"base_env">> ->
    is_map(V) andalso lists:all(fun is_binary/1, maps:values(V));
valid(_, _) -> true.

agent(R) ->
    case opt(<<"agent_id">>, R) of
        A when is_binary(A), A =/= <<>>, byte_size(A) =< 200 -> A;
        _ -> throw({bad, <<"missing agent_id">>})
    end.

actor(R) -> {agent(R), opt(<<"as_user">>, R, false) =:= true}.

job(R) ->
    case pluto_job_mgr:pkg(opt(<<"job_id">>, R, <<>>)) of
        {ok, P}    -> P;
        {error, W} -> throw({job_error, W})
    end.

source(R) ->
    P = job(R),
    St = pluto_job_mgr:state(P),
    case pluto_job_streams:source(maps:get(<<"id">>, P), St, opt(<<"stream">>, R)) of
        {ok, Src}  -> {P, St, Src};
        {error, W} -> throw({job_error, W})
    end.

%%====================================================================
%% Operations
%%====================================================================

op(Op, R) ->
    try op1(Op, R)
    catch throw:{job_error, W} -> {error, W}
    end.

op1(<<"start">>, R0) ->
    _ = agent(R0),
    R = rerun(R0),
    Ok = case {opt(<<"command">>, R), opt(<<"argv">>, R)} of
             {C, undefined} when is_binary(C), C =/= <<>> -> true;
             {undefined, [_ | _]} -> true;
             _ -> false
         end,
    Cwd = opt(<<"cwd">>, R),
    if
        not Ok -> {error, <<"give exactly one of command or argv">>};
        true ->
            case Cwd =:= undefined orelse filelib:is_dir(Cwd) of
                false -> {error, <<"cwd is not a directory">>};
                true ->
                    case pluto_job_mgr:call({start, R}) of
                        {ok, P} ->
                            pluto_stats:inc(jobs_started),
                            {ok, describe(P, pluto_job_mgr:state(P), agent(R))};
                        E -> E
                    end
            end
    end;
op1(<<"status">>, R) ->
    P = job(R),
    {ok, describe(P, pluto_job_mgr:state(P), agent(R))};
op1(<<"list">>, R) ->
    Agent = agent(R),
    Scope = opt(<<"scope">>, R, <<"mine">>),
    States = opt(<<"states">>, R, []),
    Tags = opt(<<"tags">>, R, []),
    Parent = opt(<<"parent">>, R),
    User = opt(<<"user">>, R),
    Sel = [P || P <- pluto_job_mgr:all(),
                case Scope of
                    <<"mine">> -> maps:get(<<"owner">>, P) =:= Agent orelse
                                  maps:get(<<"opener">>, P) =:= Agent;
                    <<"user">> -> maps:get(<<"user">>, P, null) =:= User;
                    _          -> true
                end,
                Parent =:= undefined orelse opt(<<"parent">>, P) =:= Parent,
                Tags -- opt(<<"tags">>, P, []) =:= []],
    Sorted = lists:sort(fun(A, B) -> maps:get(<<"opened_at">>, A) >=
                                     maps:get(<<"opened_at">>, B) end, Sel),
    Rows = [D || P <- Sorted,
                 D <- [describe(P, pluto_job_mgr:state(P), Agent)],
                 States =:= [] orelse lists:member(maps:get(<<"state">>, D), States)],
    {ok, #{<<"total">> => length(Rows),
           <<"jobs">> => lists:sublist(Rows, opt(<<"limit">>, R, 50))}};
op1(<<"open">>, R) ->
    {P, St, Src} = source(R#{<<"stream">> => null}),
    #{<<"id">> := Id} = P,
    Streams = pluto_job_streams:describe(Id, St),
    #{hi := Hi} = Src,
    Head = pluto_job_read:read(Src, 0, 1024, #{cut => true}),
    Tail = pluto_job_read:read(Src, pluto_job_read:tail_start(Src, 20, 2048), 2048, #{}),
    Extra = maps:with([<<"command">>, <<"argv">>, <<"cwd">>, <<"origin">>,
                       <<"ownership">>, <<"imported">>, <<"env_keys">>,
                       <<"timeout_s">>], P),
    {ok, maps:merge(describe(P, St, agent(R)),
                    Extra#{<<"head">> => Head,
                           <<"tail">> => case Hi > 1024 of
                                             true -> Tail;
                                             false -> null
                                         end,
                           <<"nested">> => Streams,
                           <<"children">> =>
                               [maps:get(<<"id">>, C) || C <- pluto_job_mgr:all(),
                                opt(<<"parent">>, C) =:= Id],
                           <<"reviews">> => pluto_job_store:read_jsonl(
                               pluto_job_store:file(Id, "reviews.jsonl"))})};
op1(<<"window">>, R) -> window(R);
op1(<<"next">>, R) ->
    R1 = R#{<<"max_bytes">> => max_bytes(R)},
    case pluto_job_mgr:call({next, actor(R), R1}) of
        {ok, M} ->
            count_bytes(M),
            {ok, M#{<<"state">> => maps:get(<<"state">>, pluto_job_mgr:state(job(R)))}};
        E -> E
    end;
op1(<<"grep">>, R) -> grep(R);
op1(<<"wait">>, R) -> wait(R);
op1(<<"control">>, R)  -> pluto_job_mgr:call({control, actor(R), with_job(R)});
op1(<<"input">>, R)    -> pluto_job_mgr:call({input, actor(R), with_job(R)});
op1(<<"transfer">>, R) ->
    case opt(<<"to">>, R, <<>>) of
        <<>> -> {error, <<"missing to">>};
        _    -> pluto_job_mgr:call({transfer, actor(R), with_job(R)})
    end;
op1(<<"stream">>, R) -> pluto_job_mgr:call({stream, actor(R), with_job(R)});
op1(<<"review">>, R) ->
    #{<<"id">> := Id} = job(R),
    case opt(<<"action">>, R, <<"add">>) of
        <<"list">> -> {ok, #{<<"reviews">> => pluto_job_store:read_jsonl(
                                pluto_job_store:file(Id, "reviews.jsonl"))}};
        _ -> pluto_job_mgr:call({review, actor(R), R})
    end;
op1(<<"manifest">>, R) ->
    {_, St, #{path := Path, man := Man}} = source(R#{<<"stream">> => null}),
    From = opt(<<"from">>, R, 0),
    Limit = min(opt(<<"limit">>, R, 50), 1000),
    {N, Recs} = pluto_job_read:records(Man, From, Limit),
    Base = #{<<"chunks">> => N, <<"chunk_bytes">> => 65536, <<"from">> => From,
             <<"records">> => Recs,
             <<"sha256">> => opt(<<"sha256">>, St, null)},
    {ok, case opt(<<"verify">>, R, false) of
             true ->
                 Bad = pluto_job_read:verify(Path, Recs),
                 Base#{<<"verified">> => Bad =:= [], <<"bad_chunks">> => Bad};
             _ -> Base
         end};
op1(<<"export">>, R) -> export(R);
op1(<<"import">>, R) -> import(R);
op1(<<"notices">>, R) ->
    {ok, Ns} = pluto_job_mgr:call({notices, agent(R), opt(<<"ack">>, R, 0)}),
    {ok, #{<<"notices">> => Ns}};
op1(<<"history">>, R) ->
    All = pluto_job_store:read_jsonl(
            filename:join(pluto_job_store:home(), "history.jsonl")),
    {ok, #{<<"history">> => lists:sublist(lists:reverse(All),
                                          opt(<<"limit">>, R, 200))}};
op1(<<"stats">>, _R) -> {ok, stats()};
op1(<<"prune">>, R) ->
    Before = now_ms() - opt(<<"older_than_ms">>, R, 30 * 86400000),
    {ok, Ids} = pluto_job_mgr:call({prune, Before, opt(<<"dry_run">>, R, true)}),
    {ok, #{<<"jobs">> => Ids}};
op1(_, _) -> {error, <<"unknown_operation">>}.

with_job(R) -> _ = job(R), R.

rerun(R) ->
    case opt(<<"rerun_of">>, R) of
        undefined -> R;
        Id ->
            P = job(#{<<"job_id">> => Id}),
            Spec = maps:with([<<"command">>, <<"argv">>, <<"cwd">>, <<"stdin">>,
                              <<"timeout_s">>, <<"max_output_bytes">>, <<"name">>], P),
            maps:merge(Spec, maps:filter(fun(_, V) -> V =/= null end, R))
    end.

max_bytes(R) -> max(1, min(opt(<<"max_bytes">>, R, 8192), ?MAX_WINDOW)).

count_bytes(#{<<"start">> := A, <<"end">> := B}) ->
    pluto_stats:inc(job_bytes_delivered, B - A);
count_bytes(_) -> ok.

%%====================================================================
%% Status card
%%====================================================================

describe(#{<<"id">> := Id} = P, St, Agent) ->
    #{hi := Size} = pluto_job_streams:main_source(Id, St),
    Now = now_ms(),
    State = maps:get(<<"state">>, St),
    Started = opt(<<"started_at">>, St, null),
    End = opt(<<"ended_at">>, St, Now),
    LastOut = opt(<<"last_output_at">>, St, null),
    Idle = pluto_config:get(smartstreams_idle_after_ms, 30000),
    Pos = case pluto_job_store:read_json(pluto_job_store:file(Id, "cursors.json")) of
              {ok, #{Agent := #{<<"pos">> := X}}} -> X;
              _ -> 0
          end,
    M = maps:with([<<"name">>, <<"user">>, <<"opener">>, <<"owner">>,
                   <<"opened_at">>, <<"tags">>, <<"task_id">>, <<"parent">>,
                   <<"rerun_of">>, <<"framework">>], P),
    maps:filter(fun(_, V) -> V =/= null end, M#{
        <<"job_id">> => Id,
        <<"host">> => maps:get(<<"hostname">>, opt(<<"origin">>, P, #{}), null),
        <<"state">> => State,
        <<"reason">> => opt(<<"reason">>, St, null),
        <<"activity">> =>
            if
                State =/= <<"running">>, State =/= <<"orphaned">> -> null;
                is_integer(LastOut), Now - LastOut =< Idle -> <<"active">>;
                true -> <<"idle">>
            end,
        <<"exit_code">> => opt(<<"exit_code">>, St, null),
        <<"signal">> => opt(<<"signal">>, St, null),
        <<"started_at">> => Started,
        <<"ended_at">> => opt(<<"ended_at">>, St, null),
        <<"elapsed_ms">> => case is_integer(Started) of
                                true  -> max(0, End - Started);
                                false -> 0
                            end,
        <<"queued_ms">> => case is_integer(Started) of
                               true  -> max(0, Started - maps:get(<<"opened_at">>, P));
                               false -> max(0, End - maps:get(<<"opened_at">>, P))
                           end,
        <<"paused_ms">> => opt(<<"paused_ms">>, St, 0),
        <<"size">> => Size, <<"cursor">> => Pos,
        <<"unread">> => max(0, Size - Pos),
        <<"sha256">> => opt(<<"sha256">>, St, null)}).

%%====================================================================
%% Window and grep
%%====================================================================

window(R) ->
    {P, St, #{lo := Lo, hi := Hi} = Src} = source(R),
    Max = max_bytes(R),
    Lines = opt(<<"lines">>, R, infinity),
    Before = opt(<<"before">>, R, 0),
    Agent = agent(R),
    Start = case {opt(<<"offset">>, R), opt(<<"line">>, R), opt(<<"tail">>, R)} of
                {O, _, _} when is_integer(O) -> max(Lo, O - Before);
                {_, L, _} when is_integer(L) ->
                    pluto_job_read:offset_of_line(Src, max(1, L - Before));
                {_, _, T} when is_integer(T) ->
                    pluto_job_read:tail_start(Src, max(1, T), Max);
                _ ->
                    case opt(<<"cursor">>, R, false) of
                        true -> maps:get(<<"cursor">>, describe(P, St, Agent));
                        _    -> pluto_job_read:tail_start(Src, 1 bsl 30, Max)
                    end
            end,
    M = pluto_job_read:read(Src, Start, Max,
            #{lines => Lines, cut => true,
              encoding => opt(<<"encoding">>, R, <<"utf8">>),
              strip_ansi => opt(<<"strip_ansi">>, R, false)}),
    count_bytes(M),
    _ = Hi,
    {ok, M#{<<"state">> => maps:get(<<"state">>, St)}}.

grep(R) ->
    {_, St, #{lo := Lo, hi := Hi} = Src} = source(R),
    Pattern = case opt(<<"pattern">>, R) of
                  undefined -> throw({bad, <<"missing pattern">>});
                  P -> P
              end,
    From0 = case opt(<<"from_line">>, R) of
                undefined -> opt(<<"from_offset">>, R, Lo);
                L -> pluto_job_read:offset_of_line(Src, L)
            end,
    To = case opt(<<"to_line">>, R) of
             undefined -> min(Hi, opt(<<"to_offset">>, R, Hi));
             L2 -> pluto_job_read:offset_of_line(Src, L2 + 1)
         end,
    Opts = #{from => max(Lo, min(From0, Hi)), to => To,
             before => min(20, opt(<<"before">>, R, 0)),
             'after' => min(20, opt(<<"after">>, R, 0)),
             max => max(1, min(500, opt(<<"max_matches">>, R, 50))),
             fixed => opt(<<"fixed">>, R, false),
             ignore_case => opt(<<"ignore_case">>, R, false),
             invert => opt(<<"invert">>, R, false)},
    case pluto_job_grep:grep(Src, Pattern, Opts) of
        {ok, M} ->
            pluto_stats:inc(job_greps),
            {ok, M#{<<"size">> => Hi, <<"state">> => maps:get(<<"state">>, St)}};
        E -> E
    end.

%%====================================================================
%% Wait
%%====================================================================

%% Blocks this connection's own process only.
wait(R) ->
    Agent = agent(R),
    Ids = case opt(<<"job_ids">>, R) of
              [_ | _] = L -> L;
              _           -> [maps:get(<<"id">>, job(R))]
          end,
    Jobs = [job(#{<<"job_id">> => I}) || I <- Ids],
    Until = opt(<<"until">>, R, <<"exit">>),
    lists:member(Until, [<<"exit">>, <<"output">>, <<"idle">>, <<"pattern">>])
        orelse throw({bad, <<"bad value for until">>}),
    Pattern = opt(<<"pattern">>, R),
    case Until of
        <<"pattern">> when Pattern =:= undefined -> throw({bad, <<"missing pattern">>});
        <<"pattern">> ->
            case pluto_job_grep:compile(Pattern, #{}) of
                {ok, _}    -> ok;
                {error, W} -> throw({bad, W})
            end;
        _ -> ok
    end,
    Marks = maps:from_list(
              [begin
                   Id = maps:get(<<"id">>, P),
                   Size = maps:get(<<"size">>, describe(P, pluto_job_mgr:state(P), Agent)),
                   Default = case Until of <<"pattern">> -> 0; _ -> Size end,
                   {Id, opt(<<"from_offset">>, R, Default)}
               end || P <- Jobs]),
    Timeout = min(opt(<<"timeout_ms">>, R, 50000), ?MAX_WAIT_MS),
    wait_loop(Ids, Until, R, Marks, opt(<<"all">>, R, false), Agent,
              now_ms() + Timeout).

wait_loop(Ids, Until, R, Marks0, All, Agent, Deadline) ->
    {Hits, Marks} = lists:foldl(
        fun(Id, {H, M}) ->
            case wait_hit(Id, Until, R, maps:get(Id, M), Agent) of
                {true, Why}  -> {H#{Id => Why}, M};
                {false, Pos} -> {H, M#{Id => Pos}}
            end
        end, {#{}, Marks0}, Ids),
    Enough = case All of
                 true -> map_size(Hits) =:= length(lists:usort(Ids));
                 _    -> map_size(Hits) > 0
             end,
    Late = now_ms() >= Deadline,
    if
        Enough; Late ->
            {ok, #{<<"timed_out">> => not Enough, <<"hits">> => Hits,
                   <<"jobs">> => [describe(P, pluto_job_mgr:state(P), Agent)
                                  || I <- Ids, {ok, P} <- [pluto_job_mgr:pkg(I)]]}};
        true ->
            timer:sleep(?WAIT_STEP_MS),
            wait_loop(Ids, Until, R, Marks, All, Agent, Deadline)
    end.

wait_hit(Id, Until, R, Mark, _Agent) ->
    case pluto_job_mgr:pkg(Id) of
        {error, _} -> {true, #{<<"reason">> => <<"gone">>}};
        {ok, P} ->
            St = pluto_job_mgr:state(P),
            #{hi := Hi} = Src = pluto_job_streams:main_source(Id, St),
            Done = pluto_job_mgr:is_done(P),
            Cond = case Until of
                <<"exit">>   -> false;
                <<"output">> when Hi > Mark ->
                    {true, #{<<"reason">> => <<"output">>, <<"offset">> => Mark}};
                <<"idle">> ->
                    Last = opt(<<"last_output_at">>, St),
                    case is_integer(Last) andalso
                         now_ms() - Last >= opt(<<"idle_ms">>, R, 5000) of
                        true  -> {true, #{<<"reason">> => <<"idle">>}};
                        false -> false
                    end;
                <<"pattern">> when Hi > Mark ->
                    wait_pattern(Src, R, Mark, Done);
                _ -> false
            end,
            case {Cond, Done} of
                {{true, _}, _}     -> Cond;
                {{false, Pos}, false} -> {false, Pos};
                {_, true}          -> {true, #{<<"reason">> => <<"exit">>}};
                {false, false}     -> {false, Mark}
            end
    end.

%% Scan only new, complete lines on each step.
wait_pattern(#{hi := Hi} = Src, R, Mark, Done) ->
    To = case Done of
             true  -> Hi;
             false -> last_line_end(Src, Mark, Hi)
         end,
    case To > Mark andalso
         pluto_job_grep:grep(Src, opt(<<"pattern">>, R),
                             #{from => Mark, to => To, max => 1}) of
        false -> {false, Mark};
        {ok, #{<<"groups">> := [[E | _] | _]}} ->
            {true, E#{<<"reason">> => <<"pattern">>}};
        {ok, #{<<"resume_at">> := null}} -> {false, To};
        {ok, #{<<"resume_at">> := Pos}}  -> {false, Pos};
        {error, W} -> {true, #{<<"reason">> => W}}
    end.

last_line_end(Src, Mark, Hi) ->
    From = max(Mark, Hi - 65536),
    case binary:matches(pluto_job_read:bytes(Src, From, Hi - From), <<"\n">>) of
        [] -> From;
        Ms -> {P, _} = lists:last(Ms), From + P + 1
    end.

%%====================================================================
%% Packages
%%====================================================================

export(R) ->
    #{<<"id">> := Id} = P = job(R),
    Dest = case opt(<<"path">>, R) of
               undefined ->
                   filename:join([pluto_job_store:home(), "exports",
                                  binary_to_list(Id) ++ ".pjob"]);
               Path ->
                   filename:pathtype(Path) =:= absolute
                       orelse throw({bad, <<"path must be absolute">>}),
                   filelib:is_file(Path)
                       andalso throw({bad, <<"path already exists">>}),
                   Path
           end,
    ok = filelib:ensure_dir(Dest),
    Tmp = pluto_job_store:file(Id, "export-" ++ integer_to_list(
                                   erlang:unique_integer([positive])) ++ ".json"),
    ok = pluto_job_store:write_json(
           Tmp, maps:without([<<"base_env">>, <<"request_id">>, <<"spawn_at">>], P)),
    try result(pluto_job_store:run("pluto_job_package.py",
                                   ["export", pluto_job_store:dir(Id), Tmp, Dest]))
    after file:delete(Tmp)
    end.

import(R) ->
    Path = maps:get(<<"path">>, R),
    Base = filename:join(pluto_job_store:home(), "staging"),
    ok = filelib:ensure_path(Base),
    Staging = filename:join(Base, integer_to_list(erlang:unique_integer([positive]))
                            ++ os:getpid()),
    ok = file:make_dir(Staging),
    ok = file:change_mode(Staging, 8#700),
    try result(pluto_job_store:run("pluto_job_package.py",
                                   ["import", Path, Staging])) of
        {ok, _} ->
            User = opt(<<"user">>, R, <<"unknown">>),
            case pluto_job_mgr:call({adopt, Staging, agent(R), User}) of
                {ok, Id, What} ->
                    {ok, P} = pluto_job_mgr:pkg(Id),
                    {ok, (describe(P, pluto_job_mgr:state(P), agent(R)))
                         #{<<"result">> => What}};
                E -> E
            end;
        E -> E
    after file:del_dir_r(Staging)
    end.

result({_, Out}) ->
    case pluto_protocol_json:decode(Out) of
        {ok, #{<<"error">> := E}} -> {error, E};
        {ok, M} -> {ok, M};
        _       -> {error, <<"package helper failed">>}
    end.

%%====================================================================
%% Stats
%%====================================================================

stats() ->
    Rows = [{P, pluto_job_mgr:state(P)} || P <- pluto_job_mgr:all()],
    Ds = [describe(P, St, <<>>) || {P, St} <- Rows],
    Count = fun(Key) ->
        lists:foldl(fun(D, A) ->
            maps:update_with(maps:get(Key, D, <<"unknown">>), fun(N) -> N + 1 end, 1, A)
        end, #{}, Ds) end,
    Sum = fun(Key) -> lists:sum([maps:get(Key, D) || D <- Ds]) end,
    Delivered = lists:sum(
        [opt(<<"delivered">>, C, 0) || #{<<"id">> := Id} <- pluto_job_mgr:all(),
         {ok, Cs} <- [pluto_job_store:read_json(
                        pluto_job_store:file(Id, "cursors.json"))],
         C <- maps:values(Cs)]),
    #{<<"jobs">> => length(Ds), <<"by_state">> => Count(<<"state">>),
      <<"by_owner">> => Count(<<"owner">>), <<"by_user">> => Count(<<"user">>),
      <<"by_activity">> => maps:without([<<"unknown">>], Count(<<"activity">>)),
      <<"total_elapsed_ms">> => Sum(<<"elapsed_ms">>),
      <<"total_queued_ms">> => Sum(<<"queued_ms">>),
      <<"bytes_captured">> => Sum(<<"size">>),
      <<"bytes_read_by_cursor">> => Delivered,
      <<"since_server_start">> =>
          #{<<"bytes_delivered">> => pluto_stats:get_counter(job_bytes_delivered),
            <<"greps">> => pluto_stats:get_counter(job_greps),
            <<"jobs_started">> => pluto_stats:get_counter(jobs_started)}}.
