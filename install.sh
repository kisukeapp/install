#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.kisuke/bin"

export PATH="${BIN_DIR}:${PATH}"
export DEBIAN_FRONTEND=noninteractive

if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="${ID,,}"
elif [[ "$(uname -s)" == "Darwin" ]]; then
    DISTRO="macos"
else
    echo "[KECHO] ALERT UNSUPPORTED_OS"
    exit 1
fi

FILES_DIR="${SCRIPT_DIR}/files/${DISTRO}"

function is_installed() {
    local pkg="$1"

    if command -v "$pkg" &>/dev/null; then
        return 0
    fi

    case "$DISTRO" in
        ubuntu|debian)
            dpkg -s "$pkg" &>/dev/null && return 0
            ;;
        fedora|centos|rhel)
            rpm -q "$pkg" &>/dev/null && return 0
            ;;
        arch)
            pacman -Q "$pkg" &>/dev/null && return 0
            ;;
        opensuse*|sles)
            zypper se -i "$pkg" &>/dev/null && return 0
            ;;
        darwin|macos)
            brew list "$pkg" &>/dev/null && return 0
            ;;
    esac

    return 1
}

mkdir -p "${BIN_DIR}"

if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    echo "Adding ${BIN_DIR} to PATH"
    export PATH="${BIN_DIR}:${PATH}"
fi

echo "[KECHO] DEV_ENVIRONMENT $DISTRO"

dep_pkgs=()

shopt -s nullglob

for f in "${FILES_DIR}/dependencies"/*.sh; do
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*(sudo[[:space:]]+)?(apt-get|yum|dnf|pacman|zypper|brew)[[:space:]]+(install|add|-S) ]]; then
            pkgs=$(echo "$line" |
                sed -E 's/^\s*(sudo\s+)?(apt-get|yum|dnf|pacman|zypper|brew)\s+(install|add|-S)\s+//' |
                tr -s ' ')
            for word in $pkgs; do
                if [[ ! "$word" =~ ^- && ! "$word" =~ ^/ && "$word" != ">/dev/null" ]]; then
                    dep_pkgs+=("$word")
                fi
            done
        fi
    done < "$f"
done

pkg_pkgs=()
for f in "${FILES_DIR}/packages"/*.sh; do
    pkg=$(basename "$f" .sh)
    pkg_pkgs+=("$pkg")
done

all_pkgs=("${dep_pkgs[@]}" "${pkg_pkgs[@]}")
unique_pkgs=($(printf "%s\n" "${all_pkgs[@]}" | sort -u))

missing_pkgs=()
installed_pkgs=()

for pkg in "${unique_pkgs[@]}"; do
    if is_installed "$pkg"; then
        installed_pkgs+=("$pkg")
    else
        missing_pkgs+=("$pkg")
    fi
done

echo "[KECHO] DEPENDENCIES ${dep_pkgs[*]}"
echo "[KECHO] PACKAGES ${pkg_pkgs[*]}"
echo "[KECHO] ALERT INSTALLED ${installed_pkgs[*]}"
echo "[KECHO] ALERT MISSING_DEPENDENCIES ${missing_pkgs[*]}"
