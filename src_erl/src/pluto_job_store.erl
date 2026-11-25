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

-export([home/0, jobs_dir/0, dir/1, file/2, valid_id/1, new_id/0, origin/0,
         ensure_home/0, key/0, host_id/0, lock_home/0,
         read_json/1, write_json/2, write_json/3, append_jsonl/2, read_jsonl/1,
         run/2, run/4, kill_port/1, python/0, script/1, ident/1, hex/1, opt/2, opt/3, now_ms/0]).

-ifdef(TEST).
-define(DEFAULT_HOME, "/tmp/pluto/test_smartstreams").
-else.
-define(DEFAULT_HOME, filename:join([os:getenv("HOME", "/tmp"), ".pluto",
                                     "smartstreams"])).
-endif.

-spec now_ms() -> integer().
now_ms() -> erlang:system_time(millisecond).

%% @doc Root of all SmartStreams data. Clients resolve it the same way
%% (pluto_sdk/jobs.py).
-spec home() -> file:filename().
home() ->
    case os:getenv("PLUTO_SMARTSTREAMS_HOME") of
        Dir when is_list(Dir), Dir =/= "" -> Dir;
        _ -> pluto_config:get(smartstreams_home, ?DEFAULT_HOME)
    end.

-spec jobs_dir() -> file:filename().
jobs_dir() -> filename:join(home(), ?HOME_DIR_JOBS).

%% @doc Directory of a job; the id must have passed valid_id/1.
-spec dir(job_id()) -> file:filename().
dir(Id) -> filename:join(jobs_dir(), binary_to_list(Id)).

-spec file(job_id(), file:filename()) -> file:filename().
file(Id, Name) -> filename:join(dir(Id), Name).

%% Ids become path components, so nothing else may ever pass.
-spec valid_id(term()) -> boolean().
valid_id(Id) when is_binary(Id) ->
    re:run(Id, ?JOB_ID_RE, [{capture, none}]) =:= match;
valid_id(_) -> false.

-spec new_id() -> job_id().
new_id() ->
    {{Y, M, D}, _} = calendar:universal_time(),
    Hex = hex(crypto:strong_rand_bytes(8)),
    iolist_to_binary(io_lib:format("J-~4..0w~2..0w~2..0w-~s", [Y, M, D, Hex])).

%% @doc Where a package was created.
-spec origin() -> map().
origin() ->
    {ok, Host} = inet:gethostname(),
    {_, Os} = os:type(),
    #{<<"host_id">> => host_id(), <<"hostname">> => list_to_binary(Host),
      <<"os">> => atom_to_binary(Os),
      <<"arch">> => list_to_binary(erlang:system_info(system_architecture))}.

%%====================================================================
%% Home, key, single-server lock
%%====================================================================

%% @doc Create directories, key and host id where missing.
-spec ensure_home() -> ok | {error, term()}.
ensure_home() ->
    Jobs = jobs_dir(),
    case filelib:ensure_path(Jobs) of
        ok ->
            _ = file:change_mode(home(), ?MODE_DIR),
            _ = file:change_mode(Jobs, ?MODE_DIR),
            case ensure_secret(?HOME_FILE_KEY, ?JOB_KEY_BYTES) of
                ok -> ensure_secret(?HOME_FILE_HOST_ID, 8);
                E  -> E
            end;
        E -> E
    end.

%% An empty file is what a power loss may leave of a new one.
ensure_secret(Name, Bytes) ->
    Path = filename:join(home(), Name),
    case file:read_link_info(Path) of
        {ok, #file_info{type = regular, size = 0}} -> write_secret(Path, Bytes);
        {error, enoent} -> write_secret(Path, Bytes);
        _ -> ok
    end.

%% The mode is set before the contents exist, so nobody can read them in
%% between.
write_secret(Path, Bytes) ->
    Tmp = Path ++ ".tmp",
    case file:write_file(Tmp, <<>>) of
        ok ->
            _ = file:change_mode(Tmp, ?MODE_FILE),
            replace(Tmp, Path, hex(crypto:strong_rand_bytes(Bytes)), true);
        E -> E
    end.

%% @doc The bearer key; refused when others could have read it.
-spec key() -> {ok, binary()} | {error, atom()}.
key() ->
    Path = filename:join(home(), ?HOME_FILE_KEY),
    case file:read_link_info(Path) of
        {ok, #file_info{type = regular, mode = Mode}}
          when Mode band 8#077 =:= 0 ->
            {ok, Bin} = file:read_file(Path),
            case string:trim(Bin) of
                K when byte_size(K) >= ?JOB_KEY_MIN -> {ok, K};
                _ -> {error, key_too_short}
            end;
        {ok, #file_info{type = regular}} -> {error, key_permissions_too_open};
        {ok, _}    -> {error, key_not_a_regular_file};
        {error, R} -> {error, R}
    end.

-spec host_id() -> binary().
host_id() ->
    case file:read_file(filename:join(home(), ?HOME_FILE_HOST_ID)) of
        {ok, B} -> string:trim(B);
        _       -> <<"unknown">>
    end.

%% @doc Two servers on one home would both run the same queued jobs.
%% "Cannot tell" counts as in use.
-spec lock_home() -> ok | {error, term()}.
lock_home() ->
    Path = filename:join(home(), ?HOME_FILE_LOCK),
    Me = list_to_binary(os:getpid()),
    case ident(Me) of
        unknown -> {error, python3_not_usable};
        {ok, MyIdent} ->
            Lock = #{<<"pid">> => Me, <<"ident">> => MyIdent},
            case read_json(Path) of
                {ok, #{<<"pid">> := Pid, <<"ident">> := Ident}} when Pid =/= Me,
                                                                     Ident =/= <<>> ->
                    case ident(Pid) of
                        {ok, Ident} -> {error, home_in_use_by_another_server};
                        {ok, _}     -> write_json(Path, Lock, sync);
                        unknown     -> {error, cannot_verify_home_lock}
                    end;
                _ -> write_json(Path, Lock, sync)
            end
    end.

%%====================================================================
%% JSON files
%%====================================================================

-spec read_json(file:filename_all()) -> {ok, term()} | {error, term()}.
read_json(Path) ->
    case file:read_file(Path) of
        {ok, Bin} -> pluto_protocol_json:decode(Bin);
        Err       -> Err
    end.

%% @doc Replace a JSON file atomically. With `sync' it also survives a
%% power loss, at the price of an fsync.
-spec write_json(file:filename_all(), map()) -> ok | {error, term()}.
write_json(Path, Map) -> write_json(Path, Map, nosync).

-spec write_json(file:filename_all(), map(), sync | nosync) -> ok | {error, term()}.
write_json(Path, Map, Sync) ->
    replace(iolist_to_binary([Path, ".tmp"]), Path,
            pluto_protocol_json:encode(Map), Sync =:= sync).

replace(Tmp, Path, Data, Sync) ->
    case file:open(Tmp, [write, raw, binary]) of
        {ok, Fd} ->
            Res = case file:write(Fd, Data) of
                      ok when Sync -> file:sync(Fd);
                      R            -> R
                  end,
            _ = file:close(Fd),
            case Res of
                ok -> _ = file:change_mode(Tmp, ?MODE_FILE), file:rename(Tmp, Path);
                E  -> E
            end;
        E -> E
    end.

-spec append_jsonl(file:filename_all(), map()) -> ok | {error, term()}.
append_jsonl(Path, Map) ->
    file:write_file(Path, pluto_protocol_json:encode_line(Map), [append]).

%% @doc Objects of a JSON-lines file. Unparseable lines (a torn write at
%% power loss) are skipped.
-spec read_jsonl(file:filename_all()) -> [map()].
read_jsonl(Path) ->
    case file:read_file(Path) of
        {ok, Bin} ->
            lists:filtermap(
              fun(L) ->
                  case pluto_protocol_json:decode(L) of
                      {ok, M} when is_map(M) -> {true, M};
                      _ -> false
                  end
              end, binary:split(Bin, <<"\n">>, [global, trim_all]));
        _ -> []
    end.

%%====================================================================
%% Bundled Python scripts
%%====================================================================

%% @doc Run a bundled script with a short time limit and no input.
-spec run(file:filename(), [iodata()]) -> {ok, integer(), binary()} | {error, term()}.
run(Script, Args) -> run(Script, Args, <<>>, ?JOB_SCRIPT_MS).

%% @doc Run a bundled script, feeding Input to its stdin. -I keeps the
%% user's environment and site-packages out; -B avoids writing bytecode
%% next to the scripts. {ok, ExitStatus, Stdout} or {error, Why}.
-spec run(file:filename(), [iodata()], binary(), pos_integer()) ->
    {ok, integer(), binary()} | {error, term()}.
run(Script, Args, Input, Timeout) ->
    case python() of
        false -> {error, python3_not_found};
        Py ->
            try open_port({spawn_executable, Py},
                          [{args, script(Script) ++ Args},
                           binary, exit_status, use_stdio, hide]) of
                Port ->
                    Input =:= <<>> orelse port_command(Port, Input),
                    collect(Port, <<>>, erlang:monotonic_time(millisecond) + Timeout)
            catch error:R -> {error, R}
            end
    end.

%% @doc The interpreter for the bundled scripts.
-spec python() -> file:filename_all() | false.
python() ->
    case pluto_config:get(smartstreams_python, undefined) of
        undefined -> os:find_executable("python3");
        Path      -> Path
    end.

%% @doc Interpreter arguments that run a bundled script.
-spec script(file:filename()) -> [string()].
script(Name) -> ["-I", "-B", filename:join(code:priv_dir(pluto), Name)].

collect(Port, Acc, Deadline) ->
    Left = max(0, Deadline - erlang:monotonic_time(millisecond)),
    receive
        {Port, {data, D}}        -> collect(Port, <<Acc/binary, D/binary>>, Deadline);
        {Port, {exit_status, S}} -> {ok, S, string:trim(Acc)}
    after Left ->
        catch port_close(Port),
        {error, timeout}
    end.

%% @doc Identity of a pid (changes when the pid is reused), <<>> if it is
%% not alive, or unknown if the helper did not work.
-spec ident(integer() | binary()) -> {ok, binary()} | unknown.
ident(Pid) when is_integer(Pid) -> ident(integer_to_binary(Pid));
ident(Pid) ->
    case run(?JOB_RUNNER_SCRIPT, ["ident", Pid]) of
        {ok, 0, <<"ident:", I/binary>>} -> {ok, I};
        _ -> unknown
    end.

-spec hex(binary()) -> binary().
hex(B) -> string:lowercase(binary:encode_hex(B)).

%% @doc Map lookup where JSON null and a missing key mean the same.
-spec opt(term(), map()) -> term().
opt(K, M) -> opt(K, M, undefined).
-spec opt(term(), map(), term()) -> term().
opt(K, M, Default) ->
    case maps:get(K, M, null) of
        null -> Default;
        V    -> V
    end.

%% @doc Kill the process behind a port. The port is still open, so the
%% process cannot have been reaped and the pid is still its own.
-spec kill_port(port()) -> ok.
kill_port(Port) ->
    _ = case erlang:port_info(Port, os_pid) of
            {os_pid, Pid} -> os:cmd("kill -9 " ++ integer_to_list(Pid));
            undefined     -> ok
        end,
    catch port_close(Port),
    ok.
