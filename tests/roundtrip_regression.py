"""DTS round-trip regression test.

Imports a .dts into Blender via this addon, exports it back out, then compares
the structure of the original and the round-tripped file (counts + per-mesh
geometry + decoded spatial bounds). Use it to prove a model still round-trips
before/after a change -- especially weapons, which regressed historically.

Run (folder name has spaces, so run via Blender, not bare python):

    blender --background --factory-startup \
        --python tests/roundtrip_regression.py -- <input.dts> [output.dts]

If output.dts is omitted, a temp file is used. Exit code is non-zero on FAIL.

No .dts fixture is committed (test assets stay local). Point it at any local
DTS, e.g. the gitignored Axe.dts.
"""
import bpy, sys, os, tempfile, shutil, traceback

# --- locate the addon source (parent dir of tests/) and stage a temp package ---
ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _stage_package():
    """Copy the addon .py/.ksy into a temp package with an importable name.

    The installed folder is "Tribes DTS Blender" (spaces) which Python can't
    import directly, so we mirror the sources into tribes_dts_test/.
    """
    tmp = tempfile.mkdtemp(prefix="tribes_dts_test_")
    pkg = os.path.join(tmp, "tribes_dts_test")
    os.makedirs(pkg)
    for fn in os.listdir(ADDON_DIR):
        if fn.endswith(".py") or fn.endswith(".ksy"):
            shutil.copy(os.path.join(ADDON_DIR, fn), os.path.join(pkg, fn))
    sys.path.insert(0, tmp)
    return tmp, pkg

def _parse_args():
    argv = sys.argv
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    if not args:
        print("FAIL: no input .dts given. Pass it after '--'.")
        sys.exit(2)
    src = args[0]
    dst = args[1] if len(args) > 1 else os.path.join(
        tempfile.gettempdir(), "dts_roundtrip_out.dts")
    return src, dst

def summarize(path, Dts):
    d = Dts.from_file(path)
    s = d.shape.data.obj_data
    counts = {f: getattr(s, f, None) for f in (
        'num_nodes', 'num_transforms', 'num_names',
        'num_objects', 'num_details', 'num_meshes')}
    meshes = []
    for m in d.meshes:
        verts = getattr(m, 'vertices', []) or []
        fr = getattr(m, 'frames', None)
        if fr and verts:
            sc, og = fr[0].scale, fr[0].origin
            xs = [v.x * sc.x + og.x for v in verts]
            ys = [v.y * sc.y + og.y for v in verts]
            zs = [v.z * sc.z + og.z for v in verts]
            bounds = (round(min(xs), 3), round(max(xs), 3),
                      round(min(ys), 3), round(max(ys), 3),
                      round(min(zs), 3), round(max(zs), 3))
        else:
            bounds = None
        meshes.append({'nv': getattr(m, 'num_vertices', 0),
                       'nf': getattr(m, 'num_faces', 0),
                       'bounds': bounds})
    return {'size': os.path.getsize(path), 'counts': counts, 'meshes': meshes}

def compare(orig, rt):
    """Return list of human-readable discrepancies (empty == PASS)."""
    issues = []
    for k, ov in orig['counts'].items():
        rv = rt['counts'].get(k)
        if ov != rv:
            issues.append(f"count {k}: {ov} -> {rv}")
    if len(orig['meshes']) != len(rt['meshes']):
        issues.append(f"mesh count: {len(orig['meshes'])} -> {len(rt['meshes'])}")
    else:
        for i, (om, rm) in enumerate(zip(orig['meshes'], rt['meshes'])):
            if om['nv'] != rm['nv']:
                issues.append(f"mesh{i} verts: {om['nv']} -> {rm['nv']}")
            if om['nf'] != rm['nf']:
                issues.append(f"mesh{i} faces: {om['nf']} -> {rm['nf']}")
            if om['bounds'] and rm['bounds']:
                # allow small float drift; flag meaningful spatial divergence
                drift = max(abs(a - b) for a, b in zip(om['bounds'], rm['bounds']))
                if drift > 0.01:
                    issues.append(
                        f"mesh{i} bounds drift {drift:.3f}: "
                        f"{om['bounds']} -> {rm['bounds']}")
    return issues

def main():
    src, dst = _parse_args()
    tmp, pkg = _stage_package()
    try:
        import tribes_dts_test as addon
        from tribes_dts_test.dts import Dts
        addon.register()

        bpy.ops.wm.read_factory_settings(use_empty=True)
        print(f"=== IMPORT {src} ===")
        bpy.ops.dynamix.dts(filepath=src)
        print("objects:", [o.name for o in bpy.data.objects])

        bpy.ops.object.select_all(action='SELECT')
        if bpy.data.objects:
            bpy.context.view_layer.objects.active = bpy.data.objects[0]

        print(f"=== EXPORT {dst} ===")
        bpy.ops.export_mesh.dts(filepath=dst, original_dts_path=src)

        orig = summarize(src, Dts)
        rt = summarize(dst, Dts)
        print(f"\nORIGINAL : {orig['size']} bytes  {orig['counts']}")
        for i, m in enumerate(orig['meshes']):
            print(f"  mesh{i}: nv={m['nv']} nf={m['nf']} bounds={m['bounds']}")
        print(f"ROUNDTRIP: {rt['size']} bytes  {rt['counts']}")
        for i, m in enumerate(rt['meshes']):
            print(f"  mesh{i}: nv={m['nv']} nf={m['nf']} bounds={m['bounds']}")

        issues = compare(orig, rt)
        print("\n" + "=" * 50)
        if issues:
            print("RESULT: FAIL")
            for it in issues:
                print("  - " + it)
            sys.exit(1)
        print("RESULT: PASS (structure + geometry preserved)")
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

main()
