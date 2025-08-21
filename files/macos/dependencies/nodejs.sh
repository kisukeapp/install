#!/usr/bin/env bash

set -e
if ! command -v brew &>/dev/null; then
    echo "[KECHO] ALERT HOMEBREW_REQUIRED"
    exit 1
fi

echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 0%"
brew update >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 40%"
brew install icu4c  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 60%"
brew install pkg-config  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 100%"
