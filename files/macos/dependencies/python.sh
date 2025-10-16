#!/usr/bin/env bash

set -euo pipefail

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"

# Only ensure xz is present for packaging; Python itself is standalone.
if ! command -v xz >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 40%"
        # Respect brew env from package.sh (no update, no source builds)
        brew install xz >/dev/null 2>&1 || brew install xz
    else
        echo "[KECHO] ALERT HOMEBREW_REQUIRED"
        exit 1
    fi
else
    echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 40%"
fi

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
