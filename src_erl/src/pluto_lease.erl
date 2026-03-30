%%%-------------------------------------------------------------------
%%% @doc pluto_lease — TTL and expiry utilities.
%%%
%%% All times use `erlang:monotonic_time(millisecond)` so they are
%%% immune to wall-clock adjustments.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_lease).

%% API
-export([make_expires_at/1, is_expired/1, remaining_ms/1, now_ms/0]).

%%====================================================================
%% API
%%====================================================================

%% @doc Return the current monotonic time in milliseconds.
-spec now_ms() -> integer().
now_ms() ->
    erlang:monotonic_time(millisecond).

%% @doc Compute an absolute expiry timestamp from a TTL in milliseconds.
%%
%% Example:
%%   ExpiresAt = pluto_lease:make_expires_at(30000).
-spec make_expires_at(non_neg_integer()) -> integer().
make_expires_at(TtlMs) when is_integer(TtlMs), TtlMs > 0 ->
    now_ms() + TtlMs.

%% @doc Check whether a given expiry timestamp has passed.
-spec is_expired(integer()) -> boolean().
is_expired(ExpiresAt) ->
    now_ms() >= ExpiresAt.

%% @doc Return milliseconds remaining until the deadline.
%% Negative values mean the deadline has already passed.
-spec remaining_ms(integer()) -> integer().
remaining_ms(ExpiresAt) ->
    ExpiresAt - now_ms().
