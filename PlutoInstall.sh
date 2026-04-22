#!/usr/bin/env bash
# ===========================================================================
# PlutoInstall.sh — Guided installation of Pluto's dependencies.
#
# Usage:
#   ./PlutoInstall.sh            Interactive guided install
#   ./PlutoInstall.sh --yes      Non-interactive (auto-accept all prompts)
#   ./PlutoInstall.sh --check    Only check what is installed / missing
#   ./PlutoInstall.sh -h|--help  Show this help
#
# What gets installed:
#   • Erlang/OTP 28 (from package manager or source build)
#   • rebar3
#   • Python 3.10+
#
# After installation the script verifies all tools and offers to build
# the Pluto server via PlutoServer.sh --build.
# ===========================================================================

set -uo pipefail   # -e intentionally omitted: errors are handled interactively

# ── Colours & helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
DKGRAY='\033[1;30m'
NC='\033[0m'

info()    { echo -e "  ${CYAN}ℹ${NC}  $*"; }
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
err()     { echo -e "  ${RED}✗${NC}  $*" >&2; }
section() { echo -e "\n  ${BOLD}${CYAN}▶  $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false
AUTO_YES=false

# ── Progress bar ──────────────────────────────────────────────────────────────
TOTAL_STEPS=4
CURRENT_STEP=0

# Print a progress bar at the current step.
# Usage: show_progress "Step label"
show_progress() {
    local label="$1"
    local width=32
    local filled=$(( CURRENT_STEP * width / TOTAL_STEPS ))
    local bar=""
    for ((i=0; i<filled;  i++)); do bar+="█"; done
    for ((i=filled; i<width; i++)); do bar+="░"; done
    local pct=$(( CURRENT_STEP * 100 / TOTAL_STEPS ))
    echo ""
    echo -e "  ${CYAN}[${bar}]${NC} ${BOLD}${pct}%%${NC}  ${DIM}${label}${NC}"
    echo ""
}

# Advance step counter and display the progress bar.
next_step() {
    CURRENT_STEP=$(( CURRENT_STEP + 1 ))
    show_progress "$1"
}

# ── Sudo warm-up ─────────────────────────────────────────────────────────────
# Call once before any background sudo usage.  Validates (or obtains) the sudo
# credential in the foreground so spinner-driven sudo commands never hang
# waiting for an invisible password prompt.
ensure_sudo() {
    # Already have a valid cached credential → nothing to do
    if sudo -n true 2>/dev/null; then
        return 0
    fi

    echo ""
    echo -e "  ${BOLD}${YELLOW}Administrator (sudo) access is required${NC}"
    echo -e "  ${DIM}Some steps install system packages and need elevated privileges."
    echo -e "  Please enter your password below — keystrokes are intentionally"
    echo -e "  hidden by the system (this is normal).${NC}"
    echo ""
    # Run sudo -v in the foreground so the user sees the prompt cleanly.
    # Redirect from /dev/tty so it works even when stdin is piped.
    if ! sudo -v </dev/tty; then
        err "sudo authentication failed. Cannot continue without admin access."
        exit 1
    fi

    ok "Administrator access granted."
    echo ""

    # Keep the timestamp alive in the background for long installs
    (while true; do sudo -n true; sleep 50; done) &
    SUDO_KEEPALIVE_PID=$!
    # Clean up the keep-alive on exit
    trap 'kill "${SUDO_KEEPALIVE_PID}" 2>/dev/null || true' EXIT
}

# ── Spinner for long-running commands ─────────────────────────────────────────
# Usage: run_with_spinner "Human-readable message" cmd [args...]
# Runs cmd in the background, shows a spinner, streams errors on failure.
run_with_spinner() {
    local msg="$1"; shift
    local log
    log="/tmp/pluto_install_$$.log"
    local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local rc=0

    "$@" >"${log}" 2>&1 &
    local pid=$!
    local i=0

    while kill -0 "${pid}" 2>/dev/null; do
        local ci=$(( i % ${#spin} ))
        printf "\r    ${CYAN}%s${NC}  %s  " "${spin:${ci}:1}" "${msg}"
        sleep 0.08
        i=$(( i + 1 ))
    done
    printf "\r\033[K"      # clear spinner line

    wait "${pid}" || rc=$?

    if (( rc != 0 )); then
        err "${msg} — command failed (exit ${rc})."
        echo ""
        echo -e "  ${DIM}Last output:${NC}"
        tail -20 "${log}" | sed 's/^/    /' >&2
        echo ""
    else
        ok "${msg}"
    fi

    rm -f "${log}"
    return ${rc}
}

# ── Interactive yes/no prompt ─────────────────────────────────────────────────
# Usage: ask_yes_no "Question?" [y|n]   returns 0=yes 1=no
ask_yes_no() {
    local question="$1"
    local default="${2:-y}"

    if $AUTO_YES; then
        [[ "${default}" == "y" ]] && return 0 || return 1
    fi

    local label
    if [[ "${default}" == "y" ]]; then
        label="${BOLD}Y${NC}/n"
    else
        label="y/${BOLD}N${NC}"
    fi

    while true; do
        printf "  ${YELLOW}?${NC}  %s [%b] " "${question}" "${label}"
        local ans
        read -r ans </dev/tty
        ans="${ans:-${default}}"
        case "${ans,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *) echo "      Please answer y or n." ;;
        esac
    done
}

# ── Detect platform ──────────────────────────────────────────────────────────
PLATFORM=""

detect_platform() {
    case "$(uname -s)" in
        Darwin)
            PLATFORM="macos"
            ;;
        Linux)
            if [[ -f /etc/debian_version ]] || grep -qi 'ubuntu\|debian' /etc/os-release 2>/dev/null; then
                PLATFORM="debian"
            else
                local distro
                distro=$(grep '^ID=' /etc/os-release 2>/dev/null | cut -d= -f2 || echo "unknown")
                warn "Unsupported Linux distribution: ${distro}."
                warn "Debian/Ubuntu-based systems are fully supported."
                warn "For other distros, install Erlang/OTP ${REQUIRED_OTP_MAJOR}+, rebar3, and Python 3.10+ manually."
                echo ""
                if ask_yes_no "Try to continue anyway (will attempt source build for Erlang)?" "y"; then
                    PLATFORM="debian"
                else
                    exit 1
                fi
            fi
            ;;
        *)
            err "Unsupported operating system: $(uname -s)"
            exit 1
            ;;
    esac
    ok "Platform detected: ${BOLD}${PLATFORM}${NC}"
}

# ── Version helpers ───────────────────────────────────────────────────────────

# Return 0 if $1 >= $2 (dot-separated version comparison)
version_gte() {
    local IFS=.
    local i ver1=($1) ver2=($2)
    for ((i = 0; i < ${#ver2[@]}; i++)); do
        local v1="${ver1[i]:-0}"
        local v2="${ver2[i]:-0}"
        if (( v1 > v2 )); then return 0; fi
        if (( v1 < v2 )); then return 1; fi
    done
    return 0
}

# ── Check individual tools ───────────────────────────────────────────────────

# Required OTP major version
REQUIRED_OTP_MAJOR=28

check_erlang() {
    if command -v erl &>/dev/null; then
        local otp_ver
        otp_ver=$(erl -eval 'io:format("~s",[erlang:system_info(otp_release)]),halt().' -noshell 2>/dev/null || echo "0")
        if version_gte "$otp_ver" "${REQUIRED_OTP_MAJOR}"; then
            ok "Erlang/OTP ${otp_ver} ✓"
            return 0
        else
            warn "Erlang/OTP ${otp_ver} found — OTP ${REQUIRED_OTP_MAJOR}+ is required"
            return 1
        fi
    else
        warn "Erlang not found"
        return 1
    fi
}

# ── Remove an existing (wrong-version) Erlang installation ───────────────────
remove_old_erlang() {
    local erl_path
    erl_path=$(command -v erl 2>/dev/null || true)
    [[ -z "${erl_path}" ]] && return 0  # nothing to remove

    local otp_ver
    otp_ver=$(erl -eval 'io:format("~s",[erlang:system_info(otp_release)]),halt().' -noshell 2>/dev/null || echo "?")

    echo ""
    warn  "Erlang/OTP ${otp_ver} is currently installed at ${erl_path}."
    info  "It must be removed so OTP ${REQUIRED_OTP_MAJOR} can be installed cleanly."
    echo ""

    if ! ask_yes_no "Remove existing Erlang/OTP ${otp_ver} now?" "y"; then
        err "Cannot install OTP ${REQUIRED_OTP_MAJOR} alongside an old version. Aborting."
        exit 1
    fi

    # apt-managed package
    if [[ "${PLATFORM}" == "debian" ]] && dpkg -l 'erlang*' 2>/dev/null | grep -q '^ii'; then
        run_with_spinner "Removing apt Erlang packages" \
            sudo apt-get remove -y -qq --purge 'erlang*' 'esl-erlang*' || true
        run_with_spinner "Cleaning up dependencies" \
            sudo apt-get autoremove -y -qq || true
    fi

    # Manually installed binaries under /usr/local
    if [[ "${erl_path}" == /usr/local/* ]]; then
        run_with_spinner "Removing /usr/local Erlang binaries" bash -c '
            sudo rm -f /usr/local/bin/erl /usr/local/bin/erlc /usr/local/bin/escript \
                       /usr/local/bin/epmd /usr/local/bin/ct_run /usr/local/bin/dialyzer
            sudo rm -rf /usr/local/lib/erlang
        '
    fi

    # Homebrew erlang (macOS)
    if [[ "${PLATFORM}" == "macos" ]] && command -v brew &>/dev/null; then
        if brew list erlang &>/dev/null 2>&1; then
            run_with_spinner "Removing Homebrew Erlang" brew uninstall erlang || true
        fi
    fi

    hash -r 2>/dev/null || true
    ok "Old Erlang/OTP ${otp_ver} removed."
    echo ""
}

check_rebar3() {
    if command -v rebar3 &>/dev/null; then
        local ver
        ver=$(rebar3 version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
        ok "rebar3 ${ver} ✓"
        return 0
    else
        warn "rebar3 not found"
        return 1
    fi
}

check_python() {
    local cmd
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
            if version_gte "$ver" "3.10"; then
                ok "Python ${ver} ($cmd) ✓"
                return 0
            fi
        fi
    done
    warn "Python 3.10+ not found"
    return 1
}

# ── Build Erlang/OTP from source (fallback) ───────────────────────────────────

install_erlang_from_source() {
    local otp_tag="OTP-${REQUIRED_OTP_MAJOR}.0"
    local otp_src_url="https://github.com/erlang/otp/releases/download/${otp_tag}/otp_src_${REQUIRED_OTP_MAJOR}.0.tar.gz"
    local build_dir="/tmp/otp_build"

    echo ""
    info  "Source build of Erlang/OTP ${REQUIRED_OTP_MAJOR} (${otp_tag})"
    info  "This compiles Erlang from scratch — expect 10–25 minutes."
    echo ""

    if ! ask_yes_no "Proceed with source build of OTP ${REQUIRED_OTP_MAJOR}?" "y"; then
        err "Skipped source build. OTP ${REQUIRED_OTP_MAJOR} is required — aborting."
        exit 1
    fi

    # Install build dependencies
    if [[ "$PLATFORM" == "debian" ]]; then
        run_with_spinner "Installing build dependencies" \
            sudo apt-get install -y -qq build-essential autoconf autoconf \
                libncurses5-dev libssl-dev libpng-dev libssh-dev \
                unixodbc-dev xsltproc || \
        run_with_spinner "Installing build dependencies (minimal set)" \
            sudo apt-get install -y -qq build-essential autoconf libncurses-dev libssl-dev || true
    fi

    rm -rf "${build_dir}"
    mkdir -p "${build_dir}"

    if ! run_with_spinner "Downloading OTP ${REQUIRED_OTP_MAJOR} source" \
            curl -fsSL -o "${build_dir}/otp_src.tar.gz" "${otp_src_url}"; then
        local alt_url="https://github.com/erlang/otp/archive/refs/tags/${otp_tag}.tar.gz"
        info "Primary URL failed, trying GitHub archive fallback ..."
        run_with_spinner "Downloading OTP source (fallback URL)" \
            curl -fsSL -o "${build_dir}/otp_src.tar.gz" "${alt_url}"
    fi

    run_with_spinner "Extracting source archive" \
        tar xzf "${build_dir}/otp_src.tar.gz" -C "${build_dir}"

    # Locate the extracted directory robustly
    local src_dir
    src_dir=$(find "${build_dir}" -mindepth 1 -maxdepth 1 -type d | head -1)
    if [[ -z "${src_dir}" ]]; then
        err "Could not find the extracted OTP source directory inside ${build_dir}."
        err "The archive may be corrupted — please re-run the installer."
        exit 1
    fi
    info "Source directory: ${src_dir}"

    # Generate configure if not present (happens with GitHub tag archives)
    if [[ ! -f "${src_dir}/configure" ]]; then
        info "'configure' not found — running autoconf to generate it ..."
        run_with_spinner "Generating configure script" \
            bash -c "cd '${src_dir}' && autoconf"
    fi

    export ERL_TOP="${src_dir}"
    run_with_spinner "Configuring build (a few minutes)" \
        bash -c "cd '${src_dir}' && ./configure --prefix=/usr/local --without-javac --without-wx --without-odbc"

    run_with_spinner "Compiling Erlang/OTP — please wait (10–20 min)" \
        bash -c "cd '${src_dir}' && make -j\"\$(nproc)\""

    run_with_spinner "Installing to /usr/local" \
        bash -c "cd '${src_dir}' && sudo make install"

    rm -rf "${build_dir}"
    hash -r 2>/dev/null || true

    if check_erlang; then
        ok "Erlang/OTP ${REQUIRED_OTP_MAJOR} built and installed from source."
    else
        err "Build completed but OTP ${REQUIRED_OTP_MAJOR} still not detected."
        err "Check /usr/local/bin/erl and your \$PATH."
        exit 1
    fi
}

# ── Install functions (macOS / Homebrew) ──────────────────────────────────────

ensure_homebrew() {
    if ! command -v brew &>/dev/null; then
        info "Homebrew is required to install packages on macOS."
        if ask_yes_no "Install Homebrew now?" "y"; then
            run_with_spinner "Installing Homebrew" \
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Add brew to current PATH for Apple Silicon
            if [[ -f /opt/homebrew/bin/brew ]]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [[ -f /usr/local/bin/brew ]]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            ok "Homebrew installed."
        else
            err "Homebrew is required to continue. Aborting."
            exit 1
        fi
    fi
}

install_macos() {
    ensure_sudo
    ensure_homebrew

    # ── Step 1: Erlang ────────────────────────────────────────────────────────
    next_step "Erlang/OTP ${REQUIRED_OTP_MAJOR}"
    if check_erlang; then
        ok "Erlang already satisfies the requirement — skipping."
    else
        echo ""
        info "Erlang/OTP is the runtime that powers the Pluto coordination server."
        info "Pluto requires OTP ${REQUIRED_OTP_MAJOR}+."
        echo ""
        if ask_yes_no "Install Erlang/OTP ${REQUIRED_OTP_MAJOR} via Homebrew?" "y"; then
            run_with_spinner "Installing Erlang via Homebrew" brew install erlang
            if ! check_erlang; then
                warn "Homebrew Erlang is older than OTP ${REQUIRED_OTP_MAJOR}."
                echo -e "  ${DIM}Homebrew may lag behind recent OTP releases."
                echo -e "  A source build always gives the exact version needed.${NC}"
                echo ""
                if ask_yes_no "Build Erlang/OTP ${REQUIRED_OTP_MAJOR} from source instead?" "y"; then
                    remove_old_erlang
                    install_erlang_from_source
                else
                    err "OTP ${REQUIRED_OTP_MAJOR} is required. Aborting."
                    exit 1
                fi
            fi
        else
            warn "Skipping Erlang — Pluto won't work without it."
        fi
    fi

    # ── Step 2: rebar3 ────────────────────────────────────────────────────────
    next_step "rebar3"
    if check_rebar3; then
        ok "rebar3 already installed — skipping."
    else
        echo ""
        info "rebar3 is the Erlang build tool used to compile the Pluto server."
        echo ""
        if ask_yes_no "Install rebar3 via Homebrew?" "y"; then
            run_with_spinner "Installing rebar3" brew install rebar3
        else
            warn "Skipping rebar3 — you won't be able to compile Pluto."
        fi
    fi

    # ── Step 3: Python ───────────────────────────────────────────────────────
    next_step "Python 3.10+"
    if check_python; then
        ok "Python already satisfies the requirement — skipping."
    else
        echo ""
        info "Python 3.10+ is needed to run PlutoClient and the AgentFriend wrapper."
        echo ""
        if ask_yes_no "Install Python 3 via Homebrew?" "y"; then
            run_with_spinner "Installing Python 3" brew install python@3
        else
            warn "Skipping Python — the client scripts won't be available."
        fi
    fi
}

# ── Install functions (Debian / Ubuntu) ───────────────────────────────────────

install_debian() {
    ensure_sudo
    run_with_spinner "Updating package lists" sudo apt-get update -qq

    # ── Step 1: Erlang ────────────────────────────────────────────────────────
    next_step "Erlang/OTP ${REQUIRED_OTP_MAJOR}"
    if check_erlang; then
        ok "Erlang already satisfies the requirement — skipping."
    else
        echo ""
        info "Erlang/OTP is the runtime that powers the Pluto coordination server."
        info "Pluto requires OTP ${REQUIRED_OTP_MAJOR}+."
        echo -e "  ${DIM}Will try the Erlang Solutions apt repository first."
        echo -e "  If that version is too old, a source build will be offered.${NC}"
        echo ""
        if ask_yes_no "Install Erlang/OTP ${REQUIRED_OTP_MAJOR}?" "y"; then
            # Add Erlang Solutions repo
            run_with_spinner "Installing apt prerequisites" \
                sudo apt-get install -y -qq curl gnupg apt-transport-https lsb-release

            if [[ ! -f /usr/share/keyrings/erlang-solutions-keyring.gpg ]]; then
                run_with_spinner "Importing Erlang Solutions GPG key" bash -c \
                    "curl -fsSL https://packages.erlang-solutions.com/ubuntu/erlang_solutions.asc \
                     | sudo gpg --dearmor -o /usr/share/keyrings/erlang-solutions-keyring.gpg"
            fi

            local codename
            codename=$(lsb_release -cs 2>/dev/null || echo "jammy")
            echo "deb [signed-by=/usr/share/keyrings/erlang-solutions-keyring.gpg] \
https://packages.erlang-solutions.com/ubuntu ${codename} contrib" \
                | sudo tee /etc/apt/sources.list.d/erlang-solutions.list >/dev/null

            run_with_spinner "Refreshing package lists" sudo apt-get update -qq
            run_with_spinner "Installing Erlang/OTP packages" \
                sudo apt-get install -y -qq erlang-nox erlang-dev erlang-src

            if ! check_erlang; then
                warn "The apt repository for '${codename}' does not carry OTP ${REQUIRED_OTP_MAJOR} yet."
                echo ""
                echo -e "  ${DIM}A source build will compile Erlang/OTP ${REQUIRED_OTP_MAJOR} directly."
                echo -e "  This is reliable but takes 10–25 minutes.${NC}"
                echo ""
                if ask_yes_no "Build Erlang/OTP ${REQUIRED_OTP_MAJOR} from source?" "y"; then
                    remove_old_erlang
                    install_erlang_from_source
                else
                    err "OTP ${REQUIRED_OTP_MAJOR} is required. Aborting."
                    exit 1
                fi
            fi
        else
            warn "Skipping Erlang — Pluto won't work without it."
        fi
    fi

    # ── Step 2: rebar3 ────────────────────────────────────────────────────────
    next_step "rebar3"
    if check_rebar3; then
        ok "rebar3 already installed — skipping."
    else
        echo ""
        info "rebar3 is the Erlang build tool used to compile the Pluto server."
        echo ""
        if ask_yes_no "Install rebar3?" "y"; then
            local rebar3_url="https://github.com/erlang/rebar3/releases/latest/download/rebar3"
            run_with_spinner "Downloading rebar3 binary" \
                sudo curl -fsSL -o /usr/local/bin/rebar3 "${rebar3_url}"
            sudo chmod +x /usr/local/bin/rebar3
            ok "rebar3 installed to /usr/local/bin/rebar3"
        else
            warn "Skipping rebar3 — you won't be able to compile Pluto."
        fi
    fi

    # ── Step 3: Python ───────────────────────────────────────────────────────
    next_step "Python 3.10+"
    if check_python; then
        ok "Python already satisfies the requirement — skipping."
    else
        echo ""
        info "Python 3.10+ is needed to run PlutoClient and the AgentFriend wrapper."
        echo ""
        if ask_yes_no "Install Python 3?" "y"; then
            run_with_spinner "Installing Python 3 + pip + venv" \
                sudo apt-get install -y -qq python3 python3-venv python3-pip
        else
            warn "Skipping Python — the client scripts won't be available."
        fi
    fi
}

# ── Verification ──────────────────────────────────────────────────────────────

verify_all() {
    next_step "Verifying installation"
    local ok_count=0
    local fail_count=0

    check_erlang  && ok_count=$(( ok_count + 1 )) || fail_count=$(( fail_count + 1 ))
    check_rebar3  && ok_count=$(( ok_count + 1 )) || fail_count=$(( fail_count + 1 ))
    check_python  && ok_count=$(( ok_count + 1 )) || fail_count=$(( fail_count + 1 ))

    echo ""
    if (( fail_count == 0 )); then
        echo -e "  ${GREEN}${BOLD}All ${ok_count} dependencies satisfied. ✓${NC}"
        return 0
    else
        echo -e "  ${RED}${BOLD}${fail_count} dependency/dependencies still missing.${NC}"
        return 1
    fi
}

# ── Build Pluto ───────────────────────────────────────────────────────────────

offer_build() {
    if [[ ! -f "${SCRIPT_DIR}/PlutoServer.sh" ]]; then
        return 0
    fi
    echo ""
    if ask_yes_no "Build the Pluto server now? (compiles Erlang release)" "y"; then
        run_with_spinner "Compiling Pluto server" \
            bash "${SCRIPT_DIR}/PlutoServer.sh" --build
        echo ""
        ok "${BOLD}Pluto is ready!${NC}"
        echo ""
        echo -e "    ${DIM}Start the server:${NC}"
        echo -e "      ${BOLD}./PlutoServer.sh${NC}"
        echo ""
        echo -e "    ${DIM}Test connectivity:${NC}"
        echo -e "      ${BOLD}./PlutoClient.sh ping${NC}"
        echo ""
    else
        info "Skipped build. Run ${BOLD}./PlutoServer.sh --build${NC} whenever you're ready."
    fi
}

# ── Usage / Help ──────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
${CYAN}PlutoInstall.sh${NC} — Guided Pluto dependency installer.

Usage:
  $(basename "$0")            Interactive guided install
  $(basename "$0") --yes      Non-interactive (auto-accept all prompts)
  $(basename "$0") --check    Check installed dependencies (no changes)
  $(basename "$0") -h|--help  Show this help

Supported platforms:
  • macOS (via Homebrew)
  • Debian / Ubuntu (via apt + Erlang Solutions repo)

Dependencies installed:
  • Erlang/OTP ${REQUIRED_OTP_MAJOR} (from package manager or source build)
  • rebar3
  • Python 3.10+
EOF
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    case "${1:-}" in
        -h|--help)
            usage
            exit 0
            ;;
        --check)
            CHECK_ONLY=true
            ;;
        --yes|-y)
            AUTO_YES=true
            ;;
        "")
            ;;
        *)
            err "Unknown option: $1"
            usage
            exit 1
            ;;
    esac

    # Banner (always shown, even for --check)
    local version
    version=$(cat "${SCRIPT_DIR}/VERSION.md" 2>/dev/null | tr -d '[:space:]' || echo 'unknown')
    echo ""
    echo -e "                    ${GREEN}.am######mp.${NC}"
    echo -e "                ${GREEN}.a################a.${NC}"
    echo -e "             ${GREEN}.a######################a.${NC}"
    echo -e "            ${GREEN}a##########################a${NC}"
    echo -e "           ${GREEN}####                      ####${NC}"
    echo -e "          ${GREEN}###  ${DKGRAY}########${GREEN}    ${DKGRAY}########${GREEN}  ###${NC}"
    echo -e "          ${GREEN}##  ${DKGRAY}#########${GREEN}  ${DKGRAY}##########${GREEN}  ##${NC}"
    echo -e "          ${GREEN}###  ${DKGRAY}########${GREEN}    ${DKGRAY}########${GREEN}  ###${NC}"
    echo -e "           ${GREEN}####                      ####${NC}"
    echo -e "            ${GREEN}######    ${DKGRAY}._____.${GREEN}   ######${NC}"
    echo -e "             ${GREEN}.a######################a.${NC}"
    echo -e "                ${GREEN}a################a${NC}"
    echo -e "                   ${GREEN}7##########7${NC}"
    echo -e "                      ${GREEN}'####'${NC}"
    echo -e "                        ${GREEN}''${NC}"
    echo ""
    echo -e "    ${CYAN}╔═══════════════════════════════════════════════╗${NC}"
    echo -e "    ${CYAN}║${NC}                                               ${CYAN}║${NC}"
    echo -e "    ${CYAN}║${NC}   ${GREEN}★${NC}  ${BOLD}Pluto Installer${NC}  ${DIM}${version}${NC}               ${CYAN}║${NC}"
    echo -e "    ${CYAN}║${NC}      Erlang · rebar3 · Python setup           ${CYAN}║${NC}"
    echo -e "    ${CYAN}║${NC}                                               ${CYAN}║${NC}"
    echo -e "    ${CYAN}╚═══════════════════════════════════════════════╝${NC}"
    echo ""

    detect_platform

    if $CHECK_ONLY; then
        section "Dependency check"
        check_erlang || true
        check_rebar3 || true
        check_python || true
        echo ""
        exit 0
    fi

    echo ""
    info "This installer will guide you through each dependency."
    $AUTO_YES && info "Running in non-interactive mode (--yes): all prompts auto-accepted." || true
    echo ""

    case "${PLATFORM}" in
        macos)  install_macos  ;;
        debian) install_debian ;;
    esac

    echo ""
    if verify_all; then
        offer_build
    else
        err "Some dependencies are still missing. Please resolve them and re-run."
        exit 1
    fi
}

main "$@"
