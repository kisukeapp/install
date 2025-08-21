#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin"
export PATH="${BIN_DIR}:${PATH}"

NODE_VERSION="v22.9.0"

mkdir -p "$BIN_DIR"

ARCH="$(uname -m)"
if grep -qi '^ID=raspbian' /etc/os-release 2>/dev/null; then
    ARCH="armv7l"
fi

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

case "$ARCH" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    armv7l) NODE_ARCH="armv7l" ;;
    *) echo "[KECHO] Unsupported architecture: $ARCH" && exit 1 ;;
esac

case "$OS" in
    linux) NODE_OS="linux" ;;
    darwin) NODE_OS="darwin" ;;
    *) echo "[KECHO] Unsupported OS: $OS" && exit 1 ;;
esac

cd /tmp
echo "[KECHO] PROGRESS NODEJS PACKAGE 0%"
NODE_DIST="node-${NODE_VERSION}-linux-armv7l.tar.xz"
curl -fLO --progress-bar "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-armv7l.tar.xz"
echo "[KECHO] PROGRESS NODEJS PACKAGE 30%"

tar -xf "$NODE_DIST"
echo "[KECHO] PROGRESS NODEJS PACKAGE 60%"

rm -rf "${BIN_DIR}/nodejs"
mv "node-${NODE_VERSION}-linux-armv7l" "${BIN_DIR}/nodejs"
echo "[KECHO] PROGRESS NODEJS PACKAGE 70%"

ln -sf "${BIN_DIR}/nodejs/bin/node" "${BIN_DIR}/node"
ln -sf "${BIN_DIR}/nodejs/bin/npm" "${BIN_DIR}/npm"
ln -sf "${BIN_DIR}/nodejs/bin/npx" "${BIN_DIR}/npx"

if "${BIN_DIR}/node" -v && "${BIN_DIR}/npm" -v && "${BIN_DIR}/npx" -v; then
    echo "[KECHO] PROGRESS NODEJS PACKAGE 100%"
else
    echo "[KECHO] PROGRESS NODEJS PACKAGE FAILED"
    exit 1
fi
