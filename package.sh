#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="$HOME/.kisuke/bin"
echo "[KECHO] Cleaning previous Kisuke archives and directories..."

rm -f "$HOME"/kisuke-*.tar.xz

rm -rf "$HOME/.kisuke"

echo "[KECHO] Cleanup done."

unameOut="$(uname -s)"
case "${unameOut}" in
    Linux*)     machine=linux ;;
    Darwin*)    machine=mac ;;
    CYGWIN*)    machine=cygwin ;;
    MINGW*)     machine=mingw ;;
    MSYS_NT*)   machine=msys ;;
    *)          machine="UNKNOWN:${unameOut}" ;;
esac

# Paths
NODEJS_BIN_DIR="$BIN_DIR/nodejs/bin"
export PATH="$BIN_DIR:$NODEJS_BIN_DIR:$PATH"

uname_m=$(uname -m)
os_id=""
if [[ -f /etc/os-release ]]; then
    os_id=$(grep -E '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')
fi
if [[ "$os_id" == "raspbian" ]]; then
    ARCH="aarch64"
else
    case "$uname_m" in
        arm64|aarch64) ARCH="arm64" ;;
        x86_64|amd64)  ARCH="x86_64" ;;
        *) KECHO ERROR "Unsupported architecture: $uname_m"; exit 1 ;;
    esac
fi


rm -rf install.log
echo "[KECHO] Running install.sh..."
bash install.sh | tee -a install.log

DISTRO=$(grep '\[KECHO\] DEV_ENVIRONMENT' install.log | awk '{print $3}' || true)
if [[ -z "$DISTRO" && -f /etc/os-release ]]; then
  DISTRO=$(grep -E '^ID=' /etc/os-release | cut -d= -f2)
fi

if [[ -z "$DISTRO" ]]; then
  echo "ERROR: Could not detect distro."
  exit 1
fi

echo "Detected distro: $DISTRO" | tee -a install.log
echo "Detected OS: $machine" | tee -a install.log
echo "Detected arch: $ARCH" | tee -a install.log

if [[ -d "files/$DISTRO/dependencies" ]]; then
  for dep in files/$DISTRO/dependencies/*.sh; do
    if [[ -f "$dep" ]]; then
      echo "Running dependency script: $dep" | tee -a install.log
      bash "$dep" | tee -a install.log
    fi
  done
fi

PACKAGE_ORDER=(python nodejs)

for pkg in "${PACKAGE_ORDER[@]}"; do
  PKG_SCRIPT="files/$DISTRO/packages/${pkg}.sh"
  if [[ -f "$PKG_SCRIPT" ]]; then
    echo "Running package script: $PKG_SCRIPT" | tee -a install.log
    bash "$PKG_SCRIPT" | tee -a install.log
  else
    echo "Package script not found, skipping: $PKG_SCRIPT"
  fi
done

if [[ ! -d "$BIN_DIR" ]]; then
  echo "ERROR: Directory $BIN_DIR does not exist."
  exit 1
fi

echo "Creating separate tar.xz archives for each package directory inside $BIN_DIR (ignoring symlinks)..."

cd "$BIN_DIR"
shopt -s nullglob

declare -A main_binary=(
  [python]="python3"
  [nodejs]="node"
)

declare -A version_flags=(
  [python]="--version"
  [nodejs]="-v"
)

pkg_dirs=()
for dir in */; do
  [[ -L "$dir" ]] && echo "Skipping symlink directory: $dir" && continue
  pkg_dirs+=("${dir%/}")
done
echo "Package directories found: ${pkg_dirs[*]}"

for pkg_dir in "${pkg_dirs[@]}"; do
    case "$pkg_dir" in
        python3) base_name="python" ;;
        nodejs) base_name="nodejs" ;;
        *) base_name="$pkg_dir" ;;
    esac

    echo "Processing $pkg_dir as $base_name"

    bin_name="${main_binary[$base_name]:-$base_name}"
    bin_path="$BIN_DIR/$pkg_dir/bin/$bin_name"
    PACKAGE_VERSION="unknown-version"
    version_flag="${version_flags[$base_name]:---version}"

    if [[ -x "$bin_path" ]]; then
        echo "Found executable: $bin_path"
        raw_version="$("$bin_path" "$version_flag" 2>&1 | head -n1 | xargs || true)"
        if [[ "$raw_version" =~ ([0-9]+(\.[0-9]+)*(-[a-zA-Z0-9.]+)?) ]]; then
            PACKAGE_VERSION="${BASH_REMATCH[1]}"
        fi
    else
        echo "No valid binary found for $pkg_dir, skipping archive."
        continue
    fi

    tarname="kisuke-${PACKAGE_VERSION}-${machine}-${ARCH}-${pkg_dir}.tar.xz"
    echo "Archiving $pkg_dir (version: $PACKAGE_VERSION) into $tarname"
    (
        cd "$HOME"
        if [ "$machine" = "mac" ]; then
            TAR_CMD="gtar"
        else
            TAR_CMD="tar"
        fi
        $TAR_CMD -cf "$tarname" -I "xz -9e -T0" ".kisuke/bin/${pkg_dir}" >/dev/null
    )
done

shopt -u nullglob
echo "All packages archived."
