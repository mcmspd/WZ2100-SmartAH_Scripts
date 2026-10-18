#!/usr/bin/env bash
# update_mods.sh — fetch the latest released .wz for every mapped mod repo.
#
# Mod releases are versioned (asset name embeds the tag), so
# releases/latest/download/<name> does NOT work. We resolve the tag via the
# GitHub API, then pick the single .wz asset from that release.
#
# Downloads are atomic and content-addressed: the file is only replaced when
# the bytes actually differ, so repeat runs are no-ops and cron is safe.
#
# Usage:
#   ./update_mods.sh            # fetch latest for every mapped mod
#   ./update_mods.sh --check    # report what would change, download nothing
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODS_DIR="$REPO_DIR/mods"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# Add future released mods here as  mod_name:github_owner/repo
MOD_MAP=(
  "More_Fort:mcmspd/More-Fortress"
)

log() { echo "[mods] $*"; }

for entry in "${MOD_MAP[@]}"; do
  mod_name="${entry%%:*}"
  repo="${entry#*:}"
  target="$MODS_DIR/$mod_name.wz"

  read -r tag url <<EOF
$(python3 - "$repo" <<'PY'
import json, sys, urllib.request

repo = sys.argv[1]
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases/latest",
    headers={"Accept": "application/vnd.github+json",
             "User-Agent": "wz-autohost-update-mods"})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
except Exception as e:
    print(f"ERROR\tERROR\t{e}")
    raise SystemExit(0)

tag = data.get("tag_name", "")
asset = next((a for a in data.get("assets", []) if a["name"].endswith(".wz")), None)
if not asset:
    print(f"ERROR\tERROR\tno .wz asset in latest release {tag!r}")
    raise SystemExit(0)
print(f"{tag}\t{asset['browser_download_url']}")
PY
)
EOF

  if [ "$tag" = "ERROR" ]; then
    log "$mod_name: SKIP ($url)"
    continue
  fi
  if [ -z "$tag" ] || [ -z "$url" ]; then
    log "$mod_name: SKIP (no latest release)"
    continue
  fi

  tmp="$(mktemp "$MODS_DIR/.${mod_name}.XXXXXX.wz")"
  if ! curl -sSL --fail --max-time 120 -o "$tmp" "$url"; then
    log "$mod_name: download FAILED ($url)"
    rm -f "$tmp"
    continue
  fi

  # Reject anything that isn't a readable zip before replacing a good file.
  if ! python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1])" "$tmp" 2>/dev/null; then
    log "$mod_name: download not a valid .wz archive, keeping current"
    rm -f "$tmp"
    continue
  fi

  if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
    log "$mod_name: already at $tag"
    rm -f "$tmp"
    continue
  fi

  if [ "$CHECK_ONLY" -eq 1 ]; then
    log "$mod_name: UPDATE AVAILABLE -> $tag ($(stat -c%s "$tmp") bytes)"
    rm -f "$tmp"
    continue
  fi

  sudo mv "$tmp" "$target" 2>/dev/null || mv "$tmp" "$target"
  sudo chown ubuntu:wzbot "$target" 2>/dev/null || true
  sudo chmod 644 "$target" 2>/dev/null || chmod 644 "$target"
  log "$mod_name: updated to $tag ($(stat -c%s "$target") bytes)"
done