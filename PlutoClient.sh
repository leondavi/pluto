#!/usr/bin/env bash
# ===========================================================================
# PlutoClient.sh — Python client wrapper for Pluto.
#
# Automatically creates a Python virtual environment under /tmp/pluto/.venv
# (if it doesn't exist yet) and runs the pluto_client.py CLI.
#
# Usage:
#   ./PlutoClient.sh ping                        Test server connectivity
#   ./PlutoClient.sh list                         List connected agents
#   ./PlutoClient.sh guide                        Generate the agent guide
#   ./PlutoClient.sh guide --output ./guide.md    Generate to custom path
#   ./PlutoClient.sh --host 10.0.1.5 --port 9000 ping
#
# All arguments are forwarded to pluto_client.py.
# ===========================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SRC_DIR="${SCRIPT_DIR}/src_py"
VENV_DIR="/tmp/pluto/.venv"
PYTHON_SCRIPT="${PY_SRC_DIR}/pluto_client.py"

# ── Colours ───────────────────────────────────────────────────────────────────
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}[pluto-client]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto-client]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto-client]${NC} $*"; }
err()   { echo -e "${RED}[pluto-client]${NC} $*" >&2; }

# ── Find Python 3 ────────────────────────────────────────────────────────────

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
            if [[ "$ver" == "3" ]]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    err "Python 3 not found on PATH."
    exit 1
}

# ── Ensure virtual environment ────────────────────────────────────────────────

ensure_venv() {
    local python_bin="$1"

    if [[ ! -d "${VENV_DIR}" ]]; then
        info "Creating virtual environment at ${VENV_DIR} ..."
        mkdir -p "$(dirname "${VENV_DIR}")"
        "${python_bin}" -m venv "${VENV_DIR}"
        ok "Virtual environment created."
    fi

    # Activate the venv
    source "${VENV_DIR}/bin/activate"

    # Ensure pip is up to date (quiet)
    pip install --quiet --upgrade pip 2>/dev/null || true
}

# ── Main ──────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
${CYAN}PlutoClient.sh${NC} — Python client for the Pluto coordination server.

Usage:
  $(basename "$0") [OPTIONS] COMMAND [COMMAND_OPTIONS]

Commands:
  ping                    Verify connectivity to the Pluto server.
  list                    List all connected agents.
  guide                   Generate the Pluto agent guide file.

Global options (before command):
  --host HOST             Pluto server host (default: localhost)
  --port PORT             Pluto server port (default: 9000)
  --agent-id ID           Agent ID for registration (default: pluto-cli)

Guide-specific options:
  --output PATH           Output path for the guide file.

Examples:
  $(basename "$0") ping
  $(basename "$0") --host 10.0.1.5 list
  $(basename "$0") guide --output ./agent_guide.md

EOF
}

# Check for help flag
case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
esac

# Verify the Python script exists
if [[ ! -f "${PYTHON_SCRIPT}" ]]; then
    err "Python client not found at ${PYTHON_SCRIPT}"
    exit 1
fi

PYTHON_BIN=$(find_python)
ensure_venv "${PYTHON_BIN}"

# Run the Python client with all provided arguments
exec python "${PYTHON_SCRIPT}" "$@"
