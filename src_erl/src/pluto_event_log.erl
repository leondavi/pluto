%%%-------------------------------------------------------------------
%%% @doc pluto_event_log — Durable append-only event history.
%%%
%%% Writes JSON-line entries to disk asynchronously.  Supports log
%%% rotation by entry count and retention by age.  Agents can query
%%% recent events via the `event_history` protocol op.
%%%
%%% Events are kept in memory (bounded ring buffer) for fast queries,
%%% and flushed to disk in append mode for durability.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_event_log).
-behaviour(gen_server).

-include("pluto.hrl").

%% API
-export([start_link/0, log/2, query/2]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {
    dir           :: string(),
    max_entries   :: non_neg_integer(),
    fd            :: file:io_device() | undefined,
    file_entries  :: non_neg_integer(),
    seq           :: non_neg_integer(),     %% monotonic event sequence
    ring          :: list(),                %% recent events (newest first)
    ring_size     :: non_neg_integer(),
    ring_max      :: non_neg_integer()
}).

-define(RING_MAX, 10000).  %% Keep last 10k events in memory for queries
-define(ROTATION_CHECK_MS, 60000).

%%====================================================================
%% API
%%====================================================================

%% @doc Start the event log gen_server.
-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Log an event.  Non-blocking (gen_server:cast).
%% EventType is an atom like `lock_acquired`, `agent_registered`, etc.
%% Data is a map of event-specific fields.
-spec log(atom(), map()) -> ok.
log(EventType, Data) ->
    gen_server:cast(?MODULE, {log, EventType, Data}).

%% @doc Query events with sequence number > SinceSeq, up to Limit.
%% Returns a list of event maps in chronological order.
-spec query(non_neg_integer(), non_neg_integer()) -> [map()].
query(SinceSeq, Limit) ->
    gen_server:call(?MODULE, {query, SinceSeq, Limit}, 5000).

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    Dir        = pluto_config:get(event_log_dir, ?DEFAULT_EVENT_LOG_DIR),
    MaxEntries = pluto_config:get(event_log_max_entries, 100000),

    %% Ensure directory exists
    filelib:ensure_dir(filename:join(Dir, "events.jsonl")),

    %% Open (or create) the current log file in append mode
    Fd = open_log_file(Dir),

    erlang:send_after(?ROTATION_CHECK_MS, self(), rotation_check),
    ?LOG_INFO("pluto_event_log started (dir=~s, max_entries=~w)", [Dir, MaxEntries]),
    {ok, #state{
        dir          = Dir,
        max_entries  = MaxEntries,
        fd           = Fd,
        file_entries = 0,
        seq          = 0,
        ring         = [],
        ring_size    = 0,
        ring_max     = ?RING_MAX
    }}.

handle_call({query, SinceSeq, Limit}, _From, #state{ring = Ring} = State) ->
    %% Ring is stored newest-first; filter and reverse for chronological order
    Matching = lists:filter(fun(E) -> maps:get(<<"seq">>, E, 0) > SinceSeq end, Ring),
    Sorted = lists:reverse(Matching),
    Result = lists:sublist(Sorted, Limit),
    {reply, Result, State};

handle_call(_Request, _From, State) ->
    {reply, ok, State}.

handle_cast({log, EventType, Data}, State) ->
    {noreply, do_log(EventType, Data, State)};

handle_cast(_Msg, State) ->
    {noreply, State}.

handle_info(rotation_check, #state{dir = Dir, max_entries = Max,
                                    file_entries = Count, fd = Fd} = State) ->
    NewState = case Count >= Max of
        true ->
            %% Rotate: close current file, rename, open new
            file:close(Fd),
            rotate_file(Dir),
            NewFd = open_log_file(Dir),
            State#state{fd = NewFd, file_entries = 0};
        false ->
            State
    end,
    %% Clean up old rotated files
    cleanup_old_files(Dir),
    erlang:send_after(?ROTATION_CHECK_MS, self(), rotation_check),
    {noreply, NewState};

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, #state{fd = Fd}) ->
    case Fd of
        undefined -> ok;
        _         -> file:close(Fd)
    end,
    ok.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Write one event to disk and ring buffer.
do_log(EventType, Data, #state{seq = Seq, fd = Fd,
                                file_entries = FE,
                                ring = Ring, ring_size = RS,
                                ring_max = RM} = State) ->
    NewSeq = Seq + 1,
    Ts = erlang:system_time(millisecond),
    Event = Data#{
        <<"event">> => atom_to_binary(EventType, utf8),
        <<"ts">>    => Ts,
        <<"seq">>   => NewSeq
    },

    %% Write to disk
    case Fd of
        undefined -> ok;
        _ ->
            Line = pluto_protocol_json:encode_line(Event),
            file:write(Fd, Line)
    end,

    %% Update in-memory ring buffer
    NewRing = case RS >= RM of
        true  -> [Event | lists:sublist(Ring, RM - 1)];
        false -> [Event | Ring]
    end,
    NewRS = min(RS + 1, RM),

    State#state{
        seq          = NewSeq,
        file_entries = FE + 1,
        ring         = NewRing,
        ring_size    = NewRS
    }.

%% @private Open the current log file in append mode.
open_log_file(Dir) ->
    Path = filename:join(Dir, "events.jsonl"),
    case file:open(Path, [append, binary, {encoding, utf8}]) of
        {ok, Fd}        -> Fd;
        {error, Reason} ->
            ?LOG_ERROR("pluto_event_log: failed to open ~s: ~p", [Path, Reason]),
            undefined
    end.

%% @private Rotate the current log file by renaming with a timestamp.
rotate_file(Dir) ->
    Src = filename:join(Dir, "events.jsonl"),
    Ts  = calendar:system_time_to_rfc3339(erlang:system_time(second), [{offset, "Z"}]),
    %% Replace colons with dashes for filesystem safety
    SafeTs = lists:map(fun($:) -> $-; (C) -> C end, Ts),
    Dst = filename:join(Dir, "events-" ++ SafeTs ++ ".jsonl"),
    file:rename(Src, Dst).

%% @private Remove rotated log files older than retention period.
cleanup_old_files(Dir) ->
    RetentionDays = pluto_config:get(event_log_retention_days, 7),
    CutoffSec = erlang:system_time(second) - (RetentionDays * 86400),
    case file:list_dir(Dir) of
        {ok, Files} ->
            lists:foreach(fun(F) ->
                Path = filename:join(Dir, F),
                case filelib:last_modified(Path) of
                    0 -> ok;
                    ModTime ->
                        ModSec = calendar:datetime_to_gregorian_seconds(ModTime)
                                 - 62167219200,  %% Gregorian epoch offset to Unix
                        case ModSec < CutoffSec andalso F =/= "events.jsonl" of
                            true  -> file:delete(Path);
                            false -> ok
                        end
                end
            end, Files);
        _ ->
            ok
    end.
