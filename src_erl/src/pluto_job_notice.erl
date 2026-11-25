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

-export([event/3, notify/5, page/3, load/0, history/3, read_history/1]).

-type board() :: #{agent_id() => [map()]}.

%% @doc Append to Pluto's public event log.
-spec event(atom(), pkg(), binary()) -> ok.
event(Type, #{<<"id">> := Id, <<"owner">> := O}, St) ->
    pluto_event_log:log(Type, #{<<"job_id">> => Id, <<"agent_id">> => O,
                                <<"state">> => St}).

%% @doc Record a notice for Agent and wake it if it is connected.
-spec notify(agent_id(), binary(), pkg(), binary(), board()) -> board().
notify(Agent, Event, #{<<"id">> := Id} = P, St, Board) ->
    N = #{<<"seq">> => erlang:system_time(microsecond), <<"agent">> => Agent,
          <<"event">> => Event, <<"job_id">> => Id, <<"state">> => St,
          <<"name">> => pluto_job_store:opt(<<"name">>, P, null),
          <<"at">> => pluto_job_store:now_ms()},
    append(N),
    catch pluto_msg_hub:push_event_to_agent(
            Agent, #{<<"event">> => Event, <<"job_id">> => Id,
                     <<"state">> => St, <<"from">> => <<"smartstreams">>}),
    put_notice(N, Board).

put_notice(#{<<"agent">> := A} = N, Board) ->
    L = maps:get(A, Board, []) ++ [N],
    Board#{A => lists:nthtail(max(0, length(L) - ?JOB_NOTICE_KEEP), L)}.

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
-spec load() -> board().
load() ->
    MinAt = pluto_job_store:now_ms() - ?JOB_NOTICE_TTL_MS,
    Board = lists:foldl(
        fun(#{<<"ack">> := Ack, <<"agent">> := A}, B) ->
                B#{A => [N || #{<<"seq">> := Q} = N <- maps:get(A, B, []), Q > Ack]};
           (#{<<"agent">> := _, <<"seq">> := _, <<"at">> := At} = N, B) when At > MinAt ->
                put_notice(N, B);
           (_, B) -> B
        end, #{}, pluto_job_store:read_jsonl(path())),
    Lines = [pluto_protocol_json:encode_line(N) || L <- maps:values(Board), N <- L],
    Tmp = path() ++ ".tmp",
    _ = file:write_file(Tmp, Lines),
    _ = file:rename(Tmp, path()),
    maps:filter(fun(_, L) -> L =/= [] end, Board).

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
