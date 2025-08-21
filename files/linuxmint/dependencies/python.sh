#!/usr/bin/env bash

set -e
if ! sudo -n true; then
    echo "[KECHO] ALERT SUDO_REQUIRED"
    exit 1
fi

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
sudo apt-get update >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 50%"
sudo apt-get install -y tar >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
