#!/usr/bin/env bash
set -e

if ! id "$USERNAME" &>/dev/null; then
    useradd -m -s /bin/bash "$USERNAME"
    echo "$USERNAME:$PASSWORD" | chpasswd
    usermod -aG sudo "$USERNAME"
fi

for file in /root/*.tar.xz; do
    if [ -f "$file" ]; then
        tar -xJf "$file" -C "/home/$USERNAME"
    fi
done

chown -R "$USERNAME:$USERNAME" "/home/$USERNAME"

BASH_PROFILE="/home/$USERNAME/.bash_profile"
{
    echo 'BIN_DIR="$HOME/.kisuke/bin" && export PATH="${BIN_DIR}:${PATH}"'
    echo 'NODEJS_BIN_DIR="$HOME/.kisuke/bin/nodejs/bin" && export PATH="${NODEJS_BIN_DIR}:${PATH}"'
} >> "$BASH_PROFILE"

rm -rf /root/*

cd "/home/$USERNAME"

exec "$@"
