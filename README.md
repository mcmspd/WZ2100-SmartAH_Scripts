# WZ2100-SmartAH_Scripts

Per-instance Warzone 2100 autohost bot. Each `instance_bot.py` process spawns one
headless `warzone2100` game, watches its `WZEVENT`/`WZCHAT*`/`__WZROOMSTATUS__`
output, and reacts (greetings, vote-kick, map changes, timeout migration).

## Quick start — zero-state deployment

```bash
git clone <your-repo-url> ~/WZ2100-SmartAH_Scripts
cd ~/WZ2100-SmartAH_Scripts
sudo ./deploy.sh               # installs deps, opens ports, starts proxy + instance
```

This is idempotent — safe to re-run. Then verify:

```bash
sudo ./deploy.sh status
```

You should see `proxy: UP`, `redirect: ACTIVE`, and the instance `status: ready`.
Your lobby should now appear publicly on `wzlobby.wz2100.net`.

## Server control

```bash
sudo ./deploy.sh deploy            # full zero-state deployment (idempotent)
sudo ./deploy.sh start [map_name]  # start (or restart) the game instance
sudo ./deploy.sh stop              # stop the instance (proxy+firewall stay up)
sudo ./deploy.sh status            # check proxy, firewall, instance state
```

Or manually:

```bash
tmux new-session -d -s WZ_2100_<stamp> \
  'python3 instance_bot.py 2100 WZ_2100_<stamp> NTW_3v3Full'
```

## Architecture — two UIDs (required)

The lobby MITM proxy and the game must run as **different Linux users**.
If they shared a UID, the proxy's own upstream HTTPS connections would loop
back into itself (fork bomb → EMFILE).

| Component | Runs as | Listens on |
|-----------|---------|------------|
| MITM proxy + game relay | `ubuntu` (who invoked `sudo`) | 127.0.0.1:18080 (proxy), 0.0.0.0:2200 (relay) |
| Game instance (`instance_bot.py` + `warzone2100.real`) | `wzbot` | gameport 2100–2110 (inbound from players) |

`deploy.sh` creates `wzbot` and sets up the iptables redirect that steers
`wzbot`'s outbound port 443 through the proxy (`lobby_off.sh`). Player game
traffic is never redirected.

## Layout

| Path | Purpose |
|------|---------|
| `instance_bot.py` | Bot entrypoint: `python3 instance_bot.py [port] [session] [map] [mods_csv] [suffix]` |
| `wz_config.py` | Config generation library + CLI (`--validate`, `--map`, `--all`) |
| `autohost/` | Per-map autohost JSON (`<MapName>.json`) |
| `maps/` | Map `.wz` files referenced by name from `instances.json` |
| `mods/` | Mod `.wz` files referenced per-map from `instances.json` |
| `instance_configs/` | Per-session status files + logs (writable by `wzbot`) |
| `deploy.sh` / `host.sh` | Zero-state deploy + server control |
| `lobby_mitm.py` | Transparent proxy: token capture + loopback game relay |
| `lobby_off.sh` / `lobby_on.sh` | Add/remove the UID-based 443 redirect rule |

## Configure `instances.json`

Create/edit `instances.json` before first start. `deploy.sh` generates a template
if missing but never overwrites an existing file (preserves secrets).

```json
{
  "host_name": "FreeHost",
  "host_key": "REPLACE_WITH_YOUR_B64_PUBLIC_KEY",
  "game_name": "FreeHost",
  "map_name": "NTW_3v3Full",
  "lobby_address": "https://wzlobby.wz2100.net/lobby",
  "game_password": "",
  "maps": [
    {"name": "NTW_3v3Full", "mods": []},
    {"name": "Warwick", "mods": ["More_Fort", "Survival_Mod"], "survival": true, "autostart_players": 1}
  ],
  "wz_backend": "system",
  "wz_binary": "/usr/games/warzone2100.real",
  "wz_debug_flags": ["NET", "LOBBY"],
  "max_live_sessions": 3,
  "authorized_pkeys": ["<b64-pubkey>", ...]
}
```

- **`host_key`** / **`authorized_pkeys`** — identity & admin gating. Keep this file
  off git (it contains secrets).
- **`wz_backend=system`** + **`wz_binary=/usr/games/warzone2100.real`** — required
  for MITM (flatpak ignores the host CA store).
- Every map/mod listed must exist on disk, or the bot refuses to start.

## Maps & mods

Place map files in `maps/<Name>.wz` and mods in `mods/<Mod>.wz`. They are
referenced by name from `instances.json`. Unlisted `.wz` files are hidden from
`/maps`.

Generate/validate autohost templates:

```bash
python3 wz_config.py --validate
```

## Lobby chat commands

- `/maps` — list configured maps (with mod annotations)
- `/maps <n>` — migrate to map #n (map admins only)
- `/mapsadmin list\|add <slot>\|remove <n>` — manage map admins
- `/votekick <slot>`, `/vote y\|n` — unanimous vote-kick (3+ players)
- `/report` — CPU/RAM, address, session time remaining

## WZ launch flags

Configurable via `instances.json`:
`wz_headless`, `wz_nosound`, `wz_debug_flags`, `wz_host_connection_provider`,
`wz_portmapping`, `wz_async_join_approve`, `wz_enable_lobby_slashcmd`,
`wz_extra_args`, `wz_backend: auto\|flatpak\|system`, `wz_binary`.

Mods load via `--mod_mp` from `<configdir>/mods/multiplay/`, staged automatically
from `mods/`.

## Lobby MITM (token capture + Python unlist)

`lobby_mitm.py` is a stdlib-only transparent proxy that captures the lobby
`hostToken` so Python can `DELETE` a listing while the host keeps running:

1. First proxy run generates `mitm-ca/` — install `mitm-ca/ca.crt` into the
   system trust store.
2. Game runs as `wzbot`, proxy runs as `ubuntu` — **UID separation is required**.
3. `sudo ./lobby_off.sh wzbot 18080` steers `wzbot`'s port 443 into the proxy;
   `sudo ./lobby_on.sh wzbot` restores direct access.
4. Tokens land in `instance_configs/.lobby_tokens.json`; `unlist_self_via_api()`
   uses them.

flatpak WZ ignores the host CA store — MITM'd instances must use the system
binary (`wz_backend=system`, `wz_binary=/usr/games/warzone2100.real`).

## Tests

```bash
python3 -m py_compile instance_bot.py test_instance_bot.py test_lobby_mitm.py
python3 -m unittest test_instance_bot test_lobby_mitm
```

## Safety rules

- **One listing per public IP** — never start a second lobby while one is listed.
  `deploy.sh start` refuses if any `WZ_*` session exists.
- Never `tmux kill-session` a live game with players — migrate with `/maps <n>`
  instead (redirect → drain → old self-shuts-down).
- `instances.json` contains secrets — keep the repo private or accept
  impersonation (host_key is intentionally public per operator).

See `AGENTS.md` for architecture notes, the stock-vs-fork protocol analysis, and
remaining work.