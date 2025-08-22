#!/usr/bin/env bash
set -Eeuo pipefail
set -o errtrace
# Global error handler - must be before any commands that could fail
trap 'ec=$?; printf "%b\n" "\033[31m[ERROR]\033[0m Unhandled error (exit $ec) at line $LINENO: $BASH_COMMAND" >&2' ERR

# pkg config - Bash 3.2 compatible (no associative arrays)
# Color codes
COLOR_RED="\033[31m"
COLOR_GREEN="\033[32m"
COLOR_CYAN="\033[36m"
COLOR_RESET="\033[0m"

# Version management function
get_expected_version() {
    case "$1" in
        nodejs) echo "22.9.0" ;;
        python3) echo "3.12.11" ;;
        claude_sdk) echo "0.0.19" ;;
        claude_cli) echo "1.0.71" ;;
        jq) echo "1.7.1" ;;
        ripgrep) echo "14.1.1" ;;
        websockets) echo "15.0.1" ;;
        uvloop) echo "0.21.0" ;;
        *) echo "" ;;
    esac
}

# Package manager commands
get_pkg_manager_cmd() {
    case "$1" in
        apk) echo "apk add" ;;
        apt) echo "apt-get install -y" ;;
        dnf) echo "dnf install -y" ;;
        yum) echo "yum install -y" ;;
        pacman) echo "pacman -S --noconfirm" ;;
        zypper) echo "zypper install -y" ;;
        portage) echo "emerge --ask=n" ;;
        xbps) echo "xbps-install -S" ;;
        brew) echo "brew install" ;;
        *) echo "" ;;
    esac
}

REPO="kisukeapp/install"
KISUKE_VERSION="INJECT_VERSION_HERE"
BIN_DIR="$HOME/.kisuke/bin"
NODEJS_BIN_DIR="$BIN_DIR/nodejs/bin"
INSTALL_MARKER_DIR="$HOME/.kisuke/installed"
CACHE_DIR="$HOME/.kisuke/cache"
SCRIPTS_DIR="$HOME/.kisuke/scripts"
mkdir -p "$BIN_DIR" "$INSTALL_MARKER_DIR" "$CACHE_DIR" "$SCRIPTS_DIR"

# Globals
MOBILE_APP=0 INSTALL=0 UNINSTALL=0 DEPLOY=0
DISTRO=""  # Will be set by detect_platform()
INSTALL_PACKAGES=() UNINSTALL_PACKAGES=()

[[ ":$PATH:" != *":$BIN_DIR:"* ]] && export PATH="$BIN_DIR:$PATH"
[[ ":$PATH:" != *":$NODEJS_BIN_DIR:"* ]] && export PATH="$NODEJS_BIN_DIR:$PATH"

log() {
    local level="$1" msg="$2"
    if [[ $MOBILE_APP -eq 1 ]]; then
        case "$level" in
            ERROR) echo "[KECHO] ERROR $msg" ;;
            NOTIFY) echo "[KECHO] NOTIFY $msg" ;;
            OK) echo "[KECHO] OK $msg" ;;
            ALERT) echo "[KECHO] ALERT $msg" ;;
            *) echo "$msg" ;;
        esac
    else
        case "$level" in
            ERROR) printf "%b\n" "${COLOR_RED}[ERROR]${COLOR_RESET} $msg" >&2 ;;
            NOTIFY) printf "%b\n" "${COLOR_CYAN}[INFO]${COLOR_RESET}  $msg" ;;
            OK) printf "%b\n" "${COLOR_GREEN}[OK]${COLOR_RESET}    $msg" ;;
            ALERT) printf "%b\n" "${COLOR_RED}[ALERT]${COLOR_RESET} $msg" >&2 ;;
            *) echo "$msg" ;;
        esac
    fi
}

parse_args() {
    local parsing_install=0 parsing_uninstall=0
    for arg in "$@"; do
        case "$arg" in
            --install) INSTALL=1; parsing_install=1; parsing_uninstall=0 ;;
            --uninstall) UNINSTALL=1; parsing_install=0; parsing_uninstall=1 ;;
            --deploy) DEPLOY=1; parsing_install=0; parsing_uninstall=0 ;;
            --app) MOBILE_APP=1; parsing_install=0; parsing_uninstall=0 ;;
            --*) parsing_install=0; parsing_uninstall=0 ;;
            *) 
                if [[ $parsing_install -eq 1 ]]; then
                    INSTALL_PACKAGES=("${INSTALL_PACKAGES[@]}" "$arg")
                elif [[ $parsing_uninstall -eq 1 ]]; then
                    UNINSTALL_PACKAGES=("${UNINSTALL_PACKAGES[@]}" "$arg")
                fi
                ;;
        esac
    done
}

detect_platform() {
    # Wrap in if to handle potential failures under set -e
    local raw_os raw_arch
    if ! raw_os="$(uname -s)"; then
        raw_os="Unknown"
    fi
    if ! raw_arch="$(uname -m)"; then
        raw_arch="Unknown"
    fi
    
    case "$raw_os" in
        Linux*) OS="linux" ;;
        Darwin*) OS="mac" ;;
        *) 
            log ERROR "Unsupported OS: $raw_os"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK SYSTEM_INFO OS=$raw_os ARCH=$raw_arch"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK PLATFORM_COMPAT false"
            exit 1
            ;;
    esac   
    case "$raw_arch" in
        arm64|aarch64) ARCH="arm64" ;;
        x86_64|amd64) ARCH="x86_64" ;;
        *) 
            log ERROR "Unsupported architecture: $raw_arch"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK SYSTEM_INFO OS=$OS ARCH=$raw_arch"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK PLATFORM_COMPAT false"
            exit 1
            ;;
    esac
    # Report system info for mobile app
    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK SYSTEM_INFO OS=$OS ARCH=$ARCH"
    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK PLATFORM_COMPAT true"
    
    # Detect distro for Alpine/musl handling
    DISTRO=""
    if [[ -f /etc/os-release ]]; then
        if ! DISTRO=$(grep -E '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"'); then
            DISTRO=""
        fi
    fi
}


detect_package_manager() {
    for entry in brew:brew apk:apk apt-get:apt dnf:dnf yum:yum pacman:pacman zypper:zypper emerge:portage xbps-install:xbps; do
        cmd=${entry%%:*}; name=${entry##*:}
        if command -v "$cmd" >/dev/null 2>&1; then PKG_MGR="$name"; return 0; fi
    done
    PKG_MGR="unknown"
    # Return 0 to prevent script exit under set -e
    return 0
}

cache_set() {
    local pkg="$1" version="${2:-unknown}"
    printf "version=%s\ntimestamp=%s\n" "$version" "$(date +%s)" > "$CACHE_DIR/$pkg.installed"
}

cache_get() {
    local pkg="$1"
    [[ -f "$CACHE_DIR/$pkg.installed" ]] && grep "^version=" "$CACHE_DIR/$pkg.installed" 2>/dev/null | cut -d'=' -f2
}

cache_exists() {
    [[ -f "$CACHE_DIR/$1.installed" ]]
}

cache_remove() {
    rm -f "$CACHE_DIR/$1.installed"
}

get_version() {
    local pkg="$1" type="${2:-system}"
    local cached_ver
    
    if cached_ver=$(cache_get "${pkg}_${type}") && [[ "$cached_ver" != "unknown" ]]; then
        echo "$cached_ver"
        return 0
    fi
    
    local ver="unknown"
    case "$pkg" in
        git) command -v git >/dev/null && ver=$(git --version | awk '{print $3}') ;;
        tmux) command -v tmux >/dev/null && ver=$(tmux -V | awk '{print $2}') ;;
        nodejs) [[ "$type" == "local" && -x "$BIN_DIR/nodejs/bin/node" ]] && ver=$("$BIN_DIR/nodejs/bin/node" --version 2>/dev/null | sed 's/^v//') ;;
        python3) [[ "$type" == "local" && -x "$BIN_DIR/python3/bin/python3" ]] && ver=$("$BIN_DIR/python3/bin/python3" --version | awk '{print $2}') ;;
        jq) [[ "$type" == "local" && -x "$BIN_DIR/jq" ]] && ver=$("$BIN_DIR/jq" --version 2>/dev/null | sed 's/^jq-//') ;;
        ripgrep) [[ "$type" == "local" && -x "$BIN_DIR/rg" ]] && ver=$("$BIN_DIR/rg" --version 2>/dev/null | head -n1 | awk '{print $2}') ;;
        claude_sdk) [[ -x "$BIN_DIR/python3/bin/pip" ]] && { ver=$("$BIN_DIR/python3/bin/pip" show claude-code-sdk 2>/dev/null | awk '/^Version:/ {print $2}') || ver="unknown"; } ;;
        claude_cli) command -v claude >/dev/null && ver=$(claude -v 2>/dev/null | awk '{print $1}') ;;
    esac
    
    [[ "$ver" != "unknown" ]] && cache_set "${pkg}_${type}" "$ver" || cache_remove "${pkg}_${type}"
    echo "$ver"
}

format_status() {
    local pkg="$1" current="$2" expected="${3:-}" type="${4:-system}"
    
    if [[ "$current" == "unknown" ]]; then
        echo "not installed"
    elif [[ -n "$expected" ]]; then
        [[ "$current" == "$expected" ]] && echo "up-to-date (v$current)" || echo "outdated (v$current, expected v$expected)"
    else
        echo "installed (v$current)${type:+ - not managed by script}"
    fi
}

create_symlinks() {
    local pkg="$1"
    case "$pkg" in
        jq)
            [[ -x "$BIN_DIR/jq" ]] && return 0
            ;;
        ripgrep)
            [[ -x "$BIN_DIR/rg" ]] && return 0
            ;;
        nodejs)
            local node_bin_dir="$BIN_DIR/nodejs/bin"
            if [[ -d "$node_bin_dir" ]]; then
                for binary in node npm npx; do
                    if [[ -x "$node_bin_dir/$binary" ]]; then
                        if ln -sf "$node_bin_dir/$binary" "$BIN_DIR/$binary"; then
                            log NOTIFY "Created symlink: $binary -> $node_bin_dir/$binary"
                        else
                            log ERROR "Failed to create symlink: $binary -> $node_bin_dir/$binary"
                        fi
                    fi
                done
                if [[ -x "$node_bin_dir/claude" ]]; then
                    if ln -sf "$node_bin_dir/claude" "$BIN_DIR/claude"; then
                        log NOTIFY "Created symlink: claude -> $node_bin_dir/claude"
                    else
                        log ERROR "Failed to create symlink: claude -> $node_bin_dir/claude"
                    fi
                fi
            fi
            ;;
        python3)
            local python_bin_dir="$BIN_DIR/python3/bin"
            if [[ -d "$python_bin_dir" ]]; then
                for binary in python3 pip pip3; do
                    if [[ -x "$python_bin_dir/$binary" ]]; then
                        if ln -sf "$python_bin_dir/$binary" "$BIN_DIR/$binary"; then
                            log NOTIFY "Created symlink: $binary -> $python_bin_dir/$binary"
                        else
                            log ERROR "Failed to create symlink: $binary -> $python_bin_dir/$binary"
                        fi
                    fi
                done
                if [[ -x "$python_bin_dir/python3" ]]; then
                    if ln -sf "$python_bin_dir/python3" "$BIN_DIR/python"; then
                        log NOTIFY "Created symlink: python -> $python_bin_dir/python3"
                    else
                        log ERROR "Failed to create symlink: python -> $python_bin_dir/python3"
                    fi
                fi
            fi
            ;;
        claude)
            if [[ -x "$NODEJS_BIN_DIR/claude" ]]; then
                if ln -sf "$NODEJS_BIN_DIR/claude" "$BIN_DIR/claude"; then
                    log NOTIFY "Created symlink: claude -> $NODEJS_BIN_DIR/claude"
                else
                    log ERROR "Failed to create symlink: claude -> $NODEJS_BIN_DIR/claude"
                fi
            fi
            ;;
    esac
}

remove_symlinks() {
    local pkg="$1"
    local binaries=()

    case "$pkg" in
        jq)       binaries=(jq) ;;
        ripgrep)  binaries=(rg) ;;
        nodejs)   binaries=(node npm npx claude) ;;
        python3)  binaries=(python3 python pip pip3) ;;
        claude)   binaries=(claude) ;;
        *)        return 0 ;;
    esac

    for binary in "${binaries[@]}"; do
        if [[ -L "$BIN_DIR/$binary" ]]; then
            rm -f "$BIN_DIR/$binary"
            log NOTIFY "Removed symlink: $binary"
        fi
    done
}


# Symlinking shinanigans
verify_installation() {
    local pkg="$1"
    case "$pkg" in
        jq)
            if command -v jq >/dev/null && jq --version >/dev/null 2>&1; then
                log OK "jq is working correctly"
            else
                log ERROR "jq installation verification failed"
                return 1
            fi
            ;;
        ripgrep)
            if command -v rg >/dev/null && rg --version >/dev/null 2>&1; then
                log OK "ripgrep is working correctly"
            else
                log ERROR "ripgrep installation verification failed"
                return 1
            fi
            ;;
        nodejs)
            local all_working=true
            for binary in node npm npx; do
                if ! command -v "$binary" >/dev/null; then
                    log ERROR "$binary not found in PATH"
                    all_working=false
                fi
            done
            if [[ "$all_working" == true ]]; then
                log OK "nodejs tools are working correctly"
            else
                return 1
            fi
            ;;
        python3)
            local all_working=true
            for binary in python python3 pip; do
                if ! command -v "$binary" >/dev/null; then
                    log ERROR "$binary not found in PATH"
                    all_working=false
                fi
            done
            if [[ "$all_working" == true ]]; then
                log OK "python3 tools are working correctly"
            else
                return 1
            fi
            ;;
        claude)
            if command -v claude >/dev/null && claude --version >/dev/null 2>&1; then
                log OK "claude CLI is working correctly"
            else
                log ERROR "claude CLI verification failed"
                return 1
            fi
            ;;
    esac
}

install_system_package() {
    local pkg="$1" pkg_name="$1"
    [[ "$pkg" == "git"  && "$PKG_MGR" == "portage" ]] && pkg_name="dev-vcs/git"
    [[ "$pkg" == "tmux" && "$PKG_MGR" == "portage" ]] && pkg_name="app-misc/tmux"
    if [[ "$PKG_MGR" == "unknown" ]]; then
        log ERROR "No supported package manager found"; return 1
    fi
    local sudo_cmd=""
    # Use id -u for POSIX compatibility (instead of Bash-specific $EUID)
    [[ $(id -u) -ne 0 && "$PKG_MGR" != "brew" ]] && sudo_cmd="sudo"
    local pkg_cmd
    pkg_cmd=$(get_pkg_manager_cmd "$PKG_MGR")
    if eval "$sudo_cmd $pkg_cmd $pkg_name" >/dev/null 2>&1; then
        cache_set "$pkg" "$(get_version "$pkg")"; return 0
    fi
    log ERROR "Failed to install $pkg using $PKG_MGR"; return 1
}

install_jq() {
    local current_ver expected_ver
    expected_ver=$(get_expected_version jq)
    current_ver=$(get_version jq local)
    
    if [[ "$current_ver" == "$expected_ver" ]]; then
        log OK "jq is up-to-date (v$current_ver)"
        create_symlinks jq
        return 0
    fi
    
    log NOTIFY "Installing jq v$expected_ver..."
    
    local binary_name
    case "$OS-$ARCH" in
        linux-x86_64) binary_name="jq-linux-amd64" ;;
        linux-arm64) binary_name="jq-linux-arm64" ;;
        mac-x86_64) binary_name="jq-macos-amd64" ;;
        mac-arm64) binary_name="jq-macos-arm64" ;;
        *) log ERROR "Unsupported platform for jq: $OS-$ARCH"; return 1 ;;
    esac
    
    local url="https://github.com/jqlang/jq/releases/download/jq-${expected_ver}/${binary_name}"
    if curl -sSL -o "$BIN_DIR/jq" "$url" && chmod +x "$BIN_DIR/jq"; then
        echo "installed" > "$INSTALL_MARKER_DIR/jq.installed"
        cache_set "jq_local" "$expected_ver"
        create_symlinks jq
        log OK "jq installed successfully (v$expected_ver)"
        return 0
    fi
    log ERROR "Failed to download jq"
    return 1
}

install_ripgrep() {
    # Ensure DISTRO is set for Alpine detection
    if [[ -z "$DISTRO" && -f /etc/os-release ]]; then
        if ! DISTRO=$(grep -E '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"'); then
            DISTRO=""
        fi
    fi
    
    local current_ver expected_ver
    expected_ver=$(get_expected_version ripgrep)
    current_ver=$(get_version ripgrep local)
    
    if [[ "$current_ver" == "$expected_ver" ]]; then
        log OK "ripgrep is up-to-date (v$current_ver)"
        create_symlinks ripgrep
        return 0
    fi
    
    log NOTIFY "Installing ripgrep v$expected_ver..."
    
    local archive_name binary_name
    case "$OS-$ARCH" in
        linux-x86_64) 
            if [[ "$DISTRO" == "alpine" ]]; then
                archive_name="ripgrep-${expected_ver}-x86_64-unknown-linux-musl.tar.gz"
            else
                archive_name="ripgrep-${expected_ver}-x86_64-unknown-linux-gnu.tar.gz"
            fi
            binary_name="rg"
            ;;
        linux-arm64) 
            if [[ "$DISTRO" == "alpine" ]]; then
                archive_name="ripgrep-${expected_ver}-aarch64-unknown-linux-musl.tar.gz"
            else
                archive_name="ripgrep-${expected_ver}-aarch64-unknown-linux-gnu.tar.gz"
            fi
            binary_name="rg"
            ;;
        mac-x86_64) 
            archive_name="ripgrep-${expected_ver}-x86_64-apple-darwin.tar.gz"
            binary_name="rg"
            ;;
        mac-arm64) 
            archive_name="ripgrep-${expected_ver}-aarch64-apple-darwin.tar.gz"
            binary_name="rg"
            ;;
        *) log ERROR "Unsupported platform for ripgrep: $OS-$ARCH"; return 1 ;;
    esac
    
    local url="https://github.com/BurntSushi/ripgrep/releases/download/${expected_ver}/${archive_name}"
    local temp_dir
    if ! temp_dir=$(mktemp -d /tmp/kisuke.XXXXXX); then
        log ERROR "Failed to create temporary directory"
        return 1
    fi
    trap "rm -rf '$temp_dir'" RETURN
    
    if curl -sSL -o "$temp_dir/$archive_name" "$url" && 
       tar -xzf "$temp_dir/$archive_name" -C "$temp_dir" &&
       find "$temp_dir" -name "$binary_name" -type f -exec cp {} "$BIN_DIR/rg" \; &&
       chmod +x "$BIN_DIR/rg"; then
        echo "installed" > "$INSTALL_MARKER_DIR/ripgrep.installed"
        cache_set "ripgrep_local" "$expected_ver"
        create_symlinks ripgrep
        log OK "ripgrep installed successfully (v$expected_ver)"
        return 0
    fi
    log ERROR "Failed to download or install ripgrep"
    return 1
}

validate_github_setup() {
    if [[ ! -x "$BIN_DIR/jq" ]]; then
        log ERROR "jq not found - installing"
        if install_jq; then
            verify_installation jq
        else
            log ERROR "Failed to install jq"
            return 1
        fi
    fi
    GITHUB_API="https://api.github.com/repos/$REPO"
    CURL_HEADERS=(-H "Accept: application/vnd.github+json")
    
    # Use specific version if injected, otherwise latest
    if [[ "$KISUKE_VERSION" != "INJECT_VERSION_HERE" ]]; then
        RELEASE_ENDPOINT="${GITHUB_API}/releases/tags/${KISUKE_VERSION}"
        log NOTIFY "Fetching release metadata for v${KISUKE_VERSION}"
    else
        RELEASE_ENDPOINT="${GITHUB_API}/releases/latest"
        log NOTIFY "Fetching latest release metadata (dev mode)"
    fi
    
    if ! curl -sSL "${CURL_HEADERS[@]}" "$RELEASE_ENDPOINT" -o "$CACHE_DIR/releases-latest.json"; then
        log ERROR "Failed to fetch GitHub releases"
        return 1
    fi
}

download_and_extract() {
    local pkg="$1" filename="$2"
    local download_url
    if ! download_url=$("$BIN_DIR/jq" -r ".assets[] | select(.name==\"$filename\") | .browser_download_url" "$CACHE_DIR/releases-latest.json"); then
        log ERROR "Failed to parse release metadata with jq"
        return 1
    fi
    
    if [[ -z "$download_url" || "$download_url" == "null" ]]; then
        log ERROR "Could not find download URL for $filename"
        return 1
    fi
    
    log NOTIFY "Downloading $filename..."
    
    if curl -sSL "$download_url" -o "$filename" &&
       tar -xJf "$filename" -C "$HOME"; then
        echo "installed" > "$INSTALL_MARKER_DIR/$pkg.installed"
        local pkg_version
        pkg_version=$(get_expected_version "$pkg")
        cache_set "${pkg}_local" "$pkg_version"
        create_symlinks "$pkg"
        log OK "$pkg installed successfully"
        return 0
    fi
    
    log ERROR "Failed to install $pkg"
    return 1
}

install_binary_package() {
    local pkg="$1"
    local current_ver expected_ver filename
    current_ver=$(get_version "$pkg" local)
    expected_ver=$(get_expected_version "$pkg")
    
    if [[ "$current_ver" == "$expected_ver" ]]; then
        log OK "$pkg is up-to-date (v$current_ver)"
        return 0
    fi
    
    log NOTIFY "Installing $pkg v$expected_ver..."
    
    # Use new filename format with KISUKE_VERSION and proper Alpine detection
    if [[ "$DISTRO" == "alpine" ]]; then
        filename="kisuke-${KISUKE_VERSION}-${pkg}-${expected_ver}-${OS}-musl-${ARCH}.tar.xz"
    else
        filename="kisuke-${KISUKE_VERSION}-${pkg}-${expected_ver}-${OS}-${ARCH}.tar.xz"
    fi
    
    download_and_extract "$pkg" "$filename"
}

install_claude_tools() {
    local sdk_status=0 cli_status=0
    if [[ -x "$BIN_DIR/python3/bin/pip" ]]; then
        local SDK_VER
        SDK_VER=$(get_expected_version claude_sdk)
        local current_sdk
        current_sdk=$(get_version claude_sdk)
        
        if [[ "$current_sdk" != "$SDK_VER" ]]; then
            local ws_ver uv_ver
            ws_ver=$(get_expected_version websockets)
            uv_ver=$(get_expected_version uvloop)
            if "$BIN_DIR/python3/bin/pip" install --force-reinstall --disable-pip-version-check --no-input -q "websockets==$ws_ver" "uvloop==$uv_ver" "claude-code-sdk==$SDK_VER"; then
                cache_set "claude_sdk" "$SDK_VER"
                log OK "claude-code-sdk installed v$SDK_VER"
            else
                log ERROR "claude-code-sdk install failed"
                sdk_status=1
            fi
        else
            log OK "claude-code-sdk up-to-date v$current_sdk"
        fi
    else
        log NOTIFY "pip not found, skipping claude-code-sdk"
        sdk_status=1
    fi
    if [[ -x "$NODEJS_BIN_DIR/npm" ]]; then
        local CLI_VER
        CLI_VER=$(get_expected_version claude_cli)
        local current_cli
        current_cli=$(get_version claude_cli)
        
        if [[ "$current_cli" != "$CLI_VER" ]]; then
            if "$NODEJS_BIN_DIR/npm" install -g "@anthropic-ai/claude-code@$CLI_VER" >/dev/null 2>&1; then
                cache_set "claude_cli" "$CLI_VER"
                create_symlinks claude
                log OK "@anthropic-ai/claude-code installed v$CLI_VER"
            else
                log ERROR "claude-cli install failed"
                cli_status=1
            fi
        else
            create_symlinks claude
            log OK "@anthropic-ai/claude-code up-to-date v$current_cli"
        fi
    else
        log ERROR "nodejs required for Claude CLI"
        cli_status=1
    fi
    
    return $((sdk_status + cli_status))
}

show_status() {
    if [[ $MOBILE_APP -eq 1 ]]; then
        log OK "SYSTEM_INFO OS=$OS ARCH=$ARCH"
        local platform_compat=true
        case "$(uname -s)" in
            Linux*|Darwin*) ;;
            *) platform_compat=false ;;
        esac
        case "$(uname -m)" in
            arm64|aarch64|x86_64|amd64) ;;
            *) platform_compat=false ;;
        esac
        
        if [[ "$platform_compat" == true ]]; then
            log OK "PLATFORM_COMPAT true"
        else
            log ERROR "PLATFORM_COMPAT false"
        fi
        
        for pkg in git tmux; do
            local ver
            ver=$(get_version "$pkg")
            if [[ "$ver" != "unknown" ]]; then
                log OK "SYS_PACKAGE $pkg $ver"
            else
                log ERROR "SYS_PACKAGE $pkg PACKAGE_NOT_INSTALLED"
            fi
        done
        local all_installed=true
        for pkg in jq ripgrep nodejs python3; do
            local ver expected status
            ver=$(get_version "$pkg" local)
            expected=$(get_expected_version "$pkg")
            status=$(format_status "$pkg" "$ver" "$expected")
            
            if [[ "$status" == "not installed"* ]]; then
                log ERROR "LOCAL_BIN $pkg NOT_FOUND"
                all_installed=false
            else
                log OK "LOCAL_BIN $pkg $status"
            fi
        done
        
        [[ "$all_installed" == true ]] && log OK "LOCAL_BINARIES" || log ERROR "LOCAL_BINARIES"
        local CLAUDE_SDK_VER CLAUDE_CLI_VER
        CLAUDE_SDK_VER=$(get_version claude_sdk)
        CLAUDE_CLI_VER=$(get_version claude_cli)
        
        if [[ "$CLAUDE_SDK_VER" != "unknown" && "$CLAUDE_CLI_VER" != "unknown" ]]; then
            log OK "CLAUDE_TOOLS"
            log OK "CLAUDE_TOOL SDK v$CLAUDE_SDK_VER"
            log OK "CLAUDE_TOOL CLI v$CLAUDE_CLI_VER"
        else
            log ERROR "CLAUDE_TOOLS"
            [[ "$CLAUDE_SDK_VER" == "unknown" ]] && log ERROR "CLAUDE_TOOL SDK not_installed" || log OK "CLAUDE_TOOL SDK v$CLAUDE_SDK_VER"
            [[ "$CLAUDE_CLI_VER" == "unknown" ]] && log ERROR "CLAUDE_TOOL CLI not_installed" || log OK "CLAUDE_TOOL CLI v$CLAUDE_CLI_VER"
        fi
    else
        log OK "System Information:"
        echo "  OS: $OS"
        echo "  Architecture: $ARCH"
        echo "  Package Manager: $PKG_MGR"
        echo
        local git_status tmux_status
        git_status=$(format_status git "$(get_version git)")
        tmux_status=$(format_status tmux "$(get_version tmux)")
        
        [[ "$git_status" != "not installed" && "$tmux_status" != "not installed" ]] && log OK "System Packages:" || log ERROR "System Packages:"
        echo "  git: $git_status"
        echo "  tmux: $tmux_status"
        echo
        local all_installed=true
        # Use variables instead of associative array for Bash 3.2 compatibility
        local local_status_jq local_status_ripgrep local_status_nodejs local_status_python3
        for pkg in jq ripgrep nodejs python3; do
            local ver expected
            ver=$(get_version "$pkg" local)
            expected=$(get_expected_version "$pkg")
            case "$pkg" in
                jq) local_status_jq=$(format_status "$pkg" "$ver" "$expected")
                    [[ "$local_status_jq" == "not installed"* ]] && all_installed=false ;;
                ripgrep) local_status_ripgrep=$(format_status "$pkg" "$ver" "$expected")
                    [[ "$local_status_ripgrep" == "not installed"* ]] && all_installed=false ;;
                nodejs) local_status_nodejs=$(format_status "$pkg" "$ver" "$expected")
                    [[ "$local_status_nodejs" == "not installed"* ]] && all_installed=false ;;
                python3) local_status_python3=$(format_status "$pkg" "$ver" "$expected")
                    [[ "$local_status_python3" == "not installed"* ]] && all_installed=false ;;
            esac
        done
        
        [[ "$all_installed" == true ]] && log OK "Local Binaries:" || log ERROR "Local Binaries:"
        echo "  jq: $local_status_jq"
        echo "  ripgrep: $local_status_ripgrep"
        echo "  nodejs: $local_status_nodejs"
        echo "  python3: $local_status_python3"
        echo
        log NOTIFY "Available binaries in PATH ($BIN_DIR):"
        for binary in jq rg node npm npx python python3 pip pip3 claude; do
            if [[ -L "$BIN_DIR/$binary" ]]; then
                local target
                target=$(readlink "$BIN_DIR/$binary")
                echo "  $binary -> $target"
            elif [[ -x "$BIN_DIR/$binary" ]]; then
                echo "  $binary (direct)"
            fi
        done
        echo
        local SDK_VER CLI_VER
        SDK_VER=$(get_version claude_sdk)
        CLI_VER=$(get_version claude_cli)
        
        [[ "$SDK_VER" != "unknown" && "$CLI_VER" != "unknown" ]] && log OK "Claude Tools:" || log ERROR "Claude Tools:"
        local expected_sdk expected_cli
        expected_sdk=$(get_expected_version claude_sdk)
        expected_cli=$(get_expected_version claude_cli)
        echo "  SDK: $(format_status claude_sdk "$SDK_VER" "$expected_sdk")"
        echo "  CLI: $(format_status claude_cli "$CLI_VER" "$expected_cli")"
        
        cat <<EOF

Usage:
  --install                 # Install/update all packages and deploy scripts
  --install jq ripgrep nodejs  # Install only specified packages and deploy scripts
  --deploy                  # Deploy scripts to ~/.kisuke/scripts
  --uninstall               # Remove all packages installed by script
  --uninstall ripgrep       # Remove only specified packages
Packages: jq, ripgrep, nodejs, python3, claude, git, tmux
EOF
    fi
}

handle_install() {
    local packages=("${@}")
    local failed_packages=()
    local has_critical_failure=0
    local processed_packages=()  # Track what we've already processed to avoid redundancy
    [[ ${#packages[@]} -eq 0 ]] && packages=(git tmux jq ripgrep nodejs python3 claude)
    for pkg in "${packages[@]}"; do
        case "$pkg" in
            nodejs|python3|claude) validate_github_setup; break ;;
        esac
    done
    for pkg in "${packages[@]}"; do
        # Add to processed list to avoid redundant processing
        processed_packages=("${processed_packages[@]}" "$pkg")
        
        case "$pkg" in
            git|tmux)
                if install_system_package "$pkg"; then
                    log OK "$pkg installed"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN $pkg installed"
                else
                    log ERROR "Failed to install $pkg"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install $pkg - this is a critical failure"
                    failed_packages=("${failed_packages[@]}" "$pkg")
                    has_critical_failure=1
                fi
                ;;
            jq)
                if install_jq; then
                    if verify_installation jq; then
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN jq installed"
                    else
                        log ERROR "jq verification failed"
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR jq installed but verification failed"
                        failed_packages=("${failed_packages[@]}" "jq")
                        has_critical_failure=1
                    fi
                else
                    log ERROR "Failed to install jq"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install jq - this is a critical failure"
                    failed_packages=("${failed_packages[@]}" "jq")
                    has_critical_failure=1
                fi
                ;;
            ripgrep)
                if install_ripgrep; then
                    if verify_installation ripgrep; then
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN ripgrep installed"
                    else
                        log ERROR "ripgrep verification failed"
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR ripgrep installed but verification failed"
                        failed_packages=("${failed_packages[@]}" "ripgrep")
                        has_critical_failure=1
                    fi
                else
                    log ERROR "Failed to install ripgrep"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install ripgrep - this is a critical failure"
                    failed_packages=("${failed_packages[@]}" "ripgrep")
                    has_critical_failure=1
                fi
                ;;
            nodejs|python3)
                if install_binary_package "$pkg"; then
                    if verify_installation "$pkg"; then
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN $pkg installed"
                    else
                        log ERROR "$pkg verification failed"
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR $pkg installed but verification failed"
                        failed_packages=("${failed_packages[@]}" "$pkg")
                        has_critical_failure=1
                    fi
                else
                    log ERROR "Failed to install $pkg"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install $pkg - this is a critical failure"
                    failed_packages=("${failed_packages[@]}" "$pkg")
                    has_critical_failure=1
                fi
                ;;
            claude)
                local nodejs_ver
                local deps_ok=1
                
                # Check nodejs dependency
                nodejs_ver=$(get_version nodejs local)
                if [[ "$nodejs_ver" == "unknown" ]]; then
                    # Check if nodejs was already processed (either succeeded or failed)
                    # Check if nodejs was already processed
                    local nodejs_processed=0 nodejs_failed=0
                    for p in "${processed_packages[@]}"; do
                        [[ "$p" == "nodejs" ]] && nodejs_processed=1 && break
                    done
                    if [[ $nodejs_processed -eq 1 ]]; then
                        # Already processed - check if it failed
                        for f in "${failed_packages[@]}"; do
                            [[ "$f" == "nodejs" ]] && nodejs_failed=1 && break
                        done
                        if [[ $nodejs_failed -eq 1 ]]; then
                            log ERROR "Claude requires nodejs which failed to install earlier"
                            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Claude cannot be installed - nodejs dependency failed"
                            failed_packages=("${failed_packages[@]}" "claude")
                            has_critical_failure=1
                            continue
                        fi
                    else
                        # Not processed yet - install it now (solo claude install case)
                        log NOTIFY "Claude requires Node.js. Installing..."
                        if install_binary_package nodejs; then
                            if verify_installation nodejs; then
                                [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN nodejs installed"
                            else
                                log ERROR "nodejs verification failed"
                                [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR nodejs installed but verification failed"
                                failed_packages=("${failed_packages[@]}" "nodejs")
                                deps_ok=0
                            fi
                        else
                            log ERROR "Failed to install nodejs dependency for Claude"
                            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install nodejs - required for Claude"
                            failed_packages=("${failed_packages[@]}" "nodejs")
                            deps_ok=0
                        fi
                    fi
                fi
                
                # Check ripgrep dependency
                if ! command -v rg >/dev/null 2>&1; then
                    # Check if ripgrep was already processed
                    # Check if ripgrep was already processed
                    local ripgrep_processed=0 ripgrep_failed=0
                    for p in "${processed_packages[@]}"; do
                        [[ "$p" == "ripgrep" ]] && ripgrep_processed=1 && break
                    done
                    if [[ $ripgrep_processed -eq 1 ]]; then
                        # Already processed - check if it failed
                        for f in "${failed_packages[@]}"; do
                            [[ "$f" == "ripgrep" ]] && ripgrep_failed=1 && break
                        done
                        if [[ $ripgrep_failed -eq 1 ]]; then
                            log ERROR "Claude requires ripgrep which failed to install earlier"
                            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Claude cannot be installed - ripgrep dependency failed"
                            failed_packages=("${failed_packages[@]}" "claude")
                            has_critical_failure=1
                            continue
                        fi
                    else
                        # Not processed yet - install it now (solo claude install case)
                        log NOTIFY "Claude requires ripgrep. Installing..."
                        if install_ripgrep; then
                            if verify_installation ripgrep; then
                                [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN ripgrep installed"
                            else
                                log ERROR "ripgrep verification failed"
                                [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR ripgrep installed but verification failed"
                                failed_packages=("${failed_packages[@]}" "ripgrep")
                                deps_ok=0
                            fi
                        else
                            log ERROR "Failed to install ripgrep dependency for Claude"
                            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install ripgrep - required for Claude"
                            failed_packages=("${failed_packages[@]}" "ripgrep")
                            deps_ok=0
                        fi
                    fi
                fi
                
                # Only try installing Claude if dependencies are satisfied
                if [[ $deps_ok -eq 0 ]]; then
                    log ERROR "Claude installation skipped due to missing dependencies"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Claude cannot be installed - dependencies failed"
                    failed_packages=("${failed_packages[@]}" "claude")
                    has_critical_failure=1
                    continue
                fi
                # Try installing Claude (dependencies already checked above)
                if install_claude_tools; then
                    if verify_installation claude; then
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK LOCAL_BIN claude installed"
                    else
                        log ERROR "claude verification failed"
                        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR claude installed but verification failed"
                        failed_packages=("${failed_packages[@]}" "claude")
                        has_critical_failure=1
                    fi
                else
                    log ERROR "Failed to install Claude tools"
                    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Failed to install Claude tools - this is a critical failure"
                    failed_packages=("${failed_packages[@]}" "claude")
                    has_critical_failure=1
                fi
                ;;
            *)
                log NOTIFY "Unknown package '$pkg', skipping"
                ;;
        esac
    done
    
    # Check if any critical failures occurred
    if [[ $has_critical_failure -eq 1 ]]; then
        log ERROR "Installation failed for critical packages: ${failed_packages[*]}"
        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Installation failed - critical packages missing: ${failed_packages[*]}"
        return 1
    fi
    
    log OK "Installation complete!"
    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK Installation complete - all packages installed successfully"
    
    log NOTIFY "Automatically deploying scripts..."
    if deploy_scripts; then
        log OK "Scripts deployed successfully!"
        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK Scripts deployed successfully"
    else
        log ERROR "Script deployment failed, but you can run with --deploy later"
        [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Script deployment failed"
        return 1
    fi
    
    return 0
}

deploy_scripts() {
    log NOTIFY "Deploying scripts to $SCRIPTS_DIR..."
    mkdir -p "$SCRIPTS_DIR"
    if [[ ! -x "$BIN_DIR/jq" ]]; then
        log NOTIFY "jq required for script deployment - installing..."
        if ! install_jq; then
            log ERROR "Failed to install jq"
            return 1
        fi
    fi
    local temp_dir
    if ! temp_dir=$(mktemp -d /tmp/kisuke.XXXXXX); then
        log ERROR "Failed to create temporary directory"
        return 1
    fi
    trap "rm -rf '$temp_dir'" RETURN

    local releases_json="$temp_dir/releases-latest.json"
    local curl_headers=(-H "Accept: application/vnd.github+json")
    local github_api="${GITHUB_API:-https://api.github.com/repos/$REPO}"
    
    # Use specific version if injected, otherwise latest
    if [[ "$KISUKE_VERSION" != "INJECT_VERSION_HERE" ]]; then
        RELEASE_ENDPOINT="${github_api}/releases/tags/${KISUKE_VERSION}"
        log NOTIFY "Fetching release information for v${KISUKE_VERSION}..."
    else
        RELEASE_ENDPOINT="${github_api}/releases/latest"
        log NOTIFY "Fetching latest release information (dev mode)..."
    fi
    
    if ! curl -sSL "${curl_headers[@]}" "$RELEASE_ENDPOINT" -o "$releases_json"; then
        log ERROR "Failed to fetch GitHub releases"
        return 1
    fi

    local release_tag
    if ! release_tag=$("$BIN_DIR/jq" -r '.tag_name' "$releases_json"); then
        log ERROR "Failed to parse release tag with jq"
        return 1
    fi
    if [[ -z "$release_tag" || "$release_tag" == "null" ]]; then
        log ERROR "Could not determine release version"
        return 1
    fi

    local bundle_name="kisuke-bridge-${release_tag}.tar.xz"
    local asset_id
    if ! asset_id=$("$BIN_DIR/jq" -r ".assets[] | select(.name==\"$bundle_name\") | .id" "$releases_json"); then
        log ERROR "Failed to parse asset ID with jq"
        return 1
    fi
    
    if [[ -z "$asset_id" || "$asset_id" == "null" ]]; then
        log ERROR "Scripts bundle $bundle_name not found in latest release"
        return 1
    fi

    log NOTIFY "Downloading $bundle_name..."
    local bundle_path="$temp_dir/$bundle_name"
    local download_url
    if ! download_url=$("$BIN_DIR/jq" -r ".assets[] | select(.name==\"$bundle_name\") | .browser_download_url" "$releases_json"); then
        log ERROR "Failed to parse download URL with jq"
        return 1
    fi
    
    if [[ -z "$download_url" || "$download_url" == "null" ]]; then
        log ERROR "Could not find download URL for $bundle_name"
        return 1
    fi
    
    if ! curl -sSL "$download_url" -o "$bundle_path"; then
        log ERROR "Failed to download scripts bundle"
        return 1
    fi

    log NOTIFY "Extracting scripts..."
    # Remove --no-xattrs for BSD tar compatibility (BSD tar doesn't extract xattrs by default)
    if ! tar -xf "$bundle_path" -C "$temp_dir"; then
        log ERROR "Failed to extract scripts bundle"
        return 1
    fi

    local extracted_scripts_dir="$temp_dir/scripts"
    if [[ ! -d "$extracted_scripts_dir" ]]; then
        log ERROR "Scripts directory not found in bundle"
        return 1
    fi
    
    local deployed_count=0
    local failed_count=0

    for script in "$extracted_scripts_dir"/*.py; do
        if [[ -f "$script" ]]; then
            local script_name=$(basename "$script")
            if cp "$script" "$SCRIPTS_DIR/$script_name"; then
                chmod +x "$SCRIPTS_DIR/$script_name"
                log NOTIFY "Deployed $script_name"
                deployed_count=$((deployed_count + 1))
            else
                log ERROR "Failed to deploy $script_name"
                failed_count=$((failed_count + 1))
            fi
        fi
    done

    for script in "$extracted_scripts_dir"/*; do
        if [[ -f "$script" && ! "$script" == *.py ]]; then
            local script_name=$(basename "$script")
            if cp "$script" "$SCRIPTS_DIR/$script_name"; then
                chmod +x "$SCRIPTS_DIR/$script_name"
                log NOTIFY "Deployed $script_name"
                deployed_count=$((deployed_count + 1))
            else
                log ERROR "Failed to deploy $script_name"
                failed_count=$((failed_count + 1))
            fi
        fi
    done
    
    if [[ $failed_count -eq 0 ]]; then
        log OK "Scripts deployed successfully ($deployed_count scripts)"
        return 0
    else
        log ERROR "Script deployment completed with errors ($deployed_count deployed, $failed_count failed)"
        return 1
    fi
}

copy_setup_script() {
    local setup_dest="$HOME/.kisuke/setup.sh"
    # Always download from release to ensure we get the version-injected script
    local url="https://github.com/${REPO}/releases/download/${KISUKE_VERSION}/setup.sh"
    
    if curl -sSL -o "$setup_dest" "$url"; then
        chmod +x "$setup_dest"
        log OK "setup.sh saved to $setup_dest"
    else
        log ERROR "Failed to save setup.sh (continuing anyway)"
    fi
}

write_version_file() {
    local version_file="$HOME/.kisuke/VERSION"
    echo "$KISUKE_VERSION" > "$version_file"
    log OK "Version $KISUKE_VERSION written to $version_file"
    [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK VERSION $KISUKE_VERSION"
}

handle_uninstall() {
    local packages=("${@}")
    [[ ${#packages[@]} -eq 0 ]] && packages=(jq ripgrep nodejs python3 claude git tmux)
    for pkg in "${packages[@]}"; do
        local removed=false
        # Use tr for lowercase conversion (Bash 3.2 compatible)
        # Use printf to avoid echo interpreting dash as option
        local pkg_lower=$(printf "%s" "$pkg" | tr '[:upper:]' '[:lower:]')
        case "$pkg_lower" in
            git|tmux)
                log NOTIFY "$pkg is not managed by this script"
                cache_remove "$pkg"
                ;;
            jq)
                if [[ -x "$BIN_DIR/jq" ]]; then
                    remove_symlinks jq
                    rm -f "$BIN_DIR/jq" "$INSTALL_MARKER_DIR/jq.installed"
                    removed=true
                fi
                cache_remove "jq_local"
                ;;
            nodejs)
                if [[ -d "$BIN_DIR/nodejs" ]]; then
                    remove_symlinks nodejs
                    rm -rf "$BIN_DIR"/nodejs "$INSTALL_MARKER_DIR/nodejs.installed"
                    removed=true
                fi
                cache_remove "nodejs_local"
                cache_remove "claude_cli"
                ;;
            python3)
                if [[ -d "$BIN_DIR/python3" ]]; then
                    remove_symlinks python3
                    rm -rf "$BIN_DIR/python3" "$INSTALL_MARKER_DIR/python3.installed"
                    removed=true
                fi
                cache_remove "python3_local"
                cache_remove "claude_sdk"
                ;;
            ripgrep)
                if [[ -x "$BIN_DIR/rg" ]]; then
                    remove_symlinks ripgrep
                    rm -f "$BIN_DIR/rg" "$INSTALL_MARKER_DIR/ripgrep.installed"
                    removed=true
                fi
                cache_remove "ripgrep_local"
                ;;
            claude)
                remove_symlinks claude
                [[ -x "$BIN_DIR/python3/bin/pip" ]] && "$BIN_DIR/python3/bin/pip" uninstall -y claude-code-sdk >/dev/null 2>&1 && removed=true
                [[ -x "$NODEJS_BIN_DIR/npm" ]] && "$NODEJS_BIN_DIR/npm" uninstall -g "@anthropic-ai/claude-code" >/dev/null 2>&1 && removed=true
                cache_remove "claude_sdk"
                cache_remove "claude_cli"
                ;;
            *)
                log NOTIFY "Unknown package '$pkg', skipping"
                continue
                ;;
        esac
        
        [[ "$removed" == true ]] && log OK "$pkg removed" || log OK "$pkg not found, skipping"
    done
    
    log OK "Uninstall complete"
}

main() {
    parse_args "$@"
    detect_platform
    detect_package_manager
    
    if [[ $UNINSTALL -eq 1 ]]; then
        if [[ ${#UNINSTALL_PACKAGES[@]} -gt 0 ]]; then
            handle_uninstall "${UNINSTALL_PACKAGES[@]}"
        else
            handle_uninstall
        fi
    elif [[ $INSTALL -eq 1 ]]; then
        local install_result=0
        if [[ ${#INSTALL_PACKAGES[@]} -gt 0 ]]; then
            if ! handle_install "${INSTALL_PACKAGES[@]}"; then
                install_result=1
            fi
        else
            if ! handle_install; then
                install_result=1
            fi
        fi
        
        if [[ $install_result -eq 0 ]]; then
            # Only copy script and write version file if installation succeeded
            copy_setup_script
            write_version_file
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK Installation complete"
        else
            log ERROR "Installation failed - VERSION file not created"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Installation failed - setup incomplete"
            exit 1
        fi
    elif [[ $DEPLOY -eq 1 ]]; then
        if deploy_scripts; then
            copy_setup_script
            write_version_file
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] OK Deployment complete"
        else
            log ERROR "Deployment failed - VERSION file not created"
            [[ $MOBILE_APP -eq 1 ]] && echo "[KECHO] ERROR Deployment failed"
            exit 1
        fi
    else
        show_status
    fi
}

main "$@"