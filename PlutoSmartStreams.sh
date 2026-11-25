#!/usr/bin/env bash
# ===========================================================================
# PlutoSmartStreams.sh — durable job packages over Pluto, for Claude Code and
# GitHub Copilot CLI. Run without arguments for a short wizard; --help lists
# every option. Guide: docs/guide/pluto-smart-streams.md
# ===========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="${SCRIPT_DIR}/src_py/agent_smart_streams/smart_streams.py"
PLUTO_ENTRY="${SCRIPT_DIR}/src_py/agent_mcp_friend/pluto_mcp_friend.py"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
CONFIG_FILE="${SCRIPT_DIR}/config/pluto_config.json"
VENV_DIR="${PLUTO_VENV:-/tmp/pluto/.venv}"
PY="${VENV_DIR}/bin/python"
MCP_DIR="${PLUTO_STATE_DIR:-/tmp/pluto}/mcp"
VERSION="$(head -1 "${SCRIPT_DIR}/VERSION.md" | tr -d '[:space:]')"

# Everything goes to stderr: --export may be writing an archive to stdout.
info() { printf '\033[0;36m[smartstreams]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[0;33m[smartstreams]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[smartstreams]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
PlutoSmartStreams ${VERSION} — jobs with durable output, byte cursors and
server-side grep, as MCP tools.

Launch an agent with the tools ready:
  $(basename "$0")                              short wizard
  $(basename "$0") --agent-id <id> [options] [-- <args for the CLI>]
    --framework claude|copilot   agent CLI (default: claude)
    --with-pluto                 also load the Pluto coordination tools
    --allow-rename               launch even if the id is already connected
    --no-launch                  write the MCP config and print how to use it

Manage jobs:
  --manage                       interactive: view, grep, move, export, pause,
                                 resume, cancel (plain table without a terminal)
  --history [--agent X] [--state S] [--since 7d] [--json]
  --stats [--json]
  --export <job> [-o file]       portable .pjob (stdout when piped)
  --import <file|->              never runs anything
  --prune [--older-than 30d] [--yes]   delete finished jobs

Common: --host <ip>  --http-port <port>  --version  --help

Move a job to another machine:
  $(basename "$0") --export J-... | ssh host pluto/$(basename "$0") --import -
EOF
}

ensure_venv() {
    if [[ -e "${VENV_DIR}" ]] && ! "${PY}" -c \
        'import sys; sys.exit(sys.prefix == sys.base_prefix)' 2>/dev/null; then
        die "The venv at ${VENV_DIR} is broken. Remove it and run this again."
    fi
    if [[ ! -e "${VENV_DIR}" ]]; then
        info "Creating Python venv at ${VENV_DIR} ..."
        mkdir -p "$(dirname "${VENV_DIR}")"
        python3 -m venv "${VENV_DIR}" >&2 || die "python3 -m venv failed."
    fi
    local marker="${VENV_DIR}/.requirements-installed"
    if [[ ! -f "${marker}" || "${REQUIREMENTS}" -nt "${marker}" ]]; then
        info "Installing Python dependencies ..."
        "${PY}" -m pip install -q -r "${REQUIREMENTS}" >&2 || die "pip install failed."
        date > "${marker}"
    fi
}

config_value() {
    python3 - "${CONFIG_FILE}" "$1" "$2" <<'PYEOF'
import json, sys
try:
    print(json.load(open(sys.argv[1]))["pluto_server"][sys.argv[2]])
except Exception:
    print(sys.argv[3])
PYEOF
}

health() { curl -fsS --max-time 2 "http://${host}:${port}/health" 2>/dev/null; }

ensure_server() {
    if ! health >/dev/null; then
        info "Pluto is not running at ${host}:${port}; starting it ..."
        "${SCRIPT_DIR}/PlutoServer.sh" --daemon >&2 || die "Could not start Pluto."
        sleep 1
        health >/dev/null || die "Pluto did not come up at ${host}:${port}."
    fi
    # Never restart a server that other agents may be using.
    health | grep -q '"smartstreams"' || die \
        "The Pluto server at ${host}:${port} is too old for SmartStreams. Restart it: ./PlutoServer.sh --kill && ./PlutoServer.sh --daemon"
}

agent_id_taken() {
    curl -fsS --max-time 2 "http://${host}:${port}/agents" 2>/dev/null \
        | python3 -c 'import json,sys
sys.exit(0 if sys.argv[1] in json.load(sys.stdin).get("agents", []) else 1)' "$1"
}

write_config() {
    local safe target
    safe=$(printf '%s' "${agent_id}" | tr -c 'A-Za-z0-9._-' '_')
    target="${MCP_DIR}/${safe}.smartstreams.json"
    mkdir -p "${MCP_DIR}"
    python3 - "${target}" "${PY}" "${ENTRY}" "${PLUTO_ENTRY}" "${agent_id}" \
        "${host}" "${port}" "${framework}" "${with_pluto}" <<'PYEOF'
import json, os, sys
(target, py, entry, pluto, agent, host, port, framework, with_pluto) = sys.argv[1:]
common = ["--agent-id", agent, "--host", host, "--http-port", port]
env = {}
if framework == "claude":
    # Same inbox-socket pass-through PlutoMCPFriend.sh uses for wakeups.
    for k in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
        env[k] = "${%s}" % k
home = os.environ.get("PLUTO_SMARTSTREAMS_HOME")
def server(args):
    s = {"command": py, "args": args, "env": dict(env)}
    if home:
        s["env"]["PLUTO_SMARTSTREAMS_HOME"] = home
    if framework == "copilot":
        s.update(type="local", tools=["*"])
    return s
extra = ["--framework", framework]
if with_pluto == "true":
    extra.append("--attach")    # the Pluto adapter owns the registration
servers = {"smartstreams": server([entry] + common + extra)}
if with_pluto == "true":
    servers["pluto"] = server([pluto] + common)
fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"mcpServers": servers}, f, indent=2)
print(target)
PYEOF
}

need() { [[ $# -ge 2 ]] || die "$1 needs a value."; }

agent_id="" framework="" host="" port="" mode="" with_pluto=false
no_launch=false allow_rename=false
user_args=() cli_args=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)  usage; exit 0 ;;
        --version)  echo "PlutoSmartStreams ${VERSION}"; exit 0 ;;
        --agent-id) need "$@"; agent_id="$2"; shift 2 ;;
        --framework) need "$@"; framework="$2"; shift 2 ;;
        --host)     need "$@"; host="$2"; shift 2 ;;
        --http-port) need "$@"; port="$2"; shift 2 ;;
        --with-pluto) with_pluto=true; shift ;;
        --no-launch) no_launch=true; shift ;;
        --allow-rename) allow_rename=true; shift ;;
        --manage|--history|--stats|--prune) mode=user; user_args+=("$1"); shift ;;
        --export|--import) need "$@"; mode=user; user_args+=("$1" "$2"); shift 2 ;;
        -o|--output|--agent|--state|--since|--older-than)
            need "$@"; user_args+=("$1" "$2"); shift 2 ;;
        --json|--yes) user_args+=("$1"); shift ;;
        --) shift; cli_args=("$@"); break ;;
        *)  usage >&2; die "Unknown option: $1" ;;
    esac
done

[[ -n "${host}" ]] || host="${PLUTO_HOST:-$(config_value host_ip 127.0.0.1)}"
[[ -n "${port}" ]] || port="${PLUTO_HTTP_PORT:-$(config_value host_http_port 9201)}"

if [[ "${mode}" == "user" ]]; then
    ensure_server
    ensure_venv
    exec "${PY}" "${ENTRY}" --host "${host}" --http-port "${port}" "${user_args[@]}"
fi
[[ ${#user_args[@]} -eq 0 ]] || die "${user_args[0]} only works with a management option."

if [[ -z "${agent_id}" ]]; then
    [[ -t 0 ]] || { usage >&2; die "--agent-id is required without a terminal."; }
    info "PlutoSmartStreams ${VERSION} — by using Pluto you accept CONSENT.md."
    while [[ -z "${agent_id}" ]]; do read -rp "  Agent ID: " agent_id; done
    if [[ -z "${framework}" ]]; then
        read -rp "  Agent CLI (claude/copilot) [claude]: " framework
    fi
fi
framework="${framework:-claude}"
case "${framework}" in
    claude)  flag=(--mcp-config) ; prefix="" ;;
    copilot) flag=(--additional-mcp-config) ; prefix="@" ;;
    *) die "--framework must be claude or copilot." ;;
esac

ensure_server
ensure_venv
if agent_id_taken "${agent_id}" && ! ${allow_rename}; then
    die "Agent ID '${agent_id}' is already connected. Pick another, or pass --allow-rename."
fi

config="$(write_config)"
info "MCP config: ${config}"
cmd=("${framework}" "${flag[@]}" "${prefix}${config}")
if ${no_launch}; then
    printf '%s\n' "${cmd[*]}"
    exit 0
fi
command -v "${framework}" >/dev/null || die "${framework} is not in PATH (use --no-launch)."
if (( ${#cli_args[@]} > 0 )); then cmd+=("${cli_args[@]}"); fi
exec "${cmd[@]}"
