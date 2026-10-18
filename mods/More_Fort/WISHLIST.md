# More_Fort Wishlist

Planned content for the Warzone 2100 multiplay mod.

| # | Item | Status | Files affected | BEHAVIOUR |
|---|------|--------|----------------|-----------|
| 1 | **M1299 Howitzer Fortress** | ✅ Done | `weapons.json` (`HowitzerSuper`), `structure.json` (`X-Super-Howitzer`), `research.json` (`R-Defense-Super-Howitzer`), `README.md` | 3 RPM indirect siege shell, 1400 direct + 1400 splash over a 4-tile blast, 65/50% accuracy, 2.5 s ground scorch, recoil 500, heavy 120000-weight turret |
| 2 | **SUPER RIM-216 Rolling Airframe Anti-Aircraft Missile Fortress** | ⬜ Todo | `diffs/MoreFort/stats/weapons.json`, `diffs/MoreFort/stats/structure.json`, `diffs/MoreFort/stats/research.json`, `README.md` | **1000 RPM**, fires a barrage of long-range **blue missiles**, medium damage, damage type **Anti-Tank**, **Shoot Air only** |
| 3 | **ATGM** | ✅ Done | `diffs/MoreFort/stats/weapons.json` (`Rocket-ATGM`), `README.md` | 4 blue missiles per 20 s reload at 105 damage each (matches Lancer per shot), 1.5× Lancer sustained DPS, 5–30 tile standoff band, slow flight needing lead |

## Item 3 as built

Final `Rocket-ATGM` values (`diffs/MoreFort/stats/weapons.json`), versus the stock
Lancer (`Rocket-LtA-T`):

| Field | ATGM | Lancer | Note |
|-------|------|--------|------|
| `damage` | 105 | 105 | deliberately matched to the Lancer |
| `minimumDamage` | 33 | 33 | AP floor — identical, 34 resolved |
| `numRounds` | 4 | 2 | double the rockets per salvo |
| `firePause` | 5 (0.5 s) | 1 (0.1 s) | 1.5 s ripple spread |
| `reloadTime` | 200 (20 s) | 160 (16 s) | 4 s longer than the Lancer |
| Cycle | 21.5 s | 16.1 s | |
| Damage per cycle | 420 | 210 | 2.00× |
| **DPS** | **1172** | **783** | **1.50×** |
| `flightSpeed` | 700 | 1700 | slow shells needing lead |
| `longRange` | 3840 (30 tiles) | 1152 (9 tiles) | 3.33× |
| `shortRange` | 512 (4 tiles) | 512 (4 tiles) | same |
| `minRange` | 640 (5 tiles) | 128 (1 tile) | standoff only |
| `longHit` / `shortHit` | 80 / 90 | 60 / 30 | far more accurate |
| `flightGfx` | `fxmplme-blue.pie` (local) | `FXMPLME.PIE` | blue missile model |
| `weaponClass` / `weaponSubClass` / `weaponEffect` | KINETIC / ROCKET / ANTI TANK | same | |

Design intent: identical punch per missile as the Lancer, twice as many missiles per
salvo, at the cost of a longer reload, slower flight and a wider minimum range. The
whole 1.50× DPS edge comes from salvo size, not per-shot damage.

Note `movement` is `INDIRECT`, so `actionInRange` in `src/action.cpp` bypasses
min-range for these weapons (`|| !proj_Direct(psStats)`). The 5-tile minimum therefore
shapes target selection in `combFire` rather than hard-blocking close shots.
| 4 | **Fortress Sensor (Research)** | ✅ Done | `diffs/MoreFort/stats/research.json` (`R-Sys-Sensor-FortressUpgrade`), `README.md` | On research, `FortressSensor` range 1536 → 2274 (12 → ~18 tiles), matching the Wide Spectrum Sensor. Affects only fortresses, no other sensors |

## Item 4 as built

`R-Sys-Sensor-FortressUpgrade` in `diffs/MoreFort/stats/research.json`:

| Field | Value |
|-------|-------|
| `name` | Fortress Sensor Upgrade |
| `requiredResearch` | `R-Sys-Sensor-Upgrade03` (Sensor Upgrade Mk3) |
| `results` | `{"class": "Sensor", "filterParameter": "Id", "filterValue": "FortressSensor", "parameter": "Range", "value": 48}` |
| `category` / icons | `Sensor` / `IMAGE_RES_SYSTEMTECH` + `IMAGE_RES_GRPUPG` |
| `researchPoints` / `researchPower` | 9000 / 281 |

Computed range: `1536 + ceil(1536 * 48 / 100) = 1536 + 738 = 2274`, which meets the
Wide Spectrum Sensor's 2260. (`value: 47` yields 2258 — two short — so 48 is used
because the engine applies `iDivCeil` on the scaled change.)

Affected structures (all use `sensorID: "FortressSensor"`): `X-Super-AA`, `X-Super-AG`,
`X-Super-Flame`, `X-Super-Laser`, `X-Super-Howitzer`. `Emplacement-ATGM` is unaffected —
it uses `DefaultSensor1Mk1`.

Cumulative note: base `R-Sys-Sensor-Upgrade01/02/03` add +25%/+15%/+10% to *all* sensors,
so a fully researched player sits at roughly 3593 (~28 tiles) rather than 2274.

## Item 1 as built

Final `HowitzerSuper` values (`diffs/MoreFort/stats/weapons.json`):

| Field | Value | Note |
|-------|-------|------|
| `damage` / `radiusDamage` | 1400 / 1400 | 4× the stock Ground Shaker, identical direct vs splash |
| `minimumDamage` | 33 | AP floor = 462 (percent, not flat — see notes) |
| `firePause` | 167 | 16.7 s cycle → 3.59 RPM, ~6× Ground Shaker DPS |
| `radius` / `effectSize` | 512 / 512 | 4-tile blast, visual matches damage area |
| `radiusLife` | 10 | 1 s blast visual linger |
| `periodicalDamage*` | 1 / 512 / 25 | 1 dmg per tick, 4-tile radius, 2.5 s burn |
| `recoilValue` / `weight` | 500 / 120000 | matches Laser Fortress recoil; 120000 slows turret traverse ~21% |
| `longRange` / `shortRange` / `minRange` | 10240 / 3840 / 640 | 80 / 30 / 5 tiles |
| `longHit` / `shortHit` | 65 / 50 | better than the Ground Shaker's 50/40, but deliberately unreliable — ~a third of shells miss |
| `flightSpeed` | 850 | 57% of stock — steep, slow arc (~107 s flight at max range) |
| `movement` / `weaponSubClass` / `weaponEffect` | INDIRECT / HOWITZERS / ARTILLERY ROUND | same classification as the Ground Shaker |

Damage-over-time, with accuracy factored in (this is the figure that actually matters, since
raw DPS assumes every shell hits):

| | HowitzerSuper | Ground Shaker | Ratio |
|-----|---------------|---------------|-------|
| Raw DPS (every shell hits) | 5030 | 840 | 5.99× |
| Expected damage per shell, long | 910 | 175 | 5.20× |
| **Effective long-range DPS** | **3269** | **840** | **3.89×** |

Assets: models `gnhhowt-3x.pie` / `trhhowt-3x.pie` (local); FX and sounds borrowed from stock
Ground Shaker (`fxcammo.pie`, `fxmnexp.pie`, `fxhhowt.pie`, `fxlsplsh.pie`, `lrgcan.ogg`, `lrgexpl.ogg`);
`flightGfx` overridden to the local 5× shell `fxcammo-5x.pie`; `muzzleGfx` set to the Cannon
Fortress flash `fxshcanm.pie`.

Structure `X-Super-Howitzer`: 2×2 FORTRESS, armour 15 / thermal 15, buildPoints 2000 /
buildPower 1000 (matching the stock Cannon Fortress), hitpoints 4000, height 4,
`combinesWithWall: true`, `sensorID: "FortressSensor"`.

Research `R-Defense-Super-Howitzer`: prereqs `R-Wpn-HvyHowitzer` + `R-Defense-WallUpgrade05`,
14000 RP / 437 PW, `resultStructures: ["X-Super-Howitzer"]` → `statID: "X-Super-Howitzer"`.

## Implementation notes

* `minimumDamage` is a **percentage**, not a flat floor. The engine does
  `MAX(damage - armour, damage * minimumDamage / 100)` in `objDamage()` (`src/combat.cpp`),
  so it is an armour-piercing floor. Setting it above 100 inflates damage massively.
* Time fields (`firePause`, `reloadTime`, `radiusLife`, `periodicalDamageTime`) are multiplied
  by `WEAPON_TIME` (100), and `GAME_TICKS_PER_SEC` is **1000** (`lib/gamelib/gtime.h`), so
  a JSON value of `V` lasts `V/10` seconds.
* `radiusLife` only controls blast **visual** linger — it deals no damage. Splash damage is
  applied once at impact regardless.
* Setting any `periodicalDamageRadius` + `periodicalDamageTime` also calls `tileSetFire()`,
  which applies hardcoded afterburn damage not exposed in the stats JSON. Accepted as a
  known side effect.
* `weight` above `HEAVY_WEAPON_WEIGHT` (50000) slows turret rotation in `actionTargetTurret()`
  (`src/action.cpp`). Not inert, despite non-designable weapons never reading it for cost.
* `numRounds: 1` means `reloadTime` is never applied, so a single-shot weapon's cycle is
  `firePause` alone. **Correction from earlier in this build:** `reloadTime: 100` was set
  on the Howitzer Super on the (wrong) assumption it was inert. Reading `combFire()` in
  `src/combat.cpp` shows ammo decrements per shot and a zero-ammo weapon waits out
  `reloadTime`, so on `numRounds: 1` it *does* apply — it was inflating the cycle by 10 s.
  The field has been removed from `HowitzerSuper` entirely. General cycle formula for
  multi-round weapons: `(numRounds - 1) × firePause + reloadTime`.
* Item 2 "blue missiles" may require a new blue-flight `.pie` in `components/weapons/`, or
  reuse of an existing missile pie. No in-flight sound field exists in the engine — only
  `weaponWav` and `explosionWav`.
* Item 4: base `FortressSensor.range` is 1536; Wide Spectrum Tower is 2048 (the Wide Spectrum
  Sensor *component* is 2260). Sensor range upgrades in base use
  `results: [{"class": "Sensor", "parameter": "Range", "value": <percent>}]`.
* Research `results[]` filters are **generic** — `src/research.cpp` compares `filterValue`
  against the raw stats JSON object, so `filterParameter: "Id"` works on any class
  (verified). This is what lets item 4 target only `FortressSensor`; base's own sensor
  upgrades have no filter and buff every sensor in the game.
* The engine skips applying a research `results[]` entry when the target stat's current
  value is `<= 0`, so any stat modified by research must have a positive base value.
* All numeric stat fields must be integers (engine applies `toUInt()`).
* Raw DPS is misleading for low-accuracy artillery — it assumes every shell hits. Always
  report effective DPS (`damage × hitChance × RPM`) alongside it.
* Run `python3 check.py` (online — do not use `--offline`, base refs must be verified
  against upstream `master`) after implementing — exit 0 = pass. Currently
  0 errors, 0 warnings.