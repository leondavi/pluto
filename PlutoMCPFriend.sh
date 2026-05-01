#!/usr/bin/env bash
# ===========================================================================
# PlutoMCPFriend.sh — Launch an MCP-capable agent with Pluto coordination.
#
# Wraps an agent CLI (Claude Code, Cursor, Aider, ...) by registering an MCP
# server adapter that exposes Pluto operations (send / lock / task / ...) as
# native tools. No PTY injection, no curl, no copy-pasted tokens — the agent
# calls pluto_send / pluto_lock_acquire / etc. directly.
#
# Usage:
#   ./PlutoMCPFriend.sh --agent-id <name> [--framework <name>] [options] [-- cmd...]
#   ./PlutoMCPFriend.sh --help
#
# Examples:
#   ./PlutoMCPFriend.sh --agent-id coder-1                         # auto-detect framework
#   ./PlutoMCPFriend.sh --agent-id coder-1 --framework claude --role specialist
#   ./PlutoMCPFriend.sh --agent-id coder-1 -- claude --some-flag   # pass-through args
# ===========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_ENTRY="${SCRIPT_DIR}/src_py/agent_mcp_friend/pluto_mcp_friend.py"
CONFIG_FILE="${SCRIPT_DIR}/config/pluto_config.json"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
VENV_DIR="/tmp/pluto/.venv"
PLUTO_VERSION="$(head -1 "${SCRIPT_DIR}/VERSION.md" 2>/dev/null | tr -d '[:space:]' || echo 'unknown')"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${CYAN}[pluto-mcp]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto-mcp]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto-mcp]${NC} $*"; }
err()   { echo -e "${RED}[pluto-mcp]${NC} $*" >&2; }

KNOWN_FRAMEWORKS=("claude" "copilot" "aider" "cursor")

show_help() {
    cat <<EOF
PlutoMCPFriend ${PLUTO_VERSION} — Pluto coordination over MCP.

Usage:
  $(basename "$0") --agent-id <name> [options] [-- agent-command...]

Options:
  --agent-id <name>       Agent identity in the Pluto network (required).
  --framework <name>      claude | copilot | aider | cursor.
                          Auto-detected if omitted.
  --role <name|path>      Apply a role from library/roles/<name>.md on first
                          turn (Claude only via --append-system-prompt; for
                          other frameworks use the slash command after launch).
  --host <ip>             Pluto server host (default: from config or localhost).
  --http-port <port>      Pluto HTTP port (default: from config or 9001).
  --ttl-ms <ms>           Session TTL in ms (default: 600000).
  --no-launch             Just generate .mcp.json and print the launch hint;
                          do not start the framework CLI.
  --log-level <lvl>       DEBUG | INFO | WARNING | ERROR (default: WARNING).
  --version               Show version and exit.
  --help                  Show this help.
  -- <cmd...>             Pass everything after -- to the agent CLI verbatim.

Environment:
  The script writes a project-local .mcp.json next to itself; existing
  entries are preserved when possible.

Examples:
  $(basename "$0") --agent-id coder-1 --framework claude --role specialist
  $(basename "$0") --agent-id worker-1 --framework cursor
  $(basename "$0") --agent-id reviewer-1 --no-launch        # just generate .mcp.json
EOF
}

# ── venv bootstrap ──────────────────────────────────────────────────────────

ensure_venv() {
    if [[ ! -f "${VENV_DIR}/bin/python" ]]; then
        info "Creating Python venv at ${VENV_DIR} ..."
        mkdir -p "$(dirname "${VENV_DIR}")"
        python3 -m venv "${VENV_DIR}"
    fi
    # Install / refresh deps if requirements.txt is newer than the venv marker.
    local marker="${VENV_DIR}/.requirements-installed"
    if [[ ! -f "${marker}" ]] || [[ "${REQUIREMENTS}" -nt "${marker}" ]]; then
        info "Installing Pluto Python dependencies ..."
        "${VENV_DIR}/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
        if ! "${VENV_DIR}/bin/pip" install -q -r "${REQUIREMENTS}"; then
            err "pip install -r ${REQUIREMENTS} failed."
            exit 1
        fi
        date > "${marker}"
    fi
}

# ── Pluto server reachability ───────────────────────────────────────────────

read_config_value() {
    local key="$1"
    local default="$2"
    [[ -f "${CONFIG_FILE}" ]] || { echo "${default}"; return; }
    "${VENV_DIR}/bin/python" - "${CONFIG_FILE}" "${key}" "${default}" <<'PYEOF'
import json, sys
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        data = json.load(f)
    server = data.get("pluto_server") or {}
    print(server.get(key) or default)
except Exception:
    print(default)
PYEOF
}

check_pluto_running() {
    local host="$1" port="$2"
    if curl -fsS --max-time 2 "http://${host}:${port}/health" >/dev/null 2>&1; then
        local version
        version=$(curl -fsS --max-time 2 "http://${host}:${port}/health" \
            | "${VENV_DIR}/bin/python" -c 'import json,sys;print(json.load(sys.stdin).get("version","?"))' 2>/dev/null || echo "?")
        ok "Pluto server reachable at ${host}:${port} (v${version})"
        return 0
    fi
    warn "Pluto server unreachable at ${host}:${port}"
    warn "Start the server first:  ./PlutoServer.sh --daemon"
    return 1
}

# ── .mcp.json generation ────────────────────────────────────────────────────

# Writes ${SCRIPT_DIR}/.mcp.json with a "pluto" entry pointing at our adapter.
# Preserves any existing top-level mcpServers entries (other servers).
write_mcp_json() {
    local agent_id="$1" host="$2" port="$3" ttl="$4" log_level="$5"
    local target="${SCRIPT_DIR}/.mcp.json"
    "${VENV_DIR}/bin/python" - "$target" "$agent_id" "$host" "$port" "$ttl" \
        "$log_level" "${VENV_DIR}/bin/python" "${PY_ENTRY}" <<'PYEOF'
import json, os, sys
target, agent_id, host, port, ttl, log_level, py_bin, entry = sys.argv[1:]
existing = {}
if os.path.isfile(target):
    try:
        with open(target) as f:
            existing = json.load(f) or {}
    except Exception:
        existing = {}
servers = existing.get("mcpServers") or {}
servers["pluto"] = {
    "command": py_bin,
    "args": [
        entry,
        "--agent-id", agent_id,
        "--host", host,
        "--http-port", str(port),
        "--ttl-ms", str(ttl),
        "--log-level", log_level,
    ],
}
existing["mcpServers"] = servers
with open(target, "w") as f:
    json.dump(existing, f, indent=2)
print(target)
PYEOF
}

# ── Framework detection / launch ────────────────────────────────────────────

auto_detect_framework() {
    for fw in "${KNOWN_FRAMEWORKS[@]}"; do
        if command -v "$fw" >/dev/null 2>&1; then
            echo "$fw"
            return 0
        fi
    done
    return 1
}

resolve_role_content() {
    local role="$1"
    local roles_dir="${SCRIPT_DIR}/library/roles"
    local path
    if [[ -f "$role" ]]; then
        path="$role"
    elif [[ -f "${roles_dir}/${role}.md" ]]; then
        path="${roles_dir}/${role}.md"
    else
        err "Role not found: ${role}"
        return 1
    fi
    cat "$path"
}

build_role_system_prompt() {
    local agent_id="$1" host="$2" port="$3" role="$4"
    "${VENV_DIR}/bin/python" - "$agent_id" "$host" "$port" "$role" "${SCRIPT_DIR}" <<'PYEOF'
import sys, os
agent_id, host, port, role, project_root = sys.argv[1:]
sys.path.insert(0, os.path.join(project_root, "src_py"))
from agent_mcp_friend.prompts import build_role_prompt_body
print(build_role_prompt_body(role, host=host, http_port=int(port), agent_id=agent_id))
PYEOF
}

launch_framework() {
    local framework="$1" role="$2" agent_id="$3" host="$4" port="$5"
    shift 5
    local extra=("$@")

    case "${framework}" in
        claude)
            local cmd=("claude" "--mcp-config" "${SCRIPT_DIR}/.mcp.json")
            if [[ -n "${role}" ]]; then
                local sys_prompt
                sys_prompt=$(build_role_system_prompt "${agent_id}" "${host}" "${port}" "${role}")
                cmd+=("--append-system-prompt" "${sys_prompt}")
            fi
            cmd+=("${extra[@]}")
            info "Launching: claude --mcp-config .mcp.json ${role:+(role=${role})}"
            exec "${cmd[@]}"
            ;;
        cursor|aider|copilot)
            warn "${framework} does not support startup system-prompt injection."
            if [[ -n "${role}" ]]; then
                warn "Once ${framework} is open, run the slash command:  /pluto-role-${role}"
            fi
            local cmd=("${framework}" "${extra[@]}")
            info "Launching: ${cmd[*]}"
            exec "${cmd[@]}"
            ;;
        *)
            err "Unknown framework: ${framework}"
            exit 1
            ;;
    esac
}

# ── Main ────────────────────────────────────────────────────────────────────

main() {
    local agent_id=""
    local framework=""
    local role=""
    local host=""
    local http_port=""
    local ttl_ms="600000"
    local log_level="WARNING"
    local no_launch=false
    local extra_cmd=()
    local past_separator=false

    while [[ $# -gt 0 ]]; do
        if $past_separator; then
            extra_cmd+=("$1")
            shift
            continue
        fi
        case "$1" in
            --help|-h) show_help; exit 0 ;;
            --version) echo "PlutoMCPFriend ${PLUTO_VERSION}"; exit 0 ;;
            --agent-id) agent_id="$2"; shift 2 ;;
            --framework) framework="$2"; shift 2 ;;
            --role) role="$2"; shift 2 ;;
            --host) host="$2"; shift 2 ;;
            --http-port) http_port="$2"; shift 2 ;;
            --ttl-ms) ttl_ms="$2"; shift 2 ;;
            --log-level) log_level="$2"; shift 2 ;;
            --no-launch) no_launch=true; shift ;;
            --) past_separator=true; shift ;;
            *)  err "Unknown option: $1"; show_help; exit 1 ;;
        esac
    done

    if [[ -z "${agent_id}" ]]; then
        err "--agent-id is required."
        show_help
        exit 1
    fi

    ensure_venv

    # Resolve host/port from config when not given.
    if [[ -z "${host}" ]]; then
        host=$(read_config_value "host_ip" "localhost")
    fi
    if [[ -z "${http_port}" ]]; then
        http_port=$(read_config_value "host_http_port" "9001")
    fi

    info "Agent ID:   ${BOLD}${agent_id}${NC}"
    info "Pluto:      ${host}:${http_port}"
    [[ -n "${role}" ]] && info "Role:       ${role}"

    check_pluto_running "${host}" "${http_port}" || true

    local mcp_json
    mcp_json=$(write_mcp_json "${agent_id}" "${host}" "${http_port}" "${ttl_ms}" "${log_level}")
    ok "Wrote MCP config:  ${mcp_json}"

    if $no_launch; then
        info "Skipping framework launch (--no-launch)."
        info "To use this config:  claude --mcp-config ${mcp_json}"
        exit 0
    fi

    if [[ -z "${framework}" ]]; then
        if framework=$(auto_detect_framework); then
            info "Auto-detected framework: ${framework}"
        else
            err "No supported framework found in PATH."
            err "Install one of: ${KNOWN_FRAMEWORKS[*]}, or pass --framework."
            exit 1
        fi
    fi

    launch_framework "${framework}" "${role}" "${agent_id}" "${host}" "${http_port}" "${extra_cmd[@]}"
}

main "$@"
