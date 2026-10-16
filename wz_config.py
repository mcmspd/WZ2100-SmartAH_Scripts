#!/usr/bin/env python3
"""
wz_config.py — Warzone 2100 per-instance config generation.

Standalone program AND library: `instance_bot.py` imports these helpers,
and operators can run it directly to pre-generate (and then customize)
autohost configs without starting any instance:

    python3 wz_config.py --validate                  # check maps/mods/config
    python3 wz_config.py --map Warwick               # generate configs for one map
    python3 wz_config.py --all                        # generate for every map

Autohost JSON resolution order per map:
  1. autohost/<MapName>.json if the operator created/c correlates it
     (used verbatim except instance identity, which is always patched in);
  2. otherwise built-in defaults are generated AND saved there as an
     editable starting point (existing files are never overwritten).

Stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import sys
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAPS_DIR = os.path.join(SCRIPT_DIR, "maps")
DEFAULT_MODS_DIR = os.path.join(SCRIPT_DIR, "mods")
DEFAULT_AUTOHOST_DIR = os.path.join(SCRIPT_DIR, "autohost")
DEFAULT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "instances.json")


def _default_log(tag: str, msg: str):
    print(f"[{tag}] {msg}")


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def is_safe_mod_name(name: str) -> bool:
    """Reject path traversal / shell metacharacters in mod names."""
    return bool(name) and bool(SAFE_NAME_RE.fullmatch(name)) and "/" not in name and name not in (".", "..")


def get_map_entry(entries, map_name: str) -> dict | None:
    """Look up a configured map entry {name, file, mods, survival, ...}."""
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name") == map_name:
            mods = entry.get("mods", [])
            mods = list(mods) if isinstance(mods, list) else []
            filename = entry.get("file") or f"{map_name}.wz"
            return {"name": map_name, "file": filename, "mods": mods,
                    "survival": bool(entry.get("survival", False)),
                    "survival_ai_js": entry.get("survival_ai_js", "SurvivalMod.js")}
    return None


def is_survival_map(entries, map_name: str, mods: list) -> tuple[bool, str]:
    """Survival layout if flagged or the Survival mod is loaded.

    Returns (survival, ai_js).
    """
    entry = get_map_entry(entries, map_name)
    if entry and entry.get("survival"):
        return True, entry.get("survival_ai_js", "SurvivalMod.js")
    if "Survival_Mod" in (mods or []):
        ai_js = entry.get("survival_ai_js", "SurvivalMod.js") if entry else "SurvivalMod.js"
        return True, ai_js
    return False, ""


def get_autostart_players(entries, map_name: str, mods: list) -> int:
    """Minimum humans to auto-start (`--startplayers`). Explicit
    `autostart_players` wins; survival maps default to 2, else off (0)."""
    entry = None
    for e in entries or []:
        if isinstance(e, dict) and e.get("name") == map_name:
            entry = e
            break
    if entry and isinstance(entry.get("autostart_players"), int):
        return max(0, entry["autostart_players"])
    survival, _ = is_survival_map(entries, map_name, mods if mods is not None else (entry.get("mods", []) if entry else []))
    return 2 if survival else 0


def resolve_map_file(entries, map_name: str) -> str:
    """Archive filename for a map (explicit `file` field or `<name>.wz`)."""
    entry = get_map_entry(entries, map_name)
    if entry:
        return entry["file"]
    return f"{map_name}.wz"


def resolve_mod_source(mods_dir: str, mod: str) -> tuple[str, str] | None:
    """Locate a mod as (kind, path): kind is 'file' (<mod>.wz) or 'dir' (<mod>/)."""
    wz_path = os.path.join(mods_dir, f"{mod}.wz")
    if os.path.isfile(wz_path):
        return ("file", wz_path)
    dir_path = os.path.join(mods_dir, mod)
    if os.path.isdir(dir_path):
        return ("dir", dir_path)
    return None


def validate_config(entries, maps_dir: str, mods_dir: str, default_map: str | None) -> list[str]:
    """Check maps/mods referenced by config exist on disk. Returns error list."""
    errors = []
    if entries is not None:
        for entry in entries or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            mods = entry.get("mods", []) if isinstance(entry, dict) else []
            if not name:
                errors.append("maps entry without a name")
                continue
            filename = entry.get("file", f"{name}.wz") if isinstance(entry, dict) else f"{name}.wz"
            if "/" in filename or not os.path.exists(os.path.join(maps_dir, filename)):
                errors.append(f"map missing on disk: {filename}")
            for mod in mods or []:
                if not is_safe_mod_name(mod):
                    errors.append(f"unsafe mod name for map {name}: {mod!r}")
                elif resolve_mod_source(mods_dir, mod) is None:
                    errors.append(f"mod missing on disk for map {name}: {mod} (.wz or /)")
    if default_map and entries is not None and get_map_entry(entries, default_map) is None:
        errors.append(f"default map_name {default_map!r} not present in maps list")
    return errors


def extract_map_info(maps_dir: str, map_name: str, map_file: str | None = None,
                     log=_default_log) -> dict | None:
    """Read player count from a map archive (new level.json or old .lev format)."""
    filename = map_file or f"{map_name}.wz"
    wz_path = os.path.join(maps_dir, filename)
    if not os.path.exists(wz_path):
        return None
    try:
        with zipfile.ZipFile(wz_path, 'r') as z:
            names = z.namelist()
            if 'level.json' in names:
                with z.open('level.json') as f:
                    level_data = json.load(f)
                    return {
                        "name": map_name,
                        "players": level_data.get("players", 2)
                    }
            # Old format: parse "players N" from each .lev file, take the max.
            best = 0
            for n in names:
                if n.endswith('.lev'):
                    with z.open(n) as f:
                        text = f.read().decode("utf-8", errors="replace")
                    for m in re.finditer(r"^\s*players\s+(\d+)", text, re.MULTILINE):
                        best = max(best, int(m.group(1)))
            if best > 0:
                return {"name": map_name, "players": best}
            log("WARN", f"No player count found in {filename}")
            return None
    except Exception as e:
        log("WARN", f"Failed to extract map info: {e}")
        return None


def zip_mod_dir(source_dir: str, target_wz: str):
    """Package a mod directory into a deterministic .wz (stable hash)."""
    with zipfile.ZipFile(target_wz, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            for fname in sorted(files):
                full = os.path.join(root, fname)
                arc = os.path.relpath(full, source_dir)
                info = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as f:
                    z.writestr(info, f.read())


def stage_mods(mods_dir: str, configdir: str, staged: list, mods_version: str = "4.7.0",
               log=_default_log) -> bool:
    """Copy/file or zip/dir staged mods into versioned + unversioned multiplay dirs.

    WZ resolves `--mod_mp` names as regular files under the *versioned*
    `mods/<tag>/multiplay` dir (see upstream main.cpp mod check), so each mod
    lands as `<mod>.wz` in both versioned and unversioned locations.
    `staged` items are (mod, kind, source) with kind in {"file", "dir"}.
    """
    tag = str(mods_version or "4.7.0")
    target_dirs = [
        os.path.join(configdir, "mods", tag, "multiplay"),
        os.path.join(configdir, "mods", "multiplay"),
    ]
    for target_mods_dir in target_dirs:
        os.makedirs(target_mods_dir, exist_ok=True)
        for mod, kind, source in staged:
            target = os.path.join(target_mods_dir, f"{mod}.wz")
            try:
                if kind == "file":
                    shutil.copy2(source, target)
                else:
                    if os.path.exists(target):
                        os.remove(target)
                    zip_mod_dir(source, target)
            except Exception as e:
                log("WARN", f"Failed to stage mod {mod}: {e}")
                return False
    return True


def load_autohost_template(autohost_dir: str, map_name: str, log=_default_log) -> dict | None:
    """Return the operator-customized autohost dict for a map, or None.

    Templates live in <autohost_dir>/<MapName>.json. Anything the operator
    puts there is used verbatim except instance identity (challenge map /
    maxPlayers / name / password), which is always patched in.
    """
    if not map_name or "/" in map_name or map_name in (".", ".."):
        return None
    path = os.path.join(autohost_dir, f"{map_name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            log("INFO", f"Using customized autohost template {path}")
            return data
        log("WARN", f"Ignoring autohost template {path}: not a JSON object")
    except FileNotFoundError:
        pass
    except Exception as e:
        log("WARN", f"Ignoring invalid autohost template {path}: {e}")
    return None


def save_autohost_template(autohost_dir: str, map_name: str, ah_data: dict,
                           log=_default_log):
    """Persist a generated default as an editable template (never overwrites)."""
    if not map_name or "/" in map_name or map_name in (".", ".."):
        return
    try:
        os.makedirs(autohost_dir, exist_ok=True)
        path = os.path.join(autohost_dir, f"{map_name}.json")
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ah_data, f, indent=2)
            f.write("\n")
        log("INFO", f"Saved default autohost template {path} (customize freely).")
    except Exception as e:
        log("WARN", f"Could not save autohost template: {e}")


def default_autohost_teams(ah_data: dict, players: int):
    """Fill player_N team slots when a template defines none."""
    if any(k.startswith("player_") for k in ah_data):
        return
    for i in range(players):
        team = i // max(1, (players // 2))
        ah_data[f"player_{i}"] = {"team": team}


def generate_configs(maps_dir: str, mods_dir: str, autohost_dir: str,
                     map_name: str, maps_entries,
                     configdir: str, host_name: str, game_name: str,
                     desync_kick_seconds: int = 30,
                     lag_kick_seconds: int = 30,
                     not_ready_kick_seconds: int = 30,
                     game_password: str = "",
                     host_key: str = "",
                     mods: list | None = None,
                     mods_version: str = "4.7.0",
                     log=_default_log):
    """Generate a full per-instance WZ config dir. Returns autohost name or False."""
    entry = get_map_entry(maps_entries, map_name)
    map_file = (entry["file"] if entry and entry.get("file")
                else f"{map_name}.wz")
    info = extract_map_info(maps_dir, map_name, map_file, log)
    if not info:
        return False
    mods = list(mods or [])
    staged = []
    for mod in mods:
        if not is_safe_mod_name(mod):
            log("WARN", f"Refusing to stage unsafe mod name: {mod!r}")
            return False
        found = resolve_mod_source(mods_dir, mod)
        if found is None:
            log("WARN", f"Mod missing on disk, aborting config generation: {mod}")
            return False
        staged.append((mod,) + found)

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

    # Autohost config: operator template wins; otherwise generate defaults
    # (and persist them as an editable template for next time).
    ah_dir = os.path.join(configdir, "autohost")
    os.makedirs(ah_dir, exist_ok=True)
    ah_name = f"AH_{map_name}"
    ah_path = os.path.join(ah_dir, ah_name)

    ah_data = load_autohost_template(autohost_dir, map_name, log)
    if ah_data is None:
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
        survival, survival_ai_js = is_survival_map(maps_entries, map_name, mods)
        if survival:
            # Survival lobbies must be startable (and auto-startable) with open
            # slots: stock autohost locks ready-behind-full, which would force a
            # full house before anyone can even check Ready.
            ah_data["locked"]["readybeforefull"] = False

        # Generate teams. Survival maps: slot 0 is Team A, occupied by the
        # Survival AI; every other slot is open Team B for humans. All other
        # maps keep the default 2-team split with no preassigned AI.
        if survival:
            log("INFO", f"Survival layout for {map_name}: slot 0 = Team A AI ({survival_ai_js}), "
                        f"slots 1-{players - 1} = open Team B")
            ah_data["player_0"] = {"team": 0, "ai": survival_ai_js}
            for i in range(1, players):
                ah_data[f"player_{i}"] = {"team": 1}
        else:
            default_autohost_teams(ah_data, players)
        save_autohost_template(autohost_dir, map_name, ah_data, log)

    # Instance identity: always patched in, even over operator templates.
    challenge = ah_data.setdefault("challenge", {})
    challenge["map"] = map_name
    challenge["maxPlayers"] = players
    challenge["name"] = game_name
    if game_password:
        challenge["gamePassword"] = game_password
    else:
        challenge.pop("gamePassword", None)
    default_autohost_teams(ah_data, players)

    with open(ah_path, "w") as f:
        json.dump(ah_data, f, indent=2)

    # Generate players dir and host sta2 identity if provided
    players_dir = os.path.join(configdir, "multiplay", "players")
    os.makedirs(players_dir, exist_ok=True)
    if host_key and host_name:
        sta2_path = os.path.join(players_dir, f"{host_name}.sta2")
        with open(sta2_path, "w", encoding="utf-8") as f:
            f.write(f"WZ.STA.v3\n0 0 0 0 0\n{host_key.strip()}\n")

    # Copy map file into config maps directory (keep its real filename:
    # hash-named downloads will not match <name>.wz)
    target_maps_dir = os.path.join(configdir, "maps")
    os.makedirs(target_maps_dir, exist_ok=True)
    source_map_path = os.path.join(maps_dir, map_file)
    target_map_path = os.path.join(target_maps_dir, map_file)
    if os.path.exists(source_map_path):
        shutil.copy2(source_map_path, target_map_path)
    else:
        log("WARN", f"Map file missing, aborting config generation: {map_file}")
        return False

    # Stage per-map mods (versioned + unversioned multiplay dirs).
    if staged:
        if not stage_mods(mods_dir, configdir, staged, mods_version, log):
            return False
        log("INFO", f"Staged {len(staged)} mod(s) for map {map_name}: {', '.join(mods)}")

    return ah_name


def _load_instances_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate/validate Warzone 2100 instance configs")
    ap.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    ap.add_argument("--maps-dir", default=DEFAULT_MAPS_DIR)
    ap.add_argument("--mods-dir", default=DEFAULT_MODS_DIR)
    ap.add_argument("--autohost-dir", default=DEFAULT_AUTOHOST_DIR)
    ap.add_argument("--out", default=None,
                    help="configdir root for --map/--all (default: <tmp>/wzgen)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", action="store_true")
    group.add_argument("--map", default=None)
    group.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)

    try:
        config = _load_instances_config(args.config)
    except Exception as e:
        print(f"ERROR: cannot load {args.config}: {e}", file=sys.stderr)
        return 2

    entries = config.get("maps")
    errors = validate_config(entries, args.maps_dir, args.mods_dir,
                             config.get("map_name"))
    if errors:
        print("Config problems:")
        for e in errors:
            print(f"  - {e}")
        if args.validate:
            return 1
    elif args.validate:
        print("Config OK.")
        return 0

    import tempfile
    out_root = args.out or tempfile.mkdtemp(prefix="wzgen_")
    targets = [e["name"] for e in (entries or []) if isinstance(e, dict) and e.get("name")] \
        if args.all else [args.map]
    rc = 0
    for name in targets:
        entry = get_map_entry(entries, name) or {"name": name, "mods": []}
        mods = entry.get("mods", [])
        ah = generate_configs(
            args.maps_dir, args.mods_dir, args.autohost_dir,
            name, entries, os.path.join(out_root, name),
            config.get("host_name", "Host"), config.get("game_name", "Game"),
            config.get("host_auto_desync_kick_seconds", 30),
            config.get("host_auto_lag_kick_seconds", 30),
            config.get("host_auto_not_ready_kick_seconds", 30),
            config.get("game_password", ""),
            config.get("host_key", ""),
            mods=mods,
            mods_version=config.get("wz_mods_version", "4.7.0"),
            log=_default_log)
        if not ah:
            print(f"FAILED: {name}")
            rc = 1
        else:
            print(f"OK: {name} -> {os.path.join(out_root, name)} ({ah})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
