#!/usr/bin/env bash
# ===========================================================================
# PlutoAgentFriend.sh — Launch an AI agent with Pluto coordination.
#
# Wraps an agent CLI (Claude, Copilot, Aider, etc.) with a PTY-based I/O
# proxy that monitors the agent's output and injects Pluto messages when
# the agent is idle.
#
# Usage:
#   ./PlutoAgentFriend.sh --agent-id <name> [--framework <name>] [options] [-- cmd...]
#   ./PlutoAgentFriend.sh --help
#
# Examples:
#   ./PlutoAgentFriend.sh --agent-id coder-1                          # auto-detect
#   ./PlutoAgentFriend.sh --agent-id coder-1 --framework claude       # use Claude
#   ./PlutoAgentFriend.sh --agent-id coder-1 --framework copilot      # use Copilot
#   ./PlutoAgentFriend.sh --agent-id coder-1 -- /path/to/my-agent     # custom cmd
#   ./PlutoAgentFriend.sh --agent-id coder-1 --mode confirm           # confirm mode
# ===========================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAP_SCRIPT="${SCRIPT_DIR}/src_py/agent_friend/pluto_agent_friend.py"
CONFIG_FILE="${SCRIPT_DIR}/config/pluto_config.json"
VENV_DIR="/tmp/pluto/.venv"
PLUTO_VERSION="$(cat "${SCRIPT_DIR}/VERSION.md" 2>/dev/null | tr -d '[:space:]' || echo 'unknown')"
# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── Known frameworks ─────────────────────────────────────────────────────────
KNOWN_FRAMEWORKS=("claude" "copilot" "aider" "cursor")

# ── Helper functions ──────────────────────────────────────────────────────────

info()  { echo -e "${CYAN}[pluto-friend]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto-friend]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto-friend]${NC} $*"; }
err()   { echo -e "${RED}[pluto-friend]${NC} $*" >&2; }

show_help() {
    cat <<'EOF'

  ╔══════════════════════════════════════════════════════════╗
  ║              PlutoAgentFriend                            ║
  ║   Launch AI agents with Pluto coordination & messaging  ║
  ╚══════════════════════════════════════════════════════════╝

USAGE
    ./PlutoAgentFriend.sh --agent-id <name> [OPTIONS] [-- command...]

REQUIRED
    --agent-id <name>       Agent ID for Pluto registration (e.g. coder-1)

OPTIONS
    --framework <name>      Agent framework: claude, copilot, aider, cursor
                            If omitted, scans for available agents and prompts
    --mode <mode>           Injection mode (default: auto)
                              auto    — inject messages when agent is idle
                              confirm — notify + auto-inject after 10s
                              manual  — notify only, user handles input
    --host <ip>             Pluto server host (default: from config or localhost)
    --http-port <port>      Pluto HTTP port (default: from config or 9001)
    --ready-pattern <regex> Regex matching agent's "ready for input" prompt
    --silence-timeout <sec> Seconds of silence = agent idle (default: 3.0)
    --poll-timeout <sec>    Pluto long-poll timeout (default: 15)
    --guide <path>          Skill guide file to inject on startup
                            (default: auto-discovers agent_friend_guide.md)
    --no-guide              Disable automatic guide injection
    --verbose               Enable debug logging
    --help, -h              Show this help

EXAMPLES
    # Auto-detect agent framework
    ./PlutoAgentFriend.sh --agent-id coder-1

    # Use Claude Code specifically
    ./PlutoAgentFriend.sh --agent-id coder-1 --framework claude

    # Use Copilot CLI with confirm mode
    ./PlutoAgentFriend.sh --agent-id reviewer-2 --framework copilot --mode confirm

    # Custom agent command
    ./PlutoAgentFriend.sh --agent-id worker-1 -- python3 my_agent.py

    # Manual mode — just show notifications
    ./PlutoAgentFriend.sh --agent-id watcher-1 --mode manual -- bash

HOW IT WORKS
    PlutoAgentFriend wraps an agent CLI in a pseudo-terminal (PTY), proxying
    all I/O transparently. In the background, it connects to the Pluto server
    and long-polls for incoming messages (direct, broadcast, tasks, topics).

    When the agent finishes its current turn and is idle (detected by output
    silence or a prompt pattern), the wrapper injects pending Pluto messages
    into the agent's stdin as natural-language instructions.

    Three safety rules:
      1. User input always has priority
      2. Never inject when the agent is asking the user a question
      3. Injections are shown to the user for transparency

EOF
}

# Read config file for defaults
read_config() {
    local key="$1"
    local default="$2"
    if [[ -f "${CONFIG_FILE}" ]]; then
        local val
        val=$(python3 -c "
import json, sys
try:
    with open('${CONFIG_FILE}') as f:
        c = json.load(f)
    s = c.get('pluto_server', {})
    print(s.get('${key}', ''))
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

# Detect which agent frameworks are available
detect_frameworks() {
    local found=()
    for fw in "${KNOWN_FRAMEWORKS[@]}"; do
        if command -v "${fw}" &>/dev/null; then
            found+=("${fw}")
        fi
    done
    echo "${found[@]}"
}

# Show Pluto server status
show_pluto_status() {
    local host="$1"
    local http_port="$2"

    info "Checking Pluto server at ${BOLD}${host}:${http_port}${NC}..."
    local resp
    resp=$(curl -s --connect-timeout 3 "http://${host}:${http_port}/health" 2>/dev/null || echo "")

    if [[ -n "${resp}" ]]; then
        local version
        version=$(echo "${resp}" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('version','?'))" 2>/dev/null || echo "?")
        ok "Pluto server is ${GREEN}ONLINE${NC} (v${version}) at ${host}:${http_port}"
        return 0
    else
        warn "Pluto server is ${YELLOW}OFFLINE${NC} at ${host}:${http_port}"
        warn "Agent will start without Pluto integration"
        return 1
    fi
}

# Ensure Python venv exists
ensure_venv() {
    if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
        warn "Creating Python virtual environment at ${VENV_DIR}..."
        python3 -m venv "${VENV_DIR}"
    fi
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    local agent_id=""
    local framework=""
    local mode="auto"
    local host=""
    local http_port=""
    local ready_pattern=""
    local silence_timeout=""
    local poll_timeout=""
    local verbose=""
    local guide=""
    local no_guide=""
    local extra_cmd=()
    local past_separator=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        if $past_separator; then
            extra_cmd+=("$1")
            shift
            continue
        fi
        case "$1" in
            --help|-h)
                show_help
                exit 0
                ;;
            --agent-id)
                agent_id="$2"
                shift 2
                ;;
            --framework)
                framework="$2"
                shift 2
                ;;
            --mode)
                mode="$2"
                shift 2
                ;;
            --host)
                host="$2"
                shift 2
                ;;
            --http-port)
                http_port="$2"
                shift 2
                ;;
            --ready-pattern)
                ready_pattern="$2"
                shift 2
                ;;
            --silence-timeout)
                silence_timeout="$2"
                shift 2
                ;;
            --poll-timeout)
                poll_timeout="$2"
                shift 2
                ;;
            --verbose)
                verbose="true"
                shift
                ;;
            --guide)
                guide="$2"
                shift 2
                ;;
            --no-guide)
                no_guide="true"
                shift
                ;;
            --)
                past_separator=true
                shift
                ;;
            *)
                # Treat as part of extra command if no -- separator
                extra_cmd+=("$1")
                shift
                ;;
        esac
    done

    local banner_shown=false

    # Interactive prompts when arguments are missing
    if [[ -z "${agent_id}" ]]; then
        # Show banner early for interactive mode
        banner_shown=true
        echo ""
        echo -e "${CYAN}"
        cat <<BANNER
    ╔═══════════════════════════════════════════════╗
    ║                                               ║
    ║   ★  PlutoAgentFriend  ${PLUTO_VERSION}              ║
    ║      AI Agent + Pluto Coordination Wrapper    ║
    ║                                               ║
    ╚═══════════════════════════════════════════════╝
BANNER
        echo -e "${NC}"

        # Prompt for agent-id
        info "No --agent-id provided. Enter one now."
        echo ""
        read -rp "  Agent ID (e.g. coder-1): " agent_id
        if [[ -z "${agent_id}" ]]; then
            err "Agent ID is required."
            exit 1
        fi
        echo ""

        # If no framework or custom command, scan and prompt
        if [[ -z "${framework}" && ${#extra_cmd[@]} -eq 0 ]]; then
            info "Scanning for available agent frameworks..."
            local available
            available=$(detect_frameworks)

            if [[ -n "${available}" ]]; then
                local fw_array=()
                read -ra fw_array <<< "${available}"

                echo ""
                info "Available agent frameworks:"
                echo ""
                local i=1
                for fw in "${fw_array[@]}"; do
                    local fw_path
                    fw_path=$(command -v "${fw}")
                    echo -e "    ${BOLD}${i}.${NC} ${fw} ${DIM}(${fw_path})${NC}"
                    ((i++))
                done
                echo ""
                echo -e "    ${BOLD}$((i)).${NC} Enter a custom command"
                echo ""

                local choice
                read -rp "  Select framework (number) or press Enter for #1: " choice

                if [[ -z "${choice}" ]]; then
                    choice=1
                fi

                if [[ "${choice}" =~ ^[0-9]+$ ]]; then
                    if (( choice >= 1 && choice <= ${#fw_array[@]} )); then
                        framework="${fw_array[$((choice - 1))]}"
                        ok "Selected: ${BOLD}${framework}${NC}"
                    elif (( choice == ${#fw_array[@]} + 1 )); then
                        read -rp "  Enter command: " custom_cmd
                        extra_cmd=($custom_cmd)
                    else
                        err "Invalid choice."
                        exit 1
                    fi
                else
                    err "Invalid choice."
                    exit 1
                fi
            else
                warn "No known agent frameworks found on PATH."
                warn "Known: ${KNOWN_FRAMEWORKS[*]}"
                echo ""
                read -rp "  Enter a custom agent command (or Ctrl-C to quit): " custom_cmd
                if [[ -z "${custom_cmd}" ]]; then
                    err "No command provided."
                    exit 1
                fi
                extra_cmd=($custom_cmd)
            fi
            echo ""
        fi
    fi

    # Load defaults from config
    if [[ -z "${host}" ]]; then
        host=$(read_config "host_ip" "localhost")
    fi
    if [[ -z "${http_port}" ]]; then
        http_port=$(read_config "host_http_port" "9001")
    fi

    # Banner (skip if already shown in interactive mode)
    if [[ "${banner_shown}" != "true" ]]; then
        echo ""
        echo -e "${CYAN}"
        cat <<BANNER
    ╔═══════════════════════════════════════════════╗
    ║                                               ║
    ║   ★  PlutoAgentFriend  ${PLUTO_VERSION}              ║
    ║      AI Agent + Pluto Coordination Wrapper    ║
    ║                                               ║
    ╚═══════════════════════════════════════════════╝
BANNER
        echo -e "${NC}"
    fi

    # Show Pluto server status
    show_pluto_status "${host}" "${http_port}" || true
    echo ""

    # Determine framework / command
    if [[ ${#extra_cmd[@]} -gt 0 ]]; then
        # User provided an explicit command
        info "Agent command: ${extra_cmd[*]}"
    elif [[ -n "${framework}" ]]; then
        # Framework specified
        if ! command -v "${framework}" &>/dev/null; then
            err "Framework '${framework}' not found on PATH"
            err "Install it or specify a command after --"
            exit 1
        fi
        info "Using framework: ${BOLD}${framework}${NC}"
    else
        # Auto-detect
        info "Scanning for available agent frameworks..."
        local available
        available=$(detect_frameworks)

        if [[ -z "${available}" ]]; then
            err "No known agent frameworks found on PATH."
            echo ""
            err "Known frameworks: ${KNOWN_FRAMEWORKS[*]}"
            err ""
            err "Options:"
            err "  1. Install one of the above frameworks"
            err "  2. Use --framework <name> to specify one"
            err "  3. Use -- <command> to specify a custom agent command"
            exit 1
        fi

        # Convert to array
        local fw_array=()
        read -ra fw_array <<< "${available}"

        if [[ ${#fw_array[@]} -eq 1 ]]; then
            framework="${fw_array[0]}"
            local fw_path
            fw_path=$(command -v "${framework}")
            ok "Auto-detected: ${BOLD}${framework}${NC} (${fw_path})"
        else
            echo ""
            info "Multiple agent frameworks detected:"
            echo ""
            local i=1
            for fw in "${fw_array[@]}"; do
                local fw_path
                fw_path=$(command -v "${fw}")
                echo -e "    ${BOLD}${i}.${NC} ${fw} ${DIM}(${fw_path})${NC}"
                ((i++))
            done
            echo ""
            echo -e "    ${BOLD}$((i)).${NC} Enter a custom command"
            echo ""

            local choice
            read -rp "  Select framework (number): " choice

            if [[ "${choice}" =~ ^[0-9]+$ ]]; then
                if (( choice >= 1 && choice <= ${#fw_array[@]} )); then
                    framework="${fw_array[$((choice - 1))]}"
                    ok "Selected: ${BOLD}${framework}${NC}"
                elif (( choice == ${#fw_array[@]} + 1 )); then
                    read -rp "  Enter command: " custom_cmd
                    extra_cmd=($custom_cmd)
                else
                    err "Invalid choice."
                    exit 1
                fi
            else
                err "Invalid choice."
                exit 1
            fi
        fi
    fi

    # Activate venv
    ensure_venv

    # Build the python command
    local py_args=()
    py_args+=("--agent-id" "${agent_id}")
    py_args+=("--host" "${host}")
    py_args+=("--http-port" "${http_port}")
    py_args+=("--mode" "${mode}")

    if [[ -n "${framework}" ]]; then
        py_args+=("--framework" "${framework}")
    fi
    if [[ -n "${ready_pattern}" ]]; then
        py_args+=("--ready-pattern" "${ready_pattern}")
    fi
    if [[ -n "${silence_timeout}" ]]; then
        py_args+=("--silence-timeout" "${silence_timeout}")
    fi
    if [[ -n "${poll_timeout}" ]]; then
        py_args+=("--poll-timeout" "${poll_timeout}")
    fi
    if [[ -n "${verbose}" ]]; then
        py_args+=("--verbose")
    fi
    if [[ -n "${guide}" ]]; then
        py_args+=("--guide" "${guide}")
    fi
    if [[ -n "${no_guide}" ]]; then
        py_args+=("--no-guide")
    fi

    if [[ ${#extra_cmd[@]} -gt 0 ]]; then
        py_args+=("--" "${extra_cmd[@]}")
    fi

    info "Starting agent wrapper..."
    echo ""

    exec python3 "${WRAP_SCRIPT}" "${py_args[@]}"
}

main "$@"
