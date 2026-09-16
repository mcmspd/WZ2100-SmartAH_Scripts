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
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
import glob
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
START_TIMEOUT   = 5400 # seconds: 90 minutes to start a game before auto-restart
LOBBY_ID_TIMEOUT = 30  # seconds to wait for the lobby ID after launch
MAX_LOBBY_ID_RESTARTS = 3
MAPS_DIR         = os.path.join(SCRIPT_DIR, "maps")

instance_start_time = None  # Set in main() to track session uptime


# ─── Globals ──────────────────────────────────────────────────────────────────

process  = None   # Active WZ2100 subprocess
config   = {}     # Loaded instance config dict
greetings = []    # List of greeting line strings
log_file_handle = None # Global file handle for appending logs
port_global = 0
session_global = ""
quit_after_game = False
lobby_id = None
startup_status_event = threading.Event()
startup_error = None
last_chat_log = None
last_chat_log_time = 0.0


# Roster: pk (str) -> {"type": str, "name": str, "pos": int|None, "is_spec": bool}
roster: dict[str, dict] = {}
roster_lock = threading.Lock()

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

# 60-minute start-timeout state
game_started = False
start_timeout_timer = None
start_timeout_lock = threading.Lock()

# ─── Dynamic Config & Instances ───────────────────────────────────────────────

def find_available_port(start_port: int) -> int:
    port = start_port
    while port <= 2110:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
        port += 1
    return start_port

def get_available_maps() -> list[str]:
    maps = []
    if os.path.exists(MAPS_DIR):
        for f in os.listdir(MAPS_DIR):
            if f.endswith(".wz"):
                maps.append(f[:-3])
    return sorted(maps)

def extract_map_info(map_name: str) -> dict:
    wz_path = os.path.join(MAPS_DIR, f"{map_name}.wz")
    if not os.path.exists(wz_path):
        return None
    try:
        with zipfile.ZipFile(wz_path, 'r') as z:
            with z.open('level.json') as f:
                level_data = json.load(f)
                return {
                    "name": map_name,
                    "players": level_data.get("players", 2)
                }
    except Exception as e:
        log("WARN", f"Failed to extract map info: {e}")
        return None

def generate_configs(
    map_name: str,
    configdir: str,
    host_name: str,
    game_name: str,
    desync_kick_seconds: int = 30,
    lag_kick_seconds: int = 30,
    not_ready_kick_seconds: int = 30,
    game_password: str = "",
    host_key: str = "",
):
    info = extract_map_info(map_name)
    if not info:
        return False
    
    players = info["players"]
    
    # Generate config
    os.makedirs(configdir, exist_ok=True)
    config_path = os.path.join(configdir, "config")
    with open(config_path, "w") as f:
        f.write(f"[General]\nmapName={map_name}\nmaxPlayers={players}\ngameName={game_name}\n")
        f.write("lobbyserver=https://wzlobby.wz2100.net/lobby\n")
        f.write(f"playerName={host_name}\n")
        f.write("antialiasing=0\n")
        f.write("fog=false\n")
        f.write(f"hostAutoDesyncKickSeconds={desync_kick_seconds}\n")
        f.write(f"hostAutoLagKickSeconds={lag_kick_seconds}\n")
        f.write(f"hostAutoNotReadyKickSeconds={not_ready_kick_seconds}\n")
        f.write("lobbyHostJoinOpts_botProt=2\n")
        f.write("lobbyHostJoinOpts_proxyIPs=2\n")
        f.write("lobbyHostJoinOpts_hostingIPs=2\n")
        f.write("rotateRadar=false\n")
        f.write("shadows=0\n")
        f.write("sound=0\n")
        f.write("terrainShadows=1\n")
        f.write("vsync=1\n")
        
    # Generate autohost config
    ah_dir = os.path.join(configdir, "autohost")
    os.makedirs(ah_dir, exist_ok=True)
    ah_name = f"AH_{map_name}"
    ah_path = os.path.join(ah_dir, ah_name)
    
    challenge = {
        "map": map_name,
        "maxPlayers": players,
        "scavengers": 0,
        "alliances": 3,
        "powerLevel": 2,
        "bases": 3,
        "name": game_name,
        "techLevel": 1,
        "spectatorHost": True,
        "openSpectatorSlots": 10,
        "blindMode": "none",
    }
    if game_password:
        challenge["gamePassword"] = game_password

    ah_data = {
        "locked": {"power": True, "alliances": True, "teams": True, "difficulty": False, "ai": False, "scavengers": True, "position": False, "bases": True},
        "challenge": challenge,
    }
    
    # Generate teams based on 2 teams
    for i in range(players):
        team = i // max(1, (players // 2))
        ah_data[f"player_{i}"] = {"team": team}
        
    with open(ah_path, "w") as f:
        json.dump(ah_data, f, indent=2)
        
    # Generate players dir and host sta2 identity if provided
    players_dir = os.path.join(configdir, "multiplay", "players")
    os.makedirs(players_dir, exist_ok=True)
    if host_key and host_name:
        sta2_path = os.path.join(players_dir, f"{host_name}.sta2")
        with open(sta2_path, "w", encoding="utf-8") as f:
            f.write(f"WZ.STA.v3\n0 0 0 0 0\n{host_key.strip()}\n")
        
    # Copy map file into config maps directory
    target_maps_dir = os.path.join(configdir, "maps")
    os.makedirs(target_maps_dir, exist_ok=True)
    source_map_path = os.path.join(MAPS_DIR, f"{map_name}.wz")
    target_map_path = os.path.join(target_maps_dir, f"{map_name}.wz")
    if os.path.exists(source_map_path):
        shutil.copy2(source_map_path, target_map_path)
        
    return ah_name

def spawn_new_instance(map_name: str):
    """Launch a fresh bot instance in a new tmux session."""
    log("INFO", f"Spawning new instance with map {map_name}")
    port = find_available_port(2100)
    session_name = f"WZ_{port}"
    # Run tmux new-session via subprocess
    cmd = [
        "tmux", "new-session", "-d", "-s", session_name,
        "python3", os.path.join(SCRIPT_DIR, "instance_bot.py"),
        str(port), session_name, map_name
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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


def get_public_ip() -> str:
    """Return the machine's public IP address via curl ifconfig.me."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", "ifconfig.me"],
            capture_output=True, text=True
        )
        ip = result.stdout.strip()
        return ip if ip else "N/A"
    except Exception:
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

def save_authorized_pkeys():
    """Persist the current authorized_pkeys list back to instances.json."""
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        data["authorized_pkeys"] = config.get("authorized_pkeys", [])
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        log("ADMIN", "authorized_pkeys saved to instances.json")
    except Exception as e:
        log("WARN", f"Failed to save authorized_pkeys: {e}")


# ─── Shared Utilities ─────────────────────────────────────────────────────────

def log(tag: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    output_line = f"[{ts}] [{tag}] {msg}"
    print(output_line, flush=True)

    if log_file_handle:
        log_file_handle.write(output_line + "\n")
        log_file_handle.flush()


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
    Roster entries: {type, name, pos, is_spec}
    """
    global roster
    new_roster: dict[str, dict] = {}

    # Process active players
    for p in status_json.get("players", []):
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
            "is_spec": False,
        }

    # Process spectators
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
            "is_spec": True,
        }

    with roster_lock:
        roster = new_roster

    active_count = sum(1 for e in roster.values() if not e["is_spec"])
    spec_count = sum(1 for e in roster.values() if e["is_spec"])
    log("ROSTER", f"Updated: {len(roster)} entries ({active_count} players, {spec_count} specs)")


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


def get_pos_by_pk(pk: str) -> int | None:
    """Get a player's position by their pk."""
    with roster_lock:
        entry = roster.get(pk)
        if entry and entry["type"] == "player":
            return entry.get("pos")
    return None

# ─── Game Process Communication ───────────────────────────────────────────────

WZ_COMMAND_PATTERNS = (
    re.compile(r"exit"),
    re.compile(r"admin (?:add-hash|add-public-key|remove) \S+"),
    re.compile(r"kick identity \S+(?: .+)?"),
    re.compile(r"redirect identity \S+ \S+"),
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
    """Send a private system message to one player by their pk."""
    send_cmd(f"chat direct {pk} {msg}")

# ─── Start-Timeout Auto-Restart ───────────────────────────────────────────────

def _start_timeout_expired():
    global game_started, start_timeout_timer, quit_after_game
    with start_timeout_lock:
        if game_started:
            return
        if process is None or process.poll() is not None:
            return
    log("TIMEOUT", "90-minute start window expired. Spawning new instance and shutting down.")
    spawn_new_instance(config.get("map_name", ""))
    time.sleep(30)
    with roster_lock:
        roster_snapshot = list(roster.items())
    for pk, entry in roster_snapshot:
        if entry["type"] == "player":
            send_cmd(f"kick identity {pk} Lobby stale, moving you to a fresh lobby.")
    time.sleep(5)
    quit_after_game = True
    if process:
        try:
            send_cmd("shutdown now")
        except Exception:
            process.kill()

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


def start_vote(initiator_pos: int, target_pos: int):
    """
    Begin a vote-kick session using position numbers.
    Resolves positions to pk internally, then proceeds with the vote.
    """
    global vote_state

    # Resolve positions to pk
    initiator_pk = get_pk_by_pos(initiator_pos)
    target_result = get_entry_by_pos(target_pos)

    if initiator_pk is None:
        log("VOTE", f"Initiator at slot {initiator_pos} not found in roster.")
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


def register_vote(voter_pos: int, choice: bool):
    """Register a yes/no vote from a player at a given position."""
    with vote_lock:
        if vote_state is None:
            voter_pk = get_pk_by_pos(voter_pos)
            if voter_pk:
                dm(voter_pk, "There is no active vote right now.")
            return

        vs = vote_state

        # Resolve voter position to pk for checks
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


def cancel_vote_for_pos(pos: int):
    """
    If the target of the current vote leaves, cancel the vote automatically.
    Called when a player leaves.
    """
    with vote_lock:
        global vote_state
        if vote_state is None:
            return
        if vote_state["target_pos"] == pos:
            try:
                vote_state["timer"].cancel()
            except Exception:
                pass
            bcast(f"{vote_state['target_name']} (slot {pos}) has left. Vote cancelled.")
            log("VOTE", "Vote cancelled — target left the lobby.")
            vote_state = None

        # Also remove the leaving player from eligible voters if they haven't voted
        if vote_state is not None and pos in vote_state["eligible"]:
            vote_state["eligible"].discard(pos)
            vote_state["votes"].pop(pos, None)
            _check_all_voted()

# ─── Event Handlers ───────────────────────────────────────────────────────────

def on_player_join(line: str):
    """
    Handle: WZEVENT: player join: <slot> <b64pubkey> <hash> <ip> <b64name>
    Sends greeting lines to the joining player.
    Note: Roster is now maintained via __WZROOMSTATUS__.
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
    cancel_vote_for_pos(slot)


def on_chat_cmd(line: str):
    """
    Handle: WZCHATLOB: <index> <ip> <hash> <b64pubkey> <b64name> <b64msg> <V|?>
    Upon detecting any command, update the roster first via __WZROOMSTATUS__ request,
    then process the command.
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

    msg_lower = raw_msg.strip().lower()

    
    # ── Update roster first before any action ─────────────────────────────────
    send_cmd("status")

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
                    dm(sender_pk, f"{i+1}. {m}")
        return

    # ── /maps <index> ──────────────────────────────────────────────────────
    if msg_lower.startswith("/maps "):
        raw_arg = raw_msg.strip()[6:].strip()
        auth_pkeys = config.get("authorized_pkeys", [])
        if sender_pk not in auth_pkeys:
            if sender_pk:
                dm(sender_pk, "You are not authorized to change the map.")
            return
            
        maps = get_available_maps()
        try:
            map_index = int(raw_arg) - 1
            if map_index < 0 or map_index >= len(maps):
                if sender_pk:
                    dm(sender_pk, f"Invalid map index. Use 1 to {len(maps)}.")
                return
            new_map = maps[map_index]
        except ValueError:
            if sender_pk:
                dm(sender_pk, "Please provide a valid map index. Usage: /maps <index>")
            return
            
        bcast(f"Map change to {new_map} requested by {sender_name}. Restarting lobby in 30 seconds...")
        spawn_new_instance(new_map)
        time.sleep(30)
        # Kick everyone
        with roster_lock:
            for pk, entry in roster.items():
                if entry["type"] == "player":
                    send_cmd(f"kick identity {pk} Map changing to {new_map}, please rejoin!")
        time.sleep(3)
        
        global quit_after_game
        quit_after_game = True
        send_cmd("shutdown now")
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

        start_vote(sender_slot, target_slot)

    # ── /vote <y/n> ───────────────────────────────────────────────────────────
    elif msg_lower.startswith("/vote"):
        tokens = raw_msg.strip().split()
        if len(tokens) < 2:
            if sender_pk:
                dm(sender_pk, "Usage: /vote <y/n>")
            return
        choice_str = tokens[1].lower()
        if choice_str in VOTE_YES:
            register_vote(sender_slot, True)
        elif choice_str in VOTE_NO:
            register_vote(sender_slot, False)
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
    """
    try:
        start = line.find("__WZROOMSTATUS__") + len("__WZROOMSTATUS__")
        end = line.find("__ENDWZROOMSTATUS__")
        if start < 0 or end < 0 or end <= start:
            log("WARN", "Malformed room status line")
            return

        json_str = line[start:end]
        status = json.loads(json_str)
        room_data = status.get("data", status)
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
    global game_started, start_timeout_timer, lobby_id, startup_error

    if line.startswith("WZEVENT: player join:"):
        on_player_join(line)
    elif line.startswith("WZEVENT: playerLeft:"):
        on_player_left(line)
    elif line.startswith("WZEVENT: startMultiplayerGame"):
        log("EVENT", "Game started.")
        with start_timeout_lock:
            game_started = True
            if start_timeout_timer is not None:
                start_timeout_timer.cancel()
                start_timeout_timer = None
        global quit_after_game
        quit_after_game = True
        # Spawn new instance shortly after game starts
        threading.Timer(5.0, lambda: spawn_new_instance(config.get("map_name", ""))).start()
    elif line.startswith("WZEVENT: lag-kick:"):
        log("EVENT", "Lag kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: notready-kick:"):
        log("EVENT", "Not-ready kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: lobbyid:"):
        lobby_id = line.split(":", 2)[-1].strip()
        if lobby_id:
            log("EVENT", "Lobby ID: " + lobby_id)
            startup_status_event.set()
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


def output_reader(proc: subprocess.Popen):
    """
    Background thread: read combined stdout+stderr from the game as raw bytes.
    Decodes line-by-line with error tolerance and routes each line.
    """
    # Read raw bytes to avoid text-mode buffering/encoding issues
    buf = b""
    while True:
        try:
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
    ah_config_name = generate_configs(
        map_name,
        configdir,
        host_name,
        game_name,
        config.get("host_auto_desync_kick_seconds", 30),
        config.get("host_auto_lag_kick_seconds", 30),
        config.get("host_auto_not_ready_kick_seconds", 30),
        config.get("game_password", ""),
        host_key,
    )
    if not ah_config_name:
        ah_config_name = f"AH_{map_name}"

    log("INFO", f"Config dir: {configdir}")
    log("INFO", f"WZ install : {wz_install}")

    common_args = [
        "--headless",
        "--debug=ALL",
        "--nosound",
        f"--configdir={configdir}",
        f"--autohost={ah_config_name}",
        f"--gameport={port_global}",
        "--enablecmdinterface=stdin",
    ]

    if wz_install == "flatpak":
        return [
            "flatpak", "run",
            f"--filesystem={configdir}",
            "net.wz2100.wz2100",
        ] + common_args
    else:  # system binary
        return ["warzone2100"] + common_args
def shutdown(signum=None, frame=None):
    global start_timeout_timer
    log("INFO", "Shutting down...")

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
    global process, config, greetings, log_file_handle, game_started, start_timeout_timer
    global port_global, session_global, quit_after_game
    global instance_start_time
    global lobby_id, startup_error

    # Args: [port] [session_name] [map_name]  — all optional, normally set by spawner
    port_global    = int(sys.argv[1]) if len(sys.argv) > 1 else find_available_port(2100)
    session_global = sys.argv[2]      if len(sys.argv) > 2 else f"WZ_{port_global}"

    config = load_config()

    # map_name can be overridden by the spawner as the 3rd argument
    if len(sys.argv) > 3:
        config["map_name"] = sys.argv[3]

    log_path = os.path.join(SCRIPT_DIR, f"{session_global}.log")
    log_file_handle = open(log_path, "a", encoding="utf-8")

    greetings = config.get("greeting_lines", [])

    log("INFO", f"Session       : {session_global}")
    log("INFO", f"Port          : {port_global}")
    log("INFO", f"Map           : {config.get('map_name')}")

    instance_start_time = time.time()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
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
            start_timeout_timer = threading.Timer(START_TIMEOUT, _start_timeout_expired)
            start_timeout_timer.daemon = True
            start_timeout_timer.start()

        reader = threading.Thread(target=output_reader, args=(process,), daemon=True)
        reader.start()

        startup_status_event.wait(LOBBY_ID_TIMEOUT)
        verification_failure = startup_error
        if verification_failure is None and not lobby_id:
            verification_failure = "no lobby ID received"

        if verification_failure is None:
            log("INFO", f"Lobby ID verified after launch: {lobby_id}")
            process.wait()
        else:
            if verification_failure.startswith("lobbyerror"):
                failure_description = verification_failure
            else:
                failure_description = f"missing lobby ID ({verification_failure})"

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

    if not quit_after_game and not lobby_verification_failed:
        log("INFO", "Process crashed unexpectedly. Spawning new instance.")
        spawn_new_instance(config.get("map_name", ""))

    log_file_handle.close()
    sys.exit(0)

if __name__ == "__main__":
    main()
