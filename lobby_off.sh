#!/usr/bin/env bash
# lobby_off.sh — steer a WZ instance user's outbound HTTPS into the MITM proxy.
# Usage: sudo ./lobby_off.sh <wz-user> [proxy-port]   (default port 8080)
# Requires the proxy (lobby_mitm.py) listening on 127.0.0.1:<port>.
# Idempotent. Only affects NEW connections from that UID; ESTABLISHED player
# traffic is untouched. Only port 443 is redirected; the proxy allowlists the
# lobby SNI and tunnels everything else straight through.
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Error: run as root. Try: sudo ./lobby_off.sh <wz-user> [proxy-port]" >&2
  exit 1
fi

WZ_USER=${1:?Usage: sudo ./lobby_off.sh <wz-user> [proxy-port]}
PORT=${2:-8080}

if ! id "$WZ_USER" >/dev/null 2>&1; then
  echo "Error: unknown user $WZ_USER" >&2
  exit 1
fi

if iptables -t nat -C OUTPUT -m owner --uid-owner "$WZ_USER" -p tcp --dport 443 \
    -j REDIRECT --to-port "$PORT" 2>/dev/null; then
  echo "Rule already present: $WZ_USER 443 -> 127.0.0.1:$PORT"
else
  iptables -t nat -A OUTPUT -m owner --uid-owner "$WZ_USER" -p tcp --dport 443 \
    -j REDIRECT --to-port "$PORT"
  echo "Lobby HTTPS for $WZ_USER now flows to 127.0.0.1:$PORT"
fi
echo "Verify: sudo iptables -t nat -L OUTPUT -n -v | grep $PORT"
