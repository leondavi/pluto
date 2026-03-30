%%%-------------------------------------------------------------------
%%% @doc pluto_deadlock — Wait-for graph and cycle detection.
%%%
%%% This is a pure library module (no process).  It is called
%%% synchronously from within `pluto_lock_mgr`'s gen_server callbacks,
%%% which guarantees single-threaded access.
%%%
%%% The wait-for graph is stored in the ETS_WAIT_GRAPH table as
%%% directed edges: `{Waiter, Holder}`.  After each new edge is
%%% inserted, `check_cycle/1` runs a depth-first search from the
%%% new waiter.  If the DFS reaches the starting node again, a cycle
%%% exists.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_deadlock).

-include("pluto.hrl").

%% API
-export([add_edge/2, remove_edge/1, check_cycle/1]).

%%====================================================================
%% API
%%====================================================================

%% @doc Add a directed edge: `Waiter` is waiting for a resource held
%% by `Holder`.
-spec add_edge(binary(), binary()) -> ok.
add_edge(Waiter, Holder) ->
    ets:insert(?ETS_WAIT_GRAPH, {Waiter, Holder}),
    ok.

%% @doc Remove all outgoing edges from `Waiter` (called when a wait
%% entry is removed — either granted, timed out, or deadlock-aborted).
-spec remove_edge(binary()) -> ok.
remove_edge(Waiter) ->
    ets:delete(?ETS_WAIT_GRAPH, Waiter),
    ok.

%% @doc Check for a cycle starting from `StartNode`.
%%
%% Returns `no_cycle` or `{cycle, [agent_id()]}` with the list of
%% agents forming the cycle.
-spec check_cycle(binary()) -> no_cycle | {cycle, [binary()]}.
check_cycle(StartNode) ->
    dfs(StartNode, StartNode, [StartNode], sets:new()).

%%====================================================================
%% Internal — Depth-first search
%%====================================================================

%% @private Walk the graph forward from `Current`, looking for `Target`.
%% `Path` accumulates the cycle for reporting.  `Visited` prevents
%% infinite loops on non-target repeated nodes.
dfs(Target, Current, Path, Visited) ->
    %% Find all nodes that `Current` is waiting for
    Neighbours = [Holder || {_, Holder} <- ets:lookup(?ETS_WAIT_GRAPH, Current)],
    check_neighbours(Neighbours, Target, Path, Visited).

check_neighbours([], _Target, _Path, _Visited) ->
    no_cycle;
check_neighbours([Target | _Rest], Target, Path, _Visited) ->
    %% We have reached the start node — cycle detected!
    {cycle, lists:reverse(Path)};
check_neighbours([Node | Rest], Target, Path, Visited) ->
    case sets:is_element(Node, Visited) of
        true ->
            %% Already visited this node on a different branch — skip
            check_neighbours(Rest, Target, Path, Visited);
        false ->
            case dfs(Target, Node, [Node | Path], sets:add_element(Node, Visited)) of
                {cycle, _} = Cycle ->
                    Cycle;
                no_cycle ->
                    check_neighbours(Rest, Target, Path, Visited)
            end
    end.
