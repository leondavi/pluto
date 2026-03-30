%%%-------------------------------------------------------------------
%%% @doc pluto_protocol_json — JSON wire protocol codec.
%%%
%%% Decodes incoming newline-delimited JSON into Erlang maps and
%%% encodes Erlang maps back into JSON lines for transmission.
%%%
%%% Uses the built-in `json` module available in OTP 27+.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_protocol_json).

%% API
-export([decode/1, encode/1, encode_line/1]).

%%====================================================================
%% API
%%====================================================================

%% @doc Decode a JSON binary (one line, no trailing newline) into an
%% Erlang map.  Returns `{ok, Map}` or `{error, Reason}`.
-spec decode(binary()) -> {ok, map()} | {error, term()}.
decode(Line) when is_binary(Line) ->
    try
        {ok, json:decode(Line)}
    catch
        _:Reason ->
            {error, {json_decode, Reason}}
    end.

%% @doc Encode an Erlang map into a JSON binary (no trailing newline).
-spec encode(map()) -> binary().
encode(Map) when is_map(Map) ->
    iolist_to_binary(json:encode(Map)).

%% @doc Encode an Erlang map into a JSON binary followed by `\n`.
%% Ready to be sent directly over the TCP socket.
-spec encode_line(map()) -> binary().
encode_line(Map) ->
    <<(encode(Map))/binary, $\n>>.
