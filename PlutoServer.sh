#!/usr/bin/env bash
# ===========================================================================
# PlutoServer.sh — Build, run, and manage the Pluto coordination server.
#
# Usage:
#   ./PlutoServer.sh              Start the server in the foreground
#   ./PlutoServer.sh --daemon     Start the server in the background
#   ./PlutoServer.sh --kill       Stop a running daemon
#   ./PlutoServer.sh --status     Check if the server is running
#   ./PlutoServer.sh --build      Build only (compile + release)
#   ./PlutoServer.sh --clean      Clean build artefacts
#   ./PlutoServer.sh --console    Start an interactive Erlang shell
#   ./PlutoServer.sh --version    Print the version and exit
#
# The rebar3 build and release are placed under /tmp/pluto/build to keep
# the source tree clean.
# ===========================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUTO_VERSION="$(head -1 "${SCRIPT_DIR}/VERSION.md" | tr -d '[:space:]')"
SRC_DIR="${SCRIPT_DIR}/src_erl"
BUILD_DIR="/tmp/pluto/build"
REL_DIR="${BUILD_DIR}/_build/default/rel/pluto"
PID_FILE="/tmp/pluto/pluto.pid"
PING_TOOL="${SCRIPT_DIR}/src_py/utils/ping.py"
INFO_TOOL="${SCRIPT_DIR}/src_py/utils/server_info.py"
CONFIG_FILE="${SCRIPT_DIR}/config/pluto_config.json"

# ── Config loader ─────────────────────────────────────────────────────────────
# Read a single key from config/pluto_config.json's "pluto_server" section,
# with a fallback default. Keeps PlutoServer.sh in sync with PlutoAgentFriend.sh
# and the Erlang server, which all read the same file.
read_config() {
    local key="$1"
    local default="$2"
    if [[ -f "${CONFIG_FILE}" ]]; then
        local val
        val=$(python3 -c "
import json
try:
    with open('${CONFIG_FILE}') as f:
        c = json.load(f)
    print(c.get('pluto_server', {}).get('${key}', ''))
except Exception:
    pass
" 2>/dev/null)
        if [[ -n "${val}" ]]; then
            echo "${val}"
            return
        fi
    fi
    echo "${default}"
}

# Load network settings once. Env vars (PLUTO_HOST/PLUTO_PORT/PLUTO_HTTP_PORT)
# still override, matching the rest of the codebase.
PLUTO_HOST="${PLUTO_HOST:-$(read_config host_ip 127.0.0.1)}"
PLUTO_PORT="${PLUTO_PORT:-$(read_config host_tcp_port 9200)}"
PLUTO_HTTP_PORT="${PLUTO_HTTP_PORT:-$(read_config host_http_port 9201)}"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Colour

# ── Helper functions ──────────────────────────────────────────────────────────

info()  { echo -e "${CYAN}[pluto]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto]${NC} $*"; }
err()   { echo -e "${RED}[pluto]${NC} $*" >&2; }

show_disclaimer() {
    echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  DISCLAIMER & LIABILITY NOTICE                                   ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo -e "  Pluto is provided ${BOLD}as-is${NC} for research and development purposes only,"
    echo -e "  with no warranty of any kind. The repository maintainers and developers"
    echo -e "  bear ${BOLD}no responsibility or liability${NC} for any damages, losses, security"
    echo -e "  incidents, or harm arising from the use or misuse of this software."
    echo ""
    echo -e "  ${BOLD}You, the user, are solely responsible${NC} for any harm, damage, data loss,"
    echo -e "  security incident, or other issue caused by running this server —"
    echo -e "  including exposing it to untrusted networks, granting agents access to"
    echo -e "  sensitive resources, or coordinating agents that take destructive actions."
    echo ""
    echo -e "  Pluto is built with positive intentions for legitimate multi-agent R&D."
    echo -e "  Code injection is a powerful action — inspect and understand the tool"
    echo -e "  before use. Run only in environments you own and control."
    echo -e "  See ${CYAN}CONSENT.md${NC} and ${CYAN}README.md${NC} for the full disclaimer."
    echo ""
}

# Ping the Pluto server via the Python utility (OS-independent).
# Returns 0 if the server responds with "pong", 1 otherwise.
pluto_ping() {
    python3 "${PING_TOOL}" -q --host "${PLUTO_HOST}" --port "${PLUTO_PORT}" --timeout "${1:-2}" 2>/dev/null
}

# Check that rebar3 is on the PATH
require_rebar3() {
    if ! command -v rebar3 &>/dev/null; then
        err "rebar3 not found on PATH.  Please install it first."
        err "  https://rebar3.org/docs/getting-started/"
        exit 1
    fi
}

# Find PIDs of processes holding the Pluto TCP port or matching beam.
# Returns space-separated PIDs (may be empty).
find_pluto_pids() {
    local pids=""
    # Primary: find process holding the configured TCP port
    local port_pids
    port_pids=$(lsof -ti :${PLUTO_PORT} 2>/dev/null || true)
    if [[ -n "${port_pids}" ]]; then
        pids="${port_pids}"
    fi
    # Secondary: find beam processes (covers cases where port isn't bound yet)
    local beam_pids
    beam_pids=$(pgrep -f 'beam.*pluto' 2>/dev/null || true)
    if [[ -n "${beam_pids}" ]]; then
        pids="${pids:+${pids} }${beam_pids}"
    fi
    # Deduplicate
    echo "${pids}" | tr ' ' '\n' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

# Check if another Erlang node named 'pluto' is already running.
# If so, try to auto-fix by killing stale processes/epmd entries.
check_node_conflict() {
    local epmd_conflict=false
    local port_conflict=false

    if command -v epmd &>/dev/null && epmd -names 2>/dev/null | grep -q 'name pluto '; then
        epmd_conflict=true
    fi

    if lsof -ti :${PLUTO_PORT} &>/dev/null; then
        port_conflict=true
    fi

    if ! $epmd_conflict && ! $port_conflict; then
        return 0  # no conflict
    fi

    $epmd_conflict && warn "An Erlang node named 'pluto' is already registered with epmd."
    $port_conflict && warn "Port ${PLUTO_PORT} is already in use."

    # Find and kill all Pluto-related processes
    local pids
    pids=$(find_pluto_pids)

    if [[ -n "${pids}" ]]; then
        warn "Pluto processes found (PID: ${pids// /, }). Stopping ..."

        # Try graceful stop first
        if [[ -x "${REL_DIR}/bin/pluto" ]]; then
            "${REL_DIR}/bin/pluto" stop 2>/dev/null || true
            sleep 2
        fi

        # Force-kill if still around
        pids=$(find_pluto_pids)
        if [[ -n "${pids}" ]]; then
            echo "${pids}" | xargs kill -9 2>/dev/null || true
            sleep 1
        fi
    fi

    # Clean up stale epmd registration
    if command -v epmd &>/dev/null && epmd -names 2>/dev/null | grep -q 'name pluto '; then
        info "Clearing stale 'pluto' node from epmd ..."
        epmd -kill 2>/dev/null || true
        sleep 0.5
        if epmd -names 2>/dev/null | grep -q 'name pluto '; then
            pkill -x epmd 2>/dev/null || true
            sleep 0.5
        fi
    fi

    # Verify cleanup succeeded
    local still_epmd=false still_port=false
    if command -v epmd &>/dev/null && epmd -names 2>/dev/null | grep -q 'name pluto '; then
        still_epmd=true
    fi
    if lsof -ti :${PLUTO_PORT} &>/dev/null; then
        still_port=true
    fi

    if $still_epmd || $still_port; then
        echo
        err "════════════════════════════════════════════════════════════════"
        err "Failed to clean up the previous Pluto instance."
        $still_port && err "  Port ${PLUTO_PORT} is still in use."
        $still_epmd && err "  'pluto' node is still in epmd."
        err ""
        err "Please run these commands manually:"
        err "  ${CYAN}kill -9 \$(lsof -ti :${PLUTO_PORT})${NC}"
        err "  ${CYAN}pkill -x epmd${NC}"
        err "════════════════════════════════════════════════════════════════"
        echo
        return 1
    fi

    ok "Previous Pluto instance cleaned up."
    return 0
}

# Synchronise the Erlang source into the build directory.
# We use rsync so that rebar3's _build cache is preserved between runs.
sync_source() {
    info "Syncing source to ${BUILD_DIR} ..."
    mkdir -p "${BUILD_DIR}"
    rsync -a --delete \
        --exclude '_build' \
        --exclude '.rebar3' \
        "${SRC_DIR}/" "${BUILD_DIR}/"
}

# Detect the version of the currently-built release (if any).
# Echoes the highest-numbered release directory name, or empty if no build.
get_built_version() {
    if [[ ! -d "${REL_DIR}/releases" ]]; then
        return
    fi
    ls -1 "${REL_DIR}/releases" 2>/dev/null \
        | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -V | tail -1
}

# If the source version (from VERSION.md) differs from the built release,
# wipe stale build artefacts so do_build produces a fresh release that
# matches the source. Stale releases dirs (e.g. releases/0.2.6 when source
# is 0.2.7) can otherwise leave the runtime confused about which version
# to boot.
auto_clean_if_stale() {
    local source_ver="${PLUTO_VERSION#v}"
    local built_ver
    built_ver=$(get_built_version)

    [[ -z "${built_ver}" ]] && return                        # nothing built yet
    [[ "${source_ver}" == "${built_ver}" ]] && return        # already in sync

    local sorted_top
    sorted_top=$(printf '%s\n%s\n' "${source_ver}" "${built_ver}" | sort -V | tail -1)
    if [[ "${sorted_top}" == "${source_ver}" ]]; then
        warn "Source ${source_ver} is newer than built ${built_ver} — auto-cleaning."
    else
        warn "Source ${source_ver} differs from built ${built_ver} (downgrade) — auto-cleaning."
    fi
    cmd_clean
}

# Compile and build the release.
do_build() {
    require_rebar3
    auto_clean_if_stale
    sync_source
    info "Compiling ..."
    (cd "${BUILD_DIR}" && rebar3 compile)
    info "Assembling release ..."
    (cd "${BUILD_DIR}" && rebar3 release)
    ok "Build complete: ${REL_DIR}"
}

# Ensure the release exists; build if not.
ensure_release() {
    if [[ ! -x "${REL_DIR}/bin/pluto" ]]; then
        warn "Release not found — building now ..."
        do_build
    fi
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_build() {
    do_build
}

cmd_clean() {
    info "Cleaning build directory ..."
    rm -rf "${BUILD_DIR}/_build" "${BUILD_DIR}/.rebar3"
    ok "Clean."
}

cmd_start_foreground() {
    show_disclaimer
    do_build
    if ! check_node_conflict; then
        err "Cannot start — another Pluto node is already running."
        exit 1
    fi
    info "Starting Pluto server (foreground) ..."
    PLUTO_CONFIG="${CONFIG_FILE}" exec "${REL_DIR}/bin/pluto" foreground
}

cmd_start_daemon() {
    show_disclaimer
    do_build
    if ! check_node_conflict; then
        err "Cannot start — another Pluto node is already running."
        exit 1
    fi
    info "Starting Pluto server (daemon) ..."
    PLUTO_CONFIG="${CONFIG_FILE}" "${REL_DIR}/bin/pluto" daemon

    # Wait for the server to come up
    local attempts=0
    while (( attempts < 10 )); do
        sleep 1
        if pluto_ping 1; then
            # Store the OS PID for --kill convenience
            local os_pid
            os_pid=$(lsof -ti :${PLUTO_PORT} 2>/dev/null || echo "unknown")
            echo "${os_pid}" > "${PID_FILE}"
            ok "Pluto daemon is running (pid ${os_pid})."
            return 0
        fi
        (( attempts++ ))
    done

    err "Daemon may have failed to start. Check logs in ${REL_DIR}/log/"
    exit 1
}

cmd_kill() {
    info "Stopping Pluto daemon ..."

    local stopped=false

    # 1. Try the release stop command (graceful shutdown)
    if [[ -x "${REL_DIR}/bin/pluto" ]]; then
        if "${REL_DIR}/bin/pluto" stop 2>/dev/null; then
            ok "Stopped via release command."
            stopped=true
        fi
    fi

    # 2. Fallback: kill by PID file
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid=$(<"${PID_FILE}")
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null
            sleep 1
            if kill -0 "${pid}" 2>/dev/null; then
                kill -9 "${pid}" 2>/dev/null
            fi
            ok "Killed process ${pid}."
            stopped=true
        fi
        rm -f "${PID_FILE}"
    fi

    # 3. Fallback: kill processes holding the Pluto TCP port or matching beam.*pluto
    if ! $stopped; then
        local pids
        pids=$(find_pluto_pids)
        if [[ -n "${pids}" ]]; then
            info "Killing Pluto processes: ${pids}"
            echo "${pids}" | xargs kill 2>/dev/null || true
            sleep 1
            # Force-kill stragglers
            pids=$(find_pluto_pids)
            if [[ -n "${pids}" ]]; then
                echo "${pids}" | xargs kill -9 2>/dev/null || true
                sleep 0.5
            fi
            stopped=true
        fi
    fi

    # 4. Clean up stale epmd registration
    if command -v epmd &>/dev/null && epmd -names 2>/dev/null | grep -q 'name pluto '; then
        info "Clearing stale 'pluto' node from epmd ..."
        epmd -kill 2>/dev/null || true
        sleep 0.5
        if epmd -names 2>/dev/null | grep -q 'name pluto '; then
            pkill -x epmd 2>/dev/null || true
            sleep 0.5
        fi
        ok "epmd cleared."
    fi

    if $stopped; then
        ok "Pluto server stopped."
    else
        ok "No running Pluto server found."
    fi
}

cmd_status() {
    if ! pluto_ping; then
        echo ""
        echo -e "${RED}"
        cat <<'LOGO'
    ╔════════════════════════════════════════════════╗
    ║                                                ║
    ║        *  .  ✦        .    *                   ║
    ║     .    ___         .        .  ✦             ║
    ║   ✦    /   \    *        .                     ║
    ║       /_____\        .    PLUTO SERVER          ║
    ║      |=  =  =|  .     ✦  Agent Coordination    ║
    ║      |  ---  |    *                   .         ║
    ║     /|  | |  |\       .     *                   ║
    ║    / |  |_|  | \  .       .                     ║
    ║   |  |_______|  |    ✦        .  *              ║
    ║   | /    A    \ |  .                            ║
    ║   |/   / \   \|      .    *                    ║
    ║       /   \          ✦                          ║
    ║      / ~~~ \   .        .     *                 ║
    ║     /~~~~~~~\     *         .                   ║
    ║    ~~~~~~~~~~~        .  ✦                      ║
    ║                                                ║
    ║            STATUS:  ● OFFLINE                  ║
    ╚════════════════════════════════════════════════╝
LOGO
        echo -e "${NC}"
        warn "Pluto server is not running."
        echo ""
        return 1
    fi

    # Query detailed server info from the running Erlang server.
    # Host/port come from config/pluto_config.json (PLUTO_HOST/PLUTO_PORT env
    # overrides honoured); without these, server_info.py would default to
    # localhost:9000 and every field would render as "?".
    local info_json
    info_json=$(python3 "${INFO_TOOL}" --host "${PLUTO_HOST}" --port "${PLUTO_PORT}" --timeout 3 2>/dev/null || echo "{}")

    # Parse fields from the JSON response using python3 for reliability
    local version otp_release erts_version node_name hostname os_type
    local tcp_port http_port uptime_ms schedulers
    local process_count process_limit
    local mem_total mem_ets
    local connected_agents active_locks pending_waiters
    local ip_list server_time

    version=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','?'))" 2>/dev/null || echo "?")
    otp_release=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('otp_release','?'))" 2>/dev/null || echo "?")
    erts_version=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('erts_version','?'))" 2>/dev/null || echo "?")
    node_name=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('node','?'))" 2>/dev/null || echo "?")
    hostname=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('hostname','?'))" 2>/dev/null || echo "?")
    os_type=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('os','?'))" 2>/dev/null || echo "?")
    tcp_port=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tcp_port','?'))" 2>/dev/null || echo "?")
    http_port=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('http_port','?'))" 2>/dev/null || echo "?")
    uptime_ms=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('uptime_ms',0))" 2>/dev/null || echo "0")
    schedulers=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('schedulers','?'))" 2>/dev/null || echo "?")
    process_count=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('process_count','?'))" 2>/dev/null || echo "?")
    process_limit=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('process_limit','?'))" 2>/dev/null || echo "?")
    mem_total=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); m=d.get('memory',{}); print(f\"{m.get('total',0)/(1024*1024):.1f}\")" 2>/dev/null || echo "?")
    mem_ets=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); m=d.get('memory',{}); print(f\"{m.get('ets',0)/(1024*1024):.1f}\")" 2>/dev/null || echo "?")
    connected_agents=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('live',{}).get('connected_agents','?'))" 2>/dev/null || echo "?")
    active_locks=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('live',{}).get('active_locks','?'))" 2>/dev/null || echo "?")
    pending_waiters=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('live',{}).get('pending_waiters','?'))" 2>/dev/null || echo "?")
    ip_list=$(echo "${info_json}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(d.get('ips',[])))" 2>/dev/null || echo "?")

    # Format uptime as human-readable
    local uptime_str
    uptime_str=$(python3 -c "
ms = int(${uptime_ms})
s = ms // 1000
d, s = divmod(s, 86400)
h, s = divmod(s, 3600)
m, s = divmod(s, 60)
parts = []
if d: parts.append(f'{d}d')
if h: parts.append(f'{h}h')
if m: parts.append(f'{m}m')
parts.append(f'{s}s')
print(' '.join(parts))
" 2>/dev/null || echo "?")

    local pid
    pid=$(lsof -ti :${tcp_port} 2>/dev/null || echo "?")

    echo ""
    echo -e "${CYAN}"
    cat <<'LOGO'
    ╔════════════════════════════════════════════════╗
    ║                                                ║
    ║        *  .  ✦        .    *                   ║
    ║     .    ___         .        .  ✦             ║
    ║   ✦    /   \    *        .                     ║
    ║       /_____\        .    PLUTO SERVER          ║
    ║      |=  =  =|  .     ✦  Agent Coordination    ║
    ║      |  ---  |    *                   .         ║
    ║     /|  | |  |\       .     *                   ║
    ║    / |  |_|  | \  .       .                     ║
    ║   |  |_______|  |    ✦        .  *              ║
    ║   | /    A    \ |  .                            ║
    ║   |/   / \   \|      .    *                    ║
    ║       /   \          ✦                          ║
    ║      / ~~~ \   .        .     *                 ║
    ║     /~~~~~~~\     *         .                   ║
    ║    ~~~~~~~~~~~        .  ✦                      ║
    ║                                                ║
    ║            STATUS:  ● ONLINE                   ║
    ╚════════════════════════════════════════════════╝
LOGO
    echo -e "${NC}"

    echo -e "  ${GREEN}▸ Server${NC}"
    echo -e "    Version:          ${CYAN}${version}${NC}"
    echo -e "    Node:             ${node_name}"
    echo -e "    Hostname:         ${hostname}"
    echo -e "    PID:              ${pid}"
    echo -e "    Uptime:           ${GREEN}${uptime_str}${NC}"
    echo ""

    echo -e "  ${GREEN}▸ Network${NC}"
    echo -e "    TCP Port:         ${CYAN}${tcp_port}${NC}"
    echo -e "    HTTP Port:        ${http_port}"
    echo -e "    IPs:              ${ip_list}"
    echo ""

    echo -e "  ${GREEN}▸ Runtime${NC}"
    echo -e "    OTP Release:      ${CYAN}OTP ${otp_release}${NC}"
    echo -e "    ERTS:             ${erts_version}"
    echo -e "    OS:               ${os_type}"
    echo -e "    Schedulers:       ${schedulers}"
    echo -e "    Processes:        ${process_count} / ${process_limit}"
    echo -e "    Memory (total):   ${mem_total} MB"
    echo -e "    Memory (ETS):     ${mem_ets} MB"
    echo ""

    echo -e "  ${GREEN}▸ Live${NC}"
    echo -e "    Connected Agents: ${CYAN}${connected_agents}${NC}"
    echo -e "    Active Locks:     ${active_locks}"
    echo -e "    Pending Waiters:  ${pending_waiters}"
    echo ""
}

cmd_console() {
    do_build
    if ! check_node_conflict; then
        err "Cannot start — another Pluto node is already running."
        exit 1
    fi
    info "Starting Pluto interactive console ..."
    exec "${REL_DIR}/bin/pluto" console
}

# ── Main ──────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
${CYAN}PlutoServer.sh${NC} — Build and manage the Pluto coordination server.

Usage:
  $(basename "$0") [OPTION]

Options:
  (none)        Build and start the server in the foreground.
  --daemon      Build and start the server as a background daemon.
  --kill        Stop a running Pluto daemon.
  --status      Check whether the server is running.
  --build       Compile and assemble the release (no start).
  --clean       Remove build artefacts.
  --console     Start an interactive Erlang shell with Pluto loaded.
  --version     Print the version and exit.
  -h, --help    Show this help message.

Build directory: ${BUILD_DIR}
Source directory: ${SRC_DIR}
EOF
}

case "${1:-}" in
    --daemon)   cmd_start_daemon ;;
    --kill)     cmd_kill ;;
    --status)   cmd_status ;;
    --build)    cmd_build ;;
    --clean)    cmd_clean ;;
    --console)  cmd_console ;;
    --version)  echo "Pluto ${PLUTO_VERSION}" ;;
    -h|--help)  usage ;;
    "")         cmd_start_foreground ;;
    *)          err "Unknown option: $1"; usage; exit 1 ;;
esac
