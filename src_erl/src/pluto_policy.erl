%%%-------------------------------------------------------------------
%%% @doc pluto_policy — Authentication and ACL enforcement.
%%%
%%% Checks bearer tokens on register and resource ACLs on acquire.
%%% Rules are loaded from sys.config.  When no agent_tokens or
%%% acl rules are configured, all operations are permitted (open mode).
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_policy).

-include("pluto.hrl").

%% API
-export([check_auth/2, check_acl/3]).

%%====================================================================
%% API
%%====================================================================

%% @doc Check bearer token authentication for an agent.
%% Returns `ok` if auth passes, `{error, unauthorized}` otherwise.
%% When no agent_tokens are configured, all agents are allowed.
-spec check_auth(binary(), binary() | undefined) -> ok | {error, unauthorized}.
check_auth(AgentId, Token) ->
    case pluto_config:get(agent_tokens, undefined) of
        undefined ->
            %% No authentication configured — open mode
            ok;
        Tokens when is_map(Tokens) ->
            case maps:find(AgentId, Tokens) of
                {ok, Expected} ->
                    case Token =:= Expected of
                        true  -> ok;
                        false -> {error, unauthorized}
                    end;
                error ->
                    %% Agent not in token map — check wildcard
                    case maps:find(<<"*">>, Tokens) of
                        {ok, Expected} ->
                            case Token =:= Expected of
                                true  -> ok;
                                false -> {error, unauthorized}
                            end;
                        error ->
                            %% No token configured for this agent — deny
                            {error, unauthorized}
                    end
            end
    end.

%% @doc Check ACL rules for a resource access request.
%% Returns `ok` if access is permitted, `{error, unauthorized}` otherwise.
%% When no ACL rules are configured, all access is allowed.
-spec check_acl(binary(), binary(), write | read) -> ok | {error, unauthorized}.
check_acl(AgentId, Resource, Mode) ->
    case pluto_config:get(acl, undefined) of
        undefined ->
            ok;
        [] ->
            ok;
        Rules when is_list(Rules) ->
            check_rules(AgentId, Resource, Mode, Rules)
    end.

%%====================================================================
%% Internal functions
%%====================================================================

%% @private Walk ACL rules looking for a matching allow.
%% Rules are {AgentPattern, ResourcePrefix, AllowedModes}.
check_rules(_AgentId, _Resource, _Mode, []) ->
    %% No matching rule found — deny
    {error, unauthorized};
check_rules(AgentId, Resource, Mode, [{AgentPat, ResPrefixRaw, Modes} | Rest]) ->
    ResPrefix = ensure_binary(ResPrefixRaw),
    case matches_agent(AgentId, ensure_binary(AgentPat)) andalso
         matches_resource(Resource, ResPrefix) andalso
         lists:member(Mode, Modes) of
        true  -> ok;
        false -> check_rules(AgentId, Resource, Mode, Rest)
    end;
check_rules(AgentId, Resource, Mode, [_ | Rest]) ->
    %% Skip malformed rules
    check_rules(AgentId, Resource, Mode, Rest).

%% @private Check if an agent_id matches a pattern.
%% Supports wildcard `*` at the end (e.g., "coder-*" matches "coder-1").
matches_agent(_AgentId, <<"*">>) ->
    true;
matches_agent(AgentId, Pattern) ->
    case binary:last(Pattern) of
        $* ->
            Prefix = binary:part(Pattern, 0, byte_size(Pattern) - 1),
            binary:match(AgentId, Prefix) =:= {0, byte_size(Prefix)};
        _ ->
            AgentId =:= Pattern
    end.

%% @private Check if a resource matches a prefix pattern.
%% Supports wildcard `*` at the end.
matches_resource(_Resource, <<"*">>) ->
    true;
matches_resource(Resource, Prefix) ->
    case binary:last(Prefix) of
        $* ->
            P = binary:part(Prefix, 0, byte_size(Prefix) - 1),
            binary:match(Resource, P) =:= {0, byte_size(P)};
        _ ->
            %% Exact prefix match
            PLen = byte_size(Prefix),
            case Resource of
                <<Prefix:PLen/binary, _/binary>> -> true;
                _ -> false
            end
    end.

%% @private Ensure a term is a binary.
ensure_binary(B) when is_binary(B) -> B;
ensure_binary(L) when is_list(L)   -> list_to_binary(L);
ensure_binary(A) when is_atom(A)   -> atom_to_binary(A, utf8).
