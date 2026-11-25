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

-include("pluto.hrl").

-export([source/3, main_source/2, apply_action/4, cut_at/2, find/2, describe/2]).

load(Id) ->
    case pluto_job_store:read_json(path(Id)) of
        {ok, M} -> M;
        _       -> #{<<"seq">> => 0, <<"streams">> => [], <<"followups">> => []}
    end.

path(Id) -> pluto_job_store:file(Id, ?JOB_FILE_STREAMS).
save(Id, M) ->
    case pluto_job_store:write_json(path(Id), M) of
        ok -> ok;
        {error, R} -> throw({io, R})
    end.

%% Ids become file names, so they are checked here too.
log_path(Id, SId) ->
    {match, _} = re:run(SId, ?STREAM_ID_RE),
    pluto_job_store:file(Id, filename:join(?JOB_DIR_STREAMS,
                                           binary_to_list(SId) ++ ".log")).

find_in(Key, Doc, WantedId) ->
    case [X || #{<<"id">> := I} = X <- maps:get(Key, Doc), I =:= WantedId] of
        [X | _] -> X;
        []      -> none
    end.

%% @doc A nested stream's record.
-spec find(job_id(), binary()) -> {ok, map()} | {error, binary()}.
find(Id, SId) ->
    case find_in(<<"streams">>, load(Id), SId) of
        none -> {error, ?ERR_UNKNOWN_STREAM};
        S    -> {ok, S}
    end.

%% @doc Readable source of a job, or of one nested stream.
-spec source(job_id(), jstate(), binary() | undefined) ->
    {ok, src()} | {error, binary()}.
source(Id, St, undefined) -> {ok, main_source(Id, St)};
source(Id, St, SId) ->
    case find(Id, SId) of
        {ok, S} -> {ok, stream_source(Id, St, S)};
        Err     -> Err
    end.

%% @doc The job's whole output.
-spec main_source(job_id(), jstate()) -> src().
main_source(Id, St) ->
    Path = pluto_job_store:file(Id, ?JOB_FILE_OUTPUT),
    Size = filelib:file_size(Path),
    Final = maps:get(<<"final_size">>, St, undefined),
    #{path => Path, lo => 0, hi => case Final of
                                       undefined -> Size;
                                       _         -> min(Final, Size)
                                   end,
      man => pluto_job_store:file(Id, ?JOB_FILE_MANIFEST),
      closed => Final =/= undefined orelse
                not lists:member(maps:get(<<"state">>, St, <<>>),
                                 [?JOB_QUEUED, ?JOB_STARTING, ?JOB_RUNNING,
                                  ?JOB_PAUSED, ?JOB_ORPHANED])}.

stream_source(Id, St, #{<<"kind">> := ?STREAM_SPLIT, <<"lo">> := Lo,
                        <<"hi">> := Hi0}) ->
    #{hi := Hi} = Main = main_source(Id, St),
    case Hi0 of
        null -> Main#{lo := min(Lo, Hi)};
        _    -> Main#{lo := min(Lo, Hi), hi := min(Hi, Hi0),
                      closed := Hi >= Hi0 orelse maps:get(closed, Main)}
    end;
stream_source(Id, _St, #{<<"id">> := SId, <<"state">> := State}) ->
    Path = log_path(Id, SId),
    #{path => Path, lo => 0, hi => filelib:file_size(Path), man => undefined,
      closed => State =:= ?STREAM_HALTED}.

%% @doc All nested streams with their current ranges, and follow-ups.
-spec describe(job_id(), jstate()) -> map().
describe(Id, St) ->
    #{<<"streams">> := Ss, <<"followups">> := Fs} = load(Id),
    #{<<"streams">> =>
          [begin
               #{lo := Lo, hi := Hi, closed := C} = stream_source(Id, St, S),
               S#{<<"start">> => Lo, <<"end">> => Hi, <<"bytes">> => Hi - Lo,
                  <<"closed">> => C}
           end || S <- Ss],
      <<"followups">> => Fs}.

%% @doc Apply one action (manager only). IsOwner lets the job owner manage
%% any stream; others manage their own.
-spec apply_action(job_id(), jstate(), actor(), map()) -> reply().
apply_action(Id, St, {Agent, IsOwner}, R) ->
    Action = pluto_job_store:opt(<<"action">>, R),
    Doc = load(Id),
    case Action of
        <<"list">>   -> {ok, describe(Id, St)};
        <<"split">>  -> split(Id, St, Agent, R, Doc);
        <<"push">>   -> push(Id, Agent, R, Doc);
        <<"pause">>  -> set_state(Id, St, Agent, IsOwner, R, Doc, ?STREAM_PAUSED);
        <<"resume">> -> set_state(Id, St, Agent, IsOwner, R, Doc, ?STREAM_OPEN);
        <<"halt">>   -> set_state(Id, St, Agent, IsOwner, R, Doc, ?STREAM_HALTED);
        <<"followup">> -> followup(Id, Agent, R, Doc);
        _ -> {error, <<"bad_action">>}
    end.

new_stream(Kind, Agent, R, Seq, Extra) ->
    maps:merge(#{<<"id">> => <<"S", (integer_to_binary(Seq))/binary>>,
                 <<"kind">> => Kind, <<"state">> => <<"open">>,
                 <<"name">> => name(R), <<"by">> => Agent,
                 <<"parent">> => pluto_job_store:opt(<<"stream">>, R, null),
                 <<"at">> => pluto_job_store:now_ms()}, Extra).

name(R) ->
    case pluto_job_store:opt(<<"name">>, R, <<>>) of
        N when is_binary(N), byte_size(N) =< ?STREAM_NAME_MAX -> N;
        _ -> <<>>
    end.

add(Id, #{<<"seq">> := Seq, <<"streams">> := Ss} = Doc, New) ->
    case length(Ss) + length(New) =< ?STREAM_MAX of
        true ->
            save(Id, Doc#{<<"seq">> := Seq + length(New),
                          <<"streams">> := Ss ++ New}),
            {ok, #{<<"streams">> => New}};
        false -> {error, <<"too_many_streams">>}
    end.

%% Cut the output (or a parent split) into contiguous views: either one
%% from/to range, or the ranges cut_at/2 found for a pattern.
split(Id, St, Agent, R, #{<<"seq">> := Seq} = Doc) ->
    case source(Id, St, pluto_job_store:opt(<<"stream">>, R)) of
        {error, _} = E -> E;
        {ok, #{man := undefined}} -> {error, <<"cannot_split_a_pushed_stream">>};
        {ok, Src} ->
            case ranges(Src, R) of
                {ok, Ranges, Resume} ->
                    New = [new_stream(?STREAM_SPLIT, Agent, R, Seq + I,
                                      #{<<"lo">> => A, <<"hi">> => B})
                           || {I, {A, B}} <- lists:enumerate(Ranges)],
                    case add(Id, Doc, New) of
                        {ok, M} -> {ok, M#{<<"resume_at">> => Resume}};
                        E       -> E
                    end;
                E -> E
            end
    end.

ranges(_, #{ranges := {Ranges, Resume}}) -> {ok, Ranges, Resume};
ranges(#{lo := Lo, hi := Hi} = Src, R) ->
    Open = open_end(Src),
    From = pluto_job_store:opt(<<"from_offset">>, R, Lo),
    To = pluto_job_store:opt(<<"to_offset">>, R, Open),
    Ok = From >= Lo andalso From =< Hi andalso
         (To =:= null orelse (To >= From andalso (Open =:= null orelse To =< Hi))),
    case Ok of
        true  -> {ok, [{From, To}], null};
        false -> {error, <<"bad_range">>}
    end.

%% The last view of a live stream keeps growing.
open_end(#{closed := true, hi := Hi}) -> Hi;
open_end(_) -> null.

%% @doc Ranges starting at each line that matches. May be slow, so the
%% caller computes them before asking the manager to record them.
-spec cut_at(src(), binary()) ->
    {ok, {[{non_neg_integer(), non_neg_integer() | null}],
          non_neg_integer() | null}} | {error, binary()}.
cut_at(#{lo := Lo} = Src, Pattern) ->
    case pluto_job_grep:grep(Src, Pattern, #{from => Lo, max => ?STREAM_SPLITS_MAX}) of
        {ok, #{<<"groups">> := [], <<"resume_at">> := null}} ->
            {error, <<"pattern_not_found">>};
        {ok, #{<<"groups">> := []}} -> {error, <<"pattern_search_incomplete">>};
        {ok, #{<<"groups">> := Gs, <<"resume_at">> := Resume}} ->
            Offs = [maps:get(<<"offset">>, E) || G <- Gs, E <- G],
            Last = case Resume of
                       null -> open_end(Src);
                       _    -> Resume
                   end,
            {ok, {pairs(Offs, Last), Resume}};
        E -> E
    end.

pairs([], _) -> [];
pairs([A], Last) -> [{A, Last}];
pairs([A, B | T], Last) -> [{A, B} | pairs([B | T], Last)].

%% Append to an existing pushed stream, or open a new one.
push(Id, Agent, R, #{<<"seq">> := Seq, <<"streams">> := Ss} = Doc) ->
    Text = pluto_job_store:opt(<<"text">>, R, <<>>),
    SId = pluto_job_store:opt(<<"stream">>, R),
    Target = [S || S <- Ss, maps:get(<<"id">>, S) =:= SId],
    case {is_binary(Text) andalso byte_size(Text) =< ?STREAM_PUSH_MAX, Target} of
        {false, _} -> {error, <<"text_too_large">>};
        {_, [#{<<"kind">> := ?STREAM_PUSH, <<"state">> := ?STREAM_OPEN} = S]} ->
            append(Id, S, Text);
        {_, [#{<<"kind">> := ?STREAM_PUSH, <<"state">> := State}]} ->
            {error, <<"stream_is_", State/binary>>};
        {_, []} when SId =/= undefined -> {error, ?ERR_UNKNOWN_STREAM};
        {_, _} ->
            %% No target, or a split: the pushed stream nests under it.
            S = new_stream(?STREAM_PUSH, Agent, R, Seq + 1, #{}),
            case add(Id, Doc, [S]) of
                {ok, _} -> append(Id, S, Text);
                E       -> E
            end
    end.

append(Id, #{<<"id">> := SId} = S, Text) ->
    Path = log_path(Id, SId),
    _ = filelib:ensure_dir(Path),
    case filelib:file_size(Path) + byte_size(Text) =< ?STREAM_LOG_MAX of
        true ->
            case file:write_file(Path, Text, [append]) of
                ok -> ok;
                {error, E} -> throw({io, E})
            end,
            {ok, #{<<"stream">> => S, <<"size">> => filelib:file_size(Path)}};
        false -> {error, <<"stream_full">>}
    end.

set_state(Id, St, Agent, IsOwner, R, #{<<"streams">> := Ss} = Doc, To) ->
    SId = pluto_job_store:opt(<<"stream">>, R),
    case [S || S <- Ss, maps:get(<<"id">>, S) =:= SId] of
        [] -> {error, ?ERR_UNKNOWN_STREAM};
        [#{<<"by">> := By}] when By =/= Agent, not IsOwner ->
            {error, ?ERR_NOT_OWNER};
        [#{<<"state">> := ?STREAM_HALTED}] -> {error, <<"stream_is_halted">>};
        [S] ->
            S1 = halt_range(Id, St, S#{<<"state">> := To}),
            save(Id, Doc#{<<"streams">> :=
                              [case X of S -> S1; _ -> X end || X <- Ss]}),
            {ok, #{<<"stream">> => S1}}
    end.

%% A halted split stops growing at the current end of data.
halt_range(Id, St, #{<<"state">> := ?STREAM_HALTED,
                     <<"kind">> := ?STREAM_SPLIT} = S) ->
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
        not Known -> {error, ?ERR_UNKNOWN_STREAM};
        not Valid -> {error, <<"bad_status">>};
        not is_binary(Text); byte_size(Text) > ?FOLLOWUP_TEXT_MAX -> {error, <<"bad_text">>};
        FId =:= undefined, length(Fs) >= ?FOLLOWUP_MAX -> {error, <<"too_many_followups">>};
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
                [] -> {error, <<"no_such_followup">>};
                [F] ->
                    F1 = F#{<<"status">> := Status,
                            <<"log">> := maps:get(<<"log">>, F) ++ [Entry]},
                    save(Id, Doc#{<<"followups">> :=
                                      [case X of F -> F1; _ -> X end || X <- Fs]}),
                    {ok, #{<<"followup">> => F1}}
            end
    end.
