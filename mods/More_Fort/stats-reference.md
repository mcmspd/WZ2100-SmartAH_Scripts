# Warzone 2100 Stats JSON — Human + AI Reference

This tells you what you can put in a stats file, what the game does if you leave something out, and what values are allowed.

Sources: `warzone2100/data/mp/stats/` (15 files), `data/base/stats/` (+3), `src/stats.cpp`, `src/structure.cpp`, `src/research.cpp`, `lib/framework/wzconfig.cpp`.

## How to read this doc

* **Required** = you must write it, or the entry is skipped / load fails.
* **Recommended** = game defaults to `0` / `false` / `""` if missing, so write it anyway or you get a useless stat (e.g. no damage, no range).
* **Optional** = safe to omit; default is listed.
* **Enum** = must be exactly one of these strings (case-sensitive unless noted).

## How files work

Every file is a JSON object keyed by ID:

```json
{
  "AGFort": { "id": "AGFort", "name": "Assault Gun Fortress", "damage": 35 },
  "LaserSuper": { "id": "LaserSuper", "damage": 350 }
}
```

* Key and inner `id` must match.
* `diffs/` patching: any `diffs/<anything>/stats/weapons.json` is merged onto the base file. New keys are added, existing keys are patched field-by-field, `"key": null` deletes. This is why `More_Fort` only ships 8 weapons instead of 127.
* Never delete entries starting with `ZNULL` (`ZNULLWEAPON`, `ZNULLBODY`, …) — the engine requires them first in the list.
* Time units: `firePause`, `reloadTime`, `radiusLife`, `periodicalDamageTime` are multiplied by 100 internally. `firePause: 2` = 200 game ticks.

## Minimal working example (fortress)

```json
{
  "X-Super-AG": {
    "id": "X-Super-AG",
    "name": "Assault Gun Fortress",
    "type": "FORTRESS",
    "strength": "HARD",
    "width": 2, "breadth": 2, "height": 2,
    "hitpoints": 1600, "armour": 8, "thermal": 8, "resistance": 100,
    "buildPoints": 1000, "buildPower": 500,
    "structureModel": ["STWPFCAN.PIE"],
    "sensorID": "FortressSensor",
    "combinesWithWall": true,
    "weapons": ["AGFort"]
  }
}
```

## weapons.json

What a turret does. Example: `AAGun2Mk1` (`damage:140, longRange:1536, movement:HOMING-DIRECT`).

| Field | Needed? | Type / allowed | Default if missing |
|---|---|---|---|
| `id`, `name` | Required | string | `""` |
| `damage`, `longRange`, `shortRange`, `firePause`, `reloadTime`, `numRounds`, `numExplosions`, `rotate`, `recoilValue`, `effectSize` | Recommended | integer | `0` (weapon does nothing) |
| `longHit`, `shortHit` | Optional | 0–100 (% chance) | `100` |
| `minRange`, `minimumDamage`, `empRadius`, `radius`, `radiusDamage` | Optional | integer | `0` |
| `movement` | Required | `DIRECT`, `INDIRECT`, `HOMING-DIRECT`, `HOMING-INDIRECT` | load fails |
| `weaponClass` | Required | `KINETIC`, `HEAT` | falls back to `KINETIC` + error |
| `weaponSubClass` | Required | `CANNON`, `MORTARS`, `MISSILE`, `ROCKET`, `ENERGY`, `GAUSS`, `FLAME`, `HOWITZERS`, `MACHINE GUN`, `ELECTRONIC`, `A-A GUN`, `SLOW MISSILE`, `SLOW ROCKET`, `LAS_SAT`, `BOMB`, `COMMAND`, `EMP` | load fails |
| `weaponEffect` | Required | `ANTI PERSONNEL`, `ANTI TANK`, `BUNKER BUSTER`, `ARTILLERY ROUND`, `FLAMER`, `ANTI AIRCRAFT`, `ALL ROUNDER` | load fails |
| `model`, `mountModel`, `muzzleGfx`, `flightGfx`, `hitGfx`, `missGfx`, `waterGfx`, `trailGfx` | Optional | `"file.pie"` (must exist in mod or base) | invisible |
| `weaponWav`, `explosionWav` | Optional | `"sound.ogg"` or `"-1"` = silent | `"-1"` |
| `flags` | Optional | string or list, lowercased: `AirOnly`, `ShootAir`, `NoFriendlyFire`, … | ground-only |
| `penetrate`, `facePlayer`, `faceInFlight`, `lightWorld` | Optional | boolean | `false` |
| `fireOnMove` | Optional | boolean | `true` |
| `designable` | Optional | boolean (shows in tank designer?) | `false` |
| `buildPoints`, `buildPower`, `weight`, `hitpoints` | Optional | integer | `0` |

## body.json (tank/cyborg bodies)

| Field | Needed? | Notes |
|---|---|---|
| `id`, `name`, `model` | Required | `.pie` file |
| `size` | Required | `LIGHT`, `MEDIUM`, `HEAVY`, `SUPER HEAVY` |
| `weaponSlots`, `armourKinetic`, `armourHeat`, `powerOutput`, `weight`, `hitpoints`, `buildPoints`, `buildPower` | Recommended | integers |
| `class` | Recommended | free text, e.g. `Droids`, `Cyborgs` |
| `droidType` | Optional | `PERSON`, `CYBORG`, … default `DROID` |
| `resistance` | Optional | default `150` |
| `designable` | Optional | `1` = buildable |
| `propulsionExtraModels` | Optional | `{ "tracked01": {"left": "x.pie"} }` — wrong propulsion name fails load |

## structure.json (buildings)

`_config_: {"baseStructDamageExpLevel": 0}` exists only in `data/base`, not `mp`. Leave it alone.

| Field | Needed? | Notes |
|---|---|---|
| `id`, `name` | Required | |
| `type` | Required | `HQ`, `FACTORY`, `RESEARCH`, `POWER GENERATOR`, `RESOURCE EXTRACTOR`, `DEFENSE`, `WALL`, `REPAIR FACILITY`, `REARM PAD`, `FORTRESS`, … (full list in `structure.cpp:512`). Wrong = entry skipped |
| `width`, `breadth`, `height` | Recommended | tiles / height |
| `hitpoints` | Recommended | default `1` |
| `armour`, `thermal`, `resistance` | Optional | default `0` |
| `strength` | Optional | `SOFT`, `MEDIUM`, `HARD`, `BUNKER` (wrong → `SOFT`) |
| `buildPoints`, `buildPower` | Recommended | cost |
| `structureModel[]`, `baseModel` | Optional | `.pie` files |
| `weapons[]` | Needed for `DEFENSE`/`FORTRESS` | max 3, extras cut off |
| `sensorID`, `ecmID` | Optional | e.g. `FortressSensor`, default null sensor |
| `combinesWithWall` | Optional | `true` for fortresses |
| Type extras | Optional | `productionPoints` (factory), `researchPoints` (lab), `powerPoints` (generator), `repairPoints`, `rearmPoints` — default `0` |

## research.json (tech tree)

Everything except the key is optional, but a research with no `result*` does nothing. Cycles in `requiredResearch` fail load.

| Field | What it does | Example |
|---|---|---|
| `requiredResearch[]` | prerequisites (must exist) | `["R-Wpn-MG4", "R-Defense-WallUpgrade03"]` |
| `resultStructures[]` | unlocks buildings | `["X-Super-AG"]` |
| `resultComponents[]` | unlocks parts/weapons | `["Rocket-ATGM"]` |
| `redComponents[]`, `redStructures[]` | makes old ones obsolete | `["Rocket-Pod"]` — never `[""]` |
| `replacedComponents[]` | `"old:new"` pairs | |
| `results[]` | stat upgrades, not unlocks | `[{"class":"Body","parameter":"Armour","value":30}]` |
| `statID` | reference shown in UI; convention: equals `result*[0]` | |
| `researchPoints`, `researchPower` | cost / time | |
| `iconID` | UI icon | `IMAGE_RES_DEFENCE`, `IMAGE_RES_WEAPONTECH`, … |
| `msgName` | briefing message key — keep unique, do not reuse base keys | bad: `RES_EMP_CAN` x4 |
| `category`, `subgroupIconID` | upgrade grouping | |

## Other files (rarely touched)

* `propulsion.json`: `speed` + `type` required (`Wheeled,Tracked,Legged,Hover,Lift,Propellor,Half-Tracked`).
* `sensor.json` / `ecm.json` / `repair.json` / `construction.json` / `brain.json`: component fields + `range` / `repairPoints` / `constructPoints` / `ranks`.
* `templates.json`: premade units `{body, propulsion, weapons[], type}`.
* `weaponmodifier.json` / `structuremodifier.json`: damage % tables keyed by `weaponEffect`.
* `propulsiontype.json`, `propulsionsounds.json`: movement multipliers + sounds.
* Base-only: `features.json` (map props), `effectlights.json` (cosmetic lights), `terraintable.json` (terrain speed %).

## Quick checklist before commit

1. `python3 -c "import json; json.load(open('diffs/MoreFort/stats/weapons.json'))"` — valid JSON?
2. Every `structure.weapons[]` exists in base + diff?
3. Every `research.requiredResearch` / `result*` exists?
4. Every `.pie` / `.ogg` referenced exists (watch `las_Body` vs `las_body` case on Linux)?
5. No `ZNULL*` deleted, no `msgName` reused, no `[""]` placeholders?
