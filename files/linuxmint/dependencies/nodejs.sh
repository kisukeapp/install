#!/usr/bin/env bash

set -e
if ! sudo -n true; then
    echo "[KECHO] ALERT SUDO_REQUIRED"
    exit 1
fi

echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 0%"
sudo apt-get update >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 40%"
sudo apt-get install -y build-essential  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 60%"
sudo apt-get install -y curl  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 100%"
