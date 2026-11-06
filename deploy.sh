#!/usr/bin/env bash
# deploy.sh — Zero-state deployment + server control for WZ2100 autohost
#
# Layout (Option A — everything under $HOME, UID-separated):
#   Code:         $HOME/WZ2100-SmartAH_Scripts/   (owned by ubuntu, group wzbot)
#   Runtime:      $HOME/WZ2100-SmartAH_Scripts/instance_configs/  (writable by wzbot)
#   Proxy runs as: $SUDO_USER (ubuntu)   — MITM + relay (TCP 18080 + 2200)
#   Game runs as:  wzbot                  — instance_bot.py + warzone2100.real
#
# Usage:
#   sudo ./deploy.sh              # Full deployment from zero state (idempotent)
#   sudo ./deploy.sh start [map]  # Start (or restart) the game instance
#   sudo ./deploy.sh stop         # Stop the game instance (proxy+firewall stay)
#   sudo ./deploy.sh status       # Check deployment status
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
GAME_USER="wzbot"
PROXY_USER="${SUDO_USER:-ubuntu}"
PROXY_PORT=18080
RELAY_PORT=2200
GAME_PORT=2100
SESSION_NAME="WZ_PROD"
MAP_NAME="${2:-NTW_3v3Full}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()   { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
err()   { echo -e "${RED}[deploy]${NC} $*" >&2; }

require_root() {
    if [ "$EUID" -ne 0 ]; then
        err "Please run as root: sudo $0 $*"
        exit 1
    fi
}

# ── Step 0: Refresh released mods from GitHub ───────────────────────────
update_mods() {
    if [ -x "$REPO_DIR/update_mods.sh" ]; then
        "$REPO_DIR/update_mods.sh" || warn "mod refresh failed; continuing with current mods"
    else
        warn "update_mods.sh not found; skipping mod refresh"
    fi
}

# ── Step 1: System dependencies ──────────────────────────────────────────
install_deps() {
    log "Installing system dependencies..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            tmux iptables netfilter-persistent curl openssl git warzone2100 2>&1 | tail -5
    elif command -v apk &>/dev/null; then
        apk add --no-cache tmux iptables netfilter-persistent curl openssl git warzone2100
    else
        err "Unsupported package manager. Install manually: tmux iptables netfilter-persistent curl openssl git warzone2100"
        exit 1
    fi

    # Verify Warzone binary exists after install
    if [ ! -x /usr/games/warzone2100.real ]; then
        err "Warzone binary not found at /usr/games/warzone2100.real after install."
        exit 1
    fi
    log "Warzone binary found: /usr/games/warzone2100.real"
    log "Dependencies installed."
}

# ── Step 2: Create dedicated game user (UID separation from proxy) ───────
create_user() {
    if id "$GAME_USER" &>/dev/null; then
        log "User $GAME_USER already exists."
    else
        log "Creating system user $GAME_USER..."
        useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$GAME_USER"
    fi

    # wzbot must be in ubuntu group to read files under /home/ubuntu
    # wzbot runs the game; ubuntu runs the proxy — UID separation is critical.
    if ! id -nG "$GAME_USER" | grep -qw ubuntu; then
        log "Adding $GAME_USER to ubuntu group for file access..."
        usermod -aG ubuntu "$GAME_USER"
    fi
}

# ── Step 3: Open firewall ports ──────────────────────────────────────────
open_ports() {
    log "Opening firewall ports (TCP 2100-2110 game, 2200 relay)..."
    for port in "2100:2110" "2200"; do
        if iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
            : # already present
        else
            iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
        fi
    done
    netfilter-persistent save 2>/dev/null || true
    log "Firewall ports open."
}

# ── Step 4: Generate instances.json if missing (never overwrite prod) ────
generate_config() {
    local config="$REPO_DIR/instances.json"
    if [ -f "$config" ]; then
        log "instances.json already exists — leaving untouched (preserves secrets)."
        return
    fi

    log "Creating instances.json template — EDIT IT before starting!"
    cat > "$config" <<'EOF'
{
  "host_name": "FreeHost",
  "host_key": "REPLACE_WITH_YOUR_B64_PUBLIC_KEY",
  "game_name": "FreeHost",
  "map_name": "NTW_3v3Full",
  "lobby_address": "https://wzlobby.wz2100.net/lobby",
  "game_password": "",
  "greeting_lines": [
    "Welcome to Free Host {name}!",
    "- You can initiate a vote kick by: /votekick <Player Slot>",
    "- Vote by /vote y or /vote n",
    "- Player will be kicked if 100% of the votes count to YES."
  ],
  "maps": [
    {"name": "NTW_3v3Full", "mods": []},
    {"name": "NTW_4x4_Full", "mods": []},
    {"name": "NTW_5v5Full_RQ", "mods": []},
    {"name": "NTW-Full2v2", "mods": []},
    {"name": "Opa-Shtorm-A-FB", "mods": []},
    {"name": "RO_1v1Full", "mods": []},
    {"name": "Warwick", "file": "5c-Warwick-2fe6272eb3d03888fd238019d06319732987ce16b793bf78b7b5236cdca84371.wz", "mods": ["More_Fort", "Survival_Mod"], "survival": true, "autostart_players": 1}
  ],
  "wz_headless": true,
  "wz_nosound": true,
  "wz_debug_flags": ["NET", "LOBBY"],
  "wz_host_connection_provider": "tcp",
  "wz_portmapping": false,
  "wz_async_join_approve": true,
  "wz_enable_lobby_slashcmd": false,
  "wz_extra_args": [],
  "wz_backend": "system",
  "wz_binary": "/usr/games/warzone2100.real",
  "lobby_token_store": "/home/ubuntu/WZ2100-SmartAH_Scripts/instance_configs/.lobby_tokens.json",
  "lobby_blocklist": "/home/ubuntu/WZ2100-SmartAH_Scripts/instance_configs/.lobby_blocklist.json",
  "relay_port": 2200,
  "relay_target_file": "/home/ubuntu/WZ2100-SmartAH_Scripts/instance_configs/.relay_target",
  "migration_drain_seconds": 600,
  "migration_name_suffix": " 2",
  "max_live_sessions": 3,
  "min_uptime_to_respawn_seconds": 120,
  "max_restarts_per_hour": 3,
  "authorized_pkeys": [],
  "wz_mods_version": "4.7.0"
}
EOF
    chmod 600 "$config"
    warn "IMPORTANT: Edit $config and fill in host_key + authorized_pkeys before starting!"
}

# ── Step 4b: Heal drift in existing instances.json (never touch secrets) ──
# Fresh template is correct, but long-lived prod configs predate it
# (e.g. wz_backend=auto + empty wz_binary → bot exits instantly under
# wzbot, whose PATH lacks /usr/games). Fix known-bad keys only.
migrate_config() {
    local config="$REPO_DIR/instances.json"
    [ -f "$config" ] || return 0
    python3 - "$config" <<'EOF'
import json, os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
changed = []
if d.get("wz_backend") in ("", "auto", None):
    d["wz_backend"] = "system"
    changed.append("wz_backend=system")
binary = d.get("wz_binary") or ""
if not binary or not os.path.exists(binary):
    for cand in ("/usr/games/warzone2100.real", "/usr/games/warzone2100"):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            d["wz_binary"] = cand
            changed.append(f"wz_binary={cand}")
            break
repo = os.path.dirname(os.path.abspath(path))
defaults = {
    "lobby_token_store": os.path.join(repo, "instance_configs", ".lobby_tokens.json"),
    "lobby_blocklist": os.path.join(repo, "instance_configs", ".lobby_blocklist.json"),
    "relay_target_file": os.path.join(repo, "instance_configs", ".relay_target"),
}
for k, v in defaults.items():
    if not d.get(k):
        d[k] = v
        changed.append(f"{k}=default")
if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    print("migrated instances.json: " + ", ".join(changed))
else:
    print("instances.json already current.")
EOF
}

# ── Step 5: Directories & permissions ────────────────────────────────────
setup_dirs() {
    log "Setting up directories and permissions..."
    mkdir -p "$REPO_DIR/instance_configs" "$REPO_DIR/maps" "$REPO_DIR/mods" "$REPO_DIR/autohost"

    # wzbot needs write access to instance_configs (status, logs, .relay_target)
    chown -R "$GAME_USER:$GAME_USER" "$REPO_DIR/instance_configs"
    chmod 755 "$REPO_DIR" "$REPO_DIR/instance_configs"

    # wzbot needs write access to repo root for log files (WZ_PROD.log)
    chmod g+w "$REPO_DIR"
    chown "$PROXY_USER:$GAME_USER" "$REPO_DIR"

    # Everyone needs read access to code, maps, mods, autohost
    chmod -R a+rX "$REPO_DIR" 2>/dev/null || true

    log "Directories ready."
}

# ── Step 6: Verify Warzone binary ────────────────────────────────────────
check_wz() {
    if [ -x /usr/games/warzone2100.real ]; then
        log "Warzone binary found: /usr/games/warzone2100.real"
    elif command -v warzone2100.real &>/dev/null; then
        log "Warzone binary found in PATH"
    else
        err "Warzone binary NOT found at /usr/games/warzone2100.real"
        err "  Install it (e.g. sudo apt install warzone2100) and set wz_binary in instances.json"
        exit 1
    fi
}

# ── Step 7: Start MITM proxy ─────────────────────────────────────────────
start_proxy() {
    log "Starting MITM proxy on 127.0.0.1:$PROXY_PORT..."
    if ss -tln 2>/dev/null | grep -q "127.0.0.1:$PROXY_PORT"; then
        log "Proxy already running."
        return 0
    fi

    mkdir -p "$REPO_DIR/mitm-ca"
    # mkdir ran as root; proxy runs as $PROXY_USER and must write certs here.
    chown -R "$PROXY_USER:$PROXY_USER" "$REPO_DIR/mitm-ca" 2>/dev/null || chown -R "$PROXY_USER" "$REPO_DIR/mitm-ca"

    # Proxy runs as the invoking user (ubuntu), NOT as wzbot — UID separation is critical.
    sudo -u "$PROXY_USER" setsid nohup python3 "$REPO_DIR/lobby_mitm.py" \
        --port "$PROXY_PORT" \
        --ca "$REPO_DIR/mitm-ca" \
        --tokens "$REPO_DIR/instance_configs/.lobby_tokens.json" \
        --blocklist "$REPO_DIR/instance_configs/.lobby_blocklist.json" \
        --game-relay-port "$RELAY_PORT" \
        --relay-target-file "$REPO_DIR/instance_configs/.relay_target" \
        --relay-default-port "$GAME_PORT" \
        > "$REPO_DIR/mitm-proxy.log" 2>&1 < /dev/null &

    for i in $(seq 1 10); do
        sleep 1
        if ss -tln 2>/dev/null | grep -q "127.0.0.1:$PROXY_PORT"; then
            log "Proxy listening on 127.0.0.1:$PROXY_PORT"
            return 0
        fi
    done
    err "Proxy failed to start. Check $REPO_DIR/mitm-proxy.log"
    return 1
}

# ── Step 7b: Trust the MITM CA system-wide ───────────────────────────────
# WZ verifies lobby TLS against the system store (cURL error 60 otherwise).
# Regenerated CAs must be reinstalled — idempotent, updates only on change.
install_mitm_ca() {
    local src="$REPO_DIR/mitm-ca/ca.crt"
    local dst="/usr/local/share/ca-certificates/wz2100-mitm.crt"
    [ -f "$src" ] || { warn "MITM CA not found at $src; skipping trust install."; return 0; }
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        log "MITM CA already trusted."
        return 0
    fi
    if ! command -v update-ca-certificates &>/dev/null; then
        warn "update-ca-certificates missing; install CA manually: $src"
        return 0
    fi
    cp "$src" "$dst"
    update-ca-certificates 2>&1 | tail -2
    log "MITM CA installed into system trust store."
}

# ── Step 8: Redirect game user's lobby HTTPS through proxy ───────────────
add_redirect_rule() {
    log "Adding iptables redirect: $GAME_USER 443 -> 127.0.0.1:$PROXY_PORT..."
    if iptables -t nat -C OUTPUT -m owner --uid-owner "$GAME_USER" -p tcp --dport 443 \
        -j REDIRECT --to-port "$PROXY_PORT" 2>/dev/null; then
        log "Redirect rule already present."
    else
        iptables -t nat -A OUTPUT -m owner --uid-owner "$GAME_USER" -p tcp --dport 443 \
            -j REDIRECT --to-port "$PROXY_PORT"
        log "Redirect rule added."
    fi
    netfilter-persistent save 2>/dev/null || true
}

# ── Step 9: Start game instance ──────────────────────────────────────────
start_instance() {
    log "Starting game instance on port $GAME_PORT (map: $MAP_NAME)..."

    # Already running?
    if sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep -qx "$SESSION_NAME"; then
        log "Instance $SESSION_NAME already running."
        return 0
    fi

    # One-per-IP lobby invariant: refuse if any WZ_* session exists
    local live_count
    live_count=$(sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep -c "^WZ_" || true)
    if [ "$live_count" -ge 1 ]; then
        warn "Existing WZ_* session(s): $(sudo -u "$GAME_USER" tmux ls -F "#{session_name}" | grep "^WZ_" | tr '\n' ' ')"
        warn "Not spawning another (one lobby listing per public IP)."
        return 0
    fi

    # NOTE: $GAME_USER has shell /usr/sbin/nologin, and tmux runs the
    # session command via the user's login shell — so invoke bash explicitly.
    sudo -u "$GAME_USER" tmux new-session -d -s "$SESSION_NAME" /bin/bash -c \
        "cd $REPO_DIR && exec python3 instance_bot.py $GAME_PORT $SESSION_NAME $MAP_NAME"

    log "Waiting for lobby readiness (~60s)..."
    for i in $(seq 1 12); do
        sleep 5
        local status=""
        status=$(sudo cat "$REPO_DIR/instance_configs/$SESSION_NAME.status.json" 2>/dev/null || echo "")
        if echo "$status" | grep -q '"status": "ready"'; then
            log "Instance ready!"
            return 0
        fi
        if echo "$status" | grep -q '"status": "failed"'; then
            err "Instance failed. Check $REPO_DIR/instance_configs/$SESSION_NAME.status.json and $REPO_DIR/$SESSION_NAME.log"
            return 1
        fi
    done
    warn "Still starting. Check status with: $0 status"
}

# ── Main ─────────────────────────────────────────────────────────────────
cmd="${1:-deploy}"

case "$cmd" in
    deploy)
        require_root
        install_deps
        create_user
        open_ports
        generate_config
        migrate_config
        setup_dirs
        check_wz
        update_mods
        start_proxy
        install_mitm_ca
        add_redirect_rule
        start_instance
        log "Deployment complete. Verify with: $0 status"
        ;;
    start)
        require_root
        update_mods
        migrate_config
        start_proxy
        install_mitm_ca
        add_redirect_rule
        start_instance
        ;;
    stop)
        require_root
        if sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep -qx "$SESSION_NAME"; then
            log "Stopping instance $SESSION_NAME..."
            sudo -u "$GAME_USER" tmux send-keys -t "$SESSION_NAME" C-c
            for i in $(seq 1 6); do
                sleep 5
                sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep -qx "$SESSION_NAME" || break
            done
            sudo pkill -x warzone2100.real 2>/dev/null || true
            sudo -u "$GAME_USER" tmux kill-server 2>/dev/null || true
            log "Instance stopped. Proxy and firewall left running."
        else
            log "No instance running."
        fi
        ;;
    status)
        echo "--- Proxy (127.0.0.1:$PROXY_PORT) ---"
        ss -tln 2>/dev/null | grep -q "127.0.0.1:$PROXY_PORT" && echo "  UP" || echo "  DOWN"
        echo "--- Redirect rule ($GAME_USER 443 -> $PROXY_PORT) ---"
        iptables -t nat -C OUTPUT -m owner --uid-owner "$GAME_USER" -p tcp --dport 443 \
            -j REDIRECT --to-port "$PROXY_PORT" 2>/dev/null && echo "  ACTIVE" || echo "  MISSING"
        echo "--- Instance ($SESSION_NAME) ---"
        if sudo -u "$GAME_USER" tmux ls -F "#{session_name}" 2>/dev/null | grep -qx "$SESSION_NAME"; then
            sudo cat "$REPO_DIR/instance_configs/$SESSION_NAME.status.json" 2>/dev/null || echo '  {}'
        else
            echo "  DOWN"
        fi
        echo "--- Game/relay ports ---"
        ss -tln 2>/dev/null | grep -E ":(2100|2101|2102|2200) " || echo "  None listening"
        ;;
    *)
        echo "Usage: $0 {deploy|start|stop|status} [map_name]"
        echo "  deploy   - Zero-state deployment (idempotent, safe to re-run)"
        echo "  start    - Start (or restart) the game instance"
        echo "  stop     - Stop the game instance (proxy+firewall stay running)"
        echo "  status   - Check deployment status"
        exit 1
        ;;
esac