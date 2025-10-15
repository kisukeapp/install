#!/usr/bin/env bash

set -e

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
sudo pacman -Sy --noconfirm >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 20%"
sudo pacman -S --noconfirm base-devel openssl zlib >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 40%"
sudo pacman -S --noconfirm ncurses readline >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 60%"
sudo pacman -S --noconfirm sqlite gdbm db bzip2 >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 80%"
sudo pacman -S --noconfirm expat xz tk >/dev/null
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
