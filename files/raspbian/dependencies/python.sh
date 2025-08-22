#!/usr/bin/env bash

set -e
if ! sudo -n true; then
    echo "[KECHO] ALERT SUDO_REQUIRED"
    exit 1
fi

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
DEBIAN_FRONTEND=noninteractive sudo apt-get update -qq >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 50%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq tar >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
