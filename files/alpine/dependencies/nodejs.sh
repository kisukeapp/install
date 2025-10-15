#!/usr/bin/env bash

set -e

echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 0%"
sudo apk update >/dev/null 2>&1 || true
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 40%"
sudo apk add --no-cache curl xz tar libstdc++ >/dev/null 2>&1 || true
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 100%"
