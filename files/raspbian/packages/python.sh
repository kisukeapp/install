#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin"
PYTHON_DIR="$HOME/.kisuke/bin/python3"
mkdir -p "$BIN_DIR" "$PYTHON_DIR"

# Use exported ARCH from package.sh or fall back to uname -m
ARCH="${ARCH:-$(uname -m)}"
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

echo "[KECHO] Detected OS=$OS, ARCH=$ARCH"

case "$OS" in
  linux)
    case "$ARCH" in
      x86_64)
        PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250808/cpython-3.12.11+20250808-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
        ;;
      aarch64|arm64)
        PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250808/cpython-3.12.11+20250808-armv7-unknown-linux-gnueabihf-install_only_stripped.tar.gz"
        ;;
      *)
        echo "Unsupported Linux architecture: $ARCH"
        exit 1
        ;;
    esac
    ;;
  darwin)
    case "$ARCH" in
      x86_64)
        PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250808/cpython-3.12.11+20250808-x86_64-apple-darwin-install_only_stripped.tar.gz"
        ;;
      arm64)
        PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250808/cpython-3.12.11+20250808-aarch64-apple-darwin-install_only_stripped.tar.gz"
        ;;
      *)
        echo "Unsupported macOS architecture: $ARCH"
        exit 1
        ;;
    esac
    ;;
  *)
    echo "Unsupported OS: $OS"
    exit 1
    ;;
esac

echo "[KECHO] Downloading Python from $PYTHON_URL"

cd /tmp
curl -L -o python.tar.gz "$PYTHON_URL"
tar -xzf python.tar.gz -C "$PYTHON_DIR" --strip-components=1

echo "[KECHO] Creating symlinks"
ln -sf "$PYTHON_DIR/bin/python3" "$BIN_DIR/python"
ln -sf "$PYTHON_DIR/bin/pip3" "$BIN_DIR/pip"

echo "[KECHO] Verifying install"
"$BIN_DIR/python" --version
"$BIN_DIR/pip" --version

echo "[KECHO] Python standalone install complete"
