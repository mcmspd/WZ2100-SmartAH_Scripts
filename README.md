# WZ2100-SmartAH_Scripts

Per-instance Warzone 2100 autohost bot. Each `instance_bot.py` process spawns one
headless `warzone2100` game, watches its `WZEVENT`/`WZCHAT*`/`__WZROOMSTATUS__`
output, and reacts (greetings, vote-kick, map changes, timeout migration).

## Layout

- `instance_bot.py` — the bot (run: `python3 instance_bot.py [port] [session] [map] [mods_csv] [suffix]`).
- `wz_config.py` — config generation (library + CLI). `python3 wz_config.py --validate` checks maps/mods; `--map NAME` / `--all` pre-generates instance configs.
- `autohost/` — pre-generated autohost JSON per map (`<MapName>.json`). Edit freely: the bot uses your file verbatim except instance identity (`challenge` map/maxPlayers/name/password), and fills team slots only if you define none. Missing file = bot generates + saves defaults here.
- `maps/` — map `.wz` files referenced by name from `instances.json`.
- `mods/` — mod `.wz` files or source directories, referenced by name from per-map `mods` lists.
- `test_instance_bot.py` — stdlib unit tests (`python3 -m unittest test_instance_bot`).
- `open_ports.sh` / `install_dep.sh` — firewall (TCP 2100–2110) and dependency setup.

## Setup

```bash
./install_dep.sh
sudo ./open_ports.sh
```

Place map files in `maps/<Name>.wz`, mod files in `mods/<Mod>.wz`, then edit
`instances.json`:

```json
{
  "map_name": "NTW_3v3Full",
  "maps": [
    {"name": "NTW_3v3Full", "mods": []},
    {"name": "SomeMap", "mods": ["BalanceMod", "EyeCandy"]}
  ]
}
```

The bot validates at startup that every listed map/mod exists on disk and
refuses to start otherwise. Unlisted `.wz` files are hidden from `/maps`.

## Running

One tmux session per instance (session names are unique: `WZ_<port>_<stamp>`):

```bash
tmux new-session -d -s WZ_2100 'python3 instance_bot.py 2100 WZ_2100 NTW_3v3Full'
```

Migration (timeout/map-change) spawns successors automatically, capped at
`max_live_sessions` live sessions.

## Lobby chat commands

- `/maps` — list configured maps (with mod annotations). `/maps <n>` — migrate (map admins only).
- `/mapsadmin list|add <slot>|remove <n>` — manage map admins (map admins only).
- `/votekick <slot>`, `/vote <y/n>` — unanimous vote-kick (3+ players).
- `/report` — CPU/RAM, address, session time remaining.

## WZ launch flags

Configurable via `instances.json` (`wz_headless`, `wz_nosound`,
`wz_debug_flags`, `wz_host_connection_provider`, `wz_portmapping`,
`wz_async_join_approve`, `wz_enable_lobby_slashcmd`, `wz_extra_args`,
`wz_backend: auto|flatpak|system`, `wz_binary` path override).
Mods load via `--mod_mp` from `<configdir>/mods/multiplay/`, staged
automatically from `mods/`.

## Lobby MITM (token capture + Python unlist)

`lobby_mitm.py` is a stdlib-only transparent proxy that captures the lobby
`hostToken` so Python can `DELETE` a listing while the host keeps running:

1. One-time: running the proxy generates `mitm-ca/`; install
   `mitm-ca/ca.crt` into the system trust store.
2. Run WZ instances as a dedicated user (e.g. `wzbot`); run the proxy as
   another user — otherwise the proxy's own upstream loops back into
   itself.
3. `sudo ./lobby_off.sh <wz-user> [port]` steers that UID's port 443 into
   the proxy; `sudo ./lobby_on.sh` restores direct access.
4. Tokens land in `instance_configs/.lobby_tokens.json` (0640, shared group
   so bot and proxy users both keep access across atomic replaces);
   `unlist_self_via_api()` / admin `/unlobby` uses them.

Constraints learned live: flatpak WZ ignores the host CA store — MITM'd
instances must use the system binary (`wz_backend=system`,
`wz_binary=/usr/games/warzone2100.real`). The real API base carries the
`/lobby` prefix (`.../lobby/api/v1/game`); the bot reads it from the WZ
`lobbyserver=` config so both always agree.

## Tests

```bash
python3 -m py_compile instance_bot.py
python3 -m unittest test_instance_bot
```

See `AGENTS.md` for architecture notes, the stock-vs-fork protocol analysis,
and remaining work.
