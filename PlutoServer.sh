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
#
# The rebar3 build and release are placed under /tmp/pluto/build to keep
# the source tree clean.
# ===========================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/src_erl"
BUILD_DIR="/tmp/pluto/build"
REL_DIR="${BUILD_DIR}/_build/default/rel/pluto"
PID_FILE="/tmp/pluto/pluto.pid"
PING_TOOL="${SCRIPT_DIR}/src_py/utils/ping.py"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

# ── Helper functions ──────────────────────────────────────────────────────────

info()  { echo -e "${CYAN}[pluto]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto]${NC} $*"; }
err()   { echo -e "${RED}[pluto]${NC} $*" >&2; }

# Ping the Pluto server via the Python utility (OS-independent).
# Returns 0 if the server responds with "pong", 1 otherwise.
pluto_ping() {
    python3 "${PING_TOOL}" -q --timeout "${1:-2}" 2>/dev/null
}

# Check that rebar3 is on the PATH
require_rebar3() {
    if ! command -v rebar3 &>/dev/null; then
        err "rebar3 not found on PATH.  Please install it first."
        err "  https://rebar3.org/docs/getting-started/"
        exit 1
    fi
}

# Find PIDs of processes holding the Pluto TCP port (9000) or matching beam.
# Returns space-separated PIDs (may be empty).
find_pluto_pids() {
    local pids=""
    # Primary: find process holding port 9000
    local port_pids
    port_pids=$(lsof -ti :9000 2>/dev/null || true)
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

    if lsof -ti :9000 &>/dev/null; then
        port_conflict=true
    fi

    if ! $epmd_conflict && ! $port_conflict; then
        return 0  # no conflict
    fi

    $epmd_conflict && warn "An Erlang node named 'pluto' is already registered with epmd."
    $port_conflict && warn "Port 9000 is already in use."

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
    if lsof -ti :9000 &>/dev/null; then
        still_port=true
    fi

    if $still_epmd || $still_port; then
        echo
        err "════════════════════════════════════════════════════════════════"
        err "Failed to clean up the previous Pluto instance."
        $still_port && err "  Port 9000 is still in use."
        $still_epmd && err "  'pluto' node is still in epmd."
        err ""
        err "Please run these commands manually:"
        err "  ${CYAN}kill -9 \$(lsof -ti :9000)${NC}"
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

# Compile and build the release.
do_build() {
    require_rebar3
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
    do_build
    if ! check_node_conflict; then
        err "Cannot start — another Pluto node is already running."
        exit 1
    fi
    info "Starting Pluto server (foreground) ..."
    exec "${REL_DIR}/bin/pluto" foreground
}

cmd_start_daemon() {
    do_build
    if ! check_node_conflict; then
        err "Cannot start — another Pluto node is already running."
        exit 1
    fi
    info "Starting Pluto server (daemon) ..."
    "${REL_DIR}/bin/pluto" daemon

    # Wait for the server to come up
    local attempts=0
    while (( attempts < 10 )); do
        sleep 1
        if pluto_ping 1; then
            # Store the OS PID for --kill convenience
            local os_pid
            os_pid=$(lsof -ti :9000 2>/dev/null || echo "unknown")
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

    # 3. Fallback: kill processes holding port 9000 or matching beam.*pluto
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
    if pluto_ping; then
        local pid
        pid=$(lsof -ti :9000 2>/dev/null || echo "?")
        ok "Pluto is running (pid ${pid}, port 9000)."
    else
        warn "Pluto is not running."
    fi
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
    -h|--help)  usage ;;
    "")         cmd_start_foreground ;;
    *)          err "Unknown option: $1"; usage; exit 1 ;;
esac
