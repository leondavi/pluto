%%%-------------------------------------------------------------------
%%% @doc pluto_resource — Resource key normalization and validation.
%%%
%%% Every resource identifier in Pluto passes through `normalize/1`
%%% before any lookup or insertion.  This guarantees that
%%% `"file:/repo/a.txt"` and `<<"file:/repo/a.txt">>` always resolve
%%% to the same binary key.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_resource).

%% API
-export([normalize/1, validate/1]).

%%====================================================================
%% API
%%====================================================================

%% @doc Normalize a resource identifier to a trimmed binary.
%%
%% Accepts lists (strings), binaries, and atoms.
%% Strips leading and trailing ASCII whitespace.
%% Returns `{error, empty_resource}` if the result is empty.
-spec normalize(term()) -> {ok, binary()} | {error, empty_resource}.
normalize(Resource) when is_binary(Resource) ->
    check_empty(string:trim(Resource));
normalize(Resource) when is_list(Resource) ->
    normalize(list_to_binary(Resource));
normalize(Resource) when is_atom(Resource) ->
    normalize(atom_to_binary(Resource, utf8));
normalize(_) ->
    {error, empty_resource}.

%% @doc Validate that a resource identifier is non-empty after normalization.
-spec validate(term()) -> ok | {error, empty_resource}.
validate(Resource) ->
    case normalize(Resource) of
        {ok, _}          -> ok;
        {error, _} = Err -> Err
    end.

%%====================================================================
%% Internal
%%====================================================================

%% @private Return the binary if non-empty, otherwise an error.
check_empty(<<>>) -> {error, empty_resource};
check_empty(Bin)  -> {ok, Bin}.
