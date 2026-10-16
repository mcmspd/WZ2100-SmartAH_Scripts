#!/usr/bin/env bash
# lobby_on.sh — restore direct lobby HTTPS for a user (undo lobby_off.sh).
# Usage: sudo ./lobby_on.sh <wz-user> [proxy-port]
# Idempotent; ignores a missing rule.
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Error: run as root. Try: sudo ./lobby_on.sh <wz-user> [proxy-port]" >&2
  exit 1
fi

WZ_USER=${1:?Usage: sudo ./lobby_on.sh <wz-user> [proxy-port]}
PORT=${2:-8080}

if iptables -t nat -C OUTPUT -m owner --uid-owner "$WZ_USER" -p tcp --dport 443 \
    -j REDIRECT --to-port "$PORT" 2>/dev/null; then
  iptables -t nat -D OUTPUT -m owner --uid-owner "$WZ_USER" -p tcp --dport 443 \
    -j REDIRECT --to-port "$PORT"
  echo "Removed redirect: $WZ_USER 443 direct again."
else
  echo "No redirect rule present, nothing to do."
fi
