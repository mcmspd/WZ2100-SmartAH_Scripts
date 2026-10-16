#!/usr/bin/env bash
set -e

sudo apt install -y flatpak tmux
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
if flatpak info net.wz2100.wz2100 >/dev/null 2>&1; then
  echo "net.wz2100.wz2100 already installed, nothing to do."
else
  flatpak install -y flathub net.wz2100.wz2100
fi
