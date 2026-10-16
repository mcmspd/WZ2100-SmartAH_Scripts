# AGENTS.md — WZ2100-SmartAH_Scripts

## Project
Per-instance Warzone 2100 autohost bot. Single file `instance_bot.py` (~1605 lines) spawns one headless `warzone2100` process per tmux session, monitors `WZEVENT`/`WZCHAT*`/`__WZROOMSTATUS__`, handles greetings, `/maps`, `/mapsadmin`, `/votekick`, `/vote`, `/report`, 90-min timeout and map-change migration.

- Entrypoint: `instance_bot.py:1461` `main()` — args `[port] [session_name] [map_name]`.
- Config: `instances.json` (flat, no schema validation). `host_key` is intentionally public per operator — impersonation accepted.
- `sender_pk` from `WZCHATLOB` is trusted — bound to private-key ownership, not spoofable without key sharing.
- Status IPC: `instance_configs/<session>.status.json` via `write_session_status` (`instance_bot.py:90`, atomic tmp+replace) / `wait_for_new_instance_ready` (`instance_bot.py:136`).
- Roster: rebuilt from `__WZROOMSTATUS__` in `update_roster_from_status` (`instance_bot.py:424`).
- Redirect: `redirect_all_players` (`instance_bot.py:303`) sends `redirect identity <pk> tcp:<port>:<0|1>[:password]`.
- Stock WZ stdin commands used: see `is_valid_wz_command` (`instance_bot.py:524`) and upstream `src/stdinreader.cpp`.

## Stock Warzone 2100 behavior (do not fork)
- `redirect identity <pk|hash> <tcp|gns>:<port>:<0|1>[:password]` is stock, lobby-only. Confirmed in upstream `src/stdinreader.cpp` (`parseCmdInterfaceRedirectStringToRedirectInfo`, `redirectActivePlayerWithIdentity`). 4th field is game password — valid.
- `join approve|reject|approvespec <joinID>` is stock. `reject` with `ERROR_REDIRECT` + redirect-string does join-time redirect (stock).
- `status` → `__WZROOMSTATUS__`, `chat bcast|direct`, `kick identity`, `set host ready`, `shutdown now`, `--async-join-approve`, `--enablecmdinterface=stdin` are all stock.
- `lobby off` / `lobby renew`, `WZEVENT: readyForJoins`, synthetic `WZEVENT: redirect:` are **fork-only** (retropaganda). Do not rely on them.
- Lobby traffic is separate from game traffic:
  - Lobby: outbound IPv4 HTTPS to `wzlobby.wz2100.net`, `POST /api/v1/game`, `PATCH /api/v1/game/<gameId>` keepalive every ≤30s (`SERVER_UPDATE_MAX_INTERVAL`), abandon after 120s (`SERVER_UPDATE_NO_SUCCESS_ABANDON_INTERVAL`), `DELETE /api/v1/game/<gameId>` on shutdown. Headers `Origin: wz2100://wz2100.net`, `Authorization: HostToken ...`, `noProxy=true`. See `lib/netplay/netlobby.cpp` (`sendListingUpdateImpl`, `LobbyDeleteGameImpl`, `run()`).
  - Game: inbound TCP `gameport` 2100-2110, direct player sockets.
  - On lobby loss stock only fires `lobbyGameGoneCallback` (`lib/netplay/netplay.cpp:3612`): status message + `WZEVENT: lobbyerror`. No kick, no shutdown. Safe to starve lobby while players stay connected.

## Reference: retropaganda.info/host.sh
- Native `warzone2100.install-4.7.0.x` with `--host-connection-provider=tcp --portmapping=false --enablelobbyslashcmd --enablecmdinterface=stdin --async-join-approve`.
- `/map` writes `semaphores/$autohost.maps`, sends `lobby off`. Old host stays alive.
- On `WZEVENT: lobby off`, old host symlinks `semaphores/$autohost.redirect -> /proc/$BASHPID/fd/0` (mailbox into its own stdin).
- New host on `WZEVENT: readyForJoins: <proto> <port> <addr>` appends `WZEVENT: redirect: <proto> <port>` into that mailbox.
- Old host on synthetic `WZEVENT: redirect:` iterates fresh `room_status` (`jq '.players[]->{pk,0}, .specs[]->{pk,1}'`), emits `redirect identity <pk> <proto>:<port>:<0|1>` for all `pk != null`, `sleep 5s`, `shutdown now`.

## Why our redirect fails
1. Wrong readiness gate: we wait for `lobbyid:` (`WZEVENT: lobbyid:`) via status file. Listed ≠ joinable. Must wait for `WZCMD: stdinReadReady` + first `__WZROOMSTATUS__` with `data.state=="lobby"` + `data.lobbyid`, plus TCP probe to new port.
2. Assumed port: `find_available_port` (`instance_bot.py:159`, TOCTOU `connect_ex`) + hardcoded `tcp:{port}`. Never uses new host's actual bound port. Parse and propagate it via status file.
3. Stale roster: `send_cmd("status"); sleep(0.5); redirect_all_players()` (`instance_bot.py:998`). Update is async — often misses. Wait for next `on_room_status` (`instance_bot.py:1167`) event instead.
4. Drain too short: `sleep 2` then `shutdown now`. Reference uses `sleep 5s # needed?`. Use 5s.
5. Over-filtering: `redirect_all_players` skips specs with `ip in None/-/""` and host by name (`instance_bot.py:314`). May drop real clients. Reference redirects all `pk != null`.
6. Missing flags / flatpak net: `build_command` (`instance_bot.py:1388`) lacks `--host-connection-provider=tcp --async-join-approve`. Verify flatpak `gameport` is reachable on public IP (see `open_ports.sh:12`, which hardcodes `iptables -I INPUT 5` and duplicates on re-run).
7. One-instance-per-IP: lobby rejects second `POST /api/v1/game` from same IP (`RATE_LIMITED`/`GAME_STALE`/`HOST_NOT_ACCESSIBLE`). Stock new instance will not auto-retry after failure — must (re)start it after old listing is gone. Overlapping listings without a second IP always fail.

## Stock-only migration (no WZ fork) — implemented except noted
1. Announce: `chat bcast` warning on old. Done.
2. Take old off-lobby without kicking players: documented options (`uid-owner`
   iptables / `LD_PRELOAD` flag / second IP). Tooling not yet built — open item.
3. Wait until old `gameId` disappears before new `POST`. Covered by
   `wait_for_new_instance_ready` + refusal to overlap beyond `max_live_sessions`.
4. New instance confirmed via readiness triple-gate + socket probe. Done.
5. `request_roster_refresh()` → mass redirect → `sleep 5` → old `shutdown now`. Done.
6. Join-time `ERROR_REDIRECT`: deferred; auto-approve (`on_join_approval`) implemented instead.

## Lobby doctrine (learned live — read before touching lobby flows)

- The lobby throttles aggressively (creates/deletes in quick succession).
  Rules: never rapid-retry POSTs; `RATE_LIMITED` stops all retries
  immediately (instance exits, spawner backs off 20 min); every test POST is
  followed by an immediate DELETE; name-bisect runs spaced ≥3 min apart.
- Game-name validation is server-side and picky: `INPUT_VALIDATION_FAILED`
  ("avoid special characters") rejected `(new)`/`[new]`-style suffixes even
  though live lobbies use brackets. Proven-safe: plain alphanumeric suffixes
  (`FreeHost`, `FreeHost 2`). Every new suffix must pass `is_lobby_safe_name`
  AND one spaced live POST before use; `nametest.py` is the tool for that.
- One listing per public IP: free the slot with an authenticated Python
  DELETE (captured hostToken) before any new POST; never overlap listings.

## Completed 2026-10-16 (verified: `py_compile`, 53 unittest, fix harness)
- Redirect readiness: triple-gate (`lobby_id` + `stdin_ready` + `room_state=="lobby"` + localhost port probe) via `maybe_signal_ready`/`check_lobby_ready`; startup polls `status` until `LOBBY_ID_TIMEOUT`.
- Actual bound port: `find_available_port` returns `None` when full; readiness probe confirms the child listens.
- Fresh roster: `roster_updated_event` + `request_roster_refresh()` in timeout/map migrations; chat commands run on a worker thread (`_handle_chat_cmd`) so waits never stall the reader.
- Drain `sleep 5`; redirect targets every `pk` (host/spec filters removed).
- Launch flags configurable (`wz_headless`, `wz_nosound`, `wz_debug_flags`, `wz_host_connection_provider`, `wz_portmapping`, `wz_async_join_approve`, `wz_enable_lobby_slashcmd`, `wz_extra_args`); `--async-join-approve` paired with auto-approve (`on_join_approval`).
- Spawn storm: game-start successor removed (parent survives; post-game re-arms timeout via room-state flip in `check_game_ended`); crash respawn gated by `min_uptime_to_respawn_seconds` + hourly cap (`record_restart_and_check_cap`); timeout migration has backoff + `MIGRATION_MAX_ATTEMPTS` give-up + `migration_lock` single-flight; spawn refused at `max_live_sessions` cap; unique `WZ_<port>_<stamp>` sessions, blind `kill-session` removed.
- Timeout 90 → 80 min (`START_TIMEOUT=4800`).
- Per-map mods: `mods/` mirrors `maps/`; `--mod_mp` staging into `<configdir>/mods/multiplay/`; name validation + fail-fast.
- Config-driven `"maps": [{name, mods}]` (dir scan is back-compat fallback); `/maps` annotates mods; spawn/timeout thread name+mods through.
- `save_authorized_pkeys` locked (`fcntl` + atomic replace); `get_public_ip` cached (`PUBLIC_IP_TTL`); size-based log rotation; `open_ports.sh` idempotent + `install_dep.sh` hardened; `README.md`; `test_instance_bot.py` (14 tests).

## Remaining polish
- Lobby blackhole: PROVEN live 2026-10-16 (stock WZ 4.7.0, real lobby).
  Token capture + Python `DELETE` + unlisted-but-running host all work.
  Operational requirements discovered: (a) flatpak WZ ignores the host CA
  store (cURL error 60) — MITM'd instances must use the apt binary
  (`wz_backend=system`, `wz_binary=/usr/games/warzone2100.real`; the distro
  bwrap wrapper is broken here); (b) proxy MUST run as a different UID than
  the steered traffic or its own upstream loops back (fork bomb → EMFILE);
  (c) real API base is `https://wzlobby.wz2100.net/lobby/api/v1/game`
  (note `/lobby` prefix from WZ `lobbyserver=` config). See `SERVER_HANDOFF.md §4c`.
- Slot-vs-pos: RESOLVED from upstream source (`src/stdinreader.cpp` room
  dump uses lobby `position`, chat/playerLeft carry NetPlay index; the
  `posToNetPlayer` mapper proves they diverge). Roster records both;
  vote paths translate at the boundary (35 tests).
- Soak: production instance live (see `SERVER_HANDOFF.md §5`).
