#!/usr/bin/env python3
"""
instance_bot.py — Warzone 2100 per-instance bot.

Spawns a single WZ2100 instance process, monitors its output in real-time,
and reacts to game events (e.g. greeting players on join, vote-kick).

All parameters are read from instances.json (flat config, no instance name key).
This script can be run standalone with no arguments:

    python3 instance_bot.py [port] [session_name] [map_name]
"""

import base64
import json
import lobby_mitm
import wz_config
import os
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
import re
import shutil
import socket
import zipfile

# ─── Constants ────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "instances.json")

VOTE_DURATION   = 60   # seconds players have to vote
VOTE_YES        = {"y", "yes"}
VOTE_NO         = {"n", "no"}
MIN_PLAYERS     = 3    # Minimum players required to hold a vote
START_TIMEOUT   = 4800 # seconds: 80 minutes to start a game before auto-restart
LOBBY_ID_TIMEOUT = 30  # seconds to wait for lobby readiness after launch
ROOM_STATUS_TIMEOUT = 5  # seconds to wait for a fresh __WZROOMSTATUS__ after requesting it
MAX_LOBBY_ID_RESTARTS = 3
MAPS_DIR         = os.path.join(SCRIPT_DIR, "maps")
MODS_DIR         = os.path.join(SCRIPT_DIR, "mods")
AUTOHOST_DIR     = os.path.join(SCRIPT_DIR, "autohost")
MAX_LIVE_SESSIONS = 3   # refuse to spawn when this many WZ_* tmux sessions live
# (steady state is 2 during a game: in-game parent + listed successor, so a
# migration needs a transient 3rd slot)
MIN_UPTIME_TO_RESPAWN = 120  # seconds: only auto-respawn a crashed instance this old
MAX_RESTARTS_PER_HOUR = 3    # give up respawning after this many restarts in 3600s
MIGRATION_RETRY_BASE = 600   # seconds: base delay before retrying a failed migration
MIGRATION_MAX_ATTEMPTS = 3   # give up migration retries after this many failures
MIGRATION_DRAIN_SECONDS = 600  # seconds to wait for players to rejoin before old shutdown
MIGRATION_NAME_SUFFIX = " 2"  # appended to the successor lobby name (lobby-safe charset)

# Live-fire result: the lobby rejects brackets/parens (INPUT_VALIDATION_FAILED)
# even though older listings contain them. Only alphanumerics + space proven.
LOBBY_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9 ]+$")


def is_lobby_safe_name(name: str) -> bool:
    """Lobby rejects names with special chars (INPUT_VALIDATION_FAILED)."""
    return bool(name) and len(name) <= 60 and bool(LOBBY_SAFE_NAME_RE.fullmatch(name))
LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate session log past this size
PUBLIC_IP_TTL = 300  # seconds: cache get_public_ip() this long

instance_start_time = None  # Set in main() to track session uptime


# ─── Globals ──────────────────────────────────────────────────────────────────

process  = None   # Active WZ2100 subprocess
config   = {}     # Loaded instance config dict
greetings = []    # List of greeting line strings
log_file_handle = None # Global file handle for appending logs
log_file_path = None   # Path of the current session log (for rotation)
port_global = 0
session_global = ""
quit_after_game = False
lobby_id = None
stdin_ready = False
room_state = None  # latest __WZROOMSTATUS__ data.state ("lobby"/"starting"/"started"/...)
startup_status_event = threading.Event()
startup_error = None
last_chat_log = None
last_chat_log_time = 0.0
current_map_name = ""
current_map_mods: list = []


# Roster: pk (str) -> {"type": str, "name": str, "pos": int|None, "is_spec": bool}
roster: dict[str, dict] = {}
roster_lock = threading.Lock()
roster_updated_event = threading.Event()

# Vote-kick state (None when no vote is active)
# {
#   "target_pos": int,       # slot number of target
#   "target_pk": str,        # public key of target
#   "target_name": str,
#   "initiator_pos": int,    # slot number of initiator
#   "initiator_pk": str,     # public key of initiator
#   "eligible": set[int],    # positions that must vote (active players except target)
#   "votes": dict[int, bool], # pos -> True(yes)/False(no)
#   "timer": threading.Timer,
# }
vote_state: dict | None = None
vote_lock = threading.Lock()

# 80-minute start-timeout state
game_started = False
start_timeout_timer = None
start_timeout_lock = threading.Lock()

# Lobby successor spawned when a game starts (stock WZ removes its listing on
# match start, so without this the lobby goes dark for the whole game).
# Holds the tmux session name; cleared once consumed by the post-game handover.
game_successor_session: str | None = None

# Single-flight migration lock shared by timeout and map-change migrations.
# (map_change_lock is kept as an alias so external behaviour is unchanged.)
# The lock guards only short critical sections (claim/release + attempt
# counters). The long spawn/wait/drain work runs WITHOUT the lock held;
# single-flight during that window is enforced by map_change_in_progress.
migration_lock = threading.RLock()
map_change_lock = migration_lock
map_change_in_progress = False

# Number of consecutive failed migration attempts (for backoff). Guarded by migration_lock.
migration_attempts = 0


def _try_claim_migration() -> bool:
    """Atomically claim the single migration slot. Returns False if busy."""
    with migration_lock:
        global map_change_in_progress
        if map_change_in_progress:
            return False
        map_change_in_progress = True
        return True


def _release_migration_claim():
    """Release the migration slot claimed by _try_claim_migration."""
    with migration_lock:
        global map_change_in_progress
        map_change_in_progress = False

STATUS_DIR = os.path.join(SCRIPT_DIR, "instance_configs")

def write_session_status(session_name: str, status: str, port: int, lobby_id: str = None, error: str = None):
    """Write instance lifecycle state to a status file."""
    if not session_name:
        return
    os.makedirs(STATUS_DIR, exist_ok=True)
    status_file = os.path.join(STATUS_DIR, f"{session_name}.status.json")
    data = {
        "session": session_name,
        "status": status,
        "port": port,
        "lobby_id": lobby_id,
        "error": error,
        "timestamp": time.time(),
    }
    tmp_file = status_file + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_file, status_file)
    except Exception as e:
        log("WARN", f"Failed to write session status: {e}")

def read_session_status(session_name: str) -> dict | None:
    """Read instance lifecycle state from its status file."""
    if not session_name:
        return None
    status_file = os.path.join(STATUS_DIR, f"{session_name}.status.json")
    if not os.path.exists(status_file):
        return None
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def clear_session_status(session_name: str):
    """Clear instance lifecycle state file."""
    if not session_name:
        return
    status_file = os.path.join(STATUS_DIR, f"{session_name}.status.json")
    try:
        if os.path.exists(status_file):
            os.remove(status_file)
    except Exception:
        pass

RATE_LIMIT_BACKOFF = 1200  # seconds to wait before retrying after lobby rate-limit


def is_rate_limited(error: str | None) -> bool:
    return bool(error) and "RATE_LIMITED" in error


def wait_for_new_instance_ready(session_name: str, timeout: float = 60.0) -> dict | None:
    """
    Wait until the new instance reports that it has verified its lobby ID.
    Returns the status dict when ready, or when it reports failed/exited
    (callers inspect status/error, e.g. for rate-limit backoff).
    Returns None only on timeout with no terminal status.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = read_session_status(session_name)
        if status:
            if status.get("status") == "ready" and status.get("lobby_id"):
                return status
            if status.get("status") in ("failed", "exited"):
                log("WARN", f"New instance {session_name} ended: {status.get('status')}: {status.get('error')}")
                return status
        time.sleep(0.5)
    log("WARN", f"Timed out waiting for {session_name} to verify lobby ID.")
    return None


# ─── Dynamic Config & Instances ───────────────────────────────────────────────

def request_roster_refresh(timeout: float = ROOM_STATUS_TIMEOUT) -> bool:
    """Ask WZ for room status and wait for the roster update. Never call on the reader thread."""
    roster_updated_event.clear()
    if not send_cmd("status", log_command=False):
        return False
    return roster_updated_event.wait(timeout)


def check_lobby_ready() -> bool:
    """Triple-gate: lobby ID + stdin interface + room in lobby state + local port listening."""
    if not lobby_id or not stdin_ready or room_state != "lobby":
        return False
    try:
        with socket.create_connection(("localhost", port_global), timeout=2):
            pass
        return True
    except Exception:
        return False


def maybe_signal_ready():
    """Signal startup completion once all readiness gates pass."""
    if lobby_id and stdin_ready and room_state == "lobby" and not startup_status_event.is_set():
        if check_lobby_ready():
            log("INFO", f"Lobby ready: id={lobby_id} port={port_global}")
            startup_status_event.set()


def find_available_port(start_port: int, exclude_port: int = None) -> int | None:
    """Return a free game port, or None when the whole range is busy."""
    port = start_port
    while port <= 2110:
        if exclude_port is not None and port == exclude_port:
            port += 1
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
        port += 1
    return None


def reap_dead_sessions() -> int:
    """Kill WZ_* tmux sessions whose bot process is gone. Returns reaped count."""
    reaped = 0
    try:
        result = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0
        sessions = [l.strip() for l in result.stdout.splitlines()
                    if l.strip().startswith("WZ_")]
    except Exception:
        return 0
    for sess in sessions:
        try:
            check = subprocess.run(
                ["pgrep", "-f", f"instance_bot.py.*{sess}"],
                capture_output=True, timeout=5,
            )
            if check.returncode != 0:
                subprocess.run(["tmux", "kill-session", "-t", sess],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10)
                clear_session_status(sess)
                log("SPAWN", f"Reaped dead session {sess}.")
                reaped += 1
        except Exception:
            pass
    return reaped


def count_live_sessions() -> int:
    """Count live WZ_* tmux sessions. Returns 0 when tmux is unavailable."""
    try:
        result = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0
        return sum(1 for line in result.stdout.splitlines() if line.strip().startswith("WZ_"))
    except Exception:
        return 0


def prune_stale_status_files(max_age_seconds: float = 86400):
    """Remove session status files older than max_age_seconds."""
    try:
        now = time.time()
        if not os.path.isdir(STATUS_DIR):
            return
        for fname in os.listdir(STATUS_DIR):
            if not fname.endswith(".status.json"):
                continue
            fpath = os.path.join(STATUS_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > max_age_seconds:
                    os.remove(fpath)
            except Exception:
                pass
    except Exception:
        pass


def get_available_maps() -> list[str]:
    """Configured map list (authoritative); falls back to maps/ dir scan."""
    configured = config.get("maps")
    if configured is not None:
        names = []
        for entry in configured:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
            elif isinstance(entry, str):
                names.append(entry)
        return names
    maps = []
    if os.path.exists(MAPS_DIR):
        for f in os.listdir(MAPS_DIR):
            if f.endswith(".wz"):
                maps.append(f[:-3])
    return sorted(maps)


RESTARTS_FILE = os.path.join(SCRIPT_DIR, "instance_configs", "restarts.json")

def _load_restart_times() -> list:
    try:
        with open(RESTARTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [float(t) for t in data]
    except Exception:
        pass
    return []


def record_restart_and_check_cap() -> bool:
    """Record a respawn; return False when the hourly cap is exceeded."""
    cap = int(config.get("max_restarts_per_hour", MAX_RESTARTS_PER_HOUR))
    now = time.time()
    try:
        os.makedirs(os.path.dirname(RESTARTS_FILE), exist_ok=True)
        times = [t for t in _load_restart_times() if now - t < 3600]
        if len(times) >= cap:
            return False
        times.append(now)
        tmp = RESTARTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(times, f)
        os.replace(tmp, RESTARTS_FILE)
        return True
    except Exception as e:
        log("WARN", f"Failed to record restart: {e}")
        return True




def spawn_new_instance(map_name: str, mods: list | None = None,
                        name_suffix: str = "") -> tuple[int, str] | None:
    """Launch a fresh bot instance in a new tmux session.

    Returns (port, session_name), or None when spawning is refused
    (live-session cap reached or no free port).
    """
    global port_global
    prune_stale_status_files()
    reap_dead_sessions()
    cap = int(config.get("max_live_sessions", MAX_LIVE_SESSIONS))
    live = count_live_sessions()
    if live >= cap:
        log("WARN", f"Refusing to spawn: {live} live session(s), cap is {cap}.")
        return None
    port = find_available_port(2100, exclude_port=port_global)
    if port is None:
        log("WARN", "Refusing to spawn: no free game port in range 2100-2110.")
        return None
    mods = list(mods or [])
    session_name = f"WZ_{port}_{int(time.time()) % 100000}"
    mod_suffix = f" +{len(mods)} mod(s)" if mods else ""
    log("INFO", f"Spawning new instance {session_name} on port {port} with map {map_name}{mod_suffix}")
    clear_session_status(session_name)
    cmd = [
        "tmux", "new-session", "-d", "-s", session_name,
        "python3", os.path.join(SCRIPT_DIR, "instance_bot.py"),
        str(port), session_name, map_name, ",".join(mods), name_suffix,
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return port, session_name


def is_own_host_entry(entry: dict) -> bool:
    """Our own spectator-host pseudo-slot (local; unknown IP)."""
    host_name = config.get("host_name", "")
    return bool(host_name and entry.get("name") == host_name
                and entry.get("is_spec") and entry.get("ip") in (None, "-", ""))


RELAY_PORT_DEFAULT = 2200  # must match the proxy --game-relay-port
# (players arrive at WZ from 127.0.0.1: loopback joins skip lobby token
# checks, flood throttle and IP bans — verified in upstream
# allowNonLobbyMediatedJoin/quickRejectConnection)


def relay_port() -> int:
    try:
        return int(config.get("relay_port", RELAY_PORT_DEFAULT))
    except Exception:
        return RELAY_PORT_DEFAULT


def relay_target_file() -> str:
    """File the proxy relay watches for the current gameport."""
    return config.get("relay_target_file") or os.path.join(STATUS_DIR, ".relay_target")


def publish_relay_target(port: int) -> bool:
    """Point the proxy game relay at this instance's gameport."""
    try:
        path = relay_target_file()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(port)))
        os.replace(tmp, path)
        return True
    except Exception as e:
        log("RELAY", f"Cannot publish relay target: {e}")
        return False


def redirect_all_players(new_port: int) -> int:
    """Send `redirect identity` for every real player/spec (skips own host slot).

    NOTE: stock clients direct-connect without lobby tokens, which a
    lobby-connected host rejects (ERROR_WRONGDATA). Only use this after the
    target's lobby handler has been driven offline (proxy blocklist +
    abandon), or expect failures.
    """
    with roster_lock:
        roster_snapshot = list(roster.items())

    game_pw = config.get("game_password", "")
    count = 0
    for pk, entry in roster_snapshot:
        if not pk or pk == "-":
            continue
        if is_own_host_entry(entry):
            continue
        is_spec = 1 if entry.get("is_spec", False) else 0
        if game_pw:
            conn_str = f"tcp:{new_port}:{is_spec}:{game_pw}"
        else:
            conn_str = f"tcp:{new_port}:{is_spec}"

        player_name = entry.get("name", "Unknown")
        log("REDIRECT", f"Redirecting {player_name} ({pk}) to {conn_str}")
        send_cmd(f"redirect identity {pk} {conn_str}")
        count += 1
    log("REDIRECT", f"Redirect command issued for {count} connection(s).")
    return count


# NOTE: there is deliberately NO Python keepalive thread. Keeping a listing
# alive is WZ's own job (it PATCHes every <=30s while its handler lives).
# Once a handler is gone (migrated-into instances), the server flags the
# entry (HOST_NO_KEEPALIVE) and refuses PATCHes (406) — proven live — so a
# Python keepalive would only burn API calls against a throttled lobby.
# Fresh listings (new age) come from migrations, not PATCHes.


def lobby_has_drained() -> bool:
    """True when nobody but our own host slot remains in the lobby."""
    with roster_lock:
        for pk, entry in roster.items():
            if not pk or pk == "-":
                continue
            if is_own_host_entry(entry):
                continue
            return False
    return True


def wait_for_drain(timeout: float) -> bool:
    """Wait until the lobby drains (players rejoin the new lobby manually).

    NOTE: stock `redirect identity` cannot hand public-IP players over:
    their clients direct-connect without lobby join tokens, which the new
    host rejects (ERROR_WRONGDATA) under the default Lobby Join Bypass=Local
    (see upstream `allowNonLobbyMediatedJoin`). So migration announces the
    new lobby and waits for players to rejoin it through the lobby instead.
    Must NOT run on the output-reader thread (it blocks waiting for room
    status updates that only the reader thread can process).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not request_roster_refresh():
            log("WARN", "No fresh roster while waiting for drain; retrying.")
        elif lobby_has_drained():
            return True
        time.sleep(15)
    return lobby_has_drained()


def _lobby_address_for_api() -> str:
    """Lobby base URL this instance agrees with WZ on (best effort)."""
    try:
        addr = get_wz_lobbyserver()
        if addr:
            return addr
    except Exception:
        pass
    return config.get("lobby_address", "https://wzlobby.wz2100.net/lobby")


def family_listing_ids(exclude_id: str | None = None) -> list:
    """Public lobby IDs in our game-name family, optionally excluding one.

    The family is the base game name plus the migration-suffixed variant
    ("FreeHost" and "FreeHost ..."). Never raises: fetch failures yield []
    (callers treat unknown as unlisted and today's behavior continues).
    """
    base = config.get("game_name", "")
    if not base:
        return []
    try:
        games = lobby_mitm.list_lobby_games(_lobby_address_for_api())
    except Exception as e:
        log("WARN", f"Public list check failed ({e}); proceeding as if unlisted.")
        return []
    ids = []
    for g in games:
        name = g.get("name", "")
        if (name == base or name.startswith(base + " ")) and g.get("id") != exclude_id:
            ids.append(g["id"])
    return ids


def enforce_single_listing(keep_id: str | None):
    """DELETE any publicly listed family game except keep_id. Best effort.

    The lobby sometimes accepts a second POST from our IP; this is the
    backstop that keeps the invariant (exactly one listing) true after every
    migration. Never raises.
    """
    try:
        store = config.get("lobby_token_store") or lobby_mitm.TOKEN_STORE_DEFAULT
        address = _lobby_address_for_api()
        for gid in family_listing_ids(exclude_id=keep_id):
            try:
                ok, detail = lobby_mitm.delete_listing_by_game_id(gid, store, address)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            log("UNLIST", f"Enforce single listing: DELETE {gid}: {detail}")
    except Exception as e:
        log("WARN", f"Single-listing enforcement failed: {e}")


def unlist_self_via_api(started: bool = False) -> bool:
    """DELETE our own lobby listing using the MITM-captured hostToken.

    The WZ process keeps running with its players; its next keepalive sees a
    gone game (local lobbyGameGone, no kicks). Frees the one-per-IP slot so a
    successor can POST. Returns False when no token was captured (proxy not
    running for the WZ user) or the DELETE failed.
    """
    if not lobby_id:
        log("UNLIST", "No lobby ID known; nothing to unlist.")
        return False
    lobby_address = get_wz_lobbyserver() or config.get("lobby_address", "https://wzlobby.wz2100.net/lobby")
    store_path = config.get("lobby_token_store") or lobby_mitm.TOKEN_STORE_DEFAULT
    ok, detail = lobby_mitm.delete_listing_by_game_id(
        lobby_id, store_path, lobby_address, started)
    log("UNLIST", f"DELETE {lobby_id}: {detail}")
    if not ok and "no captured hostToken" in detail:
        log("UNLIST", "Hint: run lobby_mitm.py and steer this UID's 443 via lobby_off.sh.")
    return ok


def get_wz_lobbyserver() -> str | None:
    """Read the lobbyserver= value from this instance's WZ config file.

    This is the same address WZ itself uses, so Python API calls always agree
    with the instance (including custom/alternative lobbies).
    """
    try:
        config_path = os.path.join(SCRIPT_DIR, "instance_configs", session_global, "config")
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("lobbyserver="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        return value
    except Exception:
        pass
    return None

# ─── System Info Helpers ──────────────────────────────────────────────────────

def get_cpu_load() -> str:
    """Return 1/5/15-min CPU load averages."""
    try:
        load1, load5, load15 = os.getloadavg()
        return f"{load1:.2f} / {load5:.2f} / {load15:.2f}  (1/5/15 min)"
    except OSError:
        return "N/A"


def get_ram_usage() -> str:
    """Return RAM used / total from /proc/meminfo."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])  # kB
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        used_kb = total_kb - avail_kb
        total_mb = total_kb / 1024
        used_mb = used_kb / 1024
        pct = (used_kb / total_kb * 100) if total_kb else 0
        return f"{used_mb:.0f} MB / {total_mb:.0f} MB ({pct:.1f}%)"
    except Exception:
        return "N/A"


_public_ip_cache = {"value": None, "at": 0.0}
_public_ip_lock = threading.Lock()

def get_public_ip() -> str:
    """Return the machine's public IP address (cached).

    Tries several plain-text providers in order; the first non-empty
    response wins. Result is cached for PUBLIC_IP_TTL seconds.
    """
    with _public_ip_lock:
        if _public_ip_cache["value"] and time.time() - _public_ip_cache["at"] < PUBLIC_IP_TTL:
            return _public_ip_cache["value"]
    providers = (
        ["curl", "-s", "--max-time", "5", "ifconfig.me"],
        ["curl", "-s", "--max-time", "5", "https://api.ipify.org"],
        ["curl", "-s", "--max-time", "5", "https://icanhazip.com"],
    )
    for cmd in providers:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            ip = result.stdout.strip().split()[0] if result.stdout.strip() else ""
            if ip and re.fullmatch(r"[0-9a-fA-F.:]+", ip):
                with _public_ip_lock:
                    _public_ip_cache["value"] = ip
                    _public_ip_cache["at"] = time.time()
                return ip
        except Exception:
            continue
    return "N/A"


def get_session_remaining() -> str:
    """Return remaining time before the START_TIMEOUT auto-restart."""
    if instance_start_time is None:
        return "N/A"
    with start_timeout_lock:
        if game_started:
            return "Game in progress"
    elapsed = time.time() - instance_start_time
    remaining = max(0, START_TIMEOUT - elapsed)
    mins, secs = divmod(int(remaining), 60)
    return f"{mins}m {secs}s"


# ─── Map Admin Persistence ────────────────────────────────────────────────────

try:
    import fcntl
except ImportError:  # Windows / minimal builds
    fcntl = None

_config_write_lock = threading.Lock()
CONFIG_LOCK_FILE = CONFIG_FILE + ".lock"

def save_authorized_pkeys():
    """Persist the current authorized_pkeys list back to instances.json.

    Serialized across processes with a dedicated lockfile held across the
    whole read-modify-write, plus the in-process thread lock.
    """
    with _config_write_lock:
        lock_fd = None
        try:
            lock_fd = os.open(CONFIG_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            data["authorized_pkeys"] = config.get("authorized_pkeys", [])
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
            log("ADMIN", "authorized_pkeys saved to instances.json")
        except Exception as e:
            log("WARN", f"Failed to save authorized_pkeys: {e}")
        finally:
            if lock_fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                except Exception:
                    pass


# ─── Shared Utilities ─────────────────────────────────────────────────────────

def log(tag: str, msg: str):
    global log_file_handle
    ts = datetime.now().strftime("%H:%M:%S")
    output_line = f"[{ts}] [{tag}] {msg}"
    print(output_line, flush=True)

    if log_file_handle:
        try:
            if log_file_path and os.path.exists(log_file_path):
                if os.path.getsize(log_file_path) >= LOG_MAX_BYTES:
                    try:
                        log_file_handle.close()
                    except Exception:
                        pass
                    try:
                        if os.path.exists(log_file_path + ".1"):
                            os.remove(log_file_path + ".1")
                        os.replace(log_file_path, log_file_path + ".1")
                    except Exception:
                        pass
                    log_file_handle = open(log_file_path, "a", encoding="utf-8")
            log_file_handle.write(output_line + "\n")
            log_file_handle.flush()
        except Exception:
            pass


def load_config() -> dict:
    """Load and return the flat config from instances.json."""
    if not os.path.exists(CONFIG_FILE):
        print(f"[ERROR] {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)

# ─── Roster Management ────────────────────────────────────────────────────────

def update_roster_from_status(status_json: dict):
    """
    Rebuild the roster from a parsed __WZROOMSTATUS__ JSON object.
    Roster entries: {type, name, pos, idx, is_spec}

    Vocabulary (see upstream src/stdinreader.cpp):
      idx = NetPlay slot index (players[] array order; also the WZCHATLOB /
            playerLeft sender index and the "slot" in player-join lines).
      pos = lobby position (the slot numbers players see; may differ from idx).
    Votes and /votekick slots are positions; chat senders arrive as indexes
    and must be translated via idx -> pos.
    """
    global roster
    new_roster: dict[str, dict] = {}

    # Process active players (array order == NetPlay index)
    for idx, p in enumerate(status_json.get("players", [])):
        if p.get("type") != "player":
            continue
        pk = p.get("pk")
        if not pk:
            continue
        pos = p.get("pos")
        new_roster[pk] = {
            "type": "player",
            "name": p.get("name", "Unknown"),
            "pos": pos,
            "idx": idx,
            "is_spec": False,
            "ip": p.get("ip"),
        }

    # Process spectators (array order is NOT the NetPlay index: closed and
    # empty slots are skipped upstream, so no idx is recorded for specs)
    for s in status_json.get("specs", []):
        if s.get("type") != "spec":
            continue
        pk = s.get("pk")
        if not pk:
            continue
        new_roster[pk] = {
            "type": "spec",
            "name": s.get("name", "Unknown"),
            "pos": None,
            "idx": None,
            "is_spec": True,
            "ip": s.get("ip"),
        }

    with roster_lock:
        roster = new_roster

    roster_updated_event.set()

    active_count = sum(1 for e in roster.values() if not e["is_spec"])
    spec_count = sum(1 for e in roster.values() if e["is_spec"])
    log("ROSTER", f"Updated: {len(roster)} entries ({active_count} players, {spec_count} specs)")


def get_pos_by_idx(idx: int) -> int | None:
    """Translate a NetPlay slot index (chat/playerLeft sender) to a lobby position."""
    with roster_lock:
        for entry in roster.values():
            if entry["type"] == "player" and entry.get("idx") == idx:
                return entry.get("pos")
    return None


def get_pos_by_sender(sender_pk: str | None, sender_idx: int | None) -> int | None:
    """Resolve a chat sender to a lobby position. Prefers pk, falls back to idx."""
    with roster_lock:
        if sender_pk and sender_pk in roster:
            entry = roster[sender_pk]
            if entry["type"] == "player":
                return entry.get("pos")
            return None  # spectators have no position
        if sender_idx is not None:
            for entry in roster.values():
                if entry["type"] == "player" and entry.get("idx") == sender_idx:
                    return entry.get("pos")
    return None


def get_pk_by_pos(pos: int) -> str | None:
    """Look up a player's pk by their position (slot). Returns pk or None."""
    with roster_lock:
        for pk, entry in roster.items():
            if entry["type"] == "player" and entry.get("pos") == pos:
                return pk
    return None


def get_entry_by_pos(pos: int) -> tuple[str, dict] | None:
    """Look up a player by position. Returns (pk, entry) or None."""
    with roster_lock:
        for pk, entry in roster.items():
            if entry["type"] == "player" and entry.get("pos") == pos:
                return pk, entry
    return None


def get_entry_by_pk(pk: str) -> dict | None:
    """Look up any roster entry by pk."""
    with roster_lock:
        return roster.get(pk)

# ─── Game Process Communication ───────────────────────────────────────────────

WZ_COMMAND_PATTERNS = (
    re.compile(r"exit"),
    re.compile(r"admin (?:add-hash|add-public-key|remove) \S+"),
    re.compile(r"kick identity \S+(?: .+)?"),
    re.compile(r"redirect identity \S+ \S+(?: .+)?"),
    re.compile(r"permissions set connect:(?:allow|block) \S+"),
    re.compile(r"permissions unset connect \S+"),
    re.compile(r"set chat (?:allow|quickchat|mute) \S+"),
    re.compile(r"ban ip \S+(?: .+)?"),
    re.compile(r"unban ip \S+"),
    re.compile(r"chat bcast .+"),
    re.compile(r"chat direct \S+ .+"),
    re.compile(r"join (?:approve|reject|approvespec) \S+(?: \d+)?(?: .+)?"),
    re.compile(r"status"),
    re.compile(r"set host ready [01]"),
    re.compile(r"shutdown now"),
)


def is_valid_wz_command(cmd: str) -> bool:
    """Return whether cmd matches a command supported by Warzone's stdin CLI."""
    if not cmd or cmd != cmd.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cmd):
        return False
    return any(pattern.fullmatch(cmd) for pattern in WZ_COMMAND_PATTERNS)


def send_cmd(cmd: str, log_command: bool = True) -> bool:
    """Write one validated Warzone stdin command line to the game's stdin."""
    command = cmd.rstrip("\r\n")
    if not is_valid_wz_command(command):
        log("WARN", f"Rejected unsupported Warzone CLI command: {cmd!r}")
        return False

    if process and process.stdin:
        try:
            # Encode to bytes for binary stdin pipe
            process.stdin.write((command + "\n").encode("utf-8", errors="replace"))
            process.stdin.flush()
            if log_command:
                log("CMD->", command)
            return True
        except BrokenPipeError:
            log("WARN", "stdin pipe broken; process may have exited.")
    return False


def bcast(msg: str):
    """Broadcast a system message to the whole lobby."""
    send_cmd(f"chat bcast {msg}")


def dm(pk: str, msg: str):
    """Send a private system message to one player by identity.

    Upstream accepts either the b64 public key or the public hash here
    (see checkPlayerIdentityMatchesString in src/stdinreader.cpp); the
    roster pk is used because it is stable once known.
    """
    send_cmd(f"chat direct {pk} {msg}")

# ─── Start-Timeout Auto-Restart ───────────────────────────────────────────────

def arm_start_timeout(delay: float = START_TIMEOUT):
    """(Re)arm the lobby start-timeout timer. Caller should hold start_timeout_lock."""
    global start_timeout_timer
    if start_timeout_timer is not None:
        try:
            start_timeout_timer.cancel()
        except Exception:
            pass
        start_timeout_timer = None
    start_timeout_timer = threading.Timer(delay, _start_timeout_expired)
    start_timeout_timer.daemon = True
    start_timeout_timer.start()


def _on_game_started():
    """A match just started: cancel the start timeout and keep a lobby listed.

    Stock WZ removes its listing on match start, so spawn a successor lobby
    (base name, same map+mods) in a worker thread. No redirect/shutdown here:
    the players are in-game. Post-game, check_game_ended hands them over.
    """
    global game_started, start_timeout_timer
    log("EVENT", "Game started.")
    with start_timeout_lock:
        game_started = True
        if start_timeout_timer is not None:
            start_timeout_timer.cancel()
            start_timeout_timer = None
    threading.Thread(target=_spawn_game_successor, daemon=True,
                     name="game-successor").start()


def _spawn_game_successor():
    """Worker: bring up the lobby successor that covers a running game."""
    global game_successor_session
    if not _try_claim_migration():
        log("GAME", "A migration is already in progress; skipping game-start successor.")
        return
    try:
        if game_successor_session is not None:
            log("GAME", f"Successor {game_successor_session} already spawned; one child per game.")
            return
        with start_timeout_lock:
            if not game_started or process is None or process.poll() is not None:
                return
        try:
            others = family_listing_ids(exclude_id=lobby_id)
        except Exception:
            others = []
        if others:
            log("GAME", f"Lobby already covered by {others}; skipping game-start successor.")
            return
        spawned = spawn_new_instance(current_map_name or config.get("map_name", ""),
                                     list(current_map_mods),
                                     name_suffix="")
        if spawned is None:
            log("WARN", "Game-start successor refused (cap reached or no free port). "
                         "Lobby stays dark until post-game.")
            return
        new_port, new_session = spawned
        status = wait_for_new_instance_ready(new_session, timeout=60.0)
        if status and status.get("lobby_id"):
            game_successor_session = new_session
            log("GAME", f"Lobby successor {new_session} listed during game "
                        f"(lobby {status['lobby_id']}).")
        else:
            err = status.get("error") if status else "no response from new instance"
            log("WARN", f"Game-start successor failed: {err}. Lobby dark until post-game.")
    except Exception as e:
        log("WARN", f"Error spawning game-start successor: {e}")
    finally:
        _release_migration_claim()


def check_game_ended(prev_state):
    """Stock WZ returns to the lobby in-process after a game.

    When a live game-start successor holds the lobby, hand the players over
    to it (seamless redirect; their one redirect-follow is still unused in
    this flow) and shut down. Otherwise stay on and re-arm the start timeout.
    """
    global game_started, game_successor_session
    if not (game_started and prev_state in ("starting", "started")):
        return
    succ = game_successor_session
    live = read_live_successor(succ) if succ else None
    if live is not None and _try_claim_migration():
        game_successor_session = None
        with start_timeout_lock:
            game_started = False
        log("EVENT", f"Game ended; handing players over to successor {succ}.")
        threading.Thread(target=_post_game_handover, args=(succ, live),
                         daemon=True, name="post-game-handover").start()
        return
    if succ is not None:
        log("WARN", "Game successor is gone; staying on as lobby (re-arming start timeout).")
        game_successor_session = None
    else:
        log("EVENT", "Game ended, back in lobby. Re-arming start timeout.")
    with start_timeout_lock:
        game_started = False
        if process is not None and process.poll() is None:
            arm_start_timeout()


def read_live_successor(session_name: str | None) -> tuple[int, str] | None:
    """Return (port, lobby_id) if the successor lobby is still live, else None.

    Requires both a ready status file and a live tmux session, so a successor
    that died mid-game is never handed players.
    """
    if not session_name:
        return None
    try:
        status = read_session_status(session_name)
    except Exception:
        return None
    if not status or status.get("status") != "ready" or not status.get("lobby_id"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(status["port"]), status["lobby_id"]
    except Exception:
        return None


def _post_game_handover(successor_session: str, live: tuple[int, str]):
    """Move post-game players to the live successor, then shut down.

    Runs on a worker thread; the migration claim is held by the caller.
    On failure the claim is released and the start timeout re-armed so the
    lobby is not left dark.
    """
    global quit_after_game, migration_attempts
    succeeded = False
    try:
        new_port, new_lobby_id = live
        game_name = config.get("game_name", "")
        if not migrate_to_successor(new_port, successor_session, new_lobby_id,
                                    game_name, "post-game"):
            return
        quit_after_game = True
        with migration_lock:
            migration_attempts = 0
        succeeded = True
        send_cmd("shutdown now")
    except Exception as e:
        log("WARN", f"Error during post-game handover: {e}")
    finally:
        if not succeeded:
            _release_migration_claim()
            with start_timeout_lock:
                if process is not None and process.poll() is None:
                    arm_start_timeout()


def maybe_spawn_crash_replacement(reason: str = "crashed") -> None:
    """Replacement spawn that never double-lists (called from main).

    `reason` is "crashed" for unexpected deaths or "game-ended" when stock
    quits cleanly after a game (exit 0 while a game was running). Either way
    a replacement is only spawned when no live lobby covers us; the label
    just keeps the logs honest. Frees our own slot first, then spawns only
    when no other family listing is public.
    """
    uptime = time.time() - (instance_start_time or time.time())
    min_uptime = float(config.get("min_uptime_to_respawn_seconds", MIN_UPTIME_TO_RESPAWN))
    if uptime < min_uptime:
        log("WARN", f"Process died after {uptime:.0f}s (< {min_uptime:.0f}s). Not respawning; likely a systemic failure.")
        return
    if not record_restart_and_check_cap():
        log("ERROR", "Hourly restart cap reached. Not respawning; operator intervention required.")
        return
    try:
        if lobby_id:
            unlist_self_via_api()
    except Exception:
        pass
    try:
        others = family_listing_ids(exclude_id=lobby_id)
    except Exception as e:
        log("WARN", f"Public list check failed ({e}); spawning replacement anyway.")
        others = []
    if others:
        log("WARN", f"Lobby already covered by {others}; not spawning replacement.")
        return
    if reason == "game-ended":
        log("INFO", "Game ended (process exited). Spawning replacement lobby.")
    else:
        log("INFO", "Process crashed unexpectedly. Spawning new instance.")
    spawn_new_instance(current_map_name or config.get("map_name", ""),
                       list(current_map_mods))


def successor_covering(session_name: str | None) -> bool:
    """True when the tracked successor is starting or live.

    Covers the race where the parent dies while the successor is still
    verifying (no listing yet): a starting successor with a live tmux
    session will be listed within a minute, so no replacement is needed.
    Dead tmux or failed/exited status means no coverage.
    """
    if not session_name:
        return False
    try:
        status = read_session_status(session_name)
    except Exception:
        return False
    if not status or status.get("status") in ("failed", "exited"):
        return False
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0


def crash_respawn_allowed() -> bool:
    """False when a live game-start successor already holds the lobby.

    Respawning the crashed instance then would fight it for the one-per-IP
    lobby slot, so the successor simply takes over.
    """
    if successor_covering(game_successor_session):
        log("INFO", "Successor already covering the lobby; not respawning.")
        return False
    return True


def announce_successor(game_name: str, new_port: int):
    """Tell the lobby how to reach the successor (manual rejoin)."""
    game_pw = config.get("game_password", "")
    bcast(f"New lobby is ready: {game_name} (port {new_port}). Please rejoin it from the lobby list.")
    if game_pw:
        bcast(f"The password is still: {game_pw}")
    bcast("This lobby will close once everyone has moved over.")


def migrate_to_successor(new_port: int, new_session: str, new_lobby_id: str,
                          game_name: str, reason: str) -> bool:
    """Move players to a ready successor, then shut this instance down.

    Primary path (seamless): open the localhost join relay onto the
    successor's gameport and `redirect identity` everyone at it. Arrivals
    come from 127.0.0.1, which stock Warzone always passes to approval
    (loopback exemption) without consulting the lobby — the successor stays
    fully listed the whole time, so there is no darkness and no abandon.
    Fallback path: announce the successor and wait for manual rejoin.
    Returns True when this instance should shut down afterwards.
    """
    log("MIGRATE", f"Fresh instance {new_session} ready with lobby ID {new_lobby_id}.")
    bcast(f"New instance spun up successfully: {game_name}.")
    if not publish_relay_target(new_port):
        log("WARN", "Could not publish relay target; using manual-rejoin fallback.")
        return announce_and_drain(game_name, new_port)

    public_ip = get_public_ip()
    relay = relay_port()
    bcast(f"Handover channel open. Moving you to {game_name} now - hold on, "
          "do not disconnect...")
    bcast("Note: automatic handover works only once per game session. If you "
          f"have already been moved once, please rejoin {game_name} from the "
          "lobby list manually instead. "
          f"(Direct: {public_ip}:{relay}.)")
    if not request_roster_refresh():
        log("WARN", "No fresh roster before redirect; using last known roster.")
    moved = redirect_all_players(relay)
    log("MIGRATE", f"Redirect commands issued for {moved} connection(s) via relay {relay}.")
    time.sleep(30)  # let clients complete the auto-follow handshake
    if lobby_has_drained():
        log("MIGRATE", "Lobby drained via redirect. Shutting down old instance.")
        enforce_single_listing(new_lobby_id)
        return True
    log("MIGRATE", "Some players did not follow the redirect; falling back to manual rejoin.")
    if announce_and_drain(game_name, new_port):
        enforce_single_listing(new_lobby_id)
        return True
    return False


def announce_and_drain(game_name: str, new_port: int) -> bool:
    """Fallback path: announce the successor, wait for manual rejoin."""
    announce_successor(game_name, new_port)
    bcast("Note: if automatic handover already moved you once this session, "
          "manual rejoin is expected - the game only allows one automatic "
          "move per session.")
    drain = float(config.get("migration_drain_seconds", MIGRATION_DRAIN_SECONDS))
    if wait_for_drain(drain):
        log("MIGRATE", "Lobby drained. Shutting down old instance.")
    else:
        log("MIGRATE", "Drain deadline passed with players still here. Shutting down anyway.")
        bcast("Closing this lobby now. Please rejoin the new lobby from the list.")
    return True


def _migration_backoff_delay() -> float:
    with migration_lock:
        attempts = migration_attempts
    return min(MIGRATION_RETRY_BASE * (2 ** max(0, attempts - 1)), 3600)


def _start_timeout_expired():
    global game_started, start_timeout_timer, quit_after_game, migration_attempts
    with start_timeout_lock:
        if game_started:
            return
        if process is None or process.poll() is not None:
            return
    if not _try_claim_migration():
        log("TIMEOUT", "Another migration is in progress. Skipping timeout migration.")
        return
    succeeded = False
    try:
        with migration_lock:
            if migration_attempts >= MIGRATION_MAX_ATTEMPTS:
                log("TIMEOUT", "Migration attempts exhausted. Keeping current lobby open; will not retry.")
                return
        successor_name = config.get("game_name", "") + config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX)
        if not is_lobby_safe_name(successor_name):
            log("ERROR", f"Successor lobby name {successor_name!r} has unsafe characters; aborting migration.")
            bcast("Migration aborted: lobby name not acceptable.")
            return
        mins = START_TIMEOUT // 60
        log("TIMEOUT", f"{mins}-minute start window expired. Spawning new instance to migrate players...")
        bcast("Lobby timeout expired. Spinning up new instance, this usually takes about 60 seconds...")
        if not unlist_self_via_api():
            log("WARN", "Aborting migration: lobby slot still held and no token to free it.")
            bcast("Migration aborted: could not free the lobby slot. Admins, check the lobby proxy.")
            with migration_lock:
                migration_attempts += 1
            return
        spawned = spawn_new_instance(current_map_name or config.get("map_name", ""),
                                     list(current_map_mods),
                                     name_suffix=config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX))
        if spawned is None:
            log("WARN", "Could not spawn a new instance (cap reached or no free port). Keeping current lobby open.")
            with migration_lock:
                migration_attempts += 1
            return
        new_port, new_session = spawned
        # Long waits below run WITHOUT migration_lock held; the claim flag
        # keeps other migrations out until this one finishes.
        status = wait_for_new_instance_ready(new_session, timeout=60.0)
        with start_timeout_lock:
            if game_started or process is None or process.poll() is not None:
                log("TIMEOUT", "Game started or process ended while waiting for fresh instance. Cancelling timeout migration.")
                return
        if status and status.get("lobby_id"):
            new_lobby_id = status["lobby_id"]
            game_name = config.get("game_name", "") + config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX)
            if not migrate_to_successor(new_port, new_session, new_lobby_id, game_name, "timeout"):
                return
            with migration_lock:
                migration_attempts = 0
            quit_after_game = True
            succeeded = True
            if process:
                try:
                    send_cmd("shutdown now")
                except Exception:
                    process.kill()
        else:
            err = status.get("error") if status else "no response from new instance"
            log("WARN", f"Fresh instance {new_session} failed lobby verification ({err}). Aborting timeout redirect.")
            bcast("Warning: fresh lobby failed to start. Keeping current lobby open.")
            with migration_lock:
                migration_attempts += 1
                attempts = migration_attempts
            if attempts >= MIGRATION_MAX_ATTEMPTS:
                log("TIMEOUT", "Migration attempts exhausted. Keeping current lobby open; will not retry.")
                return
            delay = _migration_backoff_delay()
            if is_rate_limited(err):
                # The lobby throttles aggressively: extra POSTs only extend the ban.
                delay = max(delay, RATE_LIMIT_BACKOFF)
                log("TIMEOUT", "Lobby is rate-limiting us; backing off to avoid extending the ban.")
            with start_timeout_lock:
                if not game_started and process and process.poll() is None:
                    log("TIMEOUT", f"Retrying migration in {int(delay)}s (attempt {attempts + 1}).")
                    arm_start_timeout(delay)
    finally:
        # On success the old instance shuts down; keep the claim held so no
        # second migration can start before shutdown completes.
        if not succeeded:
            _release_migration_claim()

def _vote_timed_out():
    """Called when the 60-second voting window expires."""
    with vote_lock:
        global vote_state
        if vote_state is None:
            return
        _resolve_vote(timed_out=True)


def _resolve_vote(timed_out: bool = False):
    """
    Tally votes, announce the result, and kick if unanimous yes.
    Must be called while holding vote_lock.
    """
    global vote_state
    if vote_state is None:
        return

    vs          = vote_state
    target_name = vs["target_name"]
    target_pk   = vs["target_pk"]
    target_pos  = vs["target_pos"]
    eligible    = vs["eligible"]
    votes       = vs["votes"]

    # Cancel the timer if we're resolving early
    try:
        vs["timer"].cancel()
    except Exception:
        pass

    # Non-voters count as NO
    yes_votes = sum(1 for s in eligible if votes.get(s) is True)
    no_votes  = len(eligible) - yes_votes
    total     = len(eligible)

    should_kick = (yes_votes == total and total > 0)

    # Clear state first so we release lock before I/O commands
    vote_state = None

    # Perform announcements and kick command outside lock
    result_line = f"Vote result for kicking {target_name} (slot {target_pos}): {yes_votes} YES / {no_votes} NO (need {total}/{total})"
    log("VOTE", result_line)
    bcast(result_line)

    if should_kick:
        bcast(f"Vote PASSED. Kicking {target_name}.")
        send_cmd(f"kick identity {target_pk} You were voted out by ALL players.")
    else:
        if timed_out:
            bcast(f"Vote FAILED (time expired). {target_name} stays.")
        else:
            bcast(f"Vote FAILED. {target_name} stays.")


def _check_all_voted():
    """
    If every eligible player has voted, resolve immediately.
    Must be called while holding vote_lock.
    """
    if vote_state is None:
        return
    if all(s in vote_state["votes"] for s in vote_state["eligible"]):
        _resolve_vote(timed_out=False)


def start_vote(initiator_idx: int, initiator_pk: str | None, target_pos: int):
    """
    Begin a vote-kick session. initiator_idx/pk identify the chat sender
    (NetPlay index space); target_pos is the user-facing lobby position.
    The initiator index is translated to a position internally; all vote
    bookkeeping stays in position space.
    """
    global vote_state

    initiator_pos = get_pos_by_sender(initiator_pk, initiator_idx)
    if initiator_pos is None:
        if initiator_pk:
            dm(initiator_pk, "Could not find you in the lobby roster. Try again in a moment.")
        else:
            log("VOTE", f"Initiator idx {initiator_idx} not found in roster.")
        return

    # Resolve positions to pk
    initiator_pk = get_pk_by_pos(initiator_pos)
    target_result = get_entry_by_pos(target_pos)

    if initiator_pk is None:
        log("VOTE", f"Initiator at position {initiator_pos} not found in roster.")
        return

    if target_result is None:
        dm(initiator_pk, f"Slot {target_pos} is not occupied by a player.")
        return

    target_pk, target_entry = target_result

    with vote_lock:
        if vote_state is not None:
            dm(initiator_pk, "A vote is already in progress. Please wait.")
            return

        # Build active players list (non-spectators)
        active_players: dict[int, str] = {}  # pos -> pk
        with roster_lock:
            for pk, entry in roster.items():
                if entry["type"] == "player":
                    active_players[entry["pos"]] = pk

        # Rule 1: A vote cannot be started until there are at least 3 players
        if len(active_players) < MIN_PLAYERS:
            dm(initiator_pk, f"At least {MIN_PLAYERS} active players are required to start a vote.")
            return

        # Rule 2: Spectators cannot initiate votes
        initiator_entry = get_entry_by_pk(initiator_pk)
        if initiator_entry is None or initiator_entry.get("is_spec", False):
            dm(initiator_pk, "Spectators are not allowed to start votes.")
            return

        # Rule 2: Spectators cannot be vote-kicked
        if target_entry.get("is_spec", False):
            dm(initiator_pk, "Cannot vote-kick a spectator.")
            return

        if target_pos == initiator_pos:
            dm(initiator_pk, "LOL. You want to kick yourself? ))))))")
            return

        # Eligible voters: active players excluding target
        eligible = {pos for pos in active_players if pos != target_pos}

        target_name = target_entry["name"]
        init_name = initiator_entry["name"] if initiator_entry else "Unknown"

        timer = threading.Timer(VOTE_DURATION, _vote_timed_out)

        # Rule 3: Initiator is automatically counted as YES
        initial_votes = {initiator_pos: True}

        vote_state = {
            "target_pos":     target_pos,
            "target_pk":      target_pk,
            "target_name":    target_name,
            "initiator_pos":  initiator_pos,
            "initiator_pk":   initiator_pk,
            "eligible":       eligible,
            "votes":          initial_votes,
            "timer":          timer,
        }

        timer.daemon = True
        timer.start()

    # Announce to the lobby
    bcast(f"=== VOTE KICKING: {target_name} (slot {target_pos}) ===")
    bcast(f"Vote using: /vote <y/n>. You have {VOTE_DURATION} seconds.")

    # Notify target and initiator privately
    dm(target_pk, "A player has started a vote to kick you. You will only be kicked if all players vote yes.")
    dm(initiator_pk, f"Votekicking: {target_name} (slot {target_pos}).")

    # Notify remaining eligible voters privately
    for pos in eligible:
        if pos != initiator_pos:
            pk = get_pk_by_pos(pos)
            if pk:
                dm(pk, f"Votekicking {target_name} (slot {target_pos}). Usage: /vote <y/n>")

    log("VOTE", f"Vote kick started against {target_name} (slot {target_pos}) by slot {initiator_pos} (Auto-YES registered)")

    # Check immediately in case initiator was the only required voter
    with vote_lock:
        _check_all_voted()


def register_vote(voter_idx: int, voter_pk: str | None, choice: bool):
    """Register a yes/no vote from a chat sender (NetPlay index space)."""
    voter_pos = get_pos_by_sender(voter_pk, voter_idx)
    with vote_lock:
        if vote_state is None:
            if voter_pk:
                dm(voter_pk, "There is no active vote right now.")
            return

        vs = vote_state

        # Resolve voter to pk for checks
        if voter_pos is None:
            if voter_pk:
                dm(voter_pk, "Spectators are not eligible to vote.")
            return
        voter_pk = get_pk_by_pos(voter_pos)
        voter_entry = get_entry_by_pk(voter_pk) if voter_pk else None

        # Rule 2: Spectators cannot vote
        if voter_entry is None or voter_entry.get("is_spec", False):
            if voter_pk:
                dm(voter_pk, "Spectators are not eligible to vote.")
            return

        if voter_pos == vs["target_pos"]:
            if voter_pk:
                dm(voter_pk, "LOL. You want to kick yourself? ))))))")
            return

        if voter_pos not in vs["eligible"]:
            if voter_pk:
                dm(voter_pk, "You don't get to vote.")
            return

        if voter_pos in vs["votes"]:
            if voter_pk:
                dm(voter_pk, "You have already voted.")
            return

        vs["votes"][voter_pos] = choice
        voter_name = voter_entry["name"] if voter_entry else f"Slot {voter_pos}"

        vote_word = "YES" if choice else "NO"
        log("VOTE", f"{voter_name} (slot {voter_pos}) voted {vote_word}")

        if voter_pk:
            dm(voter_pk, f"Your vote ({vote_word}) has been registered.")

        remaining = len(vs["eligible"]) - len(vs["votes"])
        if remaining > 0:
            bcast(f"{voter_name} voted {vote_word}. {remaining} vote(s) remaining.")

        _check_all_voted()


def cancel_vote_for_idx(left_idx: int):
    """
    A player left (playerLeft carries the NetPlay index). Translate to the
    lobby position, then cancel the vote if it targeted them, else drop them
    from the eligible set. Falls back to treating the index as a position so
    a stale roster cannot wedge an active vote.
    """
    left_pos = get_pos_by_idx(left_idx)
    candidates = {p for p in (left_pos, left_idx) if p is not None}
    with vote_lock:
        global vote_state
        if vote_state is None:
            return
        if vote_state["target_pos"] in candidates:
            try:
                vote_state["timer"].cancel()
            except Exception:
                pass
            bcast(f"{vote_state['target_name']} (slot {vote_state['target_pos']}) has left. Vote cancelled.")
            log("VOTE", "Vote cancelled — target left the lobby.")
            vote_state = None
            return

        # Otherwise remove the leaving player from eligible voters
        if vote_state is not None:
            for pos in candidates:
                if pos in vote_state["eligible"]:
                    vote_state["eligible"].discard(pos)
                    vote_state["votes"].pop(pos, None)
            _check_all_voted()

# ─── Event Handlers ───────────────────────────────────────────────────────────

def on_player_join(line: str):
    """
    Handle: WZEVENT: player join: <slot> <b64pubkey> <hash> <ip> <b64name>
    Sends greeting lines to the joining player.
    Note: Roster is now maintained via __WZROOMSTATUS__.
    The join-time hash is used here (not the roster pk) because the roster
    may not have refreshed yet; upstream accepts either identity form.
    """
    content = line[len("WZEVENT: player join: "):].strip()
    parts   = content.split(" ")

    if len(parts) < 5:
        log("WARN", f"Malformed player join line: {line}")
        return

    slot        = int(parts[0])
    b64pubkey   = parts[1]
    player_hash = parts[2]
    ip          = parts[3]
    b64name     = parts[4]

    try:
        player_name = base64.b64decode(b64name).decode("utf-8", errors="replace").strip()
    except Exception:
        player_name = b64name

    log("JOIN", f"{player_name} {b64pubkey} {ip}")

    for line_template in greetings:
        msg = line_template.format(name=player_name)
        send_cmd(f"chat direct {player_hash} {msg}", log_command=False)
    if greetings:
        log("DM", f"Welcome message sent to {player_name}")


def on_player_left(line: str):
    """
    Handle: WZEVENT: playerLeft: <playerIdx> <gameTime> <b64pubkey> <hash> <V|?> <b64name> <ip>
    playerIdx is the NetPlay slot index (translated to position internally).
    Updates any active vote. Roster is maintained via __WZROOMSTATUS__.
    """
    content = line[len("WZEVENT: playerLeft: "):].strip()
    parts   = content.split(" ")

    if len(parts) < 1:
        return

    try:
        slot = int(parts[0])
    except ValueError:
        return

    log("LEFT", f"Slot {slot}")

    # Handle vote implications by position
    cancel_vote_for_idx(slot)


def on_chat_cmd(line: str):
    """
    Handle: WZCHATLOB: <index> <ip> <hash> <b64pubkey> <b64name> <b64msg> <V|?>
    Parsed on the reader thread, then handled on a worker thread so the
    handler can wait for a fresh roster without stalling WZ output.
    """
    content = line[len("WZCHATLOB: "):].strip()
    parts   = content.split(" ")

    if len(parts) < 6:
        log("CHATCMD", content)
        return

    try:
        sender_slot = int(parts[0])
    except ValueError:
        return

    sender_pk = parts[3] if len(parts) > 3 else None

    try:
        sender_name = base64.b64decode(parts[4]).decode("utf-8", errors="replace").strip()
        raw_msg     = base64.b64decode(parts[5]).decode("utf-8", errors="replace").strip()
    except Exception:
        log("CHATCMD", content)
        return

    log_chat_message(sender_name, raw_msg)
    threading.Thread(
        target=_handle_chat_cmd,
        args=(sender_slot, sender_pk, sender_name, raw_msg),
        daemon=True,
    ).start()


def _handle_chat_cmd(sender_slot: int, sender_pk: str | None, sender_name: str, raw_msg: str):
    """Chat-command worker: refresh roster first, then act on the command."""
    msg_lower = raw_msg.strip().lower()

    # ── Refresh roster before any roster-dependent action ────────────────────
    if not request_roster_refresh():
        log("WARN", "No fresh roster before chat command; using last known roster.")

    # ── /maps ─────────────────────────────────────────────────────────────────
    if msg_lower == "/maps":
        maps = get_available_maps()
        if not maps:
            if sender_pk:
                dm(sender_pk, "No maps available.")
        else:
            if sender_pk:
                dm(sender_pk, "Available maps:")
                for i, m in enumerate(maps):
                    entry = wz_config.get_map_entry(config.get("maps"), m)
                    mods = entry["mods"] if entry else []
                    suffix = f" [+{len(mods)} mods: {', '.join(mods)}]" if mods else ""
                    dm(sender_pk, f"{i+1}. {m}{suffix}")
        return

    # ── /maps <index> ──────────────────────────────────────────────────────
    if msg_lower.startswith("/maps "):
        raw_arg = raw_msg.strip()[6:].strip()
        auth_pkeys = config.get("authorized_pkeys", [])
        if sender_pk not in auth_pkeys:
            if sender_pk:
                dm(sender_pk, "You are not authorized to change the map.")
            return

        if not _try_claim_migration():
            if sender_pk:
                dm(sender_pk, "A migration is already in progress.")
            return

        maps = get_available_maps()
        try:
            map_index = int(raw_arg) - 1
            if map_index < 0 or map_index >= len(maps):
                if sender_pk:
                    dm(sender_pk, f"Invalid map index. Use 1 to {len(maps)}.")
                _release_migration_claim()
                return
            new_map = maps[map_index]
        except ValueError:
            if sender_pk:
                dm(sender_pk, "Please provide a valid map index. Usage: /maps <index>")
            _release_migration_claim()
            return

        entry = wz_config.get_map_entry(config.get("maps"), new_map)
        new_mods = list(entry["mods"]) if entry else []
        mod_note = f" (+{len(new_mods)} mods: {', '.join(new_mods)})" if new_mods else ""
        successor_name = config.get("game_name", "") + config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX)
        if not is_lobby_safe_name(successor_name):
            if sender_pk:
                dm(sender_pk, f"Migration aborted: lobby name {successor_name!r} not acceptable.")
            log("ERROR", f"Successor lobby name {successor_name!r} has unsafe characters; aborting map change.")
            _release_migration_claim()
            return
        bcast(f"Map change to {new_map}{mod_note} requested by {sender_name}. Spinning up new instance, this usually takes about 60 seconds...")

        def _handle_map_change(map_to_load: str, mods_to_load: list):
            global quit_after_game, migration_attempts
            succeeded = False
            try:
                if not unlist_self_via_api():
                    log("WARN", "Aborting map change: lobby slot still held and no token to free it.")
                    bcast("Map change aborted: could not free the lobby slot.")
                    return
                spawned = spawn_new_instance(map_to_load, mods_to_load,
                                             name_suffix=config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX))
                if spawned is None:
                    bcast("Map change failed: could not launch a new lobby (cap reached or no free port).")
                    return
                new_port, new_session = spawned
                status = wait_for_new_instance_ready(new_session, timeout=60.0)
                with start_timeout_lock:
                    if game_started or process is None or process.poll() is not None:
                        log("MIGRATE", "Game started or process ended while waiting for new map instance. Cancelling map change.")
                        return
                if status and status.get("lobby_id"):
                    new_lobby_id = status["lobby_id"]
                    game_name = config.get("game_name", "") + config.get("migration_name_suffix", MIGRATION_NAME_SUFFIX)
                    if not migrate_to_successor(new_port, new_session, new_lobby_id, game_name, "map-change"):
                        return
                    quit_after_game = True
                    with migration_lock:
                        migration_attempts = 0
                    succeeded = True
                    send_cmd("shutdown now")
                else:
                    err = status.get("error") if status else "no response from new instance"
                    log("WARN", f"New instance {new_session} failed lobby verification ({err}). Aborting map change.")
                    if is_rate_limited(err):
                        bcast("Map change failed: lobby is rate-limiting us. Wait ~20 minutes before retrying.")
                    else:
                        bcast(f"Map change failed: new lobby could not be verified. Remaining on {current_map_name or config.get('map_name')}.")
            except Exception as e:
                log("WARN", f"Error during map change migration: {e}")
            finally:
                # On success the old instance shuts down; keep the claim held
                # so no second migration can start before shutdown completes.
                if not succeeded:
                    _release_migration_claim()

        threading.Thread(target=_handle_map_change, args=(new_map, new_mods), daemon=True).start()
        return


    # ── /report ────────────────────────────────────────────────────────────────
    if msg_lower == "/report":
        ip_addr = get_public_ip()
        lines = [
            "=== Server Report ===",
            f"CPU Load: {get_cpu_load()}",
            f"RAM Usage: {get_ram_usage()}",
            f"Address: {ip_addr}:{port_global}",
            f"Session Time Remaining: {get_session_remaining()}",
        ]
        if sender_pk:
            for l in lines:
                dm(sender_pk, l)
        return

    # ── /unlobby ───────────────────────────────────────────────────────────────
    if msg_lower == "/unlobby":
        auth_pkeys = config.get("authorized_pkeys", [])
        if sender_pk not in auth_pkeys:
            if sender_pk:
                dm(sender_pk, "You are not authorized to unlist this lobby.")
            return
        if unlist_self_via_api():
            if sender_pk:
                dm(sender_pk, f"Listing {lobby_id} deleted. This lobby is now direct-connect only.")
        else:
            if sender_pk:
                dm(sender_pk, "Unlist failed (no captured token or DELETE rejected).")
        return

    # ── /mapsadmin ─────────────────────────────────────────────────────────────
    if msg_lower.startswith("/mapsadmin"):
        auth_pkeys = config.get("authorized_pkeys", [])
        if sender_pk not in auth_pkeys:
            if sender_pk:
                dm(sender_pk, "You are not authorized to manage map admins.")
            return

        tokens = raw_msg.strip().split()
        # /mapsadmin list
        if len(tokens) >= 2 and tokens[1].lower() == "list":
            if not auth_pkeys:
                dm(sender_pk, "No map admins configured.")
            else:
                dm(sender_pk, "=== Map Admins ===")
                for i, pk in enumerate(auth_pkeys, 1):
                    # Try to find a name in the roster for this pkey
                    entry = get_entry_by_pk(pk)
                    name = entry["name"] if entry else "(not in lobby)"
                    dm(sender_pk, f"{i}. {pk}  [{name}]")
            return

        # /mapsadmin add <slot>
        if len(tokens) >= 3 and tokens[1].lower() == "add":
            try:
                target_slot = int(tokens[2])
            except ValueError:
                dm(sender_pk, "Usage: /mapsadmin add <player slot number>")
                return
            target_pk = get_pk_by_pos(target_slot)
            if target_pk is None:
                dm(sender_pk, f"No player found in slot {target_slot}.")
                return
            if target_pk in auth_pkeys:
                target_entry = get_entry_by_pk(target_pk)
                target_name = target_entry["name"] if target_entry else "Unknown"
                dm(sender_pk, f"{target_name} is already a map admin.")
                return
            auth_pkeys.append(target_pk)
            config["authorized_pkeys"] = auth_pkeys
            save_authorized_pkeys()
            target_entry = get_entry_by_pk(target_pk)
            target_name = target_entry["name"] if target_entry else "Unknown"
            dm(sender_pk, f"Added {target_name} (slot {target_slot}) as map admin.")
            dm(target_pk, "You have been granted map admin privileges.")
            return

        # /mapsadmin remove <list number>
        if len(tokens) >= 3 and tokens[1].lower() == "remove":
            try:
                list_num = int(tokens[2])
            except ValueError:
                dm(sender_pk, "Usage: /mapsadmin remove <list number>")
                return
            if list_num < 1 or list_num > len(auth_pkeys):
                dm(sender_pk, f"Invalid number. Use /mapsadmin list to see valid numbers (1-{len(auth_pkeys)}).")
                return
            removed_pk = auth_pkeys.pop(list_num - 1)
            config["authorized_pkeys"] = auth_pkeys
            save_authorized_pkeys()
            removed_entry = get_entry_by_pk(removed_pk)
            removed_name = removed_entry["name"] if removed_entry else "(not in lobby)"
            dm(sender_pk, f"Removed map admin #{list_num}: {removed_pk}  [{removed_name}]")
            return

        # Unknown subcommand
        if sender_pk:
            dm(sender_pk, "Usage: /mapsadmin list | /mapsadmin add <slot> | /mapsadmin remove <number>")
        return

    # ── /votekick <slot> ──────────────────────────────────────────────────────
    if msg_lower.startswith("/votekick"):
        tokens = raw_msg.strip().split()
        if len(tokens) < 2:
            if sender_pk:
                dm(sender_pk, "Usage: /votekick <player slot number>")
            return
        try:
            target_slot = int(tokens[1])
        except ValueError:
            if sender_pk:
                dm(sender_pk, "Invalid slot number. Usage: /votekick <slot>")
            return

        start_vote(sender_slot, sender_pk, target_slot)

    # ── /vote <y/n> ───────────────────────────────────────────────────────────
    elif msg_lower.startswith("/vote"):
        tokens = raw_msg.strip().split()
        if len(tokens) < 2:
            if sender_pk:
                dm(sender_pk, "Usage: /vote <y/n>")
            return
        choice_str = tokens[1].lower()
        if choice_str in VOTE_YES:
            register_vote(sender_slot, sender_pk, True)
        elif choice_str in VOTE_NO:
            register_vote(sender_slot, sender_pk, False)
        else:
            if sender_pk:
                dm(sender_pk, "Unknown vote option. Usage: /vote <y/n>")


def log_chat_message(sender_name: str, message: str):
    """Log a chat message once when both WZ chat events report it."""
    global last_chat_log, last_chat_log_time
    now = time.monotonic()
    message_key = (sender_name, message)
    if message_key == last_chat_log and now - last_chat_log_time < 1.0:
        return
    last_chat_log = message_key
    last_chat_log_time = now
    log("CHATCMD", f"[{sender_name}]: {message}")


def on_chat_message(line: str):
    """Decode and log a regular WZCHAT message without echoing raw protocol data."""
    content = line[len("WZCHAT: "):].strip()
    parts = content.split()
    if len(parts) < 4:
        log("CHATCMD", content)
        return

    try:
        sender_name = base64.b64decode(parts[2]).decode("utf-8", errors="replace").strip()
        message = base64.b64decode(parts[3]).decode("utf-8", errors="replace").strip()
    except Exception:
        log("CHATCMD", content)
        return

    log_chat_message(sender_name, message)


def on_room_status(line: str):
    """
    Handle: __WZROOMSTATUS__{...}__ENDWZROOMSTATUS__
    Logs the full raw line and parses the JSON to update the roster.
    Also tracks room state for readiness and post-game detection.
    """
    global room_state
    try:
        start = line.find("__WZROOMSTATUS__") + len("__WZROOMSTATUS__")
        end = line.find("__ENDWZROOMSTATUS__")
        if start < 0 or end < 0 or end <= start:
            log("WARN", "Malformed room status line")
            return

        json_str = line[start:end]
        status = json.loads(json_str)
        room_data = status.get("data", status)
        prev_state = room_state
        if isinstance(room_data, dict) and room_data.get("state"):
            room_state = room_data.get("state")
        if isinstance(room_data, dict) and room_data.get("lobbyid"):
            global lobby_id
            if not lobby_id:
                lobby_id = room_data.get("lobbyid")
        if room_state == "lobby":
            maybe_signal_ready()
            if prev_state != "lobby":
                check_game_ended(prev_state)
        players = [
            entry for entry in status.get("players", [])
            if entry.get("type") == "player"
        ]
        spectators = [
            entry for entry in status.get("specs", [])
            if entry.get("type") == "spec"
        ]
        rows = [
            (
                str(entry.get("pos", "-")),
                "PLAYER",
                str(entry.get("name", "?")),
                str(entry.get("pk", "-")),
                str(entry.get("ip") or "-"),
            )
            for entry in players
        ] + [
            (
                "-",
                "SPECTATOR",
                str(entry.get("name", "?")),
                str(entry.get("pk", "-")),
                str(entry.get("ip") or "-"),
            )
            for entry in spectators
        ]
        headers = ("POS", "TYPE", "NAME", "PUBLIC KEY", "IP")
        widths = [
            max([len(headers[index])] + [len(row[index]) for row in rows])
            for index in range(len(headers))
        ]
        log(
            "STATUS",
            f"map={room_data.get('map', '?')} | "
            f"players={len(players)} | spectators={len(spectators)}",
        )
        log("STATUS", " | ".join(
            header.ljust(widths[index])
            for index, header in enumerate(headers)
        ))
        for row in rows:
            log("STATUS", " | ".join(
                value.ljust(widths[index])
                for index, value in enumerate(row)
            ))
        update_roster_from_status(status)
    except json.JSONDecodeError as e:
        log("WARN", f"Failed to parse room status JSON: {e}")
    except Exception as e:
        log("WARN", f"Error processing room status: {e}")


def process_line(line: str):
    """Route a single line to the correct handler."""
    global game_started, start_timeout_timer, lobby_id, startup_error, stdin_ready

    if line.startswith("WZEVENT: player join:"):
        on_player_join(line)
    elif line.startswith("WZEVENT: playerLeft:"):
        on_player_left(line)
    elif line.startswith("WZEVENT: startMultiplayerGame"):
        _on_game_started()
    elif line.startswith("WZEVENT: join approval needed:"):
        on_join_approval(line)
    elif line.startswith("WZEVENT: lag-kick:"):
        log("EVENT", "Lag kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: notready-kick:"):
        log("EVENT", "Not-ready kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: lobbyid:"):
        lobby_id = line.split(":", 2)[-1].strip()
        if lobby_id:
            log("EVENT", "Lobby ID: " + lobby_id)
            maybe_signal_ready()
        else:
            log("WARN", "Received an empty lobby ID.")
    elif line.startswith("WZEVENT: lobbyerror"):
        startup_error = line[len("WZEVENT: "):].strip()
        log("ERROR", f"Lobby startup error: {startup_error}")
        startup_status_event.set()
    elif line.startswith("WZEVENT:"):
        log("EVENT", line[len("WZEVENT: "):])
    elif line.startswith("WZCMD: stdinReadReady"):
        log("INFO", "stdin command interface ready.")
        stdin_ready = True
        maybe_signal_ready()
    elif line.startswith("WZCMD:"):
        log("WZCMD", line[len("WZCMD: "):])
    elif line.startswith("WZCHATLOB:"):
        on_chat_cmd(line)
    elif line.startswith("WZCHAT:"):
        on_chat_message(line)
    elif "[NETallowJoining:" in line and " has joined, IP is:" in line:
        # The corresponding WZEVENT: player join line contains the pkey.
        return
    elif "__WZROOMSTATUS__" in line and "__ENDWZROOMSTATUS__" in line:
        on_room_status(line)
    else:
        log("WZ", line)


def on_join_approval(line: str):
    """Auto-approve joins (required with --async-join-approve).

    WZEVENT: join approval needed: <join_id> <ip> <hash> <pubkey> <b64name> <spec/play>
    """
    content = line[len("WZEVENT: join approval needed:"):].strip()
    parts = content.split(" ")
    if not parts or not parts[0]:
        log("WARN", f"Malformed join approval line: {line}")
        return
    join_id = parts[0]
    log("JOIN", f"Approving join {join_id}")
    send_cmd(f"join approve {join_id}")


def output_reader(proc: subprocess.Popen):
    """
    Background thread: read combined stdout+stderr from the game as raw bytes.
    Decodes line-by-line with error tolerance and routes each line.
    Uses select() with a short timeout where available so shutdown does not
    block indefinitely on a silent pipe; falls back to blocking reads.
    """
    # Read raw bytes to avoid text-mode buffering/encoding issues
    buf = b""
    use_select = hasattr(select, "select")
    while True:
        try:
            if use_select:
                try:
                    r, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if not r:
                        if proc.poll() is not None:
                            break
                        continue
                except Exception:
                    use_select = False  # fall back to blocking reads
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
        except Exception as e:
            log("READER", f"Read error: {e}")
            break

        buf += chunk
        # Split on line boundaries
        while b"\n" in buf or b"\r" in buf:
            # Find first newline
            nl_pos = buf.find(b"\n")
            cr_pos = buf.find(b"\r")
            if cr_pos >= 0 and (nl_pos < 0 or cr_pos < nl_pos):
                split_pos = cr_pos
                end_pos = cr_pos + 1
                # Handle \r\n
                if end_pos < len(buf) and buf[end_pos:end_pos+1] == b"\n":
                    end_pos += 1
            elif nl_pos >= 0:
                split_pos = nl_pos
                end_pos = nl_pos + 1
            else:
                break

            line_bytes = buf[:split_pos]
            buf = buf[end_pos:]

            # Decode with replacement for invalid bytes
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                process_line(line)

    # Flush any remaining data
    if buf:
        try:
            line = buf.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                process_line(line)
        except Exception:
            pass

    log("READER", "Output reader thread exited.")


def stdin_reader():
    """Background thread: read from sys.stdin and forward to game process."""
    for raw in sys.stdin:
        if raw:
            send_cmd(raw.strip())

# ─── WZ Installation Detection ───────────────────────────────────────────────

def detect_wz_install() -> str:
    """
    Detect how Warzone 2100 is installed.
    Returns 'flatpak' if the flatpak package is found,
    'system' if a system binary (e.g. from apt) is available,
    or raises RuntimeError if neither is found.
    """
    # Check for flatpak installation
    try:
        result = subprocess.run(
            ["flatpak", "info", "net.wz2100.wz2100"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log("INFO", "Warzone 2100 detected: flatpak (net.wz2100.wz2100)")
            return "flatpak"
    except FileNotFoundError:
        pass  # flatpak not installed at all

    # Check for system binary
    try:
        result = subprocess.run(
            ["which", "warzone2100"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log("INFO", f"Warzone 2100 detected: system binary ({result.stdout.strip()})")
            return "system"
    except FileNotFoundError:
        pass

    raise RuntimeError(
        "Warzone 2100 not found. Install via flatpak (net.wz2100.wz2100) or apt (warzone2100)."
    )


# ─── Process Lifecycle ────────────────────────────────────────────────────────

def build_command(wz_install: str) -> list:
    global port_global, session_global
    configdir = os.path.join(SCRIPT_DIR, "instance_configs", session_global)
    map_name = config.get("map_name")
    host_name = config.get("host_name")
    game_name = config.get("game_name")
    host_key = config.get("host_key") or config.get("host_sta2_key") or config.get("sta2_key") or ""
    mods = [m for m in current_map_mods if wz_config.is_safe_mod_name(m)]
    ah_config_name = wz_config.generate_configs(
        MAPS_DIR, MODS_DIR, AUTOHOST_DIR,
        map_name, config.get("maps"), configdir,
        host_name, game_name,
        config.get("host_auto_desync_kick_seconds", 30),
        config.get("host_auto_lag_kick_seconds", 30),
        config.get("host_auto_not_ready_kick_seconds", 30),
        config.get("game_password", ""),
        host_key,
        mods=mods,
        mods_version=config.get("wz_mods_version", "4.7.0"),
        log=log,
    )
    if not ah_config_name:
        ah_config_name = f"AH_{map_name}"

    log("INFO", f"Config dir: {configdir}")
    log("INFO", f"WZ install : {wz_install}")
    if mods:
        log("INFO", f"Map mods   : {', '.join(mods)}")

    common_args = []
    if config.get("wz_headless", True):
        common_args.append("--headless")
    for flag in config.get("wz_debug_flags", []):
        if isinstance(flag, str) and re.fullmatch(r"[A-Za-z0-9_]+", flag):
            common_args.append(f"--debug={flag}")
    if config.get("wz_nosound", True):
        common_args.append("--nosound")
    common_args += [
        f"--configdir={configdir}",
        f"--autohost={ah_config_name}",
        f"--gameport={port_global}",
        "--enablecmdinterface=stdin",
    ]
    provider = config.get("wz_host_connection_provider", "tcp")
    if provider in ("tcp", "gns"):
        common_args.append(f"--host-connection-provider={provider}")
    if config.get("wz_portmapping", False):
        common_args.append("--portmapping=true")
    else:
        common_args.append("--portmapping=false")
    if config.get("wz_async_join_approve", True):
        common_args.append("--async-join-approve")
    autostart = wz_config.get_autostart_players(config.get("maps"), current_map_name, current_map_mods)
    if autostart > 0:
        common_args.append(f"--startplayers={autostart}")
    if config.get("wz_enable_lobby_slashcmd", False):
        common_args.append("--enablelobbyslashcmd")
    for mod in mods:
        # Exact staged filename (regular file check upstream).
        common_args.append(f"--mod_mp={mod}.wz")
    # Bot-managed flags must not be overridable via wz_extra_args: a second
    # --gameport/--configdir/--autohost would leave WZ and the bot disagreeing
    # about ports/paths, and --enablecmdinterface is the bot's lifeline.
    forbidden_prefixes = ("--gameport", "--configdir", "--autohost",
                          "--enablecmdinterface", "--mod_mp")
    used_keys = {a.split("=", 1)[0].split(" ", 1)[0] for a in common_args}
    for extra in config.get("wz_extra_args", []):
        if isinstance(extra, str) and extra.startswith("--") and "\n" not in extra:
            key = extra.split("=", 1)[0].split(" ", 1)[0]
            if any(key == p or key.startswith(p + ".") for p in forbidden_prefixes):
                log("WARN", f"Ignoring bot-managed wz_extra_args entry: {extra!r}")
                continue
            if extra in common_args or key in used_keys:
                log("WARN", f"Ignoring duplicate wz_extra_args entry: {extra!r}")
                continue
            common_args.append(extra)
            used_keys.add(key)
        else:
            log("WARN", f"Ignoring unsafe wz_extra_args entry: {extra!r}")

    if wz_install == "flatpak":
        return [
            "flatpak", "run",
            f"--filesystem={configdir}",
            "net.wz2100.wz2100",
        ] + common_args
    else:  # system binary
        binary = config.get("wz_binary") or "warzone2100"
        if "/" not in binary:
            resolved = shutil.which(binary) or "/usr/games/warzone2100"
        else:
            resolved = binary
        return [resolved] + common_args
def shutdown(signum=None, frame=None):
    global start_timeout_timer
    log("INFO", "Shutting down...")
    write_session_status(session_global, "exited", port_global)

    with start_timeout_lock:
        if start_timeout_timer is not None:
            start_timeout_timer.cancel()
            start_timeout_timer = None

    if process:
        try:
            send_cmd("shutdown now")
            process.wait(timeout=5)
        except Exception:
            process.kill()

    if log_file_handle:
        log_file_handle.close()

    sys.exit(0)

# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    global process, config, greetings, log_file_handle, log_file_path, game_started, start_timeout_timer
    global port_global, session_global, quit_after_game
    global instance_start_time
    global lobby_id, startup_error, stdin_ready, room_state
    global current_map_name, current_map_mods

    # Args: [port] [session_name] [map_name] [mods_csv] [name_suffix]
    # — normally set by spawner
    if len(sys.argv) > 1:
        port_global = int(sys.argv[1])
    else:
        # Standalone fallback: find_available_port probes localhost, so ports
        # already bound by live instances are skipped. A TOCTOU race with a
        # concurrent spawner remains; the readiness port probe catches it.
        port_global = find_available_port(2100)
        if port_global is None:
            print("[ERROR] No free game port in range 2100-2110.")
            sys.exit(1)
    session_global = sys.argv[2]      if len(sys.argv) > 2 else f"WZ_{port_global}"

    write_session_status(session_global, "starting", port_global)

    config = load_config()

    # map_name / mods can be overridden by the spawner as 3rd/4th arguments
    if len(sys.argv) > 3:
        config["map_name"] = sys.argv[3]
    spawn_mods = []
    if len(sys.argv) > 4 and sys.argv[4].strip():
        spawn_mods = [m for m in sys.argv[4].split(",") if m]
    if len(sys.argv) > 5 and sys.argv[5]:
        config["game_name"] = config.get("game_name", "") + sys.argv[5]
        log("INFO", f"Display name    : {config['game_name']}")
    current_map_name = config.get("map_name", "")
    entry = wz_config.get_map_entry(config.get("maps"), current_map_name)
    current_map_mods = list(spawn_mods) if spawn_mods else (list(entry["mods"]) if entry else [])

    for problem in wz_config.validate_config(config.get("maps"), MAPS_DIR, MODS_DIR, config.get("map_name")):
        log("WARN", f"Config validation: {problem}")

    if not wz_config.extract_map_info(MAPS_DIR, current_map_name, wz_config.resolve_map_file(config.get("maps"), current_map_name), log):
        log("ERROR", f"Map not available, refusing to start: {current_map_name}")
        write_session_status(session_global, "failed", port_global, error=f"unknown map {current_map_name}")
        sys.exit(1)
    for mod in current_map_mods:
        if not wz_config.is_safe_mod_name(mod) or wz_config.resolve_mod_source(MODS_DIR, mod) is None:
            log("ERROR", f"Mod not available, refusing to start: {mod}")
            write_session_status(session_global, "failed", port_global, error=f"unknown mod {mod}")
            sys.exit(1)

    log_path = os.path.join(SCRIPT_DIR, f"{session_global}.log")
    log_file_path = log_path
    log_file_handle = open(log_path, "a", encoding="utf-8")

    greetings = config.get("greeting_lines", [])

    log("INFO", f"Session       : {session_global}")
    log("INFO", f"Port          : {port_global}")
    log("INFO", f"Map           : {current_map_name}")
    if current_map_mods:
        log("INFO", f"Mods          : {', '.join(current_map_mods)}")

    instance_start_time = time.time()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        backend = config.get("wz_backend", "auto")
        if backend in ("flatpak", "system"):
            if backend == "system":
                binary = config.get("wz_binary") or "warzone2100"
                found = binary if "/" in binary and os.path.exists(binary) else (
                    shutil.which(binary) or "/usr/games/warzone2100")
                if not found or not os.path.exists(found):
                    log("ERROR", "wz_backend=system but no warzone2100 binary found.")
                    sys.exit(1)
            wz_install = backend
            log("INFO", f"WZ install (forced by config): {wz_install}")
        else:
            wz_install = detect_wz_install()
    except RuntimeError as e:
        log("ERROR", str(e))
        sys.exit(1)

    cmd = build_command(wz_install)
    log("INFO", "Command: " + " ".join(cmd))

    in_reader = threading.Thread(target=stdin_reader, daemon=True)
    in_reader.start()

    lobby_id_restarts = 0
    lobby_verification_failed = False

    while True:
        with roster_lock:
            roster.clear()

        with start_timeout_lock:
            game_started = False
            if start_timeout_timer is not None:
                start_timeout_timer.cancel()
                start_timeout_timer = None

        lobby_id = None
        startup_error = None
        stdin_ready = False
        room_state = None
        startup_status_event.clear()
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        log("INFO", f"Process started (PID {process.pid})")

        with start_timeout_lock:
            arm_start_timeout()

        reader = threading.Thread(target=output_reader, args=(process,), daemon=True)
        reader.start()

        # Poll room status during startup: WZ only emits __WZROOMSTATUS__ on
        # request/changes, so an idle lobby would never satisfy the ready gate.
        deadline = time.time() + LOBBY_ID_TIMEOUT
        while time.time() < deadline and not startup_status_event.is_set():
            send_cmd("status", log_command=False)
            startup_status_event.wait(2)

        if check_lobby_ready():
            verification_failure = None
        elif startup_error:
            verification_failure = startup_error
        elif not lobby_id:
            verification_failure = "no lobby ID received"
        else:
            verification_failure = "lobby not ready (stdin/room state/port probe)"

        if verification_failure is None:
            log("INFO", f"Lobby ready after launch: id={lobby_id} port={port_global}")
            write_session_status(session_global, "ready", port_global, lobby_id=lobby_id)
            publish_relay_target(port_global)
            process.wait()
        else:
            if verification_failure.startswith("lobbyerror"):
                failure_description = verification_failure
            else:
                failure_description = f"missing lobby ID ({verification_failure})"

            write_session_status(session_global, "failed", port_global, error=failure_description)
            log(
                "WARN",
                f"Lobby verification failed: {failure_description}",
            )
            if process.poll() is None:
                try:
                    send_cmd("shutdown now")
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                    process.wait()

            if is_rate_limited(failure_description):
                # Retrying a POST while throttled only extends the ban: stop
                # immediately and let the spawner back off instead.
                lobby_verification_failed = True
                log(
                    "ERROR",
                    "Lobby is rate-limiting us; stopping instance without further retries.",
                )
                break

            if lobby_id_restarts >= MAX_LOBBY_ID_RESTARTS:
                lobby_verification_failed = True
                log(
                    "ERROR",
                    f"Lobby verification failed after {MAX_LOBBY_ID_RESTARTS} "
                    "restarts; stopping instance.",
                )
                break

            lobby_id_restarts += 1
            log(
                "INFO",
                f"Restarting instance to retry lobby verification "
                f"({lobby_id_restarts}/{MAX_LOBBY_ID_RESTARTS}).",
            )
            continue

        with start_timeout_lock:
            if start_timeout_timer is not None:
                start_timeout_timer.cancel()
                start_timeout_timer = None

        log("INFO", f"Process exited (code {process.returncode}).")
        break

    with start_timeout_lock:
        if start_timeout_timer is not None:
            start_timeout_timer.cancel()
            start_timeout_timer = None

    if not quit_after_game and not lobby_verification_failed and crash_respawn_allowed():
        if game_started and process is not None and process.returncode == 0:
            maybe_spawn_crash_replacement(reason="game-ended")
        else:
            maybe_spawn_crash_replacement()

    write_session_status(session_global, "exited", port_global)
    log_file_handle.close()
    sys.exit(0)

if __name__ == "__main__":
    main()
