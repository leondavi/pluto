%%%-------------------------------------------------------------------
%%% @doc pluto_heartbeat — Session liveness sweeper.
%%%
%%% Periodically scans the ETS_LIVENESS table and terminates sessions
%%% that have not sent any message (including pings) within the
%%% configured timeout.  Dead sessions enter the reconnect grace
%%% period managed by pluto_msg_hub.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_heartbeat).
-behaviour(gen_server).

-include("pluto.hrl").

%% API
-export([start_link/0, touch/1]).

%% gen_server callbacks
-export([init/1, handle_call/3, handle_cast/2, handle_info/2,
         terminate/2]).

-record(state, {
    sweep_ms   :: non_neg_integer(),
    timeout_ms :: non_neg_integer()
}).

%%====================================================================
%% API
%%====================================================================

%% @doc Start and register the heartbeat sweeper.
-spec start_link() -> {ok, pid()} | {error, term()}.
start_link() ->
    gen_server:start_link({local, ?MODULE}, ?MODULE, [], []).

%% @doc Update the liveness timestamp for a session.
%% Called by pluto_session on every received message.
-spec touch(binary()) -> ok.
touch(SessionId) ->
    ets:insert(?ETS_LIVENESS, {SessionId, pluto_lease:now_ms()}),
    ok.

%%====================================================================
%% gen_server callbacks
%%====================================================================

init([]) ->
    SweepMs   = pluto_config:get(heartbeat_sweep_ms,  ?DEFAULT_HEARTBEAT_SWEEP_MS),
    TimeoutMs = pluto_config:get(heartbeat_timeout_ms, ?DEFAULT_HEARTBEAT_TIMEOUT_MS),
    erlang:send_after(SweepMs, self(), sweep),
    ?LOG_INFO("pluto_heartbeat started (sweep=~wms, timeout=~wms)",
              [SweepMs, TimeoutMs]),
    {ok, #state{sweep_ms = SweepMs, timeout_ms = TimeoutMs}}.

handle_call(_Request, _From, State) ->
    {reply, ok, State}.

handle_cast(_Msg, State) ->
    {noreply, State}.

%% ── Periodic sweep ──────────────────────────────────────────────────
handle_info(sweep, #state{sweep_ms = SweepMs, timeout_ms = TimeoutMs} = State) ->
    Now = pluto_lease:now_ms(),
    AllEntries = ets:tab2list(?ETS_LIVENESS),

    lists:foreach(fun({SessionId, LastSeen}) ->
        case Now - LastSeen > TimeoutMs of
            true ->
                ?LOG_WARN("Session ~s heartbeat timeout -- terminating",
                          [SessionId]),
                %% Look up the session to find the session PID
                case ets:lookup(?ETS_SESSIONS, SessionId) of
                    [#session{session_pid = Pid, agent_id = AgentId}] ->
                        %% Kill the session process; its terminate/2 will
                        %% call pluto_msg_hub:unregister_agent/1
                        case is_pid(Pid) andalso is_process_alive(Pid) of
                            true  -> exit(Pid, heartbeat_timeout);
                            false -> ok
                        end,
                        %% Clean up liveness entry
                        ets:delete(?ETS_LIVENESS, SessionId),
                        %% If agent_id isn't handled by session cleanup
                        case AgentId of
                            undefined -> ok;
                            _         -> pluto_msg_hub:unregister_agent(AgentId)
                        end;
                    [] ->
                        %% Stale liveness entry — clean it up
                        ets:delete(?ETS_LIVENESS, SessionId)
                end;
            false ->
                ok
        end
    end, AllEntries),

    %% Also sweep HTTP sessions with per-session TTL
    sweep_http_sessions(Now),

    erlang:send_after(SweepMs, self(), sweep),
    {noreply, State};

handle_info(_Info, State) ->
    {noreply, State}.

terminate(_Reason, _State) ->
    ok.

%%====================================================================
%% Internal
%%====================================================================

%% @private Sweep HTTP sessions that have exceeded their individual TTL.
sweep_http_sessions(Now) ->
    AllHttpSessions = ets:tab2list(?ETS_HTTP_SESSIONS),
    lists:foreach(fun(#http_session{token = Token, agent_id = AgentId,
                                     session_id = SessId, ttl_ms = TtlMs,
                                     last_seen = LastSeen}) ->
        case Now - LastSeen > TtlMs of
            true ->
                ?LOG_WARN("HTTP session ~s (agent ~s) expired (TTL ~wms)",
                          [SessId, AgentId, TtlMs]),
                ets:delete(?ETS_HTTP_SESSIONS, Token),
                ets:delete(?ETS_SESSIONS, SessId),
                pluto_msg_hub:unregister_agent(AgentId);
            false ->
                ok
        end
    end, AllHttpSessions).
