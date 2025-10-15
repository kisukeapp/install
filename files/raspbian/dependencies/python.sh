#!/usr/bin/env bash

set -e

echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 0%"
DEBIAN_FRONTEND=noninteractive sudo apt-get update -qq >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 50%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq tar >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 80%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq curl >/dev/null 2>&1
echo "[KECHO] PROGRESS PYTHON DEPENDENCIES 100%"
