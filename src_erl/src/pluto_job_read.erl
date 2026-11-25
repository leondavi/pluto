%%%-------------------------------------------------------------------
%%% @doc pluto_job_read — Bounded reads over a job's output.
%%%
%%% The canonical position is an absolute byte offset; lines are
%%% 1-based. A `Src' map describes what is readable:
%%%   path    file to read
%%%   lo, hi  readable byte range [lo, hi)
%%%   man     manifest path, or undefined (line counts then scan from 0)
%%%   closed  true when no more bytes can ever appear
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_read).

-export([read/4, bytes/3, line_of/2, offset_of_line/2, tail_start/3,
         records/3, verify/2, sanitize/1, strip_ansi/1]).

-define(CHUNK, 65536).
-define(REC, 256).
-define(BLOCK, 262144).

%% @doc Read up to Max bytes at Off. Mode `cut' ends on a line boundary
%% when the byte limit (not the end of data) stopped the read; `raw'
%% ends exactly at the limit.
read(#{lo := Lo, hi := Hi, closed := Closed} = Src, Off0, Max, Opts) ->
    Off = max(Lo, min(Off0, Hi)),
    Raw0 = bytes(Src, Off, min(Max, Hi - Off)),
    Limited = Off + byte_size(Raw0) < Hi,
    Raw1 = case Limited andalso maps:get(cut, Opts, false) of
               true  -> cut_line(Raw0);
               false -> Raw0
           end,
    Raw2 = limit_lines(Raw1, maps:get(lines, Opts, infinity)),
    End2 = Off + byte_size(Raw2),
    Raw = case Closed andalso End2 =:= Hi of
              true  -> Raw2;
              false -> trim_utf8(Raw2)
          end,
    End = Off + byte_size(Raw),
    First = line_of(Src, Off),
    NLs = count_nl(Raw),
    Last = case ends_nl(Raw) of
               true  -> First + NLs - 1;
               false -> First + NLs
           end,
    Base = #{<<"start">> => Off, <<"end">> => End,
             <<"first_line">> => First, <<"last_line">> => max(First, Last),
             <<"size">> => Hi,
             <<"eof">> => Closed andalso End =:= Hi,
             <<"ends_mid_line">> => Raw =/= <<>> andalso not ends_nl(Raw),
             <<"sha256">> => hex(crypto:hash(sha256, Raw))},
    Text = case maps:get(strip_ansi, Opts, false) of
               true  -> strip_ansi(Raw);
               false -> Raw
           end,
    case maps:get(encoding, Opts, <<"utf8">>) of
        <<"base64">> -> Base#{<<"data">> => base64:encode(Raw)};
        _            -> Base#{<<"text">> => sanitize(Text)}
    end.

bytes(_Src, _Off, N) when N =< 0 -> <<>>;
bytes(#{path := Path}, Off, N) ->
    case file:open(Path, [read, raw, binary]) of
        {ok, Fd} ->
            try file:pread(Fd, Off, N) of
                {ok, D} -> D;
                _       -> <<>>
            after file:close(Fd)
            end;
        _ -> <<>>
    end.

%% @doc 1-based line number containing byte offset Off.
line_of(Src, Off) ->
    {Start, Line} = anchor(Src, Off div ?CHUNK, Off),
    Line + count_between(Src, Start, Off).

%% Best known {Offset, LineAtOffset} at or before Off.
anchor(#{man := undefined}, _, _) -> {0, 1};
anchor(#{man := Man}, Seq, Off) ->
    case rec(Man, Seq) of
        {ok, R} -> rec_anchor(R, Off);
        none ->
            case rec_count(Man) of
                0 -> {0, 1};
                N -> {ok, R} = rec(Man, N - 1), rec_anchor(R, Off)
            end
    end.

rec_anchor(#{<<"start">> := S, <<"end">> := E, <<"first_line">> := L,
             <<"newlines">> := NL}, Off) ->
    case Off >= E of
        true  -> {E, L + NL};
        false -> {S, L}
    end.

count_between(_Src, A, B) when A >= B -> 0;
count_between(Src, A, B) ->
    case bytes(Src, A, min(?BLOCK, B - A)) of
        <<>> -> 0;
        D    -> count_nl(D) + count_between(Src, A + byte_size(D), B)
    end.

%% @doc Byte offset where 1-based Line starts (hi if beyond the data).
offset_of_line(_Src, Line) when Line =< 1 -> 0;
offset_of_line(#{man := Man} = Src, Line) ->
    {Off, AtLine} =
        case Man =:= undefined orelse rec_count(Man) of
            true -> {0, 1};
            0    -> {0, 1};
            N    ->
                %% last chunk whose first_line is still before Line
                {ok, R} = rec(Man, search(Man, Line, 0, N - 1)),
                {maps:get(<<"start">>, R), maps:get(<<"first_line">>, R)}
        end,
    skip_lines(Src, Off, Line - AtLine).

search(_Man, _Line, A, A) -> A;
search(Man, Line, A, B) ->
    Mid = (A + B + 1) div 2,
    {ok, #{<<"first_line">> := L}} = rec(Man, Mid),
    case L < Line of
        true  -> search(Man, Line, Mid, B);
        false -> search(Man, Line, A, Mid - 1)
    end.

skip_lines(_Src, Off, 0) -> Off;
skip_lines(#{hi := Hi} = Src, Off, N) ->
    case bytes(Src, Off, min(?BLOCK, Hi - Off)) of
        <<>> -> Hi;
        D ->
            NLs = binary:matches(D, <<"\n">>),
            case length(NLs) of
                C when C >= N ->
                    {P, _} = lists:nth(N, NLs), Off + P + 1;
                C -> skip_lines(Src, Off + byte_size(D), N - C)
            end
    end.

%% @doc Start of the last N lines, bounded by Max bytes.
tail_start(#{lo := Lo, hi := Hi} = Src, N, Max) ->
    From = max(Lo, Hi - Max),
    D = bytes(Src, From, Hi - From),
    Body = case ends_nl(D) of
               true  -> binary:part(D, 0, byte_size(D) - 1);
               false -> D
           end,
    Pos = [P + 1 || {P, _} <- binary:matches(Body, <<"\n">>)],
    Starts = case From =:= Lo of
                 true -> [0 | Pos];
                 %% cut mid-line: drop the partial unless it is all we have
                 false when Pos =:= [] -> [0];
                 false -> Pos
             end,
    From + lists:nth(max(1, length(Starts) - N + 1), Starts).

%%====================================================================
%% Manifest
%%====================================================================

rec_count(Man) -> filelib:file_size(Man) div ?REC.

rec(Man, Seq) ->
    case bytes(#{path => Man}, Seq * ?REC, ?REC) of
        B when byte_size(B) =:= ?REC ->
            case pluto_protocol_json:decode(string:trim(B)) of
                {ok, M} -> {ok, M};
                _       -> none
            end;
        _ -> none
    end.

%% @doc {Total, [Record]} page.
records(Man, From, Limit) ->
    N = rec_count(Man),
    Seqs = lists:seq(From, min(N, From + Limit) - 1),
    {N, [R || S <- Seqs, {ok, R} <- [rec(Man, S)]]}.

%% @doc Re-hash chunks; [] or a list of bad sequence numbers.
verify(Path, Recs) ->
    [maps:get(<<"seq">>, R) ||
        #{<<"start">> := S, <<"end">> := E, <<"sha256">> := H} = R <- Recs,
        hex(crypto:hash(sha256, bytes(#{path => Path}, S, E - S))) =/= H].

%%====================================================================
%% Bytes and text
%%====================================================================

hex(B) -> string:lowercase(binary:encode_hex(B)).
count_nl(B) -> length(binary:matches(B, <<"\n">>)).
ends_nl(<<>>) -> false;
ends_nl(B)    -> binary:last(B) =:= $\n.

cut_line(B) ->
    case binary:matches(B, <<"\n">>) of
        [] -> B;
        Ms -> {P, _} = lists:last(Ms), binary:part(B, 0, P + 1)
    end.

limit_lines(B, infinity) -> B;
limit_lines(B, N) ->
    Ms = binary:matches(B, <<"\n">>),
    case length(Ms) >= N of
        true  -> {P, _} = lists:nth(N, Ms), binary:part(B, 0, P + 1);
        false -> B
    end.

%% Hold back an incomplete trailing UTF-8 sequence, but never everything.
trim_utf8(B) ->
    case trim_utf8(B, byte_size(B), 1) of
        <<>> -> B;
        T    -> T
    end.

trim_utf8(B, Size, Back) when Back =< 3, Back =< Size ->
    case binary:at(B, Size - Back) of
        C when C band 16#C0 =:= 16#80 -> trim_utf8(B, Size, Back + 1);
        C ->
            Need = if C >= 16#F0 -> 4; C >= 16#E0 -> 3; C >= 16#C0 -> 2;
                      true -> 1 end,
            case Need > Back of
                true  -> binary:part(B, 0, Size - Back);
                false -> B
            end
    end;
trim_utf8(B, _, _) -> B.

%% @doc Make any binary safe for JSON: bad bytes become U+FFFD.
sanitize(B) -> iolist_to_binary(sanitize(B, [])).

sanitize(B, Acc) ->
    case unicode:characters_to_binary(B, utf8, utf8) of
        Ok when is_binary(Ok) -> lists:reverse([Ok | Acc]);
        {_, Good, Rest} ->
            <<_, Tail/binary>> = iolist_to_binary(Rest),
            sanitize(Tail, [<<16#FFFD/utf8>>, Good | Acc])
    end.

strip_ansi(B) ->
    re:replace(B, "\\x1b(?:\\[[0-9;?]*[ -/]*[@-~]|\\][^\\x07\\x1b]*"
                  "(?:\\x07|\\x1b\\\\)|[@-Z\\\\-_])", "",
               [global, {return, binary}]).
