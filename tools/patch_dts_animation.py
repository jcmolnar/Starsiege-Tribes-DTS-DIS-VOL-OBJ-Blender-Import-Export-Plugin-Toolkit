"""Patch a named animation sequence into a Tribes character DTS.

Parses the original DTS, rebuilds the TS::Shape animation arrays (replacing one
named sequence's keyframes with retargeted data), and reattaches the original
mesh/material blocks verbatim. Reuses the addon's DTSWriter, so it runs inside
Blender.

    blender --background --factory-startup --python tools/patch_dts_animation.py \
        -- <original.dts> <output.dts> [sidecar.json] [seq_name]

With no sidecar -> IDENTITY rebuild (validates read/write plumbing).
sidecar.json: { "fps":30, "frames":N, "nodes": { "<nodeName>": [ [w,x,y,z, lx,ly,lz], ... ] } }
seq_name: which sequence to replace (default "run").
"""
import bpy, sys, os, struct, json, tempfile, shutil
from io import BytesIO

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _args():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(a) < 2:
        print("usage: -- <orig.dts> <out.dts> [sidecar.json] [seq_name]"); sys.exit(2)
    return a[0], a[1], (a[2] if len(a) > 2 else None), (a[3] if len(a) > 3 else "run")

def _stage():
    tmp = tempfile.mkdtemp(prefix="tribes_dts_")
    pkg = os.path.join(tmp, "tribes_dts_pkg"); os.makedirs(pkg)
    for fn in os.listdir(ADDON):
        if fn.endswith((".py", ".ksy")): shutil.copy(os.path.join(ADDON, fn), os.path.join(pkg, fn))
    sys.path.insert(0, tmp); return tmp

def find_pers(data):
    blocks = []; p = 0
    while True:
        p = data.find(b'PERS', p)
        if p == -1: break
        sz = struct.unpack('<I', data[p+4:p+8])[0]
        nl = struct.unpack('<H', data[p+8:p+10])[0]
        nm = data[p+10:p+10+nl].split(b'\x00')[0].decode('ascii', 'ignore')
        blocks.append({'pos': p, 'size': sz + 8, 'name': nm}); p += 4
    return blocks

def build_shape_data(s, names_str):
    Q = 32767.0
    def tf(t):
        r = t.rotate
        return {'rotation': (-r.x/Q, -r.y/Q, -r.z/Q, r.w/Q),
                'translation': (t.translate.x, t.translate.y, t.translate.z)}
    nodes = [{'name': n.name, 'parent': n.parent, 'num_subsequences': n.num_subsequences,
              'first_subsequence': n.first_subsequence, 'default_transform': n.default_transform}
             for n in s.nodes]
    seqs = [{'name': q.name, 'cyclic': q.cyclic, 'duration': q.duration, 'priority': q.priority,
             'first_frame_trigger': q.first_frame_trigger, 'num_frame_triggers': q.num_frame_triggers,
             'num_ifl_subsequences': q.num_ifl_subsequences, 'first_ifl_subsequence': q.first_ifl_subsequence}
            for q in s.sequences]
    subs = [{'sequence_index': u.sequence_index, 'num_keyframes': u.num_keyframes,
             'first_keyframe': u.first_keyframe} for u in s.subsequences]
    kfs = [{'position': k.position, 'key_value': k.key_value, 'mat_index': k.mat_index} for k in s.keyframes]
    xfs = [tf(t) for t in s.transforms]
    objs = [{'name': o.name, 'flags': o.flags, 'mesh_index': o.mesh_index, 'node_index': o.node_index,
             'offset': (o.object_offset.x, o.object_offset.y, o.object_offset.z),
             'num_subsequences': o.num_subsequences, 'first_subsequence': o.first_subsequence}
            for o in s.objects]
    dets = [{'root_node_index': d.root_node_index, 'size': d.size} for d in s.details]
    trans = [{'start_sequence': t.start_sequence, 'end_sequence': t.end_sequence,
              'start_position': t.start_position, 'end_position': t.end_position, 'duration': t.duration,
              'rotation': (t.rotation.x, t.rotation.y, t.rotation.z, t.rotation.w),
              'translation': (t.translation.x, t.translation.y, t.translation.z)} for t in s.transitions]
    trigs = [{'position': g.position, 'value': g.value} for g in s.frame_triggers]
    return {
        'num_nodes': s.num_nodes, 'num_sequences': s.num_seq, 'num_subsequences': s.num_subseq,
        'num_keyframes': s.num_keyframes, 'num_transforms': s.num_transforms,
        'num_objects': s.num_objects, 'num_details': s.num_details, 'num_meshes': s.num_meshes,
        'num_transitions': s.num_transitions, 'num_frame_triggers': s.num_frametriggers,
        'radius': s.radius, 'center': (s.center.x, s.center.y, s.center.z),
        'bounds_min': (s.bounds.min.x, s.bounds.min.y, s.bounds.min.z),
        'bounds_max': (s.bounds.max.x, s.bounds.max.y, s.bounds.max.z),
        'nodes': nodes, 'sequences': seqs, 'subsequences': subs, 'keyframes': kfs, 'transforms': xfs,
        'objects': objs, 'details': dets, 'transitions': trans, 'frame_triggers': trigs,
        'default_material': getattr(s, 'default_material', 1),
    }

def replace_sequence(sd, names_str, seq_name, sidecar):
    """Replace seq_name's keyframes in place.

    The subsequence ARRAY is referenced by both nodes and objects via
    first_subsequence, so we do NOT resize/reorder it. We walk it in array
    order, rebuild only the keyframe array (updating each subsequence's
    first_keyframe), and append new transforms. Node/object first_subsequence
    are left untouched.
    """
    seq_idx = next(i for i, q in enumerate(sd['sequences']) if names_str[q['name']] == seq_name)
    N = sidecar['frames']
    sd['sequences'][seq_idx]['cyclic'] = 1
    sd['sequences'][seq_idx]['duration'] = N / float(sidecar.get('fps', 30))

    # Map each subsequence index -> owning node name (transform tracks live on nodes)
    owner = {}
    for nd in sd['nodes']:
        for si in range(nd['first_subsequence'], nd['first_subsequence'] + nd['num_subsequences']):
            owner[si] = names_str[nd['name']]

    old_subs, old_kfs = sd['subsequences'], sd['keyframes']
    new_kfs = []
    new_xfs = list(sd['transforms'])      # keep original indices valid for untouched keyframes
    replaced = 0
    for si, sub in enumerate(old_subs):
        fk = len(new_kfs)
        node_name = owner.get(si)
        if sub['sequence_index'] == seq_idx and node_name in sidecar['nodes']:
            for fi, (w, x, y, z, lx, ly, lz) in enumerate(sidecar['nodes'][node_name]):
                kv = len(new_xfs)
                new_xfs.append({'rotation': (x, y, z, w), 'translation': (lx, ly, lz)})
                new_kfs.append({'position': fi / float(max(1, N - 1)), 'key_value': kv, 'mat_index': 0})
            sub['num_keyframes'] = N
            replaced += 1
        else:
            for k in range(sub['first_keyframe'], sub['first_keyframe'] + sub['num_keyframes']):
                new_kfs.append(dict(old_kfs[k]))
        sub['first_keyframe'] = fk
    sd['keyframes'] = new_kfs
    sd['transforms'] = new_xfs
    sd['num_keyframes'] = len(new_kfs)
    sd['num_transforms'] = len(new_xfs)
    print(f"  replaced {replaced} node subsequences for '{seq_name}'")

def main():
    orig_path, out_path, sidecar_path, seq_name = _args()
    tmp = _stage()
    try:
        from tribes_dts_pkg.dts import Dts
        from tribes_dts_pkg.export_dts import DTSWriter
        data = open(orig_path, 'rb').read()
        d = Dts.from_file(orig_path); s = d.shape.data.obj_data
        names_str = [n.split(b'\x00')[0].decode('ascii', 'ignore') for n in s.names]
        sd = build_shape_data(s, names_str)

        if sidecar_path:
            sidecar = json.load(open(sidecar_path))
            replace_sequence(sd, names_str, seq_name, sidecar)
            print(f"replaced '{seq_name}': subs={sd['num_subsequences']} kfs={sd['num_keyframes']} xfs={sd['num_transforms']}")
        else:
            print("IDENTITY rebuild (no sidecar)")

        w = DTSWriter(); w.shape_version = 8
        w.names = [names_str[i] for i in range(len(names_str))]
        arr = BytesIO(); w.write_ts_shape(arr, sd); arr = arr.getvalue()

        blocks = find_pers(data)
        outer = blocks[0]                          # TS::Shape
        first_mesh = next(b for b in blocks[1:] if b['name'] == 'TS::CelAnimMesh')
        meshes_and_mats = data[first_mesh['pos']: outer['pos'] + outer['size']]
        new_content = arr + meshes_and_mats

        out = BytesIO()
        w.write_pers_header(out, 'TS::Shape', len(new_content), 8)
        out.write(new_content)
        out.write(data[outer['pos'] + outer['size']:])   # trailing (usually none)
        open(out_path, 'wb').write(out.getvalue())
        print(f"WROTE {out_path}  ({len(out.getvalue())} bytes, orig {len(data)})")

        # verify re-parse
        d2 = Dts.from_file(out_path); s2 = d2.shape.data.obj_data
        n2 = [n.split(b'\x00')[0].decode('ascii', 'ignore') for n in s2.names]
        print(f"VERIFY: nodes {s2.num_nodes} seq {s2.num_seq} subs {s2.num_subseq} kfs {s2.num_keyframes} xfs {s2.num_transforms} meshes {s2.num_meshes}")
        print(f"VERIFY: sequences match = {[n2[q.name] for q in s2.sequences] == [names_str[q['name']] for q in sd['sequences']]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

main()
