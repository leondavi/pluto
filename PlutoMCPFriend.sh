#!/usr/bin/env bash
# ===========================================================================
# PlutoMCPFriend.sh — Guided launcher for the Pluto MCP adapter.
#
# Wraps an MCP-capable agent CLI (Claude Code, Cursor, Aider with the MCP
# plugin) by registering an MCP server adapter that exposes Pluto operations
# (send / lock / task / ...) as native tool calls. No PTY, no curl, no
# copy-pasted tokens.
#
# Run with no arguments for the interactive setup wizard:
#
#   ./PlutoMCPFriend.sh
#
# Or pass everything explicitly (expert mode):
#
#   ./PlutoMCPFriend.sh --agent-id coder-1 --framework claude --role specialist
#
# See ./PlutoMCPFriend.sh --help for the full option list.
# ===========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_ENTRY="${SCRIPT_DIR}/src_py/agent_mcp_friend/pluto_mcp_friend.py"
CONFIG_FILE="${SCRIPT_DIR}/config/pluto_config.json"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
ROLES_DIR="${SCRIPT_DIR}/library/roles"
VENV_DIR="/tmp/pluto/.venv"
DEFAULT_HOST="127.0.0.1"
DEFAULT_HTTP_PORT="9201"
DEFAULT_TCP_PORT="9200"
PLUTO_VERSION="$(head -1 "${SCRIPT_DIR}/VERSION.md" 2>/dev/null | tr -d '[:space:]' || echo 'unknown')"

# ── Colours ──────────────────────────────────────────────────────────────────
# Defined as real ANSI escape bytes (not the \033 literal) so heredocs that
# substitute these vars produce sequences the terminal actually interprets,
# even when rendered via plain `cat`.
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[0;33m')
CYAN=$(printf '\033[0;36m')
BOLD=$(printf '\033[1m')
DIM=$(printf '\033[2m')
NC=$(printf '\033[0m')

info()    { echo -e "${CYAN}[pluto-mcp]${NC} $*"; }
ok()      { echo -e "${GREEN}[pluto-mcp]${NC} $*"; }
warn()    { echo -e "${YELLOW}[pluto-mcp]${NC} $*"; }
err()     { echo -e "${RED}[pluto-mcp]${NC} $*" >&2; }
section() { echo ""; echo -e "${BOLD}${CYAN}▶  $*${NC}"; }

KNOWN_FRAMEWORKS=("claude" "cursor" "aider" "copilot")

# ── Help text ────────────────────────────────────────────────────────────────

show_help() {
    cat <<EOF
PlutoMCPFriend ${PLUTO_VERSION} — Pluto coordination over MCP.

Usage:
  $(basename "$0")                                        # guided wizard
  $(basename "$0") --agent-id <name> [options]           # expert mode
  $(basename "$0") --help

What it does:
  Registers a Pluto MCP server with your agent CLI so that Pluto operations
  (sending messages, acquiring locks, assigning tasks) become native tool
  calls instead of curl commands. The adapter holds your session token,
  auto-renews lock TTLs, and surfaces inbox messages on every tool result.

Options:
  --agent-id <name>       Agent identity in the Pluto network. Skipping this
                          flag in an interactive terminal launches the wizard.
  --framework <name>      claude | cursor | aider | copilot. Auto-detected.
  --role <name|path>      Apply a role from library/roles/<name>.md on the
                          first turn (Claude only via --append-system-prompt).
  --host <ip>             Pluto server host (default: from config / ${DEFAULT_HOST}).
  --http-port <port>      Pluto HTTP port (default: from config / ${DEFAULT_HTTP_PORT}).
  --ttl-ms <ms>           Session TTL in ms (default: 600000).
  --no-launch             Generate .mcp.json but do not start the framework.
  --no-wizard             Refuse the interactive wizard; require all args.
  --log-level <lvl>       DEBUG | INFO | WARNING | ERROR (default: WARNING).
  --version               Print version and exit.
  --help                  Show this help.
  -- <cmd...>             Pass everything after -- to the agent CLI verbatim.

Examples:
  $(basename "$0")                                                       # wizard
  $(basename "$0") --agent-id coder-1 --framework claude --role specialist
  $(basename "$0") --agent-id reviewer-1 --no-launch                    # config only
EOF
}

# ── Banner ───────────────────────────────────────────────────────────────────

show_banner() {
    cat <<BANNER

    ╔═══════════════════════════════════════════════════╗
    ║                                                   ║
    ║   ★  PlutoMCPFriend  ${PLUTO_VERSION}                       ║
    ║      Pluto Coordination via MCP Tools             ║
    ║                                                   ║
    ╚═══════════════════════════════════════════════════╝
BANNER
}

show_what_it_is() {
    cat <<EOF

  ${BOLD}What is PlutoMCPFriend?${NC}
  ${DIM}─────────────────────────${NC}
  An MCP (Model Context Protocol) adapter for the Pluto coordination
  server. It exposes Pluto operations as native tool calls inside
  Claude Code, Cursor, Aider, and other MCP-capable agent CLIs:

    ${CYAN}pluto_send${NC}            send a message to another agent
    ${CYAN}pluto_broadcast${NC}       broadcast to every connected agent
    ${CYAN}pluto_recv${NC}            drain pending inbox messages
    ${CYAN}pluto_lock_acquire${NC}    grab a write/read lock (auto-renewed)
    ${CYAN}pluto_lock_release${NC}    release the lock + cancel renewal
    ${CYAN}pluto_task_assign${NC}     assign a task to another agent
    ${CYAN}pluto_task_update${NC}     update task status (in_progress, completed, ...)
    ${CYAN}pluto_list_agents${NC}     discover connected peers
    ${DIM}... and 8 more.  See: docs/guide/pluto-mcp-friend.md${NC}

  Inbound messages are auto-attached to any tool result under
  ${CYAN}_pluto_inbox${NC}, so the agent picks them up as a free side-effect of
  doing Pluto-related work.

EOF
}

show_disclaimer() {
    echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  DISCLAIMER                                                      ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo -e "  Pluto is provided ${BOLD}as-is${NC} for research and development purposes only,"
    echo -e "  with no warranty of any kind. You — the user — are responsible for"
    echo -e "  any harm, data loss, or unintended actions taken by AI agents you"
    echo -e "  coordinate via Pluto. Use only in environments you control."
    echo ""
}

# ── venv bootstrap ──────────────────────────────────────────────────────────

ensure_venv() {
    if [[ ! -f "${VENV_DIR}/bin/python" ]]; then
        info "Creating Python venv at ${VENV_DIR} ..."
        mkdir -p "$(dirname "${VENV_DIR}")"
        python3 -m venv "${VENV_DIR}"
    fi
    local marker="${VENV_DIR}/.requirements-installed"
    if [[ ! -f "${marker}" ]] || [[ "${REQUIREMENTS}" -nt "${marker}" ]]; then
        info "Installing Pluto Python dependencies (mcp SDK) ..."
        "${VENV_DIR}/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
        if ! "${VENV_DIR}/bin/pip" install -q -r "${REQUIREMENTS}"; then
            err "pip install -r ${REQUIREMENTS} failed."
            exit 1
        fi
        date > "${marker}"
    fi
}

# ── Pluto server health ─────────────────────────────────────────────────────

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

server_health() {
    local host="$1" port="$2"
    if curl -fsS --max-time 2 "http://${host}:${port}/health" 2>/dev/null; then
        return 0
    fi
    return 1
}

server_version() {
    local host="$1" port="$2"
    server_health "$host" "$port" | "${VENV_DIR}/bin/python" -c \
        'import json,sys
try:
  print(json.load(sys.stdin).get("version","?"))
except Exception:
  print("?")' 2>/dev/null || echo "?"
}

# Print server status; return 0 if reachable, 1 otherwise.
check_pluto_reachable() {
    local host="$1" port="$2"
    info "Checking Pluto server at ${BOLD}${host}:${port}${NC} ..."
    if server_health "$host" "$port" >/dev/null 2>&1; then
        local v
        v=$(server_version "$host" "$port")
        ok "Pluto v${v} is ${GREEN}ONLINE${NC}"
        return 0
    fi
    warn "Pluto server is ${YELLOW}OFFLINE${NC} at ${host}:${port}"
    return 1
}

offer_to_start_server() {
    if ! [[ -t 0 ]]; then
        return 1
    fi
    echo ""
    echo -e "  Pluto is not running. The MCP adapter cannot register without it."
    echo ""
    read -rp "  Start Pluto server in the background now? [Y/n] " ans
    ans="${ans:-y}"
    case "${ans,,}" in
        y|yes)
            if [[ ! -x "${SCRIPT_DIR}/PlutoServer.sh" ]]; then
                err "PlutoServer.sh not found or not executable."
                return 1
            fi
            info "Running ./PlutoServer.sh --daemon ..."
            if ! "${SCRIPT_DIR}/PlutoServer.sh" --daemon; then
                err "Failed to start Pluto server."
                return 1
            fi
            sleep 1
            return 0
            ;;
        *)
            warn "Continuing without Pluto. The agent will start, but pluto_* tools will return errors until the server is up."
            return 0
            ;;
    esac
}

# ── .mcp.json generation ────────────────────────────────────────────────────

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

# ── Role discovery ──────────────────────────────────────────────────────────

list_available_roles() {
    [[ -d "${ROLES_DIR}" ]] || return 0
    for f in "${ROLES_DIR}"/*.md; do
        [[ -f "$f" ]] || continue
        local name
        name="$(basename "$f" .md)"
        case "$name" in
            README|_*) continue ;;
        esac
        echo "$name"
    done
}

# ── Framework detection ─────────────────────────────────────────────────────

detect_frameworks() {
    local found=()
    for fw in "${KNOWN_FRAMEWORKS[@]}"; do
        if command -v "$fw" >/dev/null 2>&1; then
            found+=("$fw")
        fi
    done
    printf '%s\n' "${found[@]}"
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

print_post_launch_tips() {
    local agent_id="$1" host="$2" port="$3" framework="$4"
    cat <<EOF

  ${BOLD}${GREEN}Setup complete.${NC}  Launching ${BOLD}${framework}${NC} ...

  ${BOLD}Inside your agent:${NC}
    • Pluto operations are tool calls — type a request and the agent
      will call e.g. ${CYAN}pluto_lock_acquire${NC} on its own.
    • Switch role mid-session via slash menu:
        ${DIM}/pluto-role-specialist${NC}
        ${DIM}/pluto-role-orchestrator${NC}
        ${DIM}/pluto-role-reviewer${NC}
    • Pending inbox without acking:
        ${DIM}@pluto://inbox${NC}

  ${BOLD}Send a test message from another terminal:${NC}
    curl -X POST http://${host}:${port}/messages/send \\
      -H 'Content-Type: application/json' \\
      -d '{"to":"${agent_id}","payload":{"type":"hello","text":"Welcome!"}}'

  ${BOLD}Stop the Pluto server later with:${NC}
    ./PlutoServer.sh --kill

EOF
}

# ── Wizard steps ────────────────────────────────────────────────────────────

wizard_intro() {
    show_banner
    show_what_it_is
    show_disclaimer
    if [[ "${SCRIPT_DIR}" != "${PWD}" ]]; then
        info "Pluto install dir: ${BOLD}${SCRIPT_DIR}${NC}"
        echo ""
    fi
    cat <<EOF
  ${BOLD}This wizard will:${NC}
    1. Verify the Pluto server is running (and offer to start it if not)
    2. Ask for an agent ID for this session
    3. Optionally pick a role for the agent
    4. Detect / pick the agent CLI to launch
    5. Generate .mcp.json and launch the agent CLI

EOF
    read -rp "  Press Enter to begin (Ctrl-C to abort) ... " _ < /dev/tty
}

wizard_step_server() {
    section "Step 1/5 — Pluto server"
    local host="$1" port="$2"
    if check_pluto_reachable "$host" "$port"; then
        return 0
    fi
    offer_to_start_server || true
    if check_pluto_reachable "$host" "$port"; then
        return 0
    fi
    return 1
}

wizard_step_agent_id() {
    {
        section "Step 2/5 — Agent ID"
        cat <<EOF

  Pick a unique identifier for this agent. Other agents will use this
  name when sending you messages. Examples: ${CYAN}coder-1${NC}, ${CYAN}reviewer-2${NC},
  ${CYAN}orchestrator${NC}.

EOF
    } >&2
    local id=""
    while [[ -z "$id" ]]; do
        # `read -rp` writes the prompt to stderr already.
        read -rp "  Agent ID: " id < /dev/tty
        if [[ -z "$id" ]]; then
            warn "Agent ID cannot be empty." >&2
        fi
    done
    echo "$id"
}

wizard_step_role() {
    section "Step 3/5 — Role" >&2
    local roles=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && roles+=("$line")
    done < <(list_available_roles)

    if (( ${#roles[@]} == 0 )); then
        warn "No role files found in ${ROLES_DIR}. Skipping role selection." >&2
        return 0
    fi

    {
        cat <<EOF

  ${DIM}A role is an instruction set (e.g. "you are a code reviewer ...") that
  is applied on the agent's first turn. You can also switch roles later
  inside the agent via the slash menu. Pick one, or skip.${NC}

    ${BOLD}0${NC}) ${DIM}none — no role applied; pick later via /pluto-role-<name>${NC}
EOF
        local i=1
        for r in "${roles[@]}"; do
            printf "    ${BOLD}%d${NC}) %s\n" "$i" "$r"
            i=$((i + 1))
        done
        echo ""
    } >&2

    local choice
    while true; do
        read -rp "  Choice [0-${#roles[@]}, default 0]: " choice < /dev/tty
        choice="${choice:-0}"
        if [[ "$choice" == "0" ]]; then
            return 0
        fi
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#roles[@]} )); then
            echo "${roles[$((choice - 1))]}"
            return 0
        fi
        warn "Invalid choice. Enter a number between 0 and ${#roles[@]}." >&2
    done
}

wizard_step_framework() {
    section "Step 4/5 — Agent CLI" >&2
    local available=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && available+=("$line")
    done < <(detect_frameworks)

    if (( ${#available[@]} == 0 )); then
        {
            warn "No supported agent CLI found in PATH (${KNOWN_FRAMEWORKS[*]})."
            echo ""
            cat <<EOF
  ${DIM}You can install one and re-run, or pass --no-launch to generate
  .mcp.json without launching anything.${NC}

EOF
        } >&2
        local cli=""
        read -rp "  Type a CLI name to attempt anyway, or 'skip' to --no-launch: " cli < /dev/tty
        if [[ -z "$cli" || "$cli" == "skip" ]]; then
            echo "__skip__"
        else
            echo "$cli"
        fi
        return 0
    fi

    if (( ${#available[@]} == 1 )); then
        local fw="${available[0]}"
        info "Auto-detected: ${BOLD}${fw}${NC} ($(command -v "$fw"))" >&2
        echo "$fw"
        return 0
    fi

    {
        cat <<EOF

  Multiple agent CLIs found. Pick one:

EOF
        local i=1
        for fw in "${available[@]}"; do
            printf "    ${BOLD}%d${NC}) %s   ${DIM}(%s)${NC}\n" "$i" "$fw" "$(command -v "$fw")"
            i=$((i + 1))
        done
        echo ""
    } >&2

    local choice
    while true; do
        read -rp "  Choice [1-${#available[@]}]: " choice < /dev/tty
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#available[@]} )); then
            echo "${available[$((choice - 1))]}"
            return 0
        fi
        warn "Invalid choice." >&2
    done
}

wizard_step_confirm() {
    section "Step 5/5 — Ready to launch"
    local agent_id="$1" host="$2" port="$3" role="$4" framework="$5"

    cat <<EOF

  ${BOLD}Summary:${NC}
    Agent ID    : ${CYAN}${agent_id}${NC}
    Pluto       : ${host}:${port}
    Role        : ${role:-${DIM}(none)${NC}}
    Framework   : ${framework}
    .mcp.json   : ${SCRIPT_DIR}/.mcp.json

EOF
    if [[ "${framework}" != "claude" && -n "${role}" ]]; then
        warn "${framework} does not support startup system-prompt injection."
        warn "After launch, run the slash command: ${CYAN}/pluto-role-${role}${NC}"
        echo ""
    fi

    read -rp "  Press Enter to launch (Ctrl-C to abort) ... " _ < /dev/tty
    echo ""
}

# ── Launch ──────────────────────────────────────────────────────────────────

launch_framework() {
    local framework="$1" role="$2" agent_id="$3" host="$4" port="$5"
    shift 5
    local extra=("$@")

    print_post_launch_tips "${agent_id}" "${host}" "${port}" "${framework}"

    case "${framework}" in
        claude)
            local cmd=("claude" "--mcp-config" "${SCRIPT_DIR}/.mcp.json")
            if [[ -n "${role}" ]]; then
                local sys_prompt
                sys_prompt=$(build_role_system_prompt "${agent_id}" "${host}" "${port}" "${role}")
                cmd+=("--append-system-prompt" "${sys_prompt}")
            fi
            # bash 3.2-safe empty-array expansion (macOS default bash).
            if (( ${#extra[@]} > 0 )); then
                cmd+=("${extra[@]}")
            fi
            exec "${cmd[@]}"
            ;;
        cursor|aider|copilot|*)
            local cmd=("${framework}")
            if (( ${#extra[@]} > 0 )); then
                cmd+=("${extra[@]}")
            fi
            info "Launching: ${cmd[*]}"
            exec "${cmd[@]}"
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
    local no_wizard=false
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
            --no-wizard) no_wizard=true; shift ;;
            --) past_separator=true; shift ;;
            *)  err "Unknown option: $1"; show_help; exit 1 ;;
        esac
    done

    ensure_venv

    # Resolve host/port from config when not given.
    [[ -n "${host}" ]] || host=$(read_config_value "host_ip" "${DEFAULT_HOST}")
    [[ -n "${http_port}" ]] || http_port=$(read_config_value "host_http_port" "${DEFAULT_HTTP_PORT}")

    # ── Wizard mode trigger ─────────────────────────────────────────────────
    # Wizard runs when --agent-id is missing AND stdin is a tty AND
    # --no-wizard wasn't passed. Otherwise fail-fast in non-interactive
    # environments.
    if [[ -z "${agent_id}" ]]; then
        if $no_wizard; then
            err "--agent-id is required (--no-wizard prevents the interactive prompt)."
            show_help
            exit 1
        fi
        if [[ ! -t 0 ]]; then
            err "--agent-id is required when stdin is not a terminal."
            show_help
            exit 1
        fi

        wizard_intro

        if ! wizard_step_server "${host}" "${http_port}"; then
            warn "Continuing without a reachable Pluto server."
        fi

        agent_id=$(wizard_step_agent_id)
        if [[ -z "${role}" ]]; then
            role=$(wizard_step_role)
        fi
        if [[ -z "${framework}" ]]; then
            framework=$(wizard_step_framework)
            if [[ "${framework}" == "__skip__" ]]; then
                no_launch=true
                framework=""
            fi
        fi

        # Full summary + confirm before launch.
        if ! $no_launch; then
            wizard_step_confirm "${agent_id}" "${host}" "${http_port}" "${role}" "${framework}"
        fi
    else
        # Expert mode: brief banner + reachability check, no prompts.
        show_banner
        info "Agent ID:   ${BOLD}${agent_id}${NC}"
        info "Pluto:      ${host}:${http_port}"
        [[ -n "${role}" ]] && info "Role:       ${role}"
        check_pluto_reachable "${host}" "${http_port}" || \
            warn "Continuing — pluto_* tools will return errors until the server is up."
    fi

    # ── Generate .mcp.json ──────────────────────────────────────────────────
    local mcp_json
    mcp_json=$(write_mcp_json "${agent_id}" "${host}" "${http_port}" "${ttl_ms}" "${log_level}")
    ok "Wrote MCP config:  ${mcp_json}"

    if $no_launch; then
        cat <<EOF

  ${BOLD}Use this config from any MCP-capable agent:${NC}
    ${CYAN}claude --mcp-config ${mcp_json}${NC}
    ${CYAN}cursor  ${DIM}# add ${mcp_json} via your Cursor MCP settings${NC}

EOF
        exit 0
    fi

    # Auto-detect framework if not chosen yet (covers expert mode without --framework)
    if [[ -z "${framework}" ]]; then
        local detected=()
        while IFS= read -r line; do
            [[ -n "$line" ]] && detected+=("$line")
        done < <(detect_frameworks)
        if (( ${#detected[@]} == 0 )); then
            err "No supported agent CLI found in PATH (${KNOWN_FRAMEWORKS[*]})."
            err "Install one of them, or re-run with --no-launch."
            exit 1
        fi
        framework="${detected[0]}"
        info "Auto-detected framework: ${framework}"
    fi

    # bash 3.2-safe empty-array expansion (macOS default bash).
    if (( ${#extra_cmd[@]} > 0 )); then
        launch_framework "${framework}" "${role}" "${agent_id}" "${host}" \
            "${http_port}" "${extra_cmd[@]}"
    else
        launch_framework "${framework}" "${role}" "${agent_id}" "${host}" \
            "${http_port}"
    fi
}

main "$@"
