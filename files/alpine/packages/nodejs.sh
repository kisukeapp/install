#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin"
export PATH="${BIN_DIR}:${PATH}"

NODE_VERSION="v22.9.0"

mkdir -p "$BIN_DIR"

# Use exported ARCH from package.sh or fall back to uname -m
ARCH="${ARCH:-$(uname -m)}"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

case "$ARCH" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *) echo "[KECHO] Unsupported architecture: $ARCH" && exit 1 ;;
esac

case "$OS" in
    linux) NODE_OS="linux" ;;
    darwin) NODE_OS="darwin" ;;
    *)
        echo "[KECHO] Unsupported OS: $OS"
        exit 1
        ;;
esac

cd /tmp
echo "[KECHO] PROGRESS NODEJS PACKAGE 0%"
NODE_DIST="node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}.tar.xz"
curl -LO "https://nodejs.org/dist/${NODE_VERSION}/${NODE_DIST}"
echo "[KECHO] PROGRESS NODEJS PACKAGE 30%"

tar -xf "$NODE_DIST"
echo "[KECHO] PROGRESS NODEJS PACKAGE 60%"

mv "node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}" "${BIN_DIR}/nodejs"
echo "[KECHO] PROGRESS NODEJS PACKAGE 70%"

# Symlinks
ln -sf "${BIN_DIR}/nodejs/bin/node" "${BIN_DIR}/node"
ln -sf "${BIN_DIR}/nodejs/bin/npm" "${BIN_DIR}/npm"
ln -sf "${BIN_DIR}/nodejs/bin/npx" "${BIN_DIR}/npx"

# Verify installation
if "${BIN_DIR}/node" -v && "${BIN_DIR}/npm" -v && "${BIN_DIR}/npx" -v; then
    echo "[KECHO] PROGRESS NODEJS PACKAGE 100%"
else
    echo "[KECHO] PROGRESS NODEJS PACKAGE FAILED"
    exit 1
fi
