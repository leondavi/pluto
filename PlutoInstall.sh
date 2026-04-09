#!/usr/bin/env bash
# ===========================================================================
# PlutoInstall.sh — Install all dependencies for the Pluto coordination
# server on macOS (Homebrew) or Debian/Ubuntu (apt).
#
# Usage:
#   ./PlutoInstall.sh            Install everything
#   ./PlutoInstall.sh --check    Only check what is installed / missing
#
# What gets installed:
#   • Erlang/OTP 28 (exact major version, from apt or built from source)
#   • rebar3
#   • Python 3.10+
#
# After installation the script verifies all tools are available and
# optionally runs PlutoServer.sh --build to compile the release.
# ===========================================================================

set -euo pipefail

# ── Colours & helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[pluto-install]${NC} $*"; }
ok()    { echo -e "${GREEN}[pluto-install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[pluto-install]${NC} $*"; }
err()   { echo -e "${RED}[pluto-install]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false

# ── Detect platform ──────────────────────────────────────────────────────────

detect_platform() {
    case "$(uname -s)" in
        Darwin)
            PLATFORM="macos"
            ;;
        Linux)
            if [[ -f /etc/debian_version ]] || grep -qi 'ubuntu\|debian' /etc/os-release 2>/dev/null; then
                PLATFORM="debian"
            else
                err "Unsupported Linux distribution. This installer supports Debian and Ubuntu."
                err "For other distributions, install Erlang/OTP 26+, rebar3, and Python 3.10+ manually."
                exit 1
            fi
            ;;
        *)
            err "Unsupported operating system: $(uname -s)"
            exit 1
            ;;
    esac
    info "Detected platform: ${BOLD}${PLATFORM}${NC}"
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
            warn "Erlang/OTP ${otp_ver} found but ${REQUIRED_OTP_MAJOR}+ required"
            return 1
        fi
    else
        warn "Erlang not found"
        return 1
    fi
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

    info "Building Erlang/OTP ${REQUIRED_OTP_MAJOR} from source (${otp_tag}) ..."

    # Install build dependencies
    if [[ "$PLATFORM" == "debian" ]]; then
        sudo apt-get install -y -qq build-essential autoconf libncurses5-dev \
            libssl-dev libwxgtk3.2-dev libglu1-mesa-dev libgl1-mesa-dev \
            libpng-dev libssh-dev unixodbc-dev xsltproc fop libxml2-utils \
            2>/dev/null || \
        sudo apt-get install -y -qq build-essential autoconf libncurses5-dev \
            libssl-dev libpng-dev libssh-dev unixodbc-dev xsltproc \
            2>/dev/null || true
    fi

    rm -rf "${build_dir}"
    mkdir -p "${build_dir}"
    cd "${build_dir}"

    info "Downloading OTP source from ${otp_src_url} ..."
    if ! curl -fsSL -o "otp_src.tar.gz" "${otp_src_url}"; then
        # Try the archive URL pattern as fallback
        local alt_url="https://github.com/erlang/otp/archive/refs/tags/${otp_tag}.tar.gz"
        info "Primary URL failed, trying ${alt_url} ..."
        curl -fsSL -o "otp_src.tar.gz" "${alt_url}"
    fi

    tar xzf otp_src.tar.gz
    cd otp*${REQUIRED_OTP_MAJOR}* || cd otp-${otp_tag} || cd otp_src_${REQUIRED_OTP_MAJOR}.0

    info "Configuring (this may take a few minutes) ..."
    export ERL_TOP="$PWD"
    ./configure --prefix=/usr/local --without-javac --without-wx --without-odbc 2>&1 | tail -5

    info "Compiling (this may take several minutes) ..."
    make -j"$(nproc)" 2>&1 | tail -5

    info "Installing to /usr/local ..."
    sudo make install 2>&1 | tail -5

    cd /
    rm -rf "${build_dir}"

    # Refresh hash table so shell picks up new erl
    hash -r 2>/dev/null || true

    if check_erlang; then
        ok "Erlang/OTP ${REQUIRED_OTP_MAJOR} built and installed from source."
    else
        err "Source build completed but OTP ${REQUIRED_OTP_MAJOR} still not detected."
        err "Check /usr/local/bin/erl and your PATH."
        exit 1
    fi
}

# ── Install functions (macOS / Homebrew) ──────────────────────────────────────

ensure_homebrew() {
    if ! command -v brew &>/dev/null; then
        info "Installing Homebrew ..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Add brew to current PATH for Apple Silicon
        if [[ -f /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -f /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        ok "Homebrew installed."
    fi
}

install_macos() {
    ensure_homebrew

    if ! check_erlang; then
        info "Installing Erlang via Homebrew ..."
        brew install erlang
        # Verify we got OTP ${REQUIRED_OTP_MAJOR}; if not, build from source
        if ! check_erlang; then
            warn "Homebrew Erlang does not provide OTP ${REQUIRED_OTP_MAJOR}. Building from source ..."
            install_erlang_from_source
        fi
    fi

    if ! check_rebar3; then
        info "Installing rebar3 via Homebrew ..."
        brew install rebar3
    fi

    if ! check_python; then
        info "Installing Python 3 via Homebrew ..."
        brew install python@3
    fi
}

# ── Install functions (Debian / Ubuntu) ───────────────────────────────────────

install_debian() {
    info "Updating package lists ..."
    sudo apt-get update -qq

    if ! check_erlang; then
        info "Installing Erlang/OTP ${REQUIRED_OTP_MAJOR} ..."
        # Try the Erlang Solutions repo first for a packaged OTP 28
        if ! dpkg -l erlang-nox &>/dev/null || ! check_erlang; then
            info "Adding Erlang Solutions repository for OTP ${REQUIRED_OTP_MAJOR} ..."
            sudo apt-get install -y -qq curl gnupg apt-transport-https
            # Import key and add repo
            if [[ ! -f /usr/share/keyrings/erlang-solutions-keyring.gpg ]]; then
                curl -fsSL https://packages.erlang-solutions.com/ubuntu/erlang_solutions.asc \
                    | sudo gpg --dearmor -o /usr/share/keyrings/erlang-solutions-keyring.gpg
            fi
            local codename
            codename=$(lsb_release -cs 2>/dev/null || echo "jammy")
            echo "deb [signed-by=/usr/share/keyrings/erlang-solutions-keyring.gpg] https://packages.erlang-solutions.com/ubuntu ${codename} contrib" \
                | sudo tee /etc/apt/sources.list.d/erlang-solutions.list > /dev/null
            sudo apt-get update -qq
        fi
        sudo apt-get install -y -qq erlang-nox erlang-dev erlang-src

        # Verify we got the right version; if not, build from source
        if ! check_erlang; then
            warn "apt did not provide OTP ${REQUIRED_OTP_MAJOR}. Building from source ..."
            install_erlang_from_source
        fi
    fi

    if ! check_rebar3; then
        info "Installing rebar3 ..."
        # rebar3 is not always in apt; install from GitHub release
        local rebar3_url="https://github.com/erlang/rebar3/releases/latest/download/rebar3"
        sudo curl -fsSL -o /usr/local/bin/rebar3 "${rebar3_url}"
        sudo chmod +x /usr/local/bin/rebar3
        ok "rebar3 installed to /usr/local/bin/rebar3"
    fi

    if ! check_python; then
        info "Installing Python 3 ..."
        sudo apt-get install -y -qq python3 python3-venv python3-pip
    fi
}

# ── Verification ──────────────────────────────────────────────────────────────

verify_all() {
    info "Verifying installation ..."
    local ok_count=0
    local fail_count=0

    if check_erlang;  then ((ok_count++)); else ((fail_count++)); fi
    if check_rebar3;  then ((ok_count++)); else ((fail_count++)); fi
    if check_python;  then ((ok_count++)); else ((fail_count++)); fi

    echo ""
    if (( fail_count == 0 )); then
        ok "All ${ok_count} dependencies satisfied."
        return 0
    else
        err "${fail_count} dependency/dependencies still missing."
        return 1
    fi
}

# ── Build Pluto ───────────────────────────────────────────────────────────────

offer_build() {
    if [[ -f "${SCRIPT_DIR}/PlutoServer.sh" ]]; then
        echo ""
        info "Building Pluto server ..."
        bash "${SCRIPT_DIR}/PlutoServer.sh" --build
        echo ""
        ok "Pluto is ready.  Start the server with:"
        echo "    ./PlutoServer.sh"
        echo ""
        ok "Test connectivity with:"
        echo "    ./PlutoClient.sh ping"
    fi
}

# ── Usage / Help ──────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
${CYAN}PlutoInstall.sh${NC} — Install Pluto dependencies.

Usage:
  $(basename "$0")            Install all dependencies and build Pluto
  $(basename "$0") --check    Check installed dependencies (no changes)
  $(basename "$0") -h|--help  Show this help

Supported platforms:
  • macOS (via Homebrew)
  • Debian / Ubuntu (via apt + Erlang Solutions repo)

Dependencies installed:
  • Erlang/OTP 28 (exact, from apt or source)
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
        "")
            ;;
        *)
            err "Unknown option: $1"
            usage
            exit 1
            ;;
    esac

    echo ""
    echo -e "${BOLD}${CYAN}  ╔══════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}  ║        Pluto Installer               ║${NC}"
    echo -e "${BOLD}${CYAN}  ╚══════════════════════════════════════╝${NC}"
    echo ""

    detect_platform

    if $CHECK_ONLY; then
        verify_all
        exit $?
    fi

    case "$PLATFORM" in
        macos)  install_macos  ;;
        debian) install_debian ;;
    esac

    echo ""
    if verify_all; then
        offer_build
    else
        err "Some dependencies could not be installed. Please install them manually and re-run."
        exit 1
    fi
}

main "$@"
