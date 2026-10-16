#!/usr/bin/env python3
"""More_Fort compliance checker.

Run: python3 check.py [--offline] (exit 0 = pass, 1 = fail).
  Base refs are always verified against Warzone2100 GitHub source
  (cached, only referenced parts). If there is no internet or GitHub
  is unreachable, the checker falls back to WARN (assumed base).
  --offline: skip network entirely.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MOD_ROOT = Path(__file__).resolve().parent
STATS_DIRS = sorted(MOD_ROOT.glob("diffs/*/stats"))
PIE_DIRS = ["components/weapons", "structs", "effects"]
PIE_FIELDS = ("model", "mountModel", "muzzleGfx", "flightGfx",
              "hitGfx", "missGfx", "waterGfx", "trailGfx")
INT_FIELDS = ("damage", "longRange", "shortRange", "firePause",
              "reloadTime", "numRounds", "numExplosions", "rotate",
              "recoilValue", "effectSize", "radius",
              "radiusDamage", "radiusLife", "periodicalDamage",
              "periodicalDamageRadius", "periodicalDamageTime",
              "buildPoints", "buildPower", "weight", "hitpoints",
              "flightSpeed", "minElevation", "maxElevation",
              "researchPoints", "researchPower", "armour", "thermal",
              "resistance", "width", "breadth", "height")
MOVEMENTS = {"DIRECT", "INDIRECT", "HOMING-DIRECT", "HOMING-INDIRECT"}
WCLASSES = {"KINETIC", "HEAT"}
WSUBS = {"CANNON", "MORTARS", "MISSILE", "ROCKET", "ENERGY", "GAUSS",
         "FLAME", "HOWITZERS", "MACHINE GUN", "ELECTRONIC", "A-A GUN",
         "SLOW MISSILE", "SLOW ROCKET", "LAS_SAT", "BOMB", "COMMAND", "EMP"}
WEFFECTS = {"ANTI PERSONNEL", "ANTI TANK", "BUNKER BUSTER",
            "ARTILLERY ROUND", "FLAMER", "ANTI AIRCRAFT", "ALL ROUNDER"}
STRENGTHS = {"SOFT", "MEDIUM", "HARD", "BUNKER"}
KNOWN_FLAGS = {"aironly", "shootair", "nofriendlyfire",
               "allowedontransporter", "teleportcapture",
               "expnoimpactdamage", "expnopenetrateimpactdamage",
               "expnopenetratesplashdamage", "expnoperiodicaldamage",
               "expnosplashdamage"}
# Fallback IDs for offline runs (subset verified against upstream master).
KNOWN_BASE_RESEARCH = {"R-Wpn-MG4", "R-Defense-WallUpgrade03",
                       "R-Wpn-Laser02", "R-Defense-WallUpgrade05",
                       "R-Wpn-AAGun02", "R-Wpn-Flame2",
                       "R-Wpn-Rocket01-LtAT"}
KNOWN_BASE_SENSORS = {"FortressSensor", "DefaultSensor1Mk1",
                      "TowerSensor", "ZNULLSENSOR"}
KNOWN_BASE_SOUNDS = {"lrgexpl.ogg", "lrgcan.ogg", "asltmg.ogg",
                     "flmthrow.ogg", "plslsr.ogg", "rocket.ogg",
                     "smlexpl.ogg", "medcan.ogg", "smlcan.ogg",
                     "mgbar1.ogg", "mgbar2.ogg", "-1"}
KNOWN_BASE_MSGNAMES = {"RES_EMP_CAN", "RES_W_RK_LTAT1"}

WZ_REPO = "Warzone2100/warzone2100"
WZ_API = f"https://api.github.com/repos/{WZ_REPO}/contents"
WZ_RAW = f"https://raw.githubusercontent.com/{WZ_REPO}"
PIE_BASE_DIRS = ("data/base/effects", "data/mp/effects",
                 "data/base/components/weapons",
                 "data/mp/components/weapons",
                 "data/base/structs", "data/mp/structs")
AUDIO_BASE_DIRS = ("data/base/audio/sfx/weapons",
                   "data/base/audio/sfx/explons",
                   "data/base/audio/sfx/vehicle",
                   "data/base/audio/sfx/misc",
                   "data/base/audio/sfx/interfce",
                   "data/base/audio/sfx/building",
                   "data/base/audio/multi")

errors, warnings, infos = [], [], []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def info(msg):
    infos.append(msg)


# ---------- online base-data layer (cached, referenced parts only) ----------

class BaseData:
    def __init__(self):
        self.available = False
        self.note = ""
        self.pies = set()       # lowercase basenames
        self.sounds = set()     # lowercase basenames
        self.research_ids = set()
        self.msgnames = set()
        self.icons = set()
        self.sensor_ids = set()
        self.weapon_ids = set()
        self.structure_ids = set()


def _cache_path(cache_dir, key):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return Path(cache_dir) / (safe + ".json")


def _cache_read(cache_dir, key, ttl_days):
    try:
        p = _cache_path(cache_dir, key)
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > ttl_days * 86400:
            return None
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _cache_write(cache_dir, key, payload):
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        _cache_path(cache_dir, key).write_text(json.dumps(payload))
    except OSError:
        pass


def _http_get(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": "morefort-check",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _api_names(path, ref, args, cache_key):
    """File basenames in a repo dir via GitHub API (cached)."""
    if args.no_cache:
        cached = None
    else:
        cached = _cache_read(args.cache_dir, cache_key, args.cache_ttl)
    if cached is not None:
        return set(cached)
    data = json.loads(_http_get(f"{WZ_API}/{path}?ref={ref}",
                                args.timeout))
    names = [e["name"] for e in data if e.get("type") == "file"]
    if not args.no_cache:
        _cache_write(args.cache_dir, cache_key, names)
    return set(names)


def _raw_json(path, ref, args, cache_key):
    if args.no_cache:
        cached = None
    else:
        cached = _cache_read(args.cache_dir, cache_key, args.cache_ttl)
    if cached is not None:
        return cached
    data = json.loads(_http_get(f"{WZ_RAW}/{ref}/{path}", args.timeout))
    if not args.no_cache:
        _cache_write(args.cache_dir, cache_key, data)
    return data


def fetch_base(args):
    """Fetch only the upstream parts our refs point at. Never fatal."""
    base = BaseData()
    if getattr(args, "offline", False):
        return base
    try:
        for d in PIE_BASE_DIRS:
            key = f"api-{args.ref}-{d}"
            base.pies.update(n.lower() for n in
                             _api_names(d, args.ref, args, key))
        for d in AUDIO_BASE_DIRS:
            key = f"api-{args.ref}-{d}"
            try:
                base.sounds.update(n.lower() for n in
                                   _api_names(d, args.ref, args, key))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue  # optional audio subdir
                raise
        rq = _raw_json("data/mp/stats/research.json", args.ref,
                       args, f"raw-{args.ref}-research")
        base.research_ids = set(rq.keys())
        for v in rq.values():
            if isinstance(v, dict):
                if v.get("msgName"):
                    base.msgnames.add(v["msgName"])
                if v.get("iconID"):
                    base.icons.add(v["iconID"])
        sq = _raw_json("data/mp/stats/sensor.json", args.ref,
                       args, f"raw-{args.ref}-sensor")
        base.sensor_ids = set(sq.keys())
        wq = _raw_json("data/mp/stats/weapons.json", args.ref,
                       args, f"raw-{args.ref}-weapons")
        base.weapon_ids = set(wq.keys())
        tq = _raw_json("data/mp/stats/structure.json", args.ref,
                       args, f"raw-{args.ref}-structure")
        base.structure_ids = set(tq.keys())
    except (urllib.error.URLError, ValueError, KeyError) as e:
        base.note = f"online check unavailable ({e}); base refs unverified"
        return base
    base.available = True
    return base


# ---------- offline checks ----------

def load_stats():
    data = {}
    if not STATS_DIRS:
        err("no diffs/*/stats/ directory found")
        return data
    for d in STATS_DIRS:
        for name in ("weapons.json", "structure.json", "research.json"):
            p = d / name
            if not p.exists():
                continue
            try:
                data.setdefault(name, {}).update(json.loads(p.read_text()))
            except json.JSONDecodeError as e:
                err(f"{p}: invalid JSON: {e}")
    for d in STATS_DIRS:
        for p in d.glob("*.json"):
            if p.name not in ("weapons.json", "structure.json", "research.json"):
                warn(f"{p}: unexpected stats file (engine still merges it)")
                try:
                    json.loads(p.read_text())
                except json.JSONDecodeError as e:
                    err(f"{p}: invalid JSON: {e}")
    return data


def mod_pie_index():
    index = {}  # exact filename -> path
    for sub in PIE_DIRS:
        d = MOD_ROOT / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file():
                index[f.name] = str(f.relative_to(MOD_ROOT))
    return index


def check_key_ids(kind, table):
    for key, obj in table.items():
        if not isinstance(obj, dict):
            if obj is None and key.startswith("ZNULL"):
                err(f"{kind}.{key}: must never delete ZNULL entries")
            elif obj is None:
                warn(f"{kind}.{key}: deletes a base entry with null")
            else:
                err(f"{kind}.{key}: value must be an object")
            continue
        if key.startswith("ZNULL"):
            err(f"{kind}.{key}: must not add/shadow ZNULL entries")
        if obj.get("id") != key:
            err(f"{kind}.{key}: key != inner id ({obj.get('id')!r})")
        if not obj.get("name"):
            err(f"{kind}.{key}: missing required 'name'")


def check_ints(kind, key, obj):
    for f in INT_FIELDS:
        if f in obj and not isinstance(obj[f], int):
            if isinstance(obj[f], bool):
                err(f"{kind}.{key}.{f}: bool not allowed, use integer")
            else:
                err(f"{kind}.{key}.{f}: must be integer, got {obj[f]!r} "
                    f"(engine toUInt() truncates floats)")


def check_pie_ref(where, ref, pie_index, pie_lowers, base):
    """Shared model/GFX resolution: local exact, else base (offline/online)."""
    if not isinstance(ref, str) or not ref.lower().endswith(".pie"):
        err(f"{where}: {ref!r} must be a '.pie' filename")
        return
    if ref in pie_index:
        return  # exact local match
    if ref.lower() in pie_lowers:
        err(f"{where}: case mismatch {ref!r} "
            f"(disk has different case; Linux break)")
        return
    if ref != ref.lower():
        err(f"{where}: {ref!r} not lowercase; "
            f"base files are lowercase, use {ref.lower()!r}")
        return
    if base is not None and base.available:
        if ref.lower() in base.pies:
            info(f"{where}: {ref!r} verified in base @{base.ref}")
        else:
            err(f"{where}: {ref!r} not in mod nor in base "
                f"(checked {len(PIE_BASE_DIRS)} base dirs @{base.ref})")
    else:
        warn(f"{where}: {ref!r} not in mod, assumed base-provided")


def check_weapons(weapons, pie_index, base):
    pie_lowers = {n.lower() for n in pie_index}
    for key, o in weapons.items():
        if not isinstance(o, dict):
            continue
        check_ints("weapons", key, o)
        for f in ("movement", "weaponClass", "weaponSubClass", "weaponEffect"):
            if f not in o:
                err(f"weapons.{key}: missing required '{f}'")
        if o.get("movement") not in MOVEMENTS:
            err(f"weapons.{key}.movement: {o.get('movement')!r} not in {sorted(MOVEMENTS)}")
        if o.get("weaponClass") not in WCLASSES:
            err(f"weapons.{key}.weaponClass: {o.get('weaponClass')!r} not in {sorted(WCLASSES)}")
        if o.get("weaponSubClass") not in WSUBS:
            err(f"weapons.{key}.weaponSubClass: {o.get('weaponSubClass')!r} invalid")
        if o.get("weaponEffect") not in WEFFECTS:
            err(f"weapons.{key}.weaponEffect: {o.get('weaponEffect')!r} invalid")
        flags = o.get("flags")
        if flags is not None:
            items = flags if isinstance(flags, list) else [flags]
            for fl in items:
                if not isinstance(fl, str) or fl.lower() not in KNOWN_FLAGS:
                    warn(f"weapons.{key}.flags: unknown flag {fl!r}")
        for f in PIE_FIELDS:
            if f in o:
                check_pie_ref(f"weapons.{key}.{f}", o[f],
                              pie_index, pie_lowers, base)
        for f in ("weaponWav", "explosionWav"):
            if f not in o:
                continue
            snd = o[f]
            if base is not None and base.available:
                if snd.lower() in base.sounds or snd == "-1":
                    info(f"weapons.{key}.{f}: {snd!r} verified in base @{base.ref}")
                else:
                    err(f"weapons.{key}.{f}: {snd!r} not in mod nor in base audio @{base.ref}")
            elif snd not in KNOWN_BASE_SOUNDS:
                warn(f"weapons.{key}.{f}: {snd!r} not in known base sounds")
        lo, hi = o.get("shortRange"), o.get("longRange")
        if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
            err(f"weapons.{key}: shortRange {lo} > longRange {hi}")
        for f in ("longHit", "shortHit"):
            if f in o and not (isinstance(o[f], int) and 0 <= o[f] <= 100):
                err(f"weapons.{key}.{f}: {o[f]!r} must be 0-100")


def check_structures(structures, weapons, pie_index, base):
    pie_lowers = {n.lower() for n in pie_index}
    for key, o in structures.items():
        if not isinstance(o, dict):
            continue
        check_ints("structure", key, o)
        if o.get("type") not in ("DEFENSE", "FORTRESS", "WALL", "HQ",
                                 "FACTORY", "RESEARCH", "POWER GENERATOR",
                                 "RESOURCE EXTRACTOR", "REPAIR FACILITY",
                                 "REARM PAD", "CORNER WALL", "GATE"):
            if o.get("type") != "FORTRESS":
                warn(f"structure.{key}.type: {o.get('type')!r} verify in structure.cpp")
        if "strength" in o and o["strength"] not in STRENGTHS:
            err(f"structure.{key}.strength: {o['strength']!r} invalid")
        for w in o.get("weapons", []):
            if w in weapons:
                continue
            if base is not None and base.available:
                if w in base.weapon_ids:
                    info(f"structure.{key}.weapons: {w!r} verified in base @{base.ref}")
                else:
                    err(f"structure.{key}.weapons: {w!r} not in diff nor base @{base.ref}")
            else:
                warn(f"structure.{key}.weapons: {w!r} not in diff, assumed base weapon")
        sid = o.get("sensorID")
        if "sensorID" in o:
            if sid in KNOWN_BASE_SENSORS:
                pass
            elif base is not None and base.available:
                if sid in base.sensor_ids:
                    info(f"structure.{key}.sensorID: {sid!r} verified in base @{base.ref}")
                else:
                    err(f"structure.{key}.sensorID: {sid!r} not in base sensor.json @{base.ref}")
            else:
                warn(f"structure.{key}.sensorID: {sid!r} not in known sensors")
        for m in o.get("structureModel", []):
            check_pie_ref(f"structure.{key}.structureModel", m,
                          pie_index, pie_lowers, base)
        if len(o.get("weapons", [])) > 3:
            err(f"structure.{key}.weapons: max 3, extras are cut off")


def check_research(research, structures, weapons, base):
    seen_msg = {}
    if base is not None and base.available:
        all_ids = set(research) | base.research_ids
        base_msgs = base.msgnames
    else:
        all_ids = set(research) | KNOWN_BASE_RESEARCH
        base_msgs = KNOWN_BASE_MSGNAMES
    for key, o in research.items():
        if not isinstance(o, dict):
            continue
        if not any(k in o for k in ("resultStructures", "resultComponents", "results")):
            warn(f"research.{key}: no result* field, does nothing")
        for r in o.get("requiredResearch", []):
            if r in all_ids:
                continue
            if base is not None and base.available:
                err(f"research.{key}.requiredResearch: {r!r} not in diff nor base @{base.ref}")
            else:
                warn(f"research.{key}.requiredResearch: {r!r} unverified "
                     f"(offline; base check skipped)")
        for s in o.get("resultStructures", []):
            if s not in structures:
                err(f"research.{key}.resultStructures: {s!r} not in diff structure.json")
        for c in o.get("resultComponents", []):
            if c not in weapons:
                err(f"research.{key}.resultComponents: {c!r} not in diff weapons.json")
        for c in o.get("redComponents", []) + o.get("redStructures", []):
            if c == "":
                err(f"research.{key}: red* entry must never be empty string")
        if "msgName" in o:
            if o["msgName"] in base_msgs:
                err(f"research.{key}.msgName: {o['msgName']!r} collides with base")
            if o["msgName"] in seen_msg:
                err(f"research.{key}.msgName: {o['msgName']!r} reused "
                    f"(also in {seen_msg[o['msgName']]})")
            seen_msg[o["msgName"]] = key
        if "iconID" in o and base is not None and base.available:
            if o["iconID"] not in base.icons:
                warn(f"research.{key}.iconID: {o['iconID']!r} not seen in base research.json")
        results = o.get("resultStructures", []) + o.get("resultComponents", [])
        if results and "statID" in o:
            if o["statID"] != results[0]:
                warn(f"research.{key}.statID: {o['statID']!r} != result*[0] {results[0]!r}")
            if o["statID"] not in structures and o["statID"] not in weapons:
                if base is not None and base.available:
                    if o["statID"] not in base.structure_ids and \
                            o["statID"] not in base.weapon_ids:
                        err(f"research.{key}.statID: {o['statID']!r} not in diff nor base @{base.ref}")
                else:
                    warn(f"research.{key}.statID: {o['statID']!r} not in diff, assumed base stat")
    visiting, done = set(), set()

    def visit(node, stack):
        if node not in research or node in done:
            return
        if node in visiting:
            err(f"research: prerequisite cycle: {' -> '.join(stack + [node])}")
            return
        visiting.add(node)
        o = research[node]
        if isinstance(o, dict):
            for r in o.get("requiredResearch", []):
                visit(r, stack + [node])
        visiting.discard(node)
        done.add(node)

    for k in research:
        visit(k, [])


def check_pie_textures(pie_index):
    for name, rel in pie_index.items():
        if not name.lower().endswith(".pie"):
            continue
        try:
            text = (MOD_ROOT / rel).read_text(errors="replace")
        except OSError as e:
            err(f"{rel}: unreadable: {e}")
            continue
        for m in re.finditer(r"TEXTURE\s+\d+\s+(\S+)", text):
            tex = m.group(1)
            if (MOD_ROOT / "texpages" / tex).exists():
                continue
            if not tex.startswith("page-") or not tex.endswith(".png"):
                warn(f"{rel}: TEXTURE {tex!r} breaks page-NN-*.png convention")


def parse_args(argv):
    p = argparse.ArgumentParser(description="More_Fort compliance checker")
    p.add_argument("--online", action="store_true",
                   help="deprecated: online verification is now default")
    p.add_argument("--offline", action="store_true",
                   help="skip network; base refs fall back to WARN")
    p.add_argument("--ref", default="master",
                   help="upstream git ref (default: master)")
    p.add_argument("--timeout", type=float, default=20,
                   help="HTTP timeout in seconds")
    p.add_argument("--cache-dir", default="/tmp/opencode/wz-check-cache",
                   help="cache dir for upstream responses")
    p.add_argument("--cache-ttl", type=float, default=7,
                   help="cache TTL in days")
    p.add_argument("--no-cache", action="store_true",
                   help="bypass cache")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show verified base refs")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    base = None if args.offline else fetch_base(args)
    if base is not None:
        base.ref = args.ref
        if base.available:
            info(f"base @{args.ref}: {len(base.pies)} pies, "
                 f"{len(base.sounds)} sounds, "
                 f"{len(base.research_ids)} research, "
                 f"{len(base.sensor_ids)} sensors verified")
        elif base.note:
            warn(base.note)
    data = load_stats()
    weapons = data.get("weapons.json", {})
    structures = data.get("structure.json", {})
    research = data.get("research.json", {})
    check_key_ids("weapons", weapons)
    check_key_ids("structure", structures)
    check_key_ids("research", research)
    pie_index = mod_pie_index()
    check_weapons(weapons, pie_index, base)
    check_structures(structures, weapons, pie_index, base)
    check_research(research, structures, weapons, base)
    check_pie_textures(pie_index)
    if args.verbose:
        for i in sorted(infos):
            print(f"INFO: {i}")
    for w in sorted(warnings):
        print(f"WARN: {w}")
    for e in sorted(errors):
        print(f"ERROR: {e}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
