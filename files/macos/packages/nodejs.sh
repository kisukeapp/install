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
    *) echo "[KECHO] Unsupported architecture: $ARCH" && exit 1 ;;
esac

case "$OS" in
    linux) NODE_OS="linux" ;;
    darwin) NODE_OS="darwin" ;;
    *) echo "[KECHO] Unsupported OS: $OS"; exit 1 ;;
esac

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
cd "$tmpdir"

echo "[KECHO] PROGRESS NODEJS PACKAGE 0%"
NODE_DIST="node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}.tar.xz"
EXTRACT_DIR="node-${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}"
curl -fSLO "https://nodejs.org/dist/${NODE_VERSION}/${NODE_DIST}"
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
# Ensure local node is resolvable for JS shims using /usr/bin/env node
export PATH="$HOME/.kisuke/bin:$HOME/.kisuke/bin/nodejs/bin:$PATH"
exec "$HOME/.kisuke/bin/nodejs/bin/npm" "$@"
EOF
chmod +x "${BIN_DIR}/npm"

cat > "${BIN_DIR}/npx" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export NPM_CONFIG_PREFIX="$HOME/.kisuke/bin/nodejs"
export NPM_CONFIG_CACHE="$HOME/.kisuke/npm-cache"
export NPM_CONFIG_USERCONFIG="$HOME/.kisuke/npmrc"
# Ensure local node is resolvable for JS shims using /usr/bin/env node
export PATH="$HOME/.kisuke/bin:$HOME/.kisuke/bin/nodejs/bin:$PATH"
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
