%%%-------------------------------------------------------------------
%%% @doc pluto_job_notice — Events, durable notices and history.
%%%
%%% Pluto's inbox forgets an agent 30 s after it disconnects, so a notice
%%% is kept in notices.jsonl until its agent acknowledges it; the inbox
%%% event is only a wakeup. Events carry ids and states, never commands:
%%% /events is readable without the key.
%%%
%%% Notices live in the manager's memory as #{Agent => [Notice]}, oldest
%%% first. The file is append-only (notices and acks) and compacted at
%%% boot.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_notice).

-include("pluto.hrl").

-export([event/3, notify/5, notify/6, move/4, page/3, load/0, history/3,
         read_history/1]).

-type board() :: #{agent_id() => [map()]}.

%% @doc Append to Pluto's public event log.
-spec event(atom(), pkg(), binary()) -> ok.
event(Type, #{<<"id">> := Id, <<"owner">> := O}, St) ->
    pluto_event_log:log(Type, #{<<"job_id">> => Id, <<"agent_id">> => O,
                                <<"state">> => St}).

%% @doc Record a notice for Agent and wake it if it is connected.
-spec notify(agent_id(), binary(), pkg(), binary(), board()) -> board().
notify(Agent, Event, P, St, Board) -> notify(Agent, Event, P, St, #{}, Board).

%% @doc With more fields. They may hold output, so they stay in the notice;
%% of them only the rule's name goes into the wakeup.
-spec notify(agent_id(), binary(), pkg(), binary(), map(), board()) -> board().
notify(Agent, Event, #{<<"id">> := Id} = P, St, Extra, Board) ->
    N = Extra#{<<"seq">> => seq(), <<"agent">> => Agent,
          <<"event">> => Event, <<"job_id">> => Id, <<"state">> => St,
          <<"name">> => pluto_job_store:opt(<<"name">>, P, null),
          <<"tags">> => pluto_job_store:opt(<<"tags">>, P, []),
          <<"follows">> => pluto_job_store:opt(<<"follows">>, P, null),
          <<"at">> => pluto_job_store:now_ms()},
    append(N),
    catch pluto_msg_hub:push_event_to_agent(
            Agent, (maps:with([<<"watch">>, <<"h">>], Extra))
                   #{<<"event">> => Event, <<"job_id">> => Id,
                     <<"state">> => St, <<"from">> => <<"smartstreams">>}),
    put_notice(N, Board).

put_notice(#{<<"agent">> := A} = N, Board) ->
    L = maps:get(A, Board, []) ++ [N],
    Board#{A => trim(L, length(L) - ?JOB_NOTICE_KEEP)}.

%% Too many: many hits of rules must not push out that a job ended.
trim(L, N) when N =< 0 -> L;
trim(L, N) ->
    case lists:search(fun is_hit/1, L) of
        {value, X} -> trim(lists:delete(X, L), N - 1);
        false      -> lists:nthtail(N, L)
    end.

is_hit(#{<<"event">> := E}) -> E =:= ?EVT_JOB_WATCH.

%% @doc A job changed hands: so do the hits its old owner has not seen.
-spec move(job_id(), agent_id(), agent_id(), board()) -> board().
move(_, A, A, Board) -> Board;
move(Id, From, To, Board) ->
    {Hits, Rest} = lists:partition(fun(#{<<"job_id">> := I} = N) ->
                                       I =:= Id andalso is_hit(N)
                                   end, maps:get(From, Board, [])),
    lists:foldl(fun(#{<<"seq">> := Q} = N, B) ->
        %% A fresh number: To may have acknowledged beyond the old one.
        New = N#{<<"agent">> := To, <<"seq">> := seq()},
        append(New),
        append(#{<<"agent">> => From, <<"drop">> => Q}),
        put_notice(New, B)
    end, case Rest of
             [] -> maps:remove(From, Board);
             _  -> Board#{From => Rest}
         end, Hits).

%% Strictly increasing: an acknowledgement covers everything up to it.
seq() ->
    Last = case get(?MODULE) of undefined -> 0; Q -> Q end,
    New = max(erlang:system_time(microsecond), Last + 1),
    put(?MODULE, New),
    New.

%% @doc Drop what Agent has seen (seq =< Ack), return the next page.
-spec page(agent_id(), non_neg_integer(), board()) -> {[map()], board()}.
page(Agent, Ack, Board) ->
    Old = maps:get(Agent, Board, []),
    Keep = [N || #{<<"seq">> := Q} = N <- Old, Q > Ack],
    case length(Keep) < length(Old) of
        true  -> append(#{<<"agent">> => Agent, <<"ack">> => Ack});
        false -> ok
    end,
    Board1 = case Keep of
                 [] -> maps:remove(Agent, Board);
                 _  -> Board#{Agent => Keep}
             end,
    {lists:sublist(Keep, ?JOB_NOTICE_PAGE), Board1}.

%% @doc Read the file, drop acknowledged and expired notices, compact.
%% Also returns the number of the last watch hit announced per job, which
%% must outlive the notices themselves.
-spec load() -> {board(), #{job_id() => integer()}}.
load() ->
    MinAt = pluto_job_store:now_ms() - ?JOB_NOTICE_TTL_MS,
    All = pluto_job_store:read_jsonl(path()),
    Told = maps:filter(
        fun(Id, _) -> pluto_job_registry:pkg(Id) =/= {error, ?ERR_UNKNOWN_JOB} end,
        lists:foldl(fun(#{<<"job_id">> := Id, <<"h">> := H}, T) ->
                            T#{Id => max(H, maps:get(Id, T, 0))};
                       (_, T) -> T
                    end, #{}, All)),
    Board = lists:foldl(
        fun(#{<<"drop">> := Q0, <<"agent">> := A}, B) ->
                B#{A => [N || #{<<"seq">> := Q} = N <- maps:get(A, B, []), Q =/= Q0]};
           (#{<<"ack">> := Ack, <<"agent">> := A}, B) ->
                B#{A => [N || #{<<"seq">> := Q} = N <- maps:get(A, B, []), Q > Ack]};
           (#{<<"agent">> := _, <<"seq">> := _, <<"at">> := At} = N, B) when At > MinAt ->
                put_notice(N, B);
           (_, B) -> B
        end, #{}, All),
    Lines = [pluto_protocol_json:encode_line(N) || L <- maps:values(Board), N <- L] ++
            [pluto_protocol_json:encode_line(#{<<"job_id">> => Id, <<"h">> => H})
             || {Id, H} <- maps:to_list(Told)],
    Tmp = path() ++ ".tmp",
    _ = file:write_file(Tmp, Lines),
    _ = file:rename(Tmp, path()),
    {maps:filter(fun(_, L) -> L =/= [] end, Board), Told}.

path() -> filename:join(pluto_job_store:home(), ?HOME_FILE_NOTICES).

append(M) ->
    case pluto_job_store:append_jsonl(path(), M) of
        ok -> ok;
        {error, R} -> ?LOG_ERROR("smartstreams: notice not saved: ~p", [R])
    end.

%% @doc Append one line to the history, which survives pruning.
-spec history(binary(), pkg(), map()) -> ok.
history(Kind, P, Extra) ->
    case pluto_job_store:append_jsonl(history_path(),
            maps:merge((maps:with(?JOB_HISTORY_KEYS, P))
                       #{<<"kind">> => Kind, <<"at">> => pluto_job_store:now_ms()},
                       Extra)) of
        ok -> ok;
        {error, R} -> ?LOG_ERROR("smartstreams: history not saved: ~p", [R])
    end.

%% @doc Newest first.
-spec read_history(pos_integer()) -> [map()].
read_history(Limit) ->
    lists:sublist(lists:reverse(pluto_job_store:read_jsonl(history_path())), Limit).

history_path() -> filename:join(pluto_job_store:home(), ?HOME_FILE_HISTORY).
