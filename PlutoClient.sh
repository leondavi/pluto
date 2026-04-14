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
  register                Register an agent and maintain presence.

Global options (place before the command):
  --host HOST             Pluto server host (default: 127.0.0.1)
  --port PORT             Pluto server port (default: 9000)
  --agent-id ID           Agent ID for registration (default: pluto-cli)

Guide-specific options:
  --output PATH           Output path for the generated guide file.

Register-specific options:
  --daemon                Run as background daemon maintaining TCP heartbeat
  --http                  Use HTTP registration (no persistent TCP needed)
  --stateless             Register as stateless agent with longer TTL
  --ttl SECONDS           TTL in seconds for HTTP/stateless mode (default: 300)
  --http-port PORT        HTTP port (default: 9001)

Examples:
  ${GREEN}# Check if the server is reachable${NC}
  $(basename "$0") ping

  ${GREEN}# Show live server statistics${NC}
  $(basename "$0") stats

  ${GREEN}# List agents on a remote server${NC}
  $(basename "$0") --host 10.0.1.5 --port 9000 list

  ${GREEN}# Generate the agent coordination guide${NC}
  $(basename "$0") guide --output ./agent_guide.md

  ${GREEN}# Register via HTTP (for CLI agents like Claude Code)${NC}
  $(basename "$0") register --http --agent-id claude-workspace

  ${GREEN}# Register with positional agent ID (same as --agent-id)${NC}
  $(basename "$0") register claude-workspace

  ${GREEN}# Register as stateless with 5-min TTL${NC}
  $(basename "$0") register --stateless --ttl 300 --agent-id my-agent

  ${GREEN}# Spawn a TCP daemon that maintains heartbeat${NC}
  $(basename "$0") register --daemon --agent-id my-agent

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

# ── Register subcommand special handling ──────────────────────────────────────

handle_register() {
    local host="127.0.0.1"
    local port="9000"
    local http_port="9001"
    local agent_id="pluto-cli"
    local mode=""
    local daemon=false
    local ttl=300

    # Parse global options and register-specific flags
    local positional_id=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --host)     host="$2"; shift 2 ;;
            --port)     port="$2"; shift 2 ;;
            --http-port) http_port="$2"; shift 2 ;;
            --agent-id) agent_id="$2"; shift 2 ;;
            --daemon)   daemon=true; shift ;;
            --http)     mode="http"; shift ;;
            --stateless) mode="stateless"; shift ;;
            --ttl)      ttl="$2"; shift 2 ;;
            register)   shift ;;  # skip the command itself
            -*)
                err "Unknown option: $1"
                err "Run '$(basename "$0") --help' for usage."
                exit 1
                ;;
            *)
                # Treat first positional arg as agent_id
                if [[ -z "$positional_id" ]]; then
                    positional_id="$1"
                else
                    err "Unexpected argument: $1"
                    err "Run '$(basename "$0") --help' for usage."
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # Positional agent_id takes effect (--agent-id flag wins if both given)
    if [[ -n "$positional_id" ]]; then
        if [[ "$agent_id" != "pluto-cli" ]]; then
            warn "Both --agent-id '${agent_id}' and positional '${positional_id}' given; using --agent-id."
        else
            agent_id="$positional_id"
        fi
    fi

    if [[ "$daemon" == true ]]; then
        # Solution 3: Spawn a background daemon that maintains TCP connection
        info "Starting TCP daemon for agent '${agent_id}'..."
        local pidfile="/tmp/pluto/daemon_${agent_id}.pid"
        mkdir -p /tmp/pluto

        # Check if daemon is already running
        if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            warn "Daemon for '${agent_id}' already running (PID $(cat "$pidfile"))"
            exit 0
        fi

        nohup python -c "
import sys, os, time, socket, json, signal

HOST = '${host}'
PORT = int('${port}')
AGENT_ID = '${agent_id}'
HB_INTERVAL = 10  # seconds

def main():
    pidfile = '${pidfile}'
    with open(pidfile, 'w') as f:
        f.write(str(os.getpid()))

    def cleanup(sig, frame):
        try: os.remove(pidfile)
        except: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    while True:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=5)
            # Register
            msg = json.dumps({'op': 'register', 'agent_id': AGENT_ID}) + '\n'
            sock.sendall(msg.encode())
            sock.settimeout(5)
            resp = sock.recv(4096)
            print(f'[pluto-daemon] Registered as {AGENT_ID}', flush=True)

            # Heartbeat loop
            while True:
                time.sleep(HB_INTERVAL)
                ping = json.dumps({'op': 'ping'}) + '\n'
                sock.sendall(ping.encode())
                data = sock.recv(4096)
                if not data:
                    break
        except Exception as e:
            print(f'[pluto-daemon] Connection lost: {e}, reconnecting...', flush=True)
            time.sleep(3)

main()
" > /tmp/pluto/daemon_${agent_id}.log 2>&1 &

        local daemon_pid=$!
        ok "Daemon started (PID ${daemon_pid}), log: /tmp/pluto/daemon_${agent_id}.log"
        ok "To stop: kill ${daemon_pid}"
        return 0
    fi

    if [[ "$mode" == "http" || "$mode" == "stateless" ]]; then
        # Solutions 1, 2, 4: HTTP-based registration
        local ttl_ms=$((ttl * 1000))
        local body="{\"agent_id\":\"${agent_id}\",\"mode\":\"${mode}\",\"ttl_ms\":${ttl_ms}}"

        info "Registering agent '${agent_id}' via HTTP (mode=${mode}, ttl=${ttl}s)..."

        local response
        response=$(curl -s -X POST \
            -H "Content-Type: application/json" \
            -d "${body}" \
            "http://${host}:${http_port}/agents/register" 2>&1)

        if [[ $? -ne 0 ]]; then
            err "Failed to connect to http://${host}:${http_port}"
            exit 1
        fi

        local status
        status=$(echo "$response" | python -c "import sys,json; print(json.loads(sys.stdin.read()).get('status',''))" 2>/dev/null)

        if [[ "$status" == "ok" ]]; then
            local token
            token=$(echo "$response" | python -c "import sys,json; print(json.loads(sys.stdin.read()).get('token',''))" 2>/dev/null)
            local actual_id
            actual_id=$(echo "$response" | python -c "import sys,json; print(json.loads(sys.stdin.read()).get('agent_id',''))" 2>/dev/null)
            ok "Registered successfully!"
            ok "  Agent ID  : ${actual_id}"
            ok "  Token     : ${token}"
            ok "  Mode      : ${mode}"
            ok "  TTL       : ${ttl}s"
            ok ""
            ok "To keep alive, periodically call:"
            ok "  curl -X POST -H 'Content-Type: application/json' \\"
            ok "    -d '{\"token\":\"${token}\"}' \\"
            ok "    http://${host}:${http_port}/agents/heartbeat"
            ok ""
            ok "To poll messages:"
            ok "  curl http://${host}:${http_port}/agents/poll?token=${token}"
        else
            err "Registration failed: ${response}"
            exit 1
        fi
        return 0
    fi

    # Default: TCP registration (foreground, maintains heartbeat until Ctrl-C)
    info "Registering agent '${agent_id}' via TCP (foreground)..."
    info "Press Ctrl-C to disconnect."
    exec python -c "
import sys, socket, json, signal, time

HOST = '${host}'
PORT = int('${port}')
AGENT_ID = '${agent_id}'
HB_INTERVAL = 10  # seconds

def main():
    def cleanup(sig, frame):
        print(f'\n[pluto] Disconnecting {AGENT_ID}...', flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    sock = socket.create_connection((HOST, PORT), timeout=5)
    msg = json.dumps({'op': 'register', 'agent_id': AGENT_ID}) + '\\n'
    sock.sendall(msg.encode())
    sock.settimeout(5)
    resp = sock.recv(4096)
    data = json.loads(resp.decode().strip())
    if data.get('status') != 'ok':
        print(f'[pluto] Registration failed: {data}', file=sys.stderr)
        sys.exit(1)
    print(f'[pluto] Registered as {AGENT_ID} (session={data.get(\"session_id\",\"?\")})', flush=True)
    print(f'[pluto] Sending heartbeat every {HB_INTERVAL}s...', flush=True)

    while True:
        time.sleep(HB_INTERVAL)
        try:
            ping = json.dumps({'op': 'ping'}) + '\\n'
            sock.sendall(ping.encode())
            pong = sock.recv(4096)
            if not pong:
                break
        except Exception as e:
            print(f'[pluto] Connection lost: {e}', file=sys.stderr)
            sys.exit(1)

main()
"
}

# Check if register subcommand is being used
for arg in "$@"; do
    if [[ "$arg" == "register" ]]; then
        handle_register "$@"
        exit $?
    fi
done

# Run the Python client with all provided arguments
exec python "${PYTHON_SCRIPT}" "$@"
