# Starsiege: Tribes DTS — Blender Import & Export Plugin

Import **and export** Starsiege: Tribes / Darkstar `.DTS` models in Blender, plus a
scripted pipeline for retargeting **Mixamo** animations onto Tribes characters and
injecting them back into game-ready DTS files.

This is a fork of the original import-only *TribesToBlender* addon, extended with a
DTS **exporter**, character animation tooling, and a round-trip regression test.

> Format support targets the Tribes shape format: `TS::Shape` v8, `TS::CelAnimMesh`
> v3, `TS::MaterialList` v4 (the same structures used by the retail game and the
> Kingdom of Kronos RPG mod).

---

## Installation

1. On GitHub: **Code → Download ZIP**.
2. In Blender: **Edit → Preferences → Add-ons → Install…** and select the ZIP.
3. Enable **"Tribes DTS Format"**.

Once enabled:

- **File → Import → Tribes DTS (.dts)**
- **File → Export → Tribes DTS (.dts)**

Developed against Blender 3.0+ (current work is on 5.0).

---

## Features

### Import
- Static, animated, collision, debris, and hulk meshes
- Levels of Detail (LODs)
- Node/sequence animations (imported as empties + timeline markers)
- **Vertex-morph (frame track) animation** — morph frames import as shape keys
  (decoded with each frame's own scale/origin) with their playback keyframed on
  the timeline; survives edit + re-export (Sensor Jammer, bows, monsters,
  vehicle flames)
- Textures (auto-applied when image files sit next to the `.dts`)
- IFL sequences (animated materials)
- Armors with bones, and vehicles

### Export
- Write meshes and node/sequence animation back out to `.DTS`
- **Character round-trips** (import → edit → export) are the validated path
- Material list and LOD structure preserved
- **Multi-texture models** — multiple single-material meshes with correct material
  `fIndex` (a texture material's index is 0; the per-mesh split avoids the
  mixed-material-per-mesh crash on equip)
- **Vertex-morph (shape-key) animation** — bake any source animation into per-frame
  shape keys and export a `CelAnimMesh` frame-track; used to give weapons their own
  swing/attack animation

### Animation pipeline (`tools/`)
A headless workflow for getting new character animations into the game:

- **`retarget_mixamo.py`** — retarget a Mixamo FBX onto a Tribes character
  skeleton and emit a per-node keyframe sidecar (`.json`). World-orientation copy
  for body/legs/spine, direction-aim for the arms.
- **`patch_dts_animation.py`** — inject a retargeted animation into a DTS by
  **replacing a named sequence** (the engine plays animations by sequence name,
  e.g. `run`, `root`). Operates directly on the DTS binary, with cyclic and
  duration options (`keep` preserves the original timing to avoid foot-sliding on
  speed-synced locomotion).
- **`render_preview.py`** — quick Workbench renders of a DTS for visual QA.

See [`tools/README.md`](tools/README.md) for exact command lines.

### Tests
- **`tests/roundtrip_regression.py`** — imports a DTS, exports it, and compares
  geometry/bounds/texverts/materials to catch round-trip regressions.

---

## Repository layout

| Path | Purpose |
|------|---------|
| `main.py`, `export_dts.py` | Import / export operators |
| `dts.py`, `dts.ksy`, `kaitaistruct.py` | DTS binary parser (Kaitai-based) |
| `tools/` | Mixamo retarget, animation patcher, render preview |
| `tests/` | Round-trip regression test |
| `docs/` | Format reference, character/animation guides, Mixamo workflow |

Useful docs:
[`docs/darkstar_dts_master_reference.md`](docs/darkstar_dts_master_reference.md),
[`docs/dts_character_creation_guide.md`](docs/dts_character_creation_guide.md),
[`docs/MIXAMO_WORKFLOW.md`](docs/MIXAMO_WORKFLOW.md),
[`docs/MIXAMO_BONE_MAPPING.md`](docs/MIXAMO_BONE_MAPPING.md).

---

## Status & known issues

- **Characters:** import, export, round-trip, and Mixamo animation injection work
  in-game (players and AI bots).
- **Weapons:** the proven authoring path is to **transplant** new geometry into a
  known-good donor weapon DTS (keeping its mount/animation/bounds/structure), which
  gives a correct in-hand mount for free. Pick the donor by class so the weapon
  inherits a fitting animation (broadsword/katana/elfinblade for swords, battleaxe
  for axes, mace/hammer for bludgeons, spear/trident for polearms, dagger/knife).
  Custom animation is added by baking a shape-key morph and injecting it as a
  frame-track. This pipeline shipped **46 weapons/shields/orbs** into the Kingdom of
  Kronos RPG. A fully general *fresh* (donor-less) weapon exporter is still open.
- **Textures:** weapon/shield skins are 8-bit MS-BMP indexed to a world multipalette
  (`bfReserved2` = paletteIndex); the orb accessory shape needs native **PBMP**.
- Animated UVs are not supported.
- Import one model per scene — importing several at once can break the hierarchy
  and overlap timeline markers.

## Wishlist
- Robust fresh weapon export (no donor transplant needed)
- Bone-based animation (auto-create bones, actions instead of markers)
- Animated UVs
- Support for `DIS`, `TED`, and `DIL` files

## Credits
Fork of the original *TribesToBlender* import addon, extended with export and the
animation pipeline for the Kingdom of Kronos RPG project.
