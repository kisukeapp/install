#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin"
NODE_VERSION="v22.9.0"

mkdir -p "$BIN_DIR"

# Use exported ARCH from package.sh or fall back to uname -m
ARCH="${ARCH:-$(uname -m)}"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

case "$ARCH" in
    x86_64|amd64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    armv7l|armv7) NODE_ARCH="armv7l" ;;
    *) echo "[KECHO] Unsupported architecture: $ARCH" && exit 1 ;;
esac

case "$OS" in
    linux) NODE_OS="linux" ;;
    darwin) NODE_OS="darwin" ;;
    *) echo "[KECHO] Unsupported OS: $OS"; exit 1 ;;
esac

# Detect musl for Linux builds
MUSL_SUFFIX=""
if [[ "$NODE_OS" == "linux" ]]; then
  if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
    if [[ "$NODE_ARCH" == "x64" || "$NODE_ARCH" == "arm64" ]]; then
      MUSL_SUFFIX="-musl"
    fi
  elif [[ -f /etc/alpine-release ]] && [[ "$NODE_ARCH" == "x64" || "$NODE_ARCH" == "arm64" ]]; then
    MUSL_SUFFIX="-musl"
  fi
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
cd "$tmpdir"

echo "[KECHO] PROGRESS NODEJS PACKAGE 0%"
NODE_DIST="node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}${MUSL_SUFFIX}.tar.xz"
EXTRACT_DIR="node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}${MUSL_SUFFIX}"
if ! curl -fSLO "https://nodejs.org/dist/${NODE_VERSION}/${NODE_DIST}"; then
  if [[ "$NODE_OS" == "linux" && "$NODE_ARCH" == "armv7l" ]]; then
    echo "[KECHO] NOTIFY Falling back to Node.js latest v20 for armv7l"
    shas_url="https://nodejs.org/dist/latest-v20.x/SHASUMS256.txt"
    tarname=$(curl -fsSL "$shas_url" | awk '{print $2}' | grep "node-v20.*-${NODE_OS}-${NODE_ARCH}\.tar\.xz" | tail -n1 || true)
    if [[ -n "$tarname" ]]; then
      curl -fSLO "https://nodejs.org/dist/latest-v20.x/${tarname}"
      NODE_DIST="$tarname"
      EXTRACT_DIR="${tarname%.tar.xz}"
    else
      echo "[KECHO] ERROR Could not find Node v20 tarball for armv7l"
      exit 1
    fi
  else
    echo "[KECHO] ERROR Failed to download Node tarball: ${NODE_DIST}"
    exit 1
  fi
fi
echo "[KECHO] PROGRESS NODEJS PACKAGE 30%"

tar -xf "$NODE_DIST"
echo "[KECHO] PROGRESS NODEJS PACKAGE 60%"

mkdir -p "${BIN_DIR}"
rm -rf "${BIN_DIR}/nodejs"
mv "$EXTRACT_DIR" "${BIN_DIR}/nodejs"
echo "[KECHO] PROGRESS NODEJS PACKAGE 70%"

# Symlinks and wrappers (no PATH changes)
ln -sf "${BIN_DIR}/nodejs/bin/node" "${BIN_DIR}/node"
ln -sf "${BIN_DIR}/nodejs/bin/corepack" "${BIN_DIR}/corepack"

cat > "${BIN_DIR}/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export NPM_CONFIG_PREFIX="$HOME/.kisuke/bin/nodejs"
export NPM_CONFIG_CACHE="$HOME/.kisuke/npm-cache"
export NPM_CONFIG_USERCONFIG="$HOME/.kisuke/npmrc"
exec "$HOME/.kisuke/bin/nodejs/bin/npm" "$@"
EOF
chmod +x "${BIN_DIR}/npm"

cat > "${BIN_DIR}/npx" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export NPM_CONFIG_PREFIX="$HOME/.kisuke/bin/nodejs"
export NPM_CONFIG_CACHE="$HOME/.kisuke/npm-cache"
export NPM_CONFIG_USERCONFIG="$HOME/.kisuke/npmrc"
exec "$HOME/.kisuke/bin/nodejs/bin/npx" "$@"
EOF
chmod +x "${BIN_DIR}/npx"

# Verify installation using absolute paths
if "${BIN_DIR}/node" -v \
   && "${BIN_DIR}/npm" --version \
   && "${BIN_DIR}/npx" --version; then
    echo "[KECHO] PROGRESS NODEJS PACKAGE 100%"
else
    echo "[KECHO] PROGRESS NODEJS PACKAGE FAILED"
    exit 1
fi
