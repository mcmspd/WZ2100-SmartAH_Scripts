"""Bisect lobby game-name validation with sacrificial listings.

Usage: python3 nametest.py "Name A" "Name B" ...
Spaced ≥3 min apart by default (NAMETEST_PAUSE); each LISTED name is
immediately DELETE'd so the one-per-IP slot stays free.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
os.chdir("/tmp")

import wz_config as wz

import instance_bot as ib

ib.config = {"host_name": "FreeHost", "game_name": "FreeHost",
             "host_auto_desync_kick_seconds": 60,
             "host_auto_lag_kick_seconds": 60,
             "host_auto_not_ready_kick_seconds": 30,
             "game_password": "", "host_key": "",
             "wz_headless": True, "wz_nosound": True, "wz_debug_flags": [],
             "wz_host_connection_provider": "tcp", "wz_portmapping": False,
             "wz_async_join_approve": False, "wz_enable_lobby_slashcmd": False,
             "wz_extra_args": []}
ib.current_map_mods = []
ib.port_global = 2110

MAPS_DIR = os.path.join(SCRIPT_DIR, "maps")
MODS_DIR = os.path.join(SCRIPT_DIR, "mods")
AUTOHOST_DIR = os.path.join(SCRIPT_DIR, "autohost")
MAP_UNDER_TEST = os.environ.get("NAMETEST_MAP", "NTW_3v3Full")
TOKEN_STORE = os.environ.get(
    "NAMETEST_TOKEN_STORE",
    os.path.join(SCRIPT_DIR, "instance_configs", ".lobby_tokens.json"))
WZ_BINARY = os.environ.get("NAMETEST_WZ_BINARY", "/usr/games/warzone2100.real")


def try_name(name, port):
    workdir = tempfile.mkdtemp(prefix="nametest_")
    configdir = os.path.join(workdir, "cfg")
    ib.port_global = port
    entries = [{"name": MAP_UNDER_TEST, "mods": []}]
    ah = wz.generate_configs(
        MAPS_DIR, MODS_DIR, AUTOHOST_DIR,
        MAP_UNDER_TEST, entries, configdir,
        "FreeHost", name,
        60, 60, 30, "", "",
        mods=[], mods_version="4.7.0")
    assert ah, "generate failed"
    cmd = [WZ_BINARY, "--headless", "--nosound",
           f"--configdir={configdir}", f"--autohost={ah}",
           f"--gameport={port}", "--enablecmdinterface=stdin"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         bufsize=1)
    got_id, got_err = None, None
    t0 = time.time()
    full_log = []
    try:
        # readline per line: WZ flushes its event lines promptly on pipes.
        # select() guards against silent stretches past the deadline.
        import select as _select
        while time.time() - t0 < 30 and p.poll() is None:
            r, _, _ = _select.select([p.stdout], [], [], 5)
            if not r:
                continue
            line = p.stdout.readline().decode("utf-8", errors="replace")
            if not line:
                break
            full_log.append(line)
            if "WZEVENT: lobbyid:" in line:
                got_id = line.split(":", 2)[-1].strip()
            elif "WZEVENT: lobbyerror" in line:
                got_err = line.strip()
            if got_id or got_err:
                break
    finally:
        try:
            p.kill()
        except Exception:
            pass
        p.wait()
    with open(os.path.join(logdir, re.sub(r"[^A-Za-z0-9_-]", "_", name) + ".log"), "w") as f:
        f.writelines(full_log)
    shutil.rmtree(workdir, ignore_errors=True)
    return got_id, got_err


results = {}
port = int(os.environ.get("NAMETEST_PORT_START", "2100"))
pause = float(os.environ.get("NAMETEST_PAUSE", "180"))
logdir = os.environ.get("NAMETEST_LOGDIR", "/tmp/nametest-logs")
os.makedirs(logdir, exist_ok=True)
first = True
for name in sys.argv[1:]:
    if not first:
        print(f"cooling down {pause:.0f}s before {name!r}...", flush=True)
        time.sleep(pause)
    first = False
    if port > 2110:
        print(f"SKIP {name!r}: out of open port range", flush=True)
        results[name] = ("SKIPPED", "no port")
        continue
    gid, err = try_name(name, port)
    port += 1
    if gid:
        results[name] = ("LISTED", gid)
        # free the slot immediately via captured token
        time.sleep(2)
        import lobby_mitm as lm
        ok, detail = lm.delete_listing_by_game_id(
            gid, TOKEN_STORE,
            "https://wzlobby.wz2100.net/lobby")
        results[name] = ("LISTED then " + ("deleted" if ok else f"DELETE-FAILED:{detail}"), gid)
        time.sleep(3)
    else:
        results[name] = ("REJECTED", (err or "")[:120])
    print(f"{name!r}: {results[name][0]} {results[name][1][:60]}", flush=True)
