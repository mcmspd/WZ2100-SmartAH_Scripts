# Server handoff — run opencode on the host

You are on the machine that actually hosts the Warzone instances.
This doc covers setup, what already changed, and the remaining live work.

## 0. Access note

No remote access was ever established. A temporary SSH keypair was generated
in the previous sandbox (`/tmp/opencode/ssh`, now abandoned) and its public
key was never installed anywhere. There is nothing to revoke. If you already
created the `opencode` user on this host, you can skip that — just work as
your normal user.

## 1. Server prerequisites

```bash
git clone <repo-url> ~/WZ2100-SmartAH_Scripts   # or copy the directory over
cd ~/WZ2100-SmartAH_Scripts
./install_dep.sh        # flatpak + tmux (idempotent)
sudo ./open_ports.sh    # TCP 2100-2110 (idempotent, safe to re-run)
```

Put map files in `maps/<Name>.wz`, mod files in `mods/<Mod>.wz`.
Edit `instances.json` (see `README.md` for the schema: `maps: [{name, mods}]`,
WZ flags, caps). The bot validates maps/mods at startup and exits non-zero
if anything is missing — check the session log on failure.

## 2. Run opencode here

Install opencode on this host, then open a session rooted at the repo:

```bash
cd ~/WZ2100-SmartAH_Scripts
opencode
```

`AGENTS.md` in this directory is the project memory: architecture, the
stock-vs-fork protocol analysis, what was completed 2026-10-16 and how it was
verified. New sessions started here will pick it up automatically.

Handy read-only checks (run before touching anything):

```bash
tmux ls                          # expect 0-1 WZ_* sessions when idle
ps aux | grep -E 'warzone|instance_bot' | grep -v grep
ls -la instance_configs/         # per-session *.status.json lifecycle files
ls -la *.log                     # per-session logs
```

## 3. What changed (already done, in git)

- `instance_bot.py` — readiness triple-gate, roster refresh events,
  worker-thread chat handling, configurable WZ flags + auto-join-approve,
  per-map mods (`--mod_mp` staging), spawn-storm fixes (no game-start
  successor, crash uptime gate + hourly cap, migration backoff/give-up,
  live-session cap, unique sessions), 80-min timeout, locked saves,
  cached IP, log rotation.
- `instances.json` — `maps` allowlist, `mods` per map, `wz_*` flags, caps.
- New: `mods/`, `test_instance_bot.py` (14 tests), `README.md`, `AGENTS.md`.
- Hardened: `install_dep.sh`, `open_ports.sh`.

Verify after checkout:

```bash
python3 -m py_compile instance_bot.py test_instance_bot.py
python3 -m unittest   # expect: OK
```

## 4. Remaining live work (do these here, in order)

### 4a. Slot-vs-pos verification (read-only, 5 min)

`/votekick` and `/vote` use the `WZCHATLOB` index as a roster `pos`
(`instance_bot.py` vote paths). With players in the lobby, compare:

```bash
grep -h WZCHATLOB <session>.log | head -5     # sender index per player
grep -A30 '__WZROOMSTATUS__' <session>.log | head -40   # pos per player
```

If a player's chat index ≠ their room-status `pos`, the vote paths need a
translation table (join `pk` → both). If they match, close the item.

### 4b. Soak test (passive, ~2 h)

Start one instance, then watch — `tmux ls` must stay at 1:

```bash
tmux new-session -d -s WZ_2100 'python3 instance_bot.py 2100 WZ_2100 NTW_3v3Full'
watch -n 30 'tmux ls; ps aux | grep -c [w]arzone2100'
```

Exercise: a `/maps` change (as map admin), one full game start→finish,
and one 80-min timeout cycle if affordable. Failure signs: extra `WZ_*`
sessions, `WZ_*.log` multiplying, `*.status.json` stuck on `failed`.

### 4c. Lobby blackhole tooling (needs root, test carefully)

Goal: per-process block of the old instance's lobby HTTPS so its listing
expires while player TCP survives — gapless migration (see AGENTS.md §
Stock-only migration, item 2). Draft, untested:

```bash
LOBBY_IP=$(dig +short wzlobby.wz2100.net | head -1)
iptables -I OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT
# old instance runs as user wz_old (started via setpriv --reuid):
iptables -A OUTPUT -m owner --uid-owner wz_old -d "$LOBBY_IP" -p tcp --dport 443 -j REJECT
```

Test on an empty lobby first: old listing must vanish from
`https://wzlobby.wz2100.net` game list while `tmux`/players stay connected;
new instance `POST` must then succeed. Alternative without extra users:
cgroupv2 + `-m cgroup`. Wrap in `lobby_off.sh <user>` / `lobby_on.sh`
(delete rule) once proven. Never block port 443 host-wide — that also kills
the new instance's listing.

### 4d. Join-time ERROR_REDIRECT (optional polish)

With `--async-join-approve` already on, newcomers during drain can be bounced
directly: `join reject <joinID> <ERROR_REDIRECT-code> tcp:<new_port>:0`.
Currently they land on the old lobby and wait for the mass redirect, which
works — this only makes handoff seamless. Confirm the numeric code from
`src/stdinreader.cpp` before wiring it into `on_join_approval`.

## 5. Production layout (Option A, everything under ubuntu) + safety rules

- WZ instances run as `wzbot` from `/home/ubuntu/WZ2100-SmartAH_Scripts`
  (single repo clone, `wz_backend=system`,
  `wz_binary=/usr/games/warzone2100.real`). `wzbot` is in the `ubuntu`
  group so it can read the repo and write `instance_configs/`; the
  `/home/wzbot/WZ` clone was removed as redundant 2026-10-18.
- Proxy runs as `ubuntu` in tmux session `MITM`
  (`python3 lobby_mitm.py --port 18080 ...`); restart with the same command
  if it ever dies (WZ keeps running unlisted meanwhile).
- `sudo ./lobby_off.sh wzbot 18080` is active and persisted via
  `netfilter-persistent save`. Verify: `sudo iptables -t nat -L OUTPUT -n`.
- Live instance: `sudo -u wzbot tmux attach -t WZ_PROD` (observe only).
  Soak in progress since 03:15 UTC: 80-min timeout, `/maps`, and games will
  exercise migration/redirect live — watch `tmux ls` (one `WZ_*`) and the
  session log.

Safety rules on the live host:

- Read-only first: `tmux ls`, logs, status files — before any restart.
- Never `tmux kill-session` a live `WZ_*` session with players; migrate with
  `/maps <n>` instead (redirect → drain → old self-shuts-down).
- One listing per public IP: never start a second lobby instance while the
  first is still listed — check the lobby game list first.
- `instances.json` contains the host private key by operator choice; keep the
  repo private or accept impersonation (documented in AGENTS.md).
- After the work: `git status`, review the diff, commit. `restarts.json` and
  `*.log` are git-ignored runtime state — leave them out of commits.
