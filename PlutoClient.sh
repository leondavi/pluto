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
#   ./PlutoClient.sh stats                        Show server statistics
#   ./PlutoClient.sh guide                        Generate the agent guide
#   ./PlutoClient.sh guide --output ./guide.md    Generate to custom path
#   ./PlutoClient.sh --host 10.0.1.5 --port 9000 ping
#   ./PlutoClient.sh --version                        Print the version
#
# All arguments are forwarded to pluto_client.py.
# ===========================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUTO_VERSION="$(cat "${SCRIPT_DIR}/VERSION.md" | tr -d '\n')"
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
  stats                   Show server statistics (locks, messages, deadlocks,
                          per-agent counters). No registration required.
  guide                   Generate the Pluto agent guide file.

Global options (place before the command):
  --host HOST             Pluto server host (default: 127.0.0.1)
  --port PORT             Pluto server port (default: 9000)
  --agent-id ID           Agent ID for registration (default: pluto-cli)

Guide-specific options:
  --output PATH           Output path for the generated guide file.

Examples:
  ${GREEN}# Check if the server is reachable${NC}
  $(basename "$0") ping

  ${GREEN}# Show live server statistics${NC}
  $(basename "$0") stats

  ${GREEN}# List agents on a remote server${NC}
  $(basename "$0") --host 10.0.1.5 --port 9000 list

  ${GREEN}# Generate the agent coordination guide${NC}
  $(basename "$0") guide --output ./agent_guide.md

Requires: Python 3, a running Pluto server (see ${CYAN}./PlutoServer.sh --help${NC}).

${YELLOW}Starting an Agent with Pluto:${NC}
  1. Start the server:       ${GREEN}./PlutoServer.sh --daemon${NC}
  2. Verify it's running:     ${GREEN}./PlutoClient.sh ping${NC}
  3. Generate the agent guide: ${GREEN}./PlutoClient.sh guide --output agent_guide.md${NC}
  4. In your agent code, connect via TCP to localhost:9000
     and send: ${CYAN}{"op":"register","agent_id":"my-agent"}${NC}
  5. Acquire locks before accessing shared resources,
     send/receive messages to coordinate with other agents,
     and ping every 15s to stay alive.
  See the generated agent_guide.md for the full protocol reference.

EOF
}

# Check for help/version flags
case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    --version)
        echo "Pluto ${PLUTO_VERSION}"
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
