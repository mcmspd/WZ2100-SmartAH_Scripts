#!/usr/bin/env bash
set -e

# install_dep.sh — Install system dependencies for WZ2100 autohost
#
# Installs: tmux, iptables, netfilter-persistent, curl, openssl, git, warzone2100
#
# NOTE: We install the SYSTEM warzone2100 binary (/usr/games/warzone2100.real),
# NOT flatpak. Flatpak ignores the host CA store, which breaks MITM token capture
# (curl error 60). The system binary uses the host CA store correctly.

sudo apt update
DEBIAN_FRONTEND=noninteractive sudo apt install -y \
    tmux iptables netfilter-persistent curl openssl git warzone2100

# Verify
if [ ! -x /usr/games/warzone2100.real ]; then
    echo "Error: /usr/games/warzone2100.real not found after install." >&2
    exit 1
fi
echo "Warzone binary found: /usr/games/warzone2100.real"
echo "Dependencies installed."
