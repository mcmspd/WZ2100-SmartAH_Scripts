#!/usr/bin/env bash
# host.sh — start/stop/status the Warzone autohost with one command.
# Usage: ./host.sh start [MapName]   ./host.sh stop   ./host.sh status
# Run as ubuntu (uses sudo internally where needed). Game runs as wzbot.
set -u

REPO="$HOME/WZ2100-SmartAH_Scripts"
GAME_REPO="/home/wzbot/WZ"
GAME_USER="wzbot"
SESSION="WZ_PROD"
PORT=2100
PROXY_PORT=18080
MAP="${2:-}"

die() { echo "Error: $*" >&2; exit 1; }

proxy_up() {
  ss -tln 2>/dev/null | grep -q "127.0.0.1:$PROXY_PORT"
}

rule_up() {
  local uid
  uid=$(id -u "$GAME_USER" 2>/dev/null) || return 1
  sudo iptables -t nat -L OUTPUT -n 2>/dev/null | grep -q "match $uid.*redir ports $PROXY_PORT"
}

bot_sessions() {
  sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep "^WZ_" || true
}

bot_up() {
  [ -n "$(bot_sessions)" ]
}

do_status() {
  echo "--- proxy ---"
  proxy_up && echo "proxy: UP (port $PROXY_PORT)" || echo "proxy: DOWN"
  echo "--- firewall ---"
  rule_up && echo "redirect: ACTIVE" || echo "redirect: MISSING"
  echo "--- instance ---"
  SESSIONS=$(bot_sessions)
  if [ -n "$SESSIONS" ]; then
    echo "tmux sessions:"
    echo "$SESSIONS" | while read -r s; do
      ST=$(sudo cat "$GAME_REPO/instance_configs/$s.status.json" 2>/dev/null)
      echo "  $s: $ST"
    done
  else
    echo "tmux: DOWN (no WZ_* sessions)"
  fi
}

do_start() {
  # 1. proxy first (token capture depends on it)
  if ! proxy_up; then
    echo "Starting proxy..."
    cd "$REPO" || die "repo not found at $REPO"
    # shellcheck disable=SC2086
    setsid nohup python3 lobby_mitm.py --port $PROXY_PORT --ca mitm-ca \
      --tokens "$GAME_REPO/instance_configs/.lobby_tokens.json" \
      --blocklist "$GAME_REPO/instance_configs/.lobby_blocklist.json" \
      > mitm-proxy.log 2>&1 < /dev/null &
    sleep 3
    proxy_up || die "proxy failed to start (see mitm-proxy.log)"
  fi
  # 2. firewall rule
  if ! rule_up; then
    echo "Adding firewall rule..."
    sudo "$REPO/lobby_off.sh" "$GAME_USER" "$PROXY_PORT" || die "firewall rule failed"
    sudo netfilter-persistent save >/dev/null 2>&1 || true
  fi
  # 3. instance
  if bot_up; then
    echo "Instance already running: $(bot_sessions | tr '\n' ' ')"
    echo "Stop it first if you want a fresh one."
  else
    echo "Starting instance..."
    if [ -n "$MAP" ]; then EXTRA=" $MAP"; else EXTRA=""; fi
    # shellcheck disable=SC2086
    sudo -u "$GAME_USER" tmux new-session -d -s "$SESSION" \
      "cd $GAME_REPO && python3 instance_bot.py $PORT $SESSION$EXTRA"
  fi
  echo "Waiting for lobby (~40s)..."
  for _ in $(seq 1 20); do
    sleep 5
    STATUS=$(sudo cat "$GAME_REPO/instance_configs/$SESSION.status.json" 2>/dev/null)
    case "$STATUS" in
      *'"status": "ready"'*) echo "READY: $STATUS"; return 0 ;;
      *'"status": "failed"'*) echo "FAILED: $STATUS"; return 1 ;;
    esac
  done
  echo "Still starting - check status with: ./host.sh status"
}

do_stop() {
  if bot_up; then
    echo "Stopping instances gracefully..."
    for s in $(bot_sessions); do
      sudo -u "$GAME_USER" tmux send-keys -t "$s" C-c
    done
    for _ in $(seq 1 6); do
      sleep 5
      bot_up || break
    done
    sudo pkill -x warzone2100.real 2>/dev/null || true
    sudo -u "$GAME_USER" tmux kill-server 2>/dev/null || true
    echo "Instances stopped. (Proxy + firewall left running for next start.)"
  else
    echo "Instance not running."
  fi
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  status) do_status ;;
  *) echo "Usage: $0 start [MapName] | stop | status" >&2; exit 1 ;;
esac
