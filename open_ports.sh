#!/usr/bin/env bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root. Try: sudo ./open_ports.sh" >&2
  exit 1
fi

START_PORT=2100
END_PORT=2110
RELAY_PORT=2200

open_one() {
  if iptables -C INPUT -p tcp --dport "$1" -j ACCEPT 2>/dev/null; then
    echo "TCP port $1 already accepted, nothing to do."
  else
    echo "Inserting TCP port $1 before REJECT rule..."
    iptables -I INPUT 5 -p tcp --dport "$1" -j ACCEPT
  fi
}

open_one "${START_PORT}:${END_PORT}"
open_one "${RELAY_PORT}"
echo "----------------------------------------"
iptables -L INPUT -n --line-numbers
netfilter-persistent save
