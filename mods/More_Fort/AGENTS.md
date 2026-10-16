# More_Fort — Agent Guide

> REQUIRED: any AI agent working in this folder MUST read this file
> plus `stats-reference.md` before editing, adding, or deleting stats/models.

Warzone 2100 multiplay mod: adds 4 Fortress-class defenses + 1 designable rocket weapon. Data-only, no JS. All `.pie` files are kept intentionally, even if unreferenced.

## Layout

```
More_Fort/
  diffs/MoreFort/stats/weapons.json     new weapons (full objects)
  diffs/MoreFort/stats/structure.json  new structures (full objects)
  diffs/MoreFort/stats/research.json   new research topics (full objects)
  components/weapons/*.pie  turret/barrel models
  structs/*.pie              building bodies
  effects/*.pie              scaled FX
  texpages/page-12-player-buildings.png
  check.py                   compliance checker (run it)
  stats-reference.md         field/enum reference + checklist
  README.md                  player-facing install + content
```

No `stats/` folder. Engine merges `diffs/*/stats/*.json` onto `data/mp/stats/*.json` (`WzConfig jsonMerge`; `null` deletes a base key). Subfolder name under `diffs/` is arbitrary.

## How the 3 stats files connect

`research → structure → weapon → .pie`. Break one link and the entry is dead:

* `research.resultStructures[]` unlocks `structure.id`; `resultComponents[]` unlocks `weapons.id`.
* `structure.weapons[]` must exist in base game + `weapons.json` (max 3).
* `weapons.model/mountModel/*Gfx` → `components/`, `effects/`; `structure.structureModel` → `structs/` or base.
* Research prerequisites (`requiredResearch`) must exist in base or in this mod; cycles fail load.

## Compliance

Authoritative source is the Warzone2100 GitHub repo (`Warzone2100/warzone2100`, ref `master`):

* Stats shape and merge: `data/mp/stats/*.json`, `lib/framework/wzconfig.cpp` (`jsonMerge`).
* Field semantics and enums: `src/stats.cpp` (`loadWeaponStats`), `src/structure.cpp`, `src/research.cpp` (`loadResearch`).
* Asset filenames on disk are lowercase; `.pie`/`.ogg` references must match exactly (Linux break otherwise). Base files on disk are lowercase even where base JSON uses uppercase — always write lowercase.

Rules that have bitten before:

* Time/damage numeric fields must be integers — the engine applies `toUInt()` then multiplies by 100, so floats truncate (`2.5` → `2`).
* `research.msgName` is optional; if present it must be unique and must not collide with base keys.
* `redComponents[]`/`redStructures[]` must never contain `""`.
* Never add, shadow, or delete `ZNULL*` entries.

Enforced by `check.py` (stdlib only):

* `python3 check.py` — verifies base refs against GitHub (cached under `/tmp/opencode/wz-check-cache`, only referenced parts); falls back to WARN with no internet. Exit 0 = pass, 1 = fail.
* `python3 check.py --offline` — skips network; base refs become WARN.
* `python3 check.py --online` — legacy flag, online is now default.

## Install / Edit

* Drop folder (or `.wz`) into `mods/<version>/multiplay`.
* Add: full object under new key + `.pie` as needed. Patch base with partial object. Delete base key with `null`.
* Validate: `python3 check.py` must report 0 errors.
