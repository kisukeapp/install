#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin" && export PATH="${BIN_DIR}:${PATH}" && NODEJS_BIN_DIR="$HOME/.kisuke/bin/nodejs/bin" && export PATH="${NODEJS_BIN_DIR}:${PATH}"

NODE_VERSION="v22.9.0"

mkdir -p "$BIN_DIR"

# Use exported ARCH from package.sh or fall back to uname -m
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *) echo "[KECHO] Unsupported architecture: $ARCH" && exit 1 ;;
esac

cd /tmp
echo "[KECHO] PROGRESS NODEJS PACKAGE 0%"
curl -LO "https://unofficial-builds.nodejs.org/download/release/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}-musl.tar.xz"
echo "[KECHO] PROGRESS NODEJS PACKAGE 30%"

tar -xf "node-${NODE_VERSION}-linux-${NODE_ARCH}-musl.tar.xz"
echo "[KECHO] PROGRESS NODEJS PACKAGE 60%"

mv "node-${NODE_VERSION}-linux-${NODE_ARCH}-musl" "${BIN_DIR}/nodejs"
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
