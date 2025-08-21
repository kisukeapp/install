#!/usr/bin/env bash

set -e
if ! command -v brew &>/dev/null; then
    echo "[KECHO] ALERT HOMEBREW_REQUIRED"
    exit 1
fi


echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
brew update >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 40%"
brew install zlib xz libffi openssl@3 >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
