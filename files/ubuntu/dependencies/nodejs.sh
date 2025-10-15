#!/usr/bin/env bash

set -e

echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 0%"
DEBIAN_FRONTEND=noninteractive sudo apt-get update -qq >/dev/null 2>&1
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 40%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq build-essential >/dev/null 2>&1
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 60%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq curl >/dev/null 2>&1
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 80%"
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y -qq xz-utils >/dev/null 2>&1
echo "[KECHO] PROGRESS NODEJS DEPENDENCIES 100%"
