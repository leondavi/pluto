v0.2.8

## Locations to update when bumping the version

| File | Field | Notes |
|---|---|---|
| `VERSION.md` | first line | canonical version string |
| `src_erl/include/pluto.hrl` | `-define(VERSION, "x.y.z").` | served by HTTP `/health` and `/ping`; checked by `PlutoAgentFriend.sh` client-vs-server mismatch guard |
| `src_erl/src/pluto.app.src` | `{vsn, "x.y.z"},` | OTP application version |
| `src_erl/rebar.config` | `{release, {pluto, "x.y.z"}, [pluto]},` | rebar3 release assembly name |

After editing all four, run:

```bash
./PlutoServer.sh --kill && ./PlutoServer.sh --clean && ./PlutoServer.sh --daemon
./PlutoServer.sh --status   # confirm Version: x.y.z
```

