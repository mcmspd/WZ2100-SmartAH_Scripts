# WZ2100-SmartAH_Scripts

Per-instance Warzone 2100 autohost bot. Each `instance_bot.py` process spawns one
headless `warzone2100` game, watches its `WZEVENT`/`WZCHAT*`/`__WZROOMSTATUS__`
output, and reacts (greetings, vote-kick, map changes, timeout migration).

## Layout

- `instance_bot.py` — the bot (run: `python3 instance_bot.py [port] [session] [map] [mods_csv]`).
- `instances.json` — config: host identity, maps + per-map mods, WZ launch flags, caps.
- `maps/` — map `.wz` files referenced by name from `instances.json`.
- `mods/` — mod `.wz` files referenced by name from per-map `mods` lists.
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
`wz_async_join_approve`, `wz_enable_lobby_slashcmd`, `wz_extra_args`).
Mods load via `--mod_mp` from `<configdir>/mods/multiplay/`, staged
automatically from `mods/`.

## Tests

```bash
python3 -m py_compile instance_bot.py
python3 -m unittest test_instance_bot
```

See `AGENTS.md` for architecture notes, the stock-vs-fork protocol analysis,
and remaining work.
