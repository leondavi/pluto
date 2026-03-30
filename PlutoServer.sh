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

# Check that rebar3 is on the PATH
require_rebar3() {
    if ! command -v rebar3 &>/dev/null; then
        err "rebar3 not found on PATH.  Please install it first."
        err "  https://rebar3.org/docs/getting-started/"
        exit 1
    fi
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
    info "Starting Pluto server (foreground) ..."
    exec "${REL_DIR}/bin/pluto" foreground
}

cmd_start_daemon() {
    do_build
    info "Starting Pluto server (daemon) ..."
    "${REL_DIR}/bin/pluto" daemon

    # Wait briefly for the node to come up
    sleep 1
    if "${REL_DIR}/bin/pluto" ping &>/dev/null; then
        # Store the OS PID for --kill convenience
        local os_pid
        os_pid=$("${REL_DIR}/bin/pluto" pid 2>/dev/null || echo "unknown")
        echo "${os_pid}" > "${PID_FILE}"
        ok "Pluto daemon is running (pid ${os_pid})."
    else
        err "Daemon may have failed to start. Check logs in ${REL_DIR}/log/"
        exit 1
    fi
}

cmd_kill() {
    info "Stopping Pluto daemon ..."
    if [[ -x "${REL_DIR}/bin/pluto" ]]; then
        "${REL_DIR}/bin/pluto" stop 2>/dev/null && ok "Stopped." || true
    fi

    # Fallback: kill by PID file
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid=$(<"${PID_FILE}")
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null
            ok "Killed process ${pid}."
        fi
        rm -f "${PID_FILE}"
    fi
}

cmd_status() {
    if [[ -x "${REL_DIR}/bin/pluto" ]] && "${REL_DIR}/bin/pluto" ping &>/dev/null; then
        ok "Pluto is running."
    else
        warn "Pluto is not running."
    fi
}

cmd_console() {
    do_build
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
