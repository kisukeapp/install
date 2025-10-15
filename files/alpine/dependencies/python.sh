#!/usr/bin/env bash

set -e

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
sudo apk update >/dev/null 2>&1 || true
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 50%"
sudo apk add --no-cache curl tar >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
