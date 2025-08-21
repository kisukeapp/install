#!/usr/bin/env bash

set -e
if ! sudo -n true; then
    echo "[KECHO] ALERT SUDO_REQUIRED"
    exit 1
fi

echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 0%"
sudo pacman -Sy --noconfirm  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 40%"
sudo pacman -S --noconfirm  base-devel >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 60%"
sudo pacman -S --noconfirm  curl  >/dev/null
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 100%"
