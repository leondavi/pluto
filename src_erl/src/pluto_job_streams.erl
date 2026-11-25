%%%-------------------------------------------------------------------
%%% @doc pluto_job_streams — Streams nested inside a job's stream.
%%%
%%% Two kinds live in a job's streams.json:
%%%   split  a view over a byte range of the output (or of another split)
%%%   push   content appended by an agent, kept in streams/<id>.log
%%% Each can be paused, resumed or halted, has its own cursor per reader,
%%% and can carry tracked follow-ups. Mutations are called only from
%%% pluto_job_mgr, which serialises them.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_streams).

-export([source/3, main_source/2, act/4, load/1, find/2, describe/2]).

-define(MAX_STREAMS, 1000).
-define(MAX_PUSH, 1048576).
-define(MAX_PUSH_TOTAL, 67108864).
-define(MAX_SPLITS, 50).

load(Id) ->
    case pluto_job_store:read_json(path(Id)) of
        {ok, M} -> M;
        _       -> #{<<"seq">> => 0, <<"streams">> => [], <<"followups">> => []}
    end.

path(Id) -> pluto_job_store:file(Id, "streams.json").
save(Id, M) -> ok = pluto_job_store:write_json(path(Id), M).
log(Id, SId) -> pluto_job_store:file(Id, filename:join("streams", binary_to_list(SId) ++ ".log")).

find(Id, SId) ->
    case [S || #{<<"id">> := I} = S <- maps:get(<<"streams">>, load(Id)), I =:= SId] of
        [S] -> {ok, S};
        []  -> {error, <<"unknown_stream">>}
    end.

%% @doc Readable source of a job, or of one nested stream.
source(Id, St, undefined) -> {ok, main_source(Id, St)};
source(Id, St, SId) ->
    case find(Id, SId) of
        {ok, S} -> {ok, stream_source(Id, St, S)};
        Err     -> Err
    end.

main_source(Id, St) ->
    Path = pluto_job_store:file(Id, "output.log"),
    Final = maps:get(<<"final_size">>, St, undefined),
    Size = filelib:file_size(Path),
    Hi = case Final of
             undefined -> Size;
             _         -> min(Final, Size)
         end,
    #{path => Path, lo => 0, hi => Hi,
      man => pluto_job_store:file(Id, "manifest.jsonl"),
      closed => Final =/= undefined orelse
                lists:member(maps:get(<<"state">>, St, <<>>),
                             [<<"lost">>, <<"snapshot">>, <<"cancelled">>,
                              <<"failed">>])}.

stream_source(Id, St, #{<<"kind">> := <<"split">>, <<"lo">> := Lo,
                        <<"hi">> := Hi0}) ->
    #{hi := Hi} = Main = main_source(Id, St),
    case Hi0 of
        null -> Main#{lo := min(Lo, Hi)};
        _    -> Main#{lo := min(Lo, Hi), hi := min(Hi, Hi0),
                      closed := Hi >= Hi0 orelse maps:get(closed, Main)}
    end;
stream_source(Id, _St, #{<<"id">> := SId, <<"state">> := State}) ->
    Path = log(Id, SId),
    #{path => Path, lo => 0, hi => filelib:file_size(Path), man => undefined,
      closed => State =:= <<"halted">>}.

describe(Id, St) ->
    #{<<"streams">> := Ss, <<"followups">> := Fs} = load(Id),
    #{<<"streams">> =>
          [begin
               #{lo := Lo, hi := Hi, closed := C} = stream_source(Id, St, S),
               S#{<<"start">> => Lo, <<"end">> => Hi, <<"bytes">> => Hi - Lo,
                  <<"closed">> => C}
           end || S <- Ss],
      <<"followups">> => Fs}.

%% @doc Apply one action. IsOwner lets the job owner manage any stream.
act(Id, St, {Agent, IsOwner}, #{<<"action">> := Action} = R) ->
    Doc = load(Id),
    case Action of
        <<"list">>   -> {ok, describe(Id, St)};
        <<"split">>  -> split(Id, St, Agent, R, Doc);
        <<"push">>   -> push(Id, Agent, R, Doc);
        <<"pause">>  -> set_state(Id, St, Agent, IsOwner, R, Doc, <<"paused">>);
        <<"resume">> -> set_state(Id, St, Agent, IsOwner, R, Doc, <<"open">>);
        <<"halt">>   -> set_state(Id, St, Agent, IsOwner, R, Doc, <<"halted">>);
        <<"followup">> -> followup(Id, Agent, R, Doc);
        _ -> {error, <<"unknown_action">>}
    end.

new(Kind, Agent, R, Seq, Extra) ->
    maps:merge(#{<<"id">> => <<"S", (integer_to_binary(Seq))/binary>>,
                 <<"kind">> => Kind, <<"state">> => <<"open">>,
                 <<"name">> => name(R), <<"by">> => Agent,
                 <<"parent">> => pluto_job_store:opt(<<"stream">>, R, null),
                 <<"at">> => pluto_job_store:now_ms()}, Extra).

name(R) ->
    case pluto_job_store:opt(<<"name">>, R, <<>>) of
        N when is_binary(N), byte_size(N) =< 200 -> N;
        _ -> <<>>
    end.

add(Id, #{<<"seq">> := Seq, <<"streams">> := Ss} = Doc, New) ->
    case length(Ss) + length(New) =< ?MAX_STREAMS of
        true ->
            save(Id, Doc#{<<"seq">> := Seq + length(New),
                          <<"streams">> := Ss ++ New}),
            {ok, #{<<"streams">> => New}};
        false -> {error, <<"too_many_streams">>}
    end.

%% Cut the output (or a parent split) into contiguous views, either at
%% from/to or at the start of every line matching a pattern.
split(Id, St, Agent, R, #{<<"seq">> := Seq} = Doc) ->
    case source(Id, St, pluto_job_store:opt(<<"stream">>, R)) of
        {error, _} = E -> E;
        {ok, #{man := undefined}} -> {error, <<"cannot_split_a_pushed_stream">>};
        {ok, #{lo := Lo, hi := Hi, closed := Closed} = Src} ->
            Open = case Closed of true -> Hi; false -> null end,
            Ranges =
                case pluto_job_store:opt(<<"pattern">>, R) of
                    undefined ->
                        From = pluto_job_store:opt(<<"from_offset">>, R, Lo),
                        To = pluto_job_store:opt(<<"to_offset">>, R, Open),
                        case is_integer(From) andalso From >= Lo andalso
                             From =< Hi andalso
                             (To =:= null orelse
                              (is_integer(To) andalso To >= From andalso
                               (Open =:= null orelse To =< Hi))) of
                            true  -> {ok, [{From, To}], null};
                            false -> {error, <<"bad_range">>}
                        end;
                    P -> cut_at(Src, P, Open)
                end,
            case Ranges of
                {ok, [], _} -> {error, <<"pattern_not_found">>};
                {ok, Rs, Resume} ->
                    New = [new(<<"split">>, Agent, R, Seq + I,
                               #{<<"lo">> => A, <<"hi">> => B})
                           || {I, {A, B}} <- lists:enumerate(Rs)],
                    case add(Id, Doc, New) of
                        {ok, M} -> {ok, M#{<<"resume_at">> => Resume}};
                        E       -> E
                    end;
                E -> E
            end
    end.

cut_at(#{lo := Lo} = Src, Pattern, Open) when is_binary(Pattern) ->
    case pluto_job_grep:grep(Src, Pattern, #{from => Lo, max => ?MAX_SPLITS}) of
        {ok, #{<<"groups">> := Gs, <<"resume_at">> := Resume}} ->
            Offs = [maps:get(<<"offset">>, E) || G <- Gs, E <- G],
            Last = case Resume of null -> Open; _ -> Resume end,
            {ok, pairs(Offs, Last), Resume};
        E -> E
    end;
cut_at(_, _, _) -> {error, <<"bad_pattern">>}.

pairs([], _) -> [];
pairs([A], Last) -> [{A, Last}];
pairs([A, B | T], Last) -> [{A, B} | pairs([B | T], Last)].

%% Append to an existing pushed stream, or open a new one.
push(Id, Agent, R, #{<<"seq">> := Seq, <<"streams">> := Ss} = Doc) ->
    Text = pluto_job_store:opt(<<"text">>, R, <<>>),
    SId = pluto_job_store:opt(<<"stream">>, R),
    Target = [S || S <- Ss, maps:get(<<"id">>, S) =:= SId],
    case {is_binary(Text) andalso byte_size(Text) =< ?MAX_PUSH, Target} of
        {false, _} -> {error, <<"text_too_large">>};
        {_, [#{<<"kind">> := <<"push">>, <<"state">> := <<"open">>} = S]} ->
            append(Id, S, Text);
        {_, [#{<<"kind">> := <<"push">>, <<"state">> := State}]} ->
            {error, <<"stream_is_", State/binary>>};
        {_, []} when SId =/= undefined -> {error, <<"unknown_stream">>};
        {_, _} ->
            %% No target, or a split: the pushed stream nests under it.
            S = new(<<"push">>, Agent, R, Seq + 1, #{}),
            case add(Id, Doc, [S]) of
                {ok, _} -> append(Id, S, Text);
                E       -> E
            end
    end.

append(Id, #{<<"id">> := SId} = S, Text) ->
    Path = log(Id, SId),
    ok = filelib:ensure_dir(Path),
    case filelib:file_size(Path) + byte_size(Text) =< ?MAX_PUSH_TOTAL of
        true ->
            ok = file:write_file(Path, Text, [append]),
            {ok, #{<<"stream">> => S, <<"size">> => filelib:file_size(Path)}};
        false -> {error, <<"stream_full">>}
    end.

set_state(Id, St, Agent, IsOwner, R, #{<<"streams">> := Ss} = Doc, To) ->
    SId = pluto_job_store:opt(<<"stream">>, R),
    case [S || S <- Ss, maps:get(<<"id">>, S) =:= SId] of
        [] -> {error, <<"unknown_stream">>};
        [#{<<"by">> := By}] when By =/= Agent, not IsOwner ->
            {error, <<"not_owner">>};
        [#{<<"state">> := <<"halted">>}] -> {error, <<"stream_is_halted">>};
        [S] ->
            S1 = halt_range(Id, St, S#{<<"state">> := To}),
            save(Id, Doc#{<<"streams">> :=
                              [case X of S -> S1; _ -> X end || X <- Ss]}),
            {ok, #{<<"stream">> => S1}}
    end.

%% A halted split stops growing at the current end of data.
halt_range(Id, St, #{<<"state">> := <<"halted">>,
                     <<"kind">> := <<"split">>} = S) ->
    #{hi := Hi} = stream_source(Id, St, S),
    S#{<<"hi">> := Hi};
halt_range(_, _, S) -> S.

%% Without followup_id: create. With it: update status and/or add a note.
followup(Id, Agent, R, #{<<"followups">> := Fs} = Doc) ->
    Now = pluto_job_store:now_ms(),
    Text = pluto_job_store:opt(<<"text">>, R, <<>>),
    Status = pluto_job_store:opt(<<"status">>, R, <<"open">>),
    SId = pluto_job_store:opt(<<"stream">>, R),
    FId = pluto_job_store:opt(<<"followup_id">>, R),
    Known = SId =:= undefined orelse
            lists:any(fun(S) -> maps:get(<<"id">>, S) =:= SId end,
                      maps:get(<<"streams">>, Doc)),
    Valid = lists:member(Status, [<<"open">>, <<"in_progress">>, <<"done">>,
                                  <<"dropped">>]),
    Entry = #{<<"at">> => Now, <<"by">> => Agent, <<"status">> => Status,
              <<"text">> => Text},
    if
        not Known -> {error, <<"unknown_stream">>};
        not Valid -> {error, <<"bad_status">>};
        not is_binary(Text); byte_size(Text) > 8192 -> {error, <<"bad_text">>};
        FId =:= undefined, length(Fs) >= 1000 -> {error, <<"too_many_followups">>};
        FId =:= undefined ->
            New = #{<<"id">> => <<"F", (integer_to_binary(length(Fs) + 1))/binary>>,
                    <<"stream">> => case SId of undefined -> null; _ -> SId end,
                    <<"text">> => Text, <<"status">> => Status,
                    <<"assignee">> => pluto_job_store:opt(<<"assignee">>, R, Agent),
                    <<"log">> => [Entry]},
            save(Id, Doc#{<<"followups">> := Fs ++ [New]}),
            {ok, #{<<"followup">> => New}};
        true ->
            case [F || F <- Fs, maps:get(<<"id">>, F) =:= FId] of
                [] -> {error, <<"unknown_followup">>};
                [F] ->
                    F1 = F#{<<"status">> := Status,
                            <<"log">> := maps:get(<<"log">>, F) ++ [Entry]},
                    save(Id, Doc#{<<"followups">> :=
                                      [case X of F -> F1; _ -> X end || X <- Fs]}),
                    {ok, #{<<"followup">> => F1}}
            end
    end.
