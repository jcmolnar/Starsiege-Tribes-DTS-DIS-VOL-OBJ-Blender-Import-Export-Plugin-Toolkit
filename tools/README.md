# Tools

Helper scripts for the Tribes DTS Blender addon. Run them through Blender in
background mode (the addon folder name has spaces, so the scripts stage the
addon sources into a temp importable package automatically).

## retarget_mixamo.py
Retarget a Mixamo FBX animation onto a Tribes DTS character skeleton and save a
`.blend`.

```
blender --background --factory-startup \
    --python tools/retarget_mixamo.py -- "<character.dts>" "<anim.fbx>" "<out.blend>"
```

- Character + animation must share the standard Tribes character skeleton
  (VICON / lowerback / thorax / l|r humerus|radius|femur|tibia|foot, etc.).
- Mixamo export settings: FBX, 30 fps, **In Place** ON, Without Skin (animation)
  plus one T-Pose (With Skin) as a visual reference.
- In-place clips only (rotation is retargeted; root translation is not).
- Method: world-orientation-copy for body/legs/spine (alignment `A = Rz(180)`),
  direction-aim for the arms (Tribes arm rest is tucked vs Mixamo T-pose).

## patch_dts_animation.py
Inject a retargeted animation into a character DTS by **replacing a named
sequence** (the engine plays animations by sequence name — see
`docs/` and memory). Works directly on the DTS binary (reuses `DTSWriter`),
so it bypasses the Blender exporter and its node hard-filter.

```
blender --background --factory-startup --python tools/patch_dts_animation.py \
    -- "<original.dts>" "<output.dts>" "<sidecar.json>" "<seq_name>"
```

- `sidecar.json` is emitted by `retarget_mixamo.py` (per-node local keyframes).
- `seq_name` e.g. `run` (forward locomotion, made cyclic), `taunt 1` (one-shot).
- With no sidecar it does an identity rebuild (validates the read/write path).
- Geometry, materials, and all other sequences are preserved verbatim.

Full pipeline:
```
# 1. retarget -> blend + sidecar
blender ... --python tools/retarget_mixamo.py -- char.dts anim.fbx out.blend
# 2. patch the 'run' sequence into a new DTS
blender ... --python tools/patch_dts_animation.py -- char.dts char_run.dts out_sidecar.json run
```

## render_preview.py
Render a posed character `.blend` to front/side PNGs (LOD36 only) for review.

```
blender --background --factory-startup \
    --python tools/render_preview.py -- "<posed.blend>" <tag> [out_dir]
```

## Mixamo bone mapping
See `docs/MIXAMO_BONE_MAPPING.md` for the full Mixamo↔Tribes correspondence and
hierarchy.
