%%%-------------------------------------------------------------------
%%% @doc pluto_job_store — On-disk layout of PlutoSmartStreams packages.
%%%
%%% Owns paths, atomic JSON files, the per-user key, and the helper that
%%% calls the bundled Python scripts. Holds no process state.
%%% @end
%%%-------------------------------------------------------------------
-module(pluto_job_store).

-include("pluto.hrl").
-include_lib("kernel/include/file.hrl").

-export([home/0, jobs_dir/0, dir/1, file/2, valid_id/1, new_id/0,
         ensure_home/0, key/0, host_id/0, lock_home/0,
         read_json/1, write_json/2, append_jsonl/2, read_jsonl/1,
         python/0, script/1, run/2, ident/1, opt/2, opt/3, now_ms/0]).

-ifdef(TEST).
-define(DEFAULT_HOME, "/tmp/pluto/test_smartstreams").
-else.
-define(DEFAULT_HOME, filename:join([os:getenv("HOME", "/tmp"), ".pluto",
                                     "smartstreams"])).
-endif.

now_ms() -> erlang:system_time(millisecond).

home() ->
    case os:getenv("PLUTO_SMARTSTREAMS_HOME") of
        Dir when is_list(Dir), Dir =/= "" -> Dir;
        _ -> pluto_config:get(smartstreams_home, ?DEFAULT_HOME)
    end.

jobs_dir() -> filename:join(home(), "jobs").
dir(Id)    -> filename:join(jobs_dir(), binary_to_list(Id)).
file(Id, Name) -> filename:join(dir(Id), Name).

%% Ids become path components, so nothing else may ever pass.
valid_id(Id) when is_binary(Id) ->
    re:run(Id, "\\AJ-[0-9]{8}-[0-9a-f]{16}\\z", [{capture, none}]) =:= match;
valid_id(_) -> false.

new_id() ->
    {{Y, M, D}, _} = calendar:universal_time(),
    Hex = string:lowercase(binary:encode_hex(crypto:strong_rand_bytes(8))),
    iolist_to_binary(io_lib:format("J-~4..0w~2..0w~2..0w-~s", [Y, M, D, Hex])).

%%====================================================================
%% Home, key, single-server lock
%%====================================================================

ensure_home() ->
    Jobs = jobs_dir(),
    case filelib:ensure_path(Jobs) of
        ok ->
            _ = file:change_mode(home(), 8#700),
            _ = file:change_mode(Jobs, 8#700),
            ensure_secret("key", 32),
            ensure_secret("host_id", 8),
            ok;
        {error, R} -> {error, R}
    end.

ensure_secret(Name, Bytes) ->
    Path = filename:join(home(), Name),
    case file:read_link_info(Path) of
        {error, enoent} ->
            Hex = string:lowercase(
                    binary:encode_hex(crypto:strong_rand_bytes(Bytes))),
            Tmp = Path ++ ".tmp",
            ok = file:write_file(Tmp, <<>>),
            ok = file:change_mode(Tmp, 8#600),
            ok = file:write_file(Tmp, Hex),
            ok = file:rename(Tmp, Path);
        _ -> ok
    end.

%% @doc The bearer key; refused when others could have read it.
key() ->
    Path = filename:join(home(), "key"),
    case file:read_link_info(Path) of
        {ok, #file_info{type = regular, mode = Mode}}
          when Mode band 8#077 =:= 0 ->
            {ok, Bin} = file:read_file(Path),
            case string:trim(Bin) of
                K when byte_size(K) >= 32 -> {ok, K};
                _ -> {error, key_too_short}
            end;
        {ok, #file_info{type = regular}} -> {error, key_permissions_too_open};
        {ok, _}    -> {error, key_not_a_regular_file};
        {error, R} -> {error, R}
    end.

host_id() ->
    case file:read_file(filename:join(home(), "host_id")) of
        {ok, B} -> string:trim(B);
        _       -> <<"unknown">>
    end.

%% @doc Two servers on one home would both run the same queued jobs.
lock_home() ->
    Path = filename:join(home(), "server.lock"),
    Me = os:getpid(),
    case read_json(Path) of
        {ok, #{<<"pid">> := Pid, <<"ident">> := Ident}} ->
            case Pid =:= list_to_binary(Me) orelse ident(Pid) =/= Ident
                 orelse Ident =:= <<>> of
                true  -> take_lock(Path, Me);
                false -> {error, home_in_use_by_another_server}
            end;
        _ -> take_lock(Path, Me)
    end.

take_lock(Path, Me) ->
    Pid = list_to_binary(Me),
    write_json(Path, #{<<"pid">> => Pid, <<"ident">> => ident(Pid)}).

%%====================================================================
%% JSON files
%%====================================================================

read_json(Path) ->
    case file:read_file(Path) of
        {ok, Bin} -> pluto_protocol_json:decode(Bin);
        Err       -> Err
    end.

write_json(Path, Map) ->
    Tmp = Path ++ ".tmp",
    case file:write_file(Tmp, pluto_protocol_json:encode(Map)) of
        ok  -> _ = file:change_mode(Tmp, 8#600), file:rename(Tmp, Path);
        Err -> Err
    end.

append_jsonl(Path, Map) ->
    file:write_file(Path, pluto_protocol_json:encode_line(Map), [append]).

%% Unparseable lines (a torn write at power loss) are skipped.
read_jsonl(Path) ->
    case file:read_file(Path) of
        {ok, Bin} ->
            lists:filtermap(
              fun(L) ->
                  case pluto_protocol_json:decode(L) of
                      {ok, M} -> {true, M};
                      _       -> false
                  end
              end, binary:split(Bin, <<"\n">>, [global, trim_all]));
        _ -> []
    end.

%%====================================================================
%% Bundled Python scripts
%%====================================================================

python() ->
    case pluto_config:get(smartstreams_python, undefined) of
        undefined ->
            case os:find_executable("python3") of
                false -> {error, python3_not_found};
                Path  -> {ok, Path}
            end;
        Path -> {ok, Path}
    end.

script(Name) -> filename:join(code:priv_dir(pluto), Name).

%% @doc Run a bundled script to completion; {ExitStatus, Stdout}.
run(Script, Args) ->
    case python() of
        {ok, Py} ->
            Port = open_port({spawn_executable, Py},
                             [{args, ["-I", script(Script) | Args]},
                              binary, exit_status, use_stdio, hide]),
            collect(Port, <<>>);
        {error, R} -> {127, atom_to_binary(R)}
    end.

collect(Port, Acc) ->
    receive
        {Port, {data, D}}        -> collect(Port, <<Acc/binary, D/binary>>);
        {Port, {exit_status, S}} -> {S, string:trim(Acc)}
    end.

%% @doc Identity string of a live pid, <<>> when dead.
ident(Pid) when is_integer(Pid) -> ident(integer_to_binary(Pid));
ident(Pid) ->
    {_, Out} = run("pluto_job_runner.py", ["ident", Pid]),
    Out.

%% JSON null and a missing key mean the same thing.
opt(K, M) -> opt(K, M, undefined).
opt(K, M, Default) ->
    case maps:get(K, M, null) of
        null -> Default;
        V    -> V
    end.
