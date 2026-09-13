#!/usr/bin/env bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root. Try: sudo ./open_ports.sh" >&2
  exit 1
fi

START_PORT=2100
END_PORT=2110

echo "Inserting TCP ports ${START_PORT}:${END_PORT} before REJECT rule..."
iptables -I INPUT 5 -p tcp --dport ${START_PORT}:${END_PORT} -j ACCEPT

echo "TCP rules inserted successfully!"
echo "----------------------------------------"
iptables -L INPUT -n --line-numbers
netfilter-persistent save
