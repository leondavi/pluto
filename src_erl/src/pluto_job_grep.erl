%%%-------------------------------------------------------------------
%%% @doc pluto_job_grep — Server-side regex search with context.
%%%
%%% Streams line by line in a separate process with a heap cap and a
%%% deadline, so a hostile pattern cannot hurt the server. Matching is
%%% bytewise.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_grep).

-export([grep/3, compile/2]).

-define(BLOCK, 262144).
-define(MAX_LINE, 1048576).          %% longer lines are matched in pieces
-define(CLIP, 4096).
-define(BUDGET, 268435456).
-define(DEADLINE_MS, 10000).
-define(MAX_OUT, 65536).             %% total text returned
-define(RE_LIMIT, 100000).

compile(Pattern, Opts) ->
    P = case maps:get(fixed, Opts, false) of
            true  -> re:replace(Pattern, "[][\\\\^$.|?*+(){}-]", "\\\\&",
                                [global, {return, binary}]);
            false -> Pattern
        end,
    case re:compile(P, [caseless || maps:get(ignore_case, Opts, false)]) of
        {ok, Re} -> {ok, Re};
        {error, {Msg, At}} ->
            {error, iolist_to_binary(io_lib:format("~ts at ~w", [Msg, At]))}
    end.

grep(Src, Pattern, Opts) ->
    case compile(Pattern, Opts) of
        {error, R} -> {error, R};
        {ok, _} ->
            {_Pid, Ref} = spawn_opt(
                fun() -> exit({done, run(Src, Pattern, Opts)}) end,
                [monitor, {max_heap_size, #{size => 16#1000000, kill => true,
                                            error_logger => false}}]),
            receive
                {'DOWN', Ref, process, _, {done, R}} -> {ok, R};
                {'DOWN', Ref, process, _, _} -> {error, <<"grep_aborted">>}
            after ?DEADLINE_MS + 5000 ->
                exit(_Pid, kill),
                erlang:demonitor(Ref, [flush]),
                {error, <<"grep_deadline">>}
            end
    end.

run(#{hi := Hi} = Src, Pattern, Opts) ->
    {ok, Re} = compile(Pattern, Opts),
    From = maps:get(from, Opts),
    S = #{src => Src, re => Re, hi => min(Hi, maps:get(to, Opts, Hi)),
          invert => maps:get(invert, Opts, false),
          before => maps:get(before, Opts, 0),
          'after' => maps:get('after', Opts, 0),
          max => maps:get(max, Opts, 50),
          stop => erlang:monotonic_time(millisecond) + ?DEADLINE_MS,
          budget => From + ?BUDGET,
          line => pluto_job_read:line_of(Src, From),
          prev => [], left => 0, hits => 0, out => [], bytes => 0,
          limited => 0, groups => [], resume => undefined},
    Done = scan(S, From, <<>>),
    #{<<"matches">> => maps:get(hits, Done),
      <<"groups">> => lists:reverse(maps:get(groups, flush(Done))),
      <<"complete">> => maps:get(resume, Done) =:= undefined,
      <<"resume_at">> => case maps:get(resume, Done) of
                             undefined -> null;
                             R -> R
                         end,
      <<"lines_over_regex_limit">> => maps:get(limited, Done)}.

%% Off is the file offset of the first byte of Carry (a partial line).
scan(#{src := Src, hi := Hi, stop := Stop, budget := Budget} = S, Off, Carry) ->
    Pos = Off + byte_size(Carry),
    case Pos >= Budget orelse erlang:monotonic_time(millisecond) > Stop of
        true  -> S#{resume := Off};
        false ->
            case pluto_job_read:bytes(Src, Pos, min(?BLOCK, Hi - Pos)) of
                <<>> when Carry =:= <<>> -> S;
                <<>> ->
                    case line(S, Off, Carry, true) of
                        {stop, S1} -> S1;
                        S1         -> S1
                    end;
                D -> lines(S, Off, <<Carry/binary, D/binary>>)
            end
    end.

lines(S, Off, Bin) ->
    case binary:match(Bin, <<"\n">>) of
        {P, 1} ->
            <<L:P/binary, _, Rest/binary>> = Bin,
            case line(S, Off, L, true) of
                {stop, S1} -> S1;
                S1         -> lines(S1, Off + P + 1, Rest)
            end;
        nomatch when byte_size(Bin) >= ?MAX_LINE ->
            case line(S, Off, Bin, false) of
                {stop, S1} -> S1;
                S1         -> scan(S1, Off + byte_size(Bin), <<>>)
            end;
        nomatch -> scan(S, Off, binary:copy(Bin))
    end.

line(#{re := Re, invert := Inv, line := N} = S0, Off, L, EndsLine) ->
    S = case EndsLine of
            true  -> S0#{line := N + 1};
            false -> S0
        end,
    Res = re:run(L, Re, [{capture, first, index}, report_errors,
                         {match_limit, ?RE_LIMIT}]),
    case Res of
        {error, _} -> context(S#{limited := maps:get(limited, S) + 1}, N, Off, L);
        nomatch when Inv -> hit(S, N, Off, L, 0);
        nomatch          -> context(S, N, Off, L);
        {match, _} when Inv    -> context(S, N, Off, L);
        {match, [{Col, _} | _]} -> hit(S, N, Off, L, Col)
    end.

hit(#{hits := H, max := Max, bytes := B} = S, _N, Off, _L, _C)
  when H >= Max; B > ?MAX_OUT ->
    {stop, S#{resume := Off}};
hit(#{hits := H, out := Out, prev := Prev, left := Left,
      'after' := After} = S, N, Off, L, Col) ->
    %% Context that touches the previous group merges into it.
    S1 = case Left =:= 0 andalso Out =/= [] andalso Prev =/= [] andalso
              not adjacent(hd(Out), lists:last(Prev)) of
             true  -> flush(S);
             false -> case Left =:= 0 andalso Prev =:= [] andalso Out =/= []
                           andalso not adjacent(hd(Out), {N, Off, L}) of
                          true  -> flush(S);
                          false -> S
                      end
         end,
    Ctx = [entry(PN, PO, PL, false, 0) || {PN, PO, PL} <- Prev],
    E = entry(N, Off, L, true, Col),
    S1#{hits := H + 1, out := [E | Ctx ++ maps:get(out, S1)], prev := [],
        left := After,
        bytes := lists:sum([byte_size(maps:get(<<"text">>, X))
                            || X <- [E | Ctx]]) + maps:get(bytes, S1)}.

adjacent(#{<<"line">> := A}, {B, _, _}) -> B =< A + 1.

context(#{left := Left, out := Out, bytes := B} = S, N, Off, L) when Left > 0 ->
    E = entry(N, Off, L, false, 0),
    S#{left := Left - 1, out := [E | Out],
       bytes := B + byte_size(maps:get(<<"text">>, E))};
context(#{before := 0} = S, _N, _Off, _L) -> S;
context(#{before := B, prev := Prev} = S, N, Off, L) ->
    Clip = binary:copy(binary:part(L, 0, min(byte_size(L), ?CLIP))),
    S#{prev := lists:sublist([{N, Off, Clip} | Prev], B)}.

flush(#{out := []} = S) -> S;
flush(#{out := Out, groups := G} = S) ->
    S#{out := [], groups := [lists:reverse(Out) | G]}.

%% A clipped line is shown around its match, not from its start.
entry(N, Off, L, Match, Col) ->
    Size = byte_size(L),
    From = case Size > ?CLIP of
               true  -> max(0, min(Col - ?CLIP div 4, Size - ?CLIP));
               false -> 0
           end,
    Text = pluto_job_read:sanitize(binary:part(L, From, min(?CLIP, Size - From))),
    E = #{<<"line">> => N, <<"offset">> => Off, <<"match">> => Match,
          <<"text">> => Text},
    case Size > ?CLIP of
        true  -> E#{<<"clipped">> => true};
        false -> E
    end.
