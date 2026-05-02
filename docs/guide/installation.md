# Installing Pluto

This guide covers installing Pluto and its dependencies on macOS, Debian,
or Ubuntu. If you are new to Erlang or multi-agent tooling, follow the
**Guided install** path. If you already have Erlang and Python set up,
jump to [Verify](#verify-the-install).

## Prerequisites

Pluto needs three things on the host:

| Tool | Version | Used by |
|-|-|-|
| **Erlang/OTP** | 26 or newer (28 recommended) | The Pluto server |
| **rebar3** | 3.20+ | Building the server |
| **Python** | 3.10+ | The Pluto client and `PlutoAgentFriend` wrapper |

Optional, only if you plan to wrap a particular agent:

- `claude` (Anthropic Claude Code), `aider`, `cursor`, or `copilot` CLI on `PATH`.

## Guided install (recommended)

Pluto ships a one-shot installer that detects your OS, asks before each
step, and verifies the result. From the repository root:

```bash
./PlutoInstall.sh            # interactive
./PlutoInstall.sh --yes      # non-interactive (auto-accept all prompts)
./PlutoInstall.sh --check    # print what is installed / missing, then exit
```

The installer:

1. Detects macOS (Homebrew) or Debian/Ubuntu (apt) and installs Erlang/OTP.
2. Installs `rebar3` (via Homebrew, apt, or a single `curl | bash` if neither has it).
3. Verifies Python 3.10+ is on `PATH`.
4. Offers to build the Pluto release immediately via `./PlutoServer.sh --build`.

If a step fails, the installer prints the exact command it tried so you
can retry manually. None of the steps modify your shell profile or `PATH`
without asking first.

## Manual install

If you prefer to install dependencies yourself:

### macOS (Homebrew)

```bash
brew install erlang rebar3 python@3.12
```

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install erlang erlang-dev python3 python3-pip
# rebar3 from upstream (apt's version may be old)
curl -fsSL https://s3.amazonaws.com/rebar3/rebar3 -o /usr/local/bin/rebar3
sudo chmod +x /usr/local/bin/rebar3
```

### Other Linux / from source

Build Erlang from source via [kerl](https://github.com/kerl/kerl) or
[asdf](https://asdf-vm.com/), then install rebar3 with the `curl` command
above. Python is available from your package manager or via
[pyenv](https://github.com/pyenv/pyenv).

## Build the Pluto server

After dependencies are in place:

```bash
./PlutoServer.sh --build
```

This compiles every Erlang module and assembles a release under
`/tmp/pluto/build`. The first build downloads no dependencies (Pluto
has none beyond the OTP standard library); subsequent builds are
incremental.

## Verify the install

Three checks confirm the install is healthy:

```bash
# 1. Server starts and responds
./PlutoServer.sh --daemon
./PlutoClient.sh ping
# Expected: {"status":"pong","ts":...}

# 2. Server reports the version you just built
./PlutoServer.sh --status
# Expected: STATUS: ONLINE, Version: 0.2.8 (or current)

# 3. Tests pass
python3 -m pytest tests/test_agent_friend.py
# Expected: 42 passed, 1 skipped
```

If any of these fail, see [Troubleshooting](#troubleshooting) below.

## Configure ports and host

All Pluto components read network settings from
`config/pluto_config.json`:

```json
{
  "pluto_server": {
    "host_ip": "127.0.0.1",
    "host_tcp_port": 9200,
    "host_http_port": 9201
  }
}
```

The defaults bind to localhost only. If you want Pluto reachable on the
LAN, set `host_ip` to your interface address — but read
[CONSENT.md](../../CONSENT.md) first; an open Pluto port lets any client
inject prompts into the agents you've wrapped.

Environment overrides take precedence over the config file:

- `PLUTO_HOST`, `PLUTO_PORT`, `PLUTO_HTTP_PORT`

## Troubleshooting

**`rebar3: command not found`** — the installer tried to add it to
`/usr/local/bin`; check it's on your `PATH`. On macOS with Homebrew,
`brew install rebar3` is the most reliable form.

**`Erlang/OTP version too old`** — Pluto needs OTP 26 or newer for the
binary patterns and JSON parsing it uses. Distros sometimes ship 24/25;
in that case install via Homebrew/kerl/asdf.

**`./PlutoServer.sh --daemon` hangs** — almost always a stale EPMD
registration from a previous beam crash. Run `./PlutoServer.sh --kill`,
which now detects this case explicitly (since v0.2.7) and clears the
stale entry.

**`./PlutoServer.sh --status` shows the wrong version** — the build
directory holds a release dir per version (`releases/0.2.6/`,
`releases/0.2.8/`, …). v0.2.7+ auto-cleans on version mismatch; if you
hit it on an older checkout, run `./PlutoServer.sh --clean` then rebuild.

**Port already in use** — change `host_tcp_port` / `host_http_port` in
`config/pluto_config.json`, or set `PLUTO_PORT` / `PLUTO_HTTP_PORT` in
your environment, then restart.

**Python import errors** — the client lives in `src_py/`; run from the
repo root or set `PYTHONPATH=src_py`. The `PlutoClient.sh` /
`PlutoAgentFriend.sh` wrappers handle this for you.

## What to read next

- [Pluto Usage Guide](index.md) — overview of every way to talk to Pluto.
- [PlutoMCPFriend](pluto-mcp-friend.md) — recommended for Claude Code users.
- [PlutoAgentFriend](pluto-agent-friend.md) — wraps any TUI agent CLI.
- [PlutoClient](pluto-client.md) — inspection and operations CLI.
- [TCP / Python protocol](tcp-connection.md) — wire-level reference.
