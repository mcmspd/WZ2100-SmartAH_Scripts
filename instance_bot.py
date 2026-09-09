#!/usr/bin/env python3
"""
instance_bot.py — Warzone 2100 per-instance bot.

Spawns a single WZ2100 instance process, monitors its output in real-time,
and reacts to game events (e.g. greeting players on join, vote-kick).

Greeting lines and all instance parameters are read from instances.json.
This script is normally launched by manager.py inside a tmux session, but
can also be run standalone:

    python3 instance_bot.py <instance_name>
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
MAPS_DIR        = os.path.join(SCRIPT_DIR, "maps")


# ─── Globals ──────────────────────────────────────────────────────────────────

process  = None   # Active WZ2100 subprocess
config   = {}     # Loaded instance config dict
greetings = []    # List of greeting line strings
log_file_handle = None # Global file handle for appending logs
instance_name_global = ""
port_global = 0
session_global = ""
quit_after_game = False


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

def generate_configs(map_name: str, configdir: str, gameName: str):
    info = extract_map_info(map_name)
    if not info:
        return False
    
    players = info["players"]
    
    # Generate config
    os.makedirs(configdir, exist_ok=True)
    config_path = os.path.join(configdir, "config")
    with open(config_path, "w") as f:
        f.write(f"[General]\nmapName={map_name}\nmaxPlayers={players}\ngameName={gameName}\n")
        f.write("lobbyserver=https://wzlobby.wz2100.net/lobby\n")
        f.write("playerName=FreedomHost\n")
        f.write("vsync=0\n")
        
    # Generate autohost config
    ah_dir = os.path.join(configdir, "autohost")
    os.makedirs(ah_dir, exist_ok=True)
    ah_name = f"AH_{map_name}"
    ah_path = os.path.join(ah_dir, ah_name)
    
    ah_data = {
        "locked": {"power": True, "alliances": True, "teams": True, "difficulty": False, "ai": False, "scavengers": True, "position": False, "bases": True},
        "challenge": {
            "map": map_name,
            "maxPlayers": players,
            "scavengers": 0,
            "alliances": 3,
            "powerLevel": 2,
            "bases": 3,
            "name": gameName,
            "techLevel": 1,
            "spectatorHost": True,
            "openSpectatorSlots": 10,
            "blindMode": "none"
        }
    }
    
    # Generate teams based on 2 teams
    for i in range(players):
        team = i // max(1, (players // 2))
        ah_data[f"player_{i}"] = {"team": team}
        
    with open(ah_path, "w") as f:
        json.dump(ah_data, f, indent=2)
        
    # Generate empty players dir
    players_dir = os.path.join(configdir, "multiplay", "players")
    os.makedirs(players_dir, exist_ok=True)
        
    # Copy map file into config maps directory
    target_maps_dir = os.path.join(configdir, "maps")
    os.makedirs(target_maps_dir, exist_ok=True)
    source_map_path = os.path.join(MAPS_DIR, f"{map_name}.wz")
    target_map_path = os.path.join(target_maps_dir, f"{map_name}.wz")
    if os.path.exists(source_map_path):
        shutil.copy2(source_map_path, target_map_path)
        
    return ah_name

def spawn_new_instance(base_name: str, map_name: str):
    log("INFO", f"Spawning new instance of {base_name} with map {map_name}")
    port = find_available_port(2100)
    session_name = f"WZ_{base_name}_{port}"
    # Run tmux new-session via subprocess
    cmd = [
        "tmux", "new-session", "-d", "-s", session_name,
        "python3", os.path.join(SCRIPT_DIR, "instance_bot.py"), base_name, str(port), session_name, map_name
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ─── Shared Utilities ─────────────────────────────────────────────────────────

def log(tag: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    output_line = f"[{ts}] [{tag}] {msg}"
    print(output_line, flush=True)

    if log_file_handle:
        log_file_handle.write(output_line + "\n")
        log_file_handle.flush()


def load_instances() -> dict:
    """Load and return all instances from instances.json."""
    if not os.path.exists(CONFIG_FILE):
        print(f"[ERROR] {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_instance(instance_name: str) -> dict:
    """Load a single named instance from instances.json."""
    data = load_instances()
    if instance_name not in data:
        print(f"[ERROR] Instance '{instance_name}' not found in instances.json.")
        print(f"        Available: {', '.join(data.keys())}")
        sys.exit(1)
    return data[instance_name]

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

def send_cmd(cmd: str):
    """Write a single command line to the game's stdin."""
    if process and process.stdin:
        try:
            # Encode to bytes for binary stdin pipe
            process.stdin.write((cmd.rstrip("\n") + "\n").encode("utf-8", errors="replace"))
            process.stdin.flush()
            log("CMD->", cmd.strip())
        except BrokenPipeError:
            log("WARN", "stdin pipe broken; process may have exited.")


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
    spawn_new_instance(instance_name_global, config.get("map_name", ""))
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

    log("JOIN", f"Slot {slot} | '{player_name}' | {ip}")

    for line_template in greetings:
        msg = line_template.format(name=player_name)
        send_cmd(f"chat direct {player_hash} {msg}")


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

    log("CHATCMD", f"[{sender_name}]: {raw_msg}")

    msg_lower = raw_msg.strip().lower()

    
    # ── Update roster first before any action ─────────────────────────────────
    send_cmd("roomstatus")

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
        spawn_new_instance(instance_name_global, new_map)
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


def on_room_status(line: str):
    """
    Handle: __WZROOMSTATUS__{...}__ENDWZROOMSTATUS__
    Logs the full raw line and parses the JSON to update the roster.
    """
    # ALWAYS log the full raw status line so it appears in the log file
    # and manager.py can read it
    log("STATUS", line)

    try:
        start = line.find("__WZROOMSTATUS__") + len("__WZROOMSTATUS__")
        end = line.find("__ENDWZROOMSTATUS__")
        if start < 0 or end < 0 or end <= start:
            log("WARN", "Malformed room status line")
            return

        json_str = line[start:end]
        status = json.loads(json_str)
        update_roster_from_status(status)
    except json.JSONDecodeError as e:
        log("WARN", f"Failed to parse room status JSON: {e}")
    except Exception as e:
        log("WARN", f"Error processing room status: {e}")


def process_line(line: str):
    """Route a single line to the correct handler."""
    global game_started, start_timeout_timer

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
        threading.Timer(5.0, lambda: spawn_new_instance(instance_name_global, config.get("map_name", ""))).start()
    elif line.startswith("WZEVENT: lag-kick:"):
        log("EVENT", "Lag kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: notready-kick:"):
        log("EVENT", "Not-ready kick: " + line[len("WZEVENT: "):])
    elif line.startswith("WZEVENT: lobbyid:"):
        log("EVENT", "Lobby ID: " + line.split()[-1])
    elif line.startswith("WZEVENT:"):
        log("EVENT", line[len("WZEVENT: "):])
    elif line.startswith("WZCMD: stdinReadReady"):
        log("INFO", "stdin command interface ready.")
    elif line.startswith("WZCMD:"):
        log("WZCMD", line[len("WZCMD: "):])
    elif line.startswith("WZCHATLOB:"):
        on_chat_cmd(line)
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

# ─── Process Lifecycle ────────────────────────────────────────────────────────

def build_command() -> list:
    global port_global, session_global
    configdir = os.path.join(SCRIPT_DIR, "instance_configs", session_global)
    map_name = config.get("map_name", "NTW-Full2v2")
    ah_config_name = generate_configs(map_name, configdir, "No Rambo means No Bans")
    if not ah_config_name:
        ah_config_name = f"AH_{map_name}"

    log("INFO", f"Config dir: {configdir}")

    return [
        "flatpak", "run",
        f"--filesystem={configdir}",
        "net.wz2100.wz2100",
        "--headless",
        "--nosound",
        f"--configdir={configdir}",
        f"--autohost={ah_config_name}",
        f"--gameport={port_global}",
        "--enablecmdinterface=stdin",
    ]
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
    global instance_name_global, port_global, session_global, quit_after_game

    if len(sys.argv) < 2:
        print(f"Usage: python3 {os.path.basename(__file__)} <instance_name> [port] [session]")
        sys.exit(1)

    instance_name = sys.argv[1]
    instance_name_global = instance_name
    config = load_instance(instance_name)
    
    port_global = int(sys.argv[2]) if len(sys.argv) > 2 else find_available_port(2100)
    session_global = sys.argv[3] if len(sys.argv) > 3 else f"WZ_{instance_name}_{port_global}"

    # Override map name if provided as an arg? Or we just read from config.
    # Actually, spawn_new_instance sets the map name by temporarily writing to instances.json?
    # No, we can pass map name as the 4th argument!
    if len(sys.argv) > 4:
        config["map_name"] = sys.argv[4]

    log_path = os.path.join(SCRIPT_DIR, f"{session_global}.log")
    log_file_handle = open(log_path, "a", encoding="utf-8")

    greetings = config.get("greeting_lines", [])

    log("INFO", f"Instance      : {instance_name}")
    log("INFO", f"Session       : {session_global}")
    log("INFO", f"Port          : {port_global}")
    log("INFO", f"Map           : {config.get('map_name')}")

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    cmd = build_command()
    log("INFO", "Command: " + " ".join(cmd))

    in_reader = threading.Thread(target=stdin_reader, daemon=True)
    in_reader.start()

    with roster_lock:
        roster.clear()

    with start_timeout_lock:
        game_started = False
        if start_timeout_timer is not None:
            start_timeout_timer.cancel()
            start_timeout_timer = None

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

    process.wait()

    with start_timeout_lock:
        if start_timeout_timer is not None:
            start_timeout_timer.cancel()
            start_timeout_timer = None

    log("INFO", f"Process exited (code {process.returncode}).")
    
    if not quit_after_game:
        # If it crashed and game didn't start, wait and exit so manager can restart it?
        # Actually, let it just exit. Manager can restart it, or we just spawn a new one.
        log("INFO", "Process crashed unexpectedly? Spawning new instance.")
        spawn_new_instance(instance_name_global, config.get("map_name", ""))

    log_file_handle.close()
    sys.exit(0)

if __name__ == "__main__":
    main()
