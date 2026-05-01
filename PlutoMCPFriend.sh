#!/usr/bin/env bash
# ===========================================================================
# PlutoMCPFriend.sh — Guided launcher for the Pluto MCP adapter (Claude Code).
#
# Registers an MCP server adapter with Claude Code that exposes Pluto
# operations (send / lock / task / ...) as native tool calls. No PTY, no
# curl, no copy-pasted tokens.
#
# Only Claude Code is supported as the agent CLI: the launcher relies on
# Claude's --mcp-config and --append-system-prompt flags for the role
# auto-injection path, neither of which has stable equivalents in Cursor
# or Aider. (For non-Claude CLIs use PlutoAgentFriend.sh instead.)
#
# Run with no arguments for the interactive setup wizard:
#
#   ./PlutoMCPFriend.sh
#
# Or pass everything explicitly (expert mode):
#
#   ./PlutoMCPFriend.sh --agent-id coder-1 --role specialist
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
DEFAULT_WAIT_TIMEOUT_S="60"
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

# Supported agent CLI. Hard-coded to claude — Cursor / Aider don't have
# stable equivalents to claude's --mcp-config / --append-system-prompt
# pair, which the role auto-injection path depends on.
SUPPORTED_CLI="claude"

# ── Help text ────────────────────────────────────────────────────────────────

show_help() {
    cat <<EOF
PlutoMCPFriend ${PLUTO_VERSION} — Pluto coordination over MCP for Claude Code.

Usage:
  $(basename "$0")                                        # guided wizard
  $(basename "$0") --agent-id <name> [options]           # expert mode
  $(basename "$0") --help

What it does:
  Registers a Pluto MCP server with Claude Code so that Pluto operations
  (sending messages, acquiring locks, assigning tasks) become native tool
  calls instead of curl commands. The adapter holds your session token,
  auto-renews lock TTLs, and surfaces inbox messages on every tool result.

  Only Claude Code is supported. For Cursor / Aider / Copilot, use
  PlutoAgentFriend.sh instead — the PTY-based wrapper works everywhere.

Options:
  --agent-id <name>       Agent identity in the Pluto network. Skipping this
                          flag in an interactive terminal launches the wizard.
  --role <name|path>      Apply a role from library/roles/<name>.md on the
                          first turn via Claude's --append-system-prompt.
  --host <ip>             Pluto server host (default: from config / ${DEFAULT_HOST}).
  --http-port <port>      Pluto HTTP port (default: from config / ${DEFAULT_HTTP_PORT}).
  --ttl-ms <ms>           Session TTL in ms (default: 600000).
  --wait-timeout-s <sec>  pluto_wait_for_messages per-call block duration
                          (default: ${DEFAULT_WAIT_TIMEOUT_S}). The watcher subagent loops short
                          calls of this length so it produces output
                          regularly and never trips Claude Code's stream
                          watchdog. Keep <=120 to be safe.
  --no-launch             Generate .mcp.json but do not start Claude.
  --no-wizard             Refuse the interactive wizard; require all args.
  --log-level <lvl>       DEBUG | INFO | WARNING | ERROR (default: WARNING).
  --version               Print version and exit.
  --help                  Show this help.
  -- <cmd...>             Pass everything after -- to claude verbatim.

Examples:
  $(basename "$0")                                                  # wizard
  $(basename "$0") --agent-id coder-1 --role specialist
  $(basename "$0") --agent-id reviewer-1 --wait-timeout-s 90         # tune watcher cycle
  $(basename "$0") --agent-id worker-1 --no-launch                   # config only
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
  ${BOLD}Claude Code${NC}:

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

# Verify ${VENV_DIR} is a real isolated venv with working pip.
# Returns 0 if valid, 1 otherwise.
#
# Several signals must all agree because partial / corrupt venvs are
# surprisingly common on macOS:
#
#   • bin/python exists and is executable
#   • pyvenv.cfg is present (created by `python3 -m venv`)
#   • sys.prefix != sys.base_prefix (Python is *actually* isolated, not
#     a stray symlink to the host Homebrew Python — that latter case
#     causes pip install to hit the PEP 668 "externally-managed-
#     environment" wall as if the venv weren't there)
#   • pip is importable as a module (``python -m pip --version``)
is_valid_venv() {
    local py="${VENV_DIR}/bin/python"
    [[ -x "${py}" ]] || return 1
    [[ -f "${VENV_DIR}/pyvenv.cfg" ]] || return 1
    "${py}" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)' \
        >/dev/null 2>&1 || return 1
    "${py}" -m pip --version >/dev/null 2>&1 || return 1
    return 0
}

ensure_venv() {
    local py="${VENV_DIR}/bin/python"

    # If the venv directory exists but is not a valid isolated venv,
    # nuke it. Any partial / corrupt state — bin/python missing, pip
    # module missing, sys.prefix == base_prefix (i.e. not isolated),
    # missing pyvenv.cfg — is treated the same way: rebuild from
    # scratch. Cheaper and far more reliable than trying to repair.
    if [[ -e "${VENV_DIR}" ]] && ! is_valid_venv; then
        warn "Existing venv at ${VENV_DIR} is invalid or partial. Recreating ..."
        rm -rf "${VENV_DIR}"
    fi

    if [[ ! -e "${VENV_DIR}" ]]; then
        info "Creating Python venv at ${VENV_DIR} ..."
        mkdir -p "$(dirname "${VENV_DIR}")"
        if ! python3 -m venv "${VENV_DIR}"; then
            err "Failed to create venv at ${VENV_DIR}."
            err "Diagnose with:  python3 -m venv /tmp/pluto-venv-test"
            err "On macOS Homebrew, try: brew reinstall python@3"
            exit 1
        fi
        if ! is_valid_venv; then
            err "Created venv at ${VENV_DIR} is not properly isolated."
            err "  pyvenv.cfg present? $([ -f "${VENV_DIR}/pyvenv.cfg" ] && echo yes || echo NO)"
            err "  python isolated?    $("${py}" -c 'import sys; print(sys.prefix != sys.base_prefix)' 2>/dev/null || echo unknown)"
            err "  pip module present? $("${py}" -m pip --version 2>&1 | head -1)"
            err ""
            err "Your host python3 may be misconfigured. Try:"
            err "  brew reinstall python@3   # macOS Homebrew"
            err "  apt install python3-venv  # Debian / Ubuntu"
            exit 1
        fi
    fi

    local marker="${VENV_DIR}/.requirements-installed"
    if [[ -f "${marker}" ]] && [[ ! "${REQUIREMENTS}" -nt "${marker}" ]]; then
        return 0  # already installed, nothing to do
    fi

    info "Installing Pluto Python dependencies (mcp SDK) ..."
    # Always use ``python -m pip``; never ``bin/pip``. Some Python builds
    # skip creating the pip console script even in valid venvs.
    "${py}" -m pip install -q --upgrade pip >/dev/null 2>&1 || true

    local pip_log
    pip_log=$(mktemp -t pluto-pip.XXXXXX)
    if ! "${py}" -m pip install -r "${REQUIREMENTS}" >"${pip_log}" 2>&1; then
        # PEP 668 inside a freshly-created venv means our isolation check
        # passed but pip is still seeing the host's EXTERNALLY_MANAGED
        # marker — symptom of a deeply broken Python install. Surface
        # the actual pip output so the user can see what went wrong.
        if grep -q "externally-managed-environment" "${pip_log}"; then
            err "pip install hit PEP 668 even inside the venv at ${VENV_DIR}."
            err "This means your host python3 isn't producing real isolated"
            err "venvs. The venv has been left in place for inspection:"
            err "  ${VENV_DIR}/pyvenv.cfg"
            err "  ${py} -c 'import sys; print(sys.prefix, sys.base_prefix)'"
            err ""
            err "Likely fixes:"
            err "  • macOS Homebrew:  brew reinstall python@3"
            err "  • Debian/Ubuntu:   apt install python3-venv"
            err "  • Multiple Pythons in PATH: hash -r and re-run, or"
            err "    point to a known-good python3 explicitly."
        else
            err "pip install -r ${REQUIREMENTS} failed:"
            tail -20 "${pip_log}" | sed 's/^/    /' >&2
        fi
        rm -f "${pip_log}"
        exit 1
    fi
    rm -f "${pip_log}"
    date > "${marker}"
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
    local agent_id="$1" host="$2" port="$3" ttl="$4" log_level="$5" wait_s="$6"
    local target="${SCRIPT_DIR}/.mcp.json"
    "${VENV_DIR}/bin/python" - "$target" "$agent_id" "$host" "$port" "$ttl" \
        "$log_level" "$wait_s" "${VENV_DIR}/bin/python" "${PY_ENTRY}" <<'PYEOF'
import json, os, sys
(target, agent_id, host, port, ttl, log_level, wait_s, py_bin,
 entry) = sys.argv[1:]
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
        "--wait-timeout-s", str(wait_s),
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

# ── Claude detection ────────────────────────────────────────────────────────

claude_path() {
    command -v "${SUPPORTED_CLI}" 2>/dev/null
}

build_role_system_prompt() {
    local agent_id="$1" host="$2" port="$3" role="$4" wait_s="$5"
    "${VENV_DIR}/bin/python" - "$agent_id" "$host" "$port" "$role" \
        "$wait_s" "${SCRIPT_DIR}" <<'PYEOF'
import sys, os
agent_id, host, port, role, wait_s, project_root = sys.argv[1:]
sys.path.insert(0, os.path.join(project_root, "src_py"))
from agent_mcp_friend.prompts import build_role_prompt_body
print(build_role_prompt_body(
    role, host=host, http_port=int(port), agent_id=agent_id,
    wait_timeout_s=int(wait_s),
))
PYEOF
}

print_post_launch_tips() {
    local agent_id="$1" host="$2" port="$3" wait_s="$4"

    cat <<EOF

  ${BOLD}${GREEN}Setup complete.${NC}  Launching ${BOLD}claude${NC} ...

  ${BOLD}═══ Pluto Skills API — what's available inside Claude ═══${NC}

  ${BOLD}Slash commands${NC} ${DIM}(type /pluto- to autocomplete from the slash menu)${NC}

    ${CYAN}Quick actions${NC} ${DIM}— one-keystroke shortcuts; no need to type a request${NC}
      ${DIM}/pluto-check${NC}        drain inbox now and summarize what arrived
      ${DIM}/pluto-watch${NC}        start a chat-speed inbox watcher (background Task)
      ${DIM}/pluto-status${NC}       my id, connected peers, inbox depth, locks I hold

    ${CYAN}Roles${NC} ${DIM}— adopt a behavioural role; switch any time${NC}
EOF

    # Dynamically list every role file as a slash command.
    local roles=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && roles+=("$line")
    done < <(list_available_roles)

    local r
    for r in "${roles[@]}"; do
        printf "      ${DIM}/pluto-role-%s${NC}\n" "$r"
    done

    cat <<EOF

    ${CYAN}Reference${NC} ${DIM}— inline a doc on the next turn${NC}
      ${DIM}/pluto-protocol${NC}     shared coordination protocol (library/protocol.md)
      ${DIM}/pluto-guide${NC}        agent skill guide (agent_friend_guide.md)

  ${BOLD}Resources${NC} ${DIM}(@-mention for an on-demand snapshot)${NC}
      ${DIM}@pluto://inbox${NC}      pending messages addressed to you (read-only; pluto_recv to drain)
      ${DIM}@pluto://locks${NC}      locks currently held by you (auto-renewed in the background)
      ${DIM}@pluto://agents${NC}     every agent connected to the server
      ${DIM}@pluto://server${NC}     server health / version

  ${BOLD}Tools${NC} ${DIM}(the agent calls these on its own; you don't run them manually)${NC}
      ${CYAN}pluto_send${NC} / ${CYAN}pluto_broadcast${NC} / ${CYAN}pluto_recv${NC} / ${CYAN}pluto_wait_for_messages${NC}
      ${CYAN}pluto_lock_acquire${NC} / ${CYAN}pluto_lock_release${NC} / ${CYAN}pluto_lock_renew${NC} / ${CYAN}pluto_lock_info${NC}
      ${CYAN}pluto_task_assign${NC} / ${CYAN}pluto_task_update${NC} / ${CYAN}pluto_task_list${NC}
      ${CYAN}pluto_list_agents${NC} / ${CYAN}pluto_find_agents${NC} / ${CYAN}pluto_set_status${NC}
      ${CYAN}pluto_publish${NC} / ${CYAN}pluto_subscribe${NC} / ${CYAN}pluto_list_locks${NC}

  ${DIM}Watcher block duration: ${wait_s}s (--wait-timeout-s).${NC}
  ${DIM}Tip: type /pluto-watch once after Claude is up to start the watcher.${NC}

  ${BOLD}═══ Quick smoke test (run from another terminal) ═══${NC}
    curl -X POST http://${host}:${port}/messages/send \\
      -H 'Content-Type: application/json' \\
      -d '{"to":"${agent_id}","payload":{"type":"hello","text":"Welcome!"}}'

  ${DIM}Stop the Pluto server later:  ./PlutoServer.sh --kill${NC}

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
    3. Verify Claude Code is installed
    4. Generate .mcp.json and launch Claude Code

  ${DIM}Roles, protocol, guide, and quick actions are slash commands inside
  Claude. Pick one at any time after launch (/pluto-…).${NC}

EOF
    read -rp "  Press Enter to begin (Ctrl-C to abort) ... " _ < /dev/tty
}

wizard_step_server() {
    section "Step 1/4 — Pluto server"
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
        section "Step 2/4 — Agent ID"
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

wizard_step_check_claude() {
    section "Step 3/4 — Claude Code"
    local path
    path=$(claude_path)
    if [[ -n "${path}" ]]; then
        ok "Found ${SUPPORTED_CLI} at ${BOLD}${path}${NC}"
        return 0
    fi
    {
        warn "${SUPPORTED_CLI} not found in PATH."
        echo ""
        cat <<EOF
  ${DIM}PlutoMCPFriend only supports Claude Code. Install it from
  https://claude.com/claude-code and re-run, or pass --no-launch to
  generate .mcp.json without launching anything (you can wire it into
  Claude later by hand).${NC}

  ${DIM}For Cursor / Aider / Copilot, use ./PlutoAgentFriend.sh instead —
  the PTY-based wrapper works with any agent CLI.${NC}
EOF
    } >&2
    return 1
}

wizard_step_confirm() {
    section "Step 4/4 — Ready to launch"
    local agent_id="$1" host="$2" port="$3" role="$4" wait_s="$5"

    local role_display
    if [[ -n "${role}" ]]; then
        role_display="${CYAN}${role}${NC}  (auto-applied on first turn)"
    else
        role_display="${DIM}(none — pick after launch via /pluto-role-<name>)${NC}"
    fi

    cat <<EOF

  ${BOLD}Summary:${NC}
    Agent ID         : ${CYAN}${agent_id}${NC}
    Pluto            : ${host}:${port}
    Agent CLI        : ${SUPPORTED_CLI}
    Role             : ${role_display}
    Watcher block    : ${wait_s}s  (--wait-timeout-s)
    .mcp.json        : ${SCRIPT_DIR}/.mcp.json

EOF
    read -rp "  Press Enter to launch (Ctrl-C to abort) ... " _ < /dev/tty
    echo ""
}

# ── Launch ──────────────────────────────────────────────────────────────────

launch_claude() {
    local role="$1" agent_id="$2" host="$3" port="$4" wait_s="$5"
    shift 5
    local extra=("$@")

    print_post_launch_tips "${agent_id}" "${host}" "${port}" "${wait_s}"

    local cmd=("${SUPPORTED_CLI}" "--mcp-config" "${SCRIPT_DIR}/.mcp.json")
    if [[ -n "${role}" ]]; then
        local sys_prompt
        sys_prompt=$(build_role_system_prompt \
            "${agent_id}" "${host}" "${port}" "${role}" "${wait_s}")
        cmd+=("--append-system-prompt" "${sys_prompt}")
    fi
    # bash 3.2-safe empty-array expansion (macOS default bash).
    if (( ${#extra[@]} > 0 )); then
        cmd+=("${extra[@]}")
    fi
    exec "${cmd[@]}"
}

# ── Main ────────────────────────────────────────────────────────────────────

main() {
    local agent_id=""
    local role=""
    local host=""
    local http_port=""
    local ttl_ms="600000"
    local wait_timeout_s="${DEFAULT_WAIT_TIMEOUT_S}"
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
            --role) role="$2"; shift 2 ;;
            --host) host="$2"; shift 2 ;;
            --http-port) http_port="$2"; shift 2 ;;
            --ttl-ms) ttl_ms="$2"; shift 2 ;;
            --wait-timeout-s) wait_timeout_s="$2"; shift 2 ;;
            --log-level) log_level="$2"; shift 2 ;;
            --no-launch) no_launch=true; shift ;;
            --no-wizard) no_wizard=true; shift ;;
            --framework)
                err "--framework was removed in v0.2.8 — Claude Code only."
                err "For Cursor/Aider/Copilot use ./PlutoAgentFriend.sh instead."
                exit 1
                ;;
            --) past_separator=true; shift ;;
            *)  err "Unknown option: $1"; show_help; exit 1 ;;
        esac
    done

    # Validate wait timeout is a positive integer.
    if ! [[ "${wait_timeout_s}" =~ ^[0-9]+$ ]] || (( wait_timeout_s < 1 )); then
        err "--wait-timeout-s must be a positive integer (seconds)."
        exit 1
    fi

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

        if ! $no_launch && ! wizard_step_check_claude; then
            err "Cannot launch — falling back to --no-launch (config-only mode)."
            no_launch=true
        fi

        # Full summary + confirm before launch.
        if ! $no_launch; then
            wizard_step_confirm "${agent_id}" "${host}" "${http_port}" \
                "${role}" "${wait_timeout_s}"
        fi
    else
        # Expert mode: brief banner + reachability check, no prompts.
        show_banner
        info "Agent ID:   ${BOLD}${agent_id}${NC}"
        info "Pluto:      ${host}:${http_port}"
        [[ -n "${role}" ]] && info "Role:       ${role}"
        info "Watcher:    ${wait_timeout_s}s block duration"
        check_pluto_reachable "${host}" "${http_port}" || \
            warn "Continuing — pluto_* tools will return errors until the server is up."
    fi

    # ── Generate .mcp.json ──────────────────────────────────────────────────
    local mcp_json
    mcp_json=$(write_mcp_json "${agent_id}" "${host}" "${http_port}" \
        "${ttl_ms}" "${log_level}" "${wait_timeout_s}")
    ok "Wrote MCP config:  ${mcp_json}"

    if $no_launch; then
        cat <<EOF

  ${BOLD}Wire this config into Claude Code:${NC}
    ${CYAN}claude --mcp-config ${mcp_json}${NC}

EOF
        exit 0
    fi

    # If we got here in expert mode, we still need to verify Claude is in PATH.
    if [[ -z "$(claude_path)" ]]; then
        err "${SUPPORTED_CLI} not found in PATH."
        err "Install Claude Code, or re-run with --no-launch."
        exit 1
    fi

    # bash 3.2-safe empty-array expansion (macOS default bash).
    if (( ${#extra_cmd[@]} > 0 )); then
        launch_claude "${role}" "${agent_id}" "${host}" "${http_port}" \
            "${wait_timeout_s}" "${extra_cmd[@]}"
    else
        launch_claude "${role}" "${agent_id}" "${host}" "${http_port}" \
            "${wait_timeout_s}"
    fi
}

main "$@"
