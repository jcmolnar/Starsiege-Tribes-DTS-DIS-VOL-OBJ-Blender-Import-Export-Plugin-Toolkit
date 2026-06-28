"""Retarget a Mixamo FBX animation onto a Tribes DTS character skeleton.

Bakes the animation onto the imported DTS node-empties and saves a .blend you
can review/export. Proven on rpgmalehuman with a cyclic walk and an acrobatic
flair. See memory dts-animation-model.md for the full rationale.

Run via Blender (the addon folder name has spaces, so this stages the addon
sources into a temp importable package):

    blender --background --factory-startup \
        --python tools/retarget_mixamo.py -- <character.dts> <anim.fbx> <out.blend>

Defaults (if args omitted): .dts files/rpgmalehuman.dts, Animations/Walk.fbx,
Animations/retarget_out.blend (paths resolved relative to the addon folder).

Method summary:
  - Rest pose captured from BLENDER matrices at frame 0 (the importer stores
    rotations in an inverted (-w,x,y,z) convention, so don't recompose DTS quats).
  - World alignment A = Rz(180): both rigs are Z-up; they differ only by facing
    (Mixamo faces -Y, Tribes +Y). Deriving A from bone rest frames adds a twist.
  - Body/legs/spine/head: WORLD-ORIENTATION-COPY  worldq = (A D A^-1) restW,
    D = Wsrc(t) Wsrc_rest^-1. Drive VICON too (e.g. zombie hunch is intended).
  - Arms (humerus, radius): direction-AIM. The Tribes arm rest is tucked while
    Mixamo rest is a T-pose, so delta methods fold the forearm into the torso.
    Instead point each arm segment along the A-rotated Mixamo bone direction.
  - Same local rotations apply to all LODs (36/10/2 share an identical skeleton).
  - In-place clips only (rotation-only; no root translation).
"""
import bpy, os, sys, tempfile, shutil, math
from mathutils import Quaternion, Vector

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _args():
    argv = sys.argv
    a = argv[argv.index("--") + 1:] if "--" in argv else []
    dts = a[0] if len(a) > 0 else os.path.join(ADDON, ".dts files", "rpgmalehuman.dts")
    fbx = a[1] if len(a) > 1 else os.path.join(ADDON, "Animations", "Zombie Walk.fbx")
    out = a[2] if len(a) > 2 else os.path.join(ADDON, "Animations", "retarget_out.blend")
    return dts, fbx, out

# Tribes base node -> Mixamo source bone
TGT2SRC = {
    "VICON": "mixamorig:Hips",
    "lowerback": "mixamorig:Spine", "thorax": "mixamorig:Spine2", "head": "mixamorig:Head",
    "rhumerus": "mixamorig:RightArm", "rradius": "mixamorig:RightForeArm", "dummy hand": "mixamorig:RightHand",
    "lhumerus": "mixamorig:LeftArm", "lradius": "mixamorig:LeftForeArm",
    "rfemur": "mixamorig:RightUpLeg", "rtibia": "mixamorig:RightLeg", "rfoot": "mixamorig:RightFoot",
    "lfemur": "mixamorig:LeftUpLeg", "ltibia": "mixamorig:LeftLeg", "lfoot": "mixamorig:LeftFoot",
}
AIM = {"rhumerus", "rradius", "lhumerus", "lradius"}                 # direction-aim these
AIMCHILD = {"rhumerus": "rradius", "lhumerus": "lradius",
            "rradius": "dummy hand", "lradius": "submesh_larm"}      # joint each arm bone aims toward

def base_of(nm):
    b = nm[:-2] if nm[-2:] in ("36", "10") else (nm[:-1] if nm[-1:] == "2" else nm)
    return b.rstrip()

def lod_of(nm):
    return "36" if nm.endswith("36") else ("10" if nm.endswith("10") else ("2" if nm.endswith("2") else ""))

def _stage_addon_pkg():
    tmp = tempfile.mkdtemp(prefix="tribes_dts_")
    pkg = os.path.join(tmp, "tribes_dts_pkg")
    os.makedirs(pkg)
    for fn in os.listdir(ADDON):
        if fn.endswith((".py", ".ksy")):
            shutil.copy(os.path.join(ADDON, fn), os.path.join(pkg, fn))
    sys.path.insert(0, tmp)
    return tmp

def main():
    dts, fbx, out = _args()
    tmp = _stage_addon_pkg()
    try:
        import tribes_dts_pkg as addon
        addon.register()

        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.dynamix.dts(filepath=dts)
        tribes = {o.name: o for o in bpy.data.objects}

        # Rest from Blender matrices (frame 0 = before first imported keyframe)
        bpy.context.scene.frame_set(0); bpy.context.view_layer.update()
        restW = {n: o.matrix_world.to_quaternion() for n, o in tribes.items()}
        restL_rot = {n: o.matrix_local.to_quaternion() for n, o in tribes.items()}
        restL_loc = {n: o.matrix_local.translation.copy() for n, o in tribes.items()}
        parent = {n: (o.parent.name if o.parent else None) for n, o in tribes.items()}

        def depth(n):
            d = 0; p = parent[n]
            while p: d += 1; p = parent[p]
            return d
        order = sorted(tribes, key=depth)

        def find_child(n, cbase):
            for k in tribes:
                if base_of(k) == cbase and lod_of(k) == lod_of(n):
                    return k
            return None

        for o in tribes.values():
            o.animation_data_clear(); o.rotation_mode = 'QUATERNION'
        for n, o in tribes.items():
            o.rotation_quaternion = restL_rot[n]; o.location = restL_loc[n]

        bpy.ops.import_scene.fbx(filepath=fbx)
        arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
        AW = arm.matrix_world
        f0, f1 = (int(arm.animation_data.action.frame_range[0]),
                  int(arm.animation_data.action.frame_range[1]))

        def sw(mb):  return (AW @ arm.pose.bones[mb].matrix).to_quaternion()
        def swr(mb): return (AW @ arm.pose.bones[mb].bone.matrix_local).to_quaternion()
        def sdir(mb):  # Mixamo bone world direction (bone local +Y is along the bone)
            return ((AW @ arm.pose.bones[mb].matrix).to_3x3().col[1]).normalized()

        Wsrc_rest = {mb: swr(mb) for mb in set(TGT2SRC.values())}
        A = Quaternion((0, 0, 1), math.pi)

        # Sidecar of per-node local keyframes for the DTS animation patcher.
        sidecar = {"fps": 30, "frames": f1 - f0 + 1, "nodes": {}}

        for f in range(f0, f1 + 1):
            bpy.context.scene.frame_set(f)
            Dd = {mb: (sw(mb) @ Wsrc_rest[mb].inverted()) for mb in set(TGT2SRC.values())}
            worldq = {}
            for n in order:
                p = parent[n]; b = base_of(n); src = TGT2SRC.get(b)
                pq = worldq[p] if (p and p in worldq) else (restW[p] if p else Quaternion((1, 0, 0, 0)))
                if src and b in AIM:
                    child = find_child(n, AIMCHILD[b])
                    cdir = (restL_loc[child]).normalized() if child else Vector((0, 0, 1))
                    base_world = pq @ restL_rot[n]
                    base_dir = base_world @ cdir
                    tgt_dir = (A @ sdir(src)).normalized()
                    worldq[n] = base_dir.rotation_difference(tgt_dir) @ base_world
                elif src:
                    worldq[n] = (A @ Dd[src] @ A.inverted()) @ restW[n]
                else:
                    worldq[n] = pq @ restL_rot[n]
                if src:
                    o = tribes[n]
                    q = pq.inverted() @ worldq[n]
                    o.rotation_quaternion = q
                    o.keyframe_insert("rotation_quaternion", frame=f - f0 + 1)
                    lc = restL_loc[n]  # rotation-only retarget: keep rest translation
                    sidecar["nodes"].setdefault(n, []).append(
                        [q.w, q.x, q.y, q.z, lc.x, lc.y, lc.z])

        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = f1 - f0 + 1
        for o in [o for o in bpy.data.objects if o.type == 'ARMATURE']:
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.ops.wm.save_as_mainfile(filepath=out)
        import json
        sidecar_path = os.path.splitext(out)[0] + "_sidecar.json"
        json.dump(sidecar, open(sidecar_path, "w"))
        print(f"RETARGET OK: {os.path.basename(fbx)} -> {out} ({f1 - f0 + 1} frames)")
        print(f"SIDECAR: {sidecar_path} ({len(sidecar['nodes'])} nodes)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

main()
