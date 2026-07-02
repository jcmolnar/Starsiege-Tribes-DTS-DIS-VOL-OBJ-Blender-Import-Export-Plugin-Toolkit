r"""DTS visual verification viewer.

Renders one or more Tribes .DTS files in a self-contained HTML page (three.js)
so round-trips can be verified visually WITHOUT copying files into the game and
restarting the client. Parses the binary directly with the addon's dts.py --
no Blender required.

Features:
- multiple models side by side (e.g. original vs round-trip)
- textures embedded as data URIs (BMP/PNG found next to each .dts)
- node hierarchy posed from default transforms (Z-up, like the engine)
- sequence playback: node transform tracks (slerp) + vertex-morph frame tracks
- detail-level (LOD) selection, wireframe and double-side toggles

Usage:
    python tools/dts_viewer.py model.dts [more.dts ...] -o viewer.html
    python tools/dts_viewer.py original.dts roundtrip.dts   # side-by-side

Mounting (engine-accurate attachment, from the Darkstar source):
    --equip weapon.dts    attach to model 0's "dummy hand" node -- exactly how
                          Player::getImageTransform mounts a PrimaryMount
                          ItemImage (mountPoint 0, zero offset like KoK weapons)
    --pilot player.dts    attach to model 0's "dummy pilot" node -- exactly how
                          Vehicle::getObjectMountTransform seats the pilot
                          (mountPoint 1)
    --attach file.dts:nodePrefix[:ox,oy,oz[:rx,ry,rz]]
                          generic: attach to any node of model 0 (prefix match,
                          e.g. "dummy passenger1", "dummy exit"), with optional
                          mountOffset / mountRotation (radians) like
                          ItemImageData

    python tools/dts_viewer.py rpgmalehuman.dts --equip Axe.dts
    python tools/dts_viewer.py dragon_flyer.DTS --pilot rpgmalehuman.dts

Game-accurate textures: pass --voldir <game>\base (recursive) and textures
are pulled from .vol archives, including Darkstar PBMP bitmaps colored via
the world .ppl multipalette (auto-discovered; override with --ppl). Material
flags render like the engine: 0x1000 transparent cutout, 0x2000 translucent,
ShadingNone fullbright (flames/glows), with palette index 0 transparent.

    python tools/dts_viewer.py newflyer.DTS --pilot player.dts \
        --voldir "C:\Dynamix\Tribes\base"

Output defaults to dts_viewer.html next to the first input.
"""
import argparse
import base64
import io
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(_TOOLS_DIR)
sys.path.insert(0, _ADDON_DIR)

from dts import Dts  # noqa: E402

FLAG_FRAME_TRACK = 0x1000
FLAG_VISIBILITY = 0x8000


def _name(n):
    if isinstance(n, bytes):
        n = n.split(b'\x00')[0].decode('ascii', 'ignore')
    return n.split('\x00')[0].strip()


def _get(s, *names):
    for nm in names:
        v = getattr(s, nm, None)
        if v:
            return v
    return []


def read_vol_index(volpath):
    """Index a Darkstar .vol archive: {filename.lower(): (offset, size)}.

    Format (engine Core/volstrm.cpp): 'PVOL' magic, u32 pointer to the trailer
    blocks; trailer = 'vols' block (null-terminated name strings) followed by
    'voli' block of packed 17-byte items (id u32, name-offset u32, blockOffset
    u32, size u32, compressType u8). File bytes live at blockOffset + 8
    (skipping the 'VBLK' sub-block header).
    """
    import struct
    index = {}
    with open(volpath, 'rb') as f:
        data = f.read()
    if data[:4] not in (b'PVOL', b'VOL '):
        return index
    (trailer_off,) = struct.unpack_from('<I', data, 4)
    p = trailer_off
    strings = b''
    items = b''
    while p + 8 <= len(data):
        tag = data[p:p + 4]
        (size,) = struct.unpack_from('<I', data, p + 4)
        size &= 0x00FFFFFF
        body = data[p + 8:p + 8 + size]
        if tag == b'vols':
            strings = body
        elif tag == b'voli':
            items = body
            break
        p += 8 + size + (size & 1)
    for off in range(0, len(items) - 16, 17):
        _id, s_off, blk_off, fsize, ctype = struct.unpack_from('<IIIIB', items, off)
        if s_off >= len(strings):
            continue
        name = strings[s_off:strings.find(b'\x00', s_off)].decode('ascii', 'ignore')
        if not name:
            continue
        if ctype != 0:
            continue  # compressed entries unsupported; engine uses 0 for skins
        index[name.lower()] = (blk_off + 8, fsize)
    return index


def parse_ppl(data):
    """Parse a PL98 multipalette (.ppl): {paletteIndex: [(r,g,b)]*256}.

    Also keyed by None for the first table (fallback). Stock PBMP textures
    carry only a PiDX palette index; the colors live in the world palette.
    """
    import struct
    if data[:4] != b'PL98':
        raise ValueError('not a PL98 palette')
    (num_pal,) = struct.unpack_from('<i', data, 4)
    off = 4 + 16 + 32   # magic + header ints + allowedColorMatches bitvector
    tables = {}
    for _ in range(num_pal):
        colors = [tuple(data[off + c * 4: off + c * 4 + 3]) for c in range(256)]
        off += 1024
        (pidx, _ptype) = struct.unpack_from('<ii', data, off)
        off += 8
        tables[pidx] = colors
        tables.setdefault(None, colors)
    return tables


def parse_pbmp(data):
    """Parse a Darkstar PBMP (GFXBitmap): (width, height, level-0 index
    bytes, paletteIndex, embedded_palette or None). Chunked format from
    engine Dgfx/g_bitmap.cpp: head / data / DETL / PiDX / RIFF."""
    import struct
    off = 0
    width = height = 0
    indices = None
    palette_index = None
    embedded = None
    while off + 8 <= len(data):
        cid = data[off:off + 4]
        (csize,) = struct.unpack_from('<I', data, off + 4)
        off += 8
        if cid == b'PBMP':
            continue                       # outer wrapper; size covers rest
        if cid == b'head':
            _ver, width, height, _depth, _attr = struct.unpack_from('<5I', data, off)
        elif cid == b'data':
            indices = data[off:off + csize]
        elif cid == b'RIFF':
            # embedded MS palette: skip 20-byte RIFF/PAL header to entries
            pal = data[off + 20:off + 20 + 1024]
            embedded = [tuple(pal[c * 4:c * 4 + 3]) for c in range(256)]
        elif cid == b'PiDX':
            (palette_index,) = struct.unpack_from('<I', data, off)
        off += csize
    if not (width and height and indices):
        raise ValueError('incomplete PBMP')
    return width, height, indices[:width * height], palette_index, embedded


def _texture_data_uri(map_file, search_dirs, vols=None, translucent=False,
                      ppl_tables=None):
    """Find map_file (or .png/.bmp sibling) and return a PNG data URI.

    Search order: loose files next to the .dts / --texdir folders, then .vol
    archives. Handles MS BMP / PNG (via Pillow) AND raw Darkstar PBMP
    (indexed via the world .ppl multipalette). For translucent/transparent
    materials, palette index 0 becomes transparent (engine convention).
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    def encode(img):
        if translucent and img.mode == 'P':
            # palette index 0 = transparent (engine translucent convention)
            idx = img.point(lambda v: 255 if v else 0, mode='L')
            img = img.convert('RGBA')
            img.putalpha(idx)
        else:
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')

    def raw_to_uri(raw):
        if raw[:4] == b'PBMP':
            w, h, idx, pidx, embedded = parse_pbmp(raw)
            pal = embedded
            if pal is None and ppl_tables:
                pal = ppl_tables.get(pidx) or ppl_tables.get(None)
            if pal is None:
                pal = [(v, v, v) for v in range(256)]   # grayscale fallback
            img = Image.frombytes('P', (w, h), idx)
            img.putpalette([c for rgb in pal for c in rgb])
            return encode(img)
        return encode(Image.open(io.BytesIO(raw)))

    base = os.path.splitext(map_file)[0]
    candidates = []
    for d in search_dirs:
        for ext in ('.png', '.PNG', '.bmp', '.BMP'):
            candidates.append(os.path.join(d, base + ext))
        candidates.append(os.path.join(d, map_file))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, 'rb') as f:
                    return raw_to_uri(f.read())
            except Exception:
                continue

    # fall back to vol archives
    for want in (map_file.lower(), base.lower() + '.bmp', base.lower() + '.png'):
        for volpath, index in (vols or {}).items():
            hit = index.get(want)
            if hit:
                off, size = hit
                with open(volpath, 'rb') as f:
                    f.seek(off)
                    raw = f.read(size)
                try:
                    return raw_to_uri(raw)
                except Exception:
                    continue
    return None


def extract_model(path, texdirs=None, vols=None, ppl_tables=None):
    d = Dts.from_file(path)
    s = d.shape.data.obj_data
    names = [_name(n) for n in s.names]

    nodes_raw = _get(s, 'nodes', 'nodes_v7')
    transforms_raw = _get(s, 'transforms', 'transforms_v7')
    objects_raw = _get(s, 'objects', 'objects_v7')
    subsequences = _get(s, 'subsequences', 'subsequences_v7')
    keyframes = _get(s, 'keyframes', 'keyframes_v7')
    sequences = getattr(s, 'sequences', []) or []
    details_raw = getattr(s, 'details', []) or []

    transforms = [{
        'q': [t.rotate.x / 32767.0, t.rotate.y / 32767.0,
              t.rotate.z / 32767.0, t.rotate.w / 32767.0],
        't': [t.translate.x, t.translate.y, t.translate.z],
    } for t in transforms_raw]

    nodes = []
    for n in nodes_raw:
        # node transform subsequences -> per-sequence keyframe tracks
        tracks = {}
        for si in range(n.num_subsequences):
            sub = subsequences[n.first_subsequence + si]
            kfs = []
            for ki in range(sub.num_keyframes):
                kf = keyframes[sub.first_keyframe + ki]
                kfs.append({'p': round(kf.position, 5),
                            'v': kf.key_value,
                            'vis': 0 if (kf.mat_index & FLAG_VISIBILITY) == 0
                                   and kf.mat_index != 0 else 1})
            tracks[sub.sequence_index] = kfs
        nodes.append({
            'name': names[n.name] if 0 <= n.name < len(names) else '?',
            'parent': n.parent,
            'dt': n.default_transform,
            'tracks': tracks,
        })

    seqs = [{'name': names[q.name] if 0 <= q.name < len(names) else '?',
             'cyclic': int(q.cyclic), 'duration': round(q.duration, 4)}
            for q in sequences]

    details = [{'root': det.root_node_index, 'size': round(det.size, 2)}
               for det in details_raw]

    # materials
    mats = []
    search_dirs = [os.path.dirname(os.path.abspath(path))] + list(texdirs or [])
    if getattr(d, 'materials', None):
        for p in d.materials.params:
            mf = _name(getattr(p, 'map_file', '') or '')
            flags = getattr(p, 'flags', 0)
            # ts_material.h TextureType: 0x1000 Transparent (cutout),
            # 0x2000 Translucent (blend); palette index 0 renders
            # transparent in-engine for both
            translucent = bool(flags & 0x3000)
            uri = (_texture_data_uri(mf, search_dirs, vols=vols,
                                     translucent=translucent,
                                     ppl_tables=ppl_tables)
                   if mf else None)
            rgb = getattr(p, 'rgb', None)
            color = [rgb.red, rgb.green, rgb.blue] if rgb else [200, 200, 200]
            mats.append({'map': mf, 'uri': uri, 'color': color,
                         'flags': flags})

    # objects + meshes
    objs = []
    for o in objects_raw:
        mesh = d.meshes[o.mesh_index]
        frames = _get(mesh, 'frames', 'frames_v2')
        nvpf = getattr(mesh, 'num_vertices_per_frame', 0) or 0
        faces_raw = getattr(mesh, 'faces', []) or []
        if not faces_raw or not nvpf:
            continue  # degenerate placeholder / bounds

        verts = mesh.vertices
        tvs = getattr(mesh, 'texture_vertices', []) or []

        frame_positions = []
        for fr in frames:
            sc = getattr(fr, 'scale', None) or getattr(mesh, 'scale_v2', None)
            og = getattr(fr, 'origin', None) or getattr(mesh, 'origin_v2', None)
            first = getattr(fr, 'first_vert', 0)
            flat = []
            for i in range(nvpf):
                v = verts[first + i]
                flat.extend((round(v.x * sc.x + og.x, 4),
                             round(v.y * sc.y + og.y, 4),
                             round(v.z * sc.z + og.z, 4)))
            frame_positions.append(flat)

        faces = []
        for f in faces_raw:
            tri = []
            for vp in f.vip:
                u_ = tvs[vp.texture_index].x if vp.texture_index < len(tvs) else 0.0
                v_ = tvs[vp.texture_index].y if vp.texture_index < len(tvs) else 0.0
                tri.append([vp.vertex_index, round(u_, 5), round(v_, 5)])
            faces.append({'v': tri, 'm': f.material})

        # object-level tracks (frame track morphs / visibility)
        otracks = {}
        n_sub = getattr(o, 'num_subsequences', 0) or 0
        first_sub = getattr(o, 'first_subsequence', 0) or 0
        for si in range(n_sub):
            sub = subsequences[first_sub + si]
            kfs = []
            for ki in range(sub.num_keyframes):
                kf = keyframes[sub.first_keyframe + ki]
                kfs.append({'p': round(kf.position, 5), 'v': kf.key_value,
                            'ft': 1 if (kf.mat_index & FLAG_FRAME_TRACK) else 0})
            otracks[sub.sequence_index] = kfs

        off = getattr(o, 'object_offset', None)
        if off is not None and hasattr(off, 'x'):
            offset = [off.x, off.y, off.z]
        elif off is not None and hasattr(off, 'p'):  # tmat3f (v7)
            offset = [off.p.x, off.p.y, off.p.z]
        else:
            offset = [0.0, 0.0, 0.0]

        objs.append({
            'name': names[o.name] if 0 <= o.name < len(names) else '?',
            'node': o.node_index,
            'offset': offset,
            'frames': frame_positions,
            'faces': faces,
            'tracks': otracks,
        })

    return {
        'label': os.path.basename(path),
        'radius': getattr(s, 'radius', 1.0),
        'center': [s.center.x, s.center.y, s.center.z],
        'nodes': nodes,
        'transforms': transforms,
        'sequences': seqs,
        'details': details,
        'materials': mats,
        'objects': objs,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DTS Viewer</title>
<style>
  body { margin:0; background:#1a1d24; color:#cdd3de; font:13px/1.4 system-ui, sans-serif; overflow:hidden; }
  #ui { position:absolute; top:10px; left:10px; background:rgba(20,23,30,.88); padding:10px 12px;
        border:1px solid #333a46; border-radius:8px; z-index:10; max-width:290px; }
  #ui label { display:block; margin:4px 0; }
  #ui select, #ui input[type=range] { width:100%; }
  #labels { position:absolute; bottom:10px; left:0; right:0; text-align:center; z-index:9; pointer-events:none; }
  .mlabel { display:inline-block; margin:0 24px; padding:3px 10px; background:rgba(20,23,30,.8);
            border-radius:6px; border:1px solid #333a46; }
  button { background:#2a3140; color:#cdd3de; border:1px solid #40495c; border-radius:5px;
           padding:3px 10px; cursor:pointer; margin-right:6px;}
  button.on { background:#3d6fa8; }
</style>
</head>
<body>
<div id="ui">
  <div style="font-weight:600;margin-bottom:6px">DTS Viewer</div>
  <label>Sequence
    <select id="seqSel"></select>
  </label>
  <label><button id="playBtn">Play</button>
    <span id="timeLabel">t=0.00</span></label>
  <label><input type="range" id="scrub" min="0" max="1" step="0.001" value="0"></label>
  <label>Detail level <select id="lodSel"></select></label>
  <label>
    <button id="wireBtn">Wireframe</button>
    <button id="dsBtn">2-sided</button>
    <button id="spinBtn">Spin</button>
  </label>
</div>
<div id="labels"></div>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const MODELS = /*__MODELS__*/;

// ---------- scene ----------
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1a1d24);
const camera = new THREE.PerspectiveCamera(50, innerWidth/innerHeight, 0.01, 5000);
camera.up.set(0,0,1);                       // Tribes / DTS is Z-up
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);
const controls = new THREE.OrbitControls(camera, renderer.domElement);
scene.add(new THREE.HemisphereLight(0xffffff, 0x333944, 0.9));
const dl = new THREE.DirectionalLight(0xffffff, 0.7); dl.position.set(3,-4,6); scene.add(dl);
scene.add(new THREE.GridHelper(20, 20, 0x39404e, 0x262b35).rotateX(Math.PI/2));
scene.add(new THREE.AxesHelper(1));

// DTS quat -> three.js: the stored quaternion rotates like the engine with W negated
// (same convention the Blender importer uses).
function dtsQuat(q){ return new THREE.Quaternion(q[0], q[1], q[2], -q[3]); }

const loader = new THREE.TextureLoader();
const builds = [];      // per model runtime data
const FREE = MODELS.filter(m => !m.attach);   // side-by-side (non-mounted) models
let spacing = 0;
FREE.forEach(m => spacing = Math.max(spacing, m.radius*2.2 || 2));
let freeIdx = 0;

MODELS.forEach((M, mi) => {
  const root = new THREE.Group();
  if (!M.attach){
    root.position.x = (freeIdx - (FREE.length-1)/2) * spacing;
    freeIdx++;
  }
  scene.add(root);

  // materials
  const mats = M.materials.map(mm => {
    const opts = { color: new THREE.Color(mm.color[0]/255, mm.color[1]/255, mm.color[2]/255),
                   side: THREE.FrontSide };
    const mat = new THREE.MeshLambertMaterial(opts);
    if (mm.uri) { const t = loader.load(mm.uri); t.wrapS = t.wrapT = THREE.RepeatWrapping;
                  mat.map = t; mat.color.set(0xffffff); }
    // Engine material flags (ts_material.h): type nibble 0x0f (0x3 =
    // textured); ShadingType 0xf00 (0x100 = ShadingNone = fullbright);
    // TextureType 0xf000 (0x1000 = Transparent cutout, 0x2000 = Translucent
    // blend). Palette index 0 was made transparent at texture-decode time.
    const fl = mm.flags || 0;
    if (fl & 0x2000){ mat.transparent = true; mat.depthWrite = false; }
    else if (fl & 0x1000){ mat.alphaTest = 0.5; }
    if ((fl & 0xf00) === 0x100){        // fullbright (flames, glows)
      mat.emissive = new THREE.Color(0xffffff);
      if (mat.map) mat.emissiveMap = mat.map;
      else mat.emissive.copy(mat.color);
    }
    return mat;
  });
  const fallbackMat = new THREE.MeshLambertMaterial({color:0x8a93a5, side:THREE.FrontSide});

  // node world transforms from default pose
  const nodeObjs = M.nodes.map(n => { const g = new THREE.Group(); g.name = n.name; return g; });
  M.nodes.forEach((n, i) => {
    (n.parent >= 0 ? nodeObjs[n.parent] : root).add(nodeObjs[i]);
    applyTransform(nodeObjs[i], M.transforms[n.dt]);
  });

  // detail subtrees (LOD filtering)
  const subtree = M.details.map(det => {
    const s = new Set([det.root]);
    let changed = true;
    while (changed){ changed = false;
      M.nodes.forEach((n,i)=>{ if(!s.has(i) && s.has(n.parent)){ s.add(i); changed = true; } });
    }
    return s;
  });
  const inAnyDetail = new Set();
  subtree.forEach(s => s.forEach(i => inAnyDetail.add(i)));

  // meshes
  const meshRecs = [];
  M.objects.forEach(o => {
    // group faces by material for BufferGeometry groups
    const order = o.faces.map((f,i)=>i).sort((a,b)=>o.faces[a].m - o.faces[b].m);
    const nCorner = o.faces.length * 3;
    const uvArr = new Float32Array(nCorner*2);
    const idxPerFrame = M => null;
    // per-frame corner positions
    const framePos = o.frames.map(fp => {
      const arr = new Float32Array(nCorner*3);
      order.forEach((fi, k) => {
        const f = o.faces[fi];
        // DTS is CW; three.js front faces are CCW -> reverse corner order
        const c = [f.v[2], f.v[1], f.v[0]];
        for (let j=0;j<3;j++){
          const vi = c[j][0];
          arr[(k*3+j)*3+0] = fp[vi*3+0];
          arr[(k*3+j)*3+1] = fp[vi*3+1];
          arr[(k*3+j)*3+2] = fp[vi*3+2];
        }
      });
      return arr;
    });
    order.forEach((fi, k) => {
      const f = o.faces[fi];
      const c = [f.v[2], f.v[1], f.v[0]];
      for (let j=0;j<3;j++){
        uvArr[(k*3+j)*2+0] = c[j][1];
        uvArr[(k*3+j)*2+1] = 1.0 - c[j][2];
      }
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(framePos[0].slice(), 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(uvArr, 2));
    // material groups over the sorted faces
    let gStart = 0;
    for (let k=0;k<order.length;){
      const m0 = o.faces[order[k]].m; let k2 = k;
      while (k2 < order.length && o.faces[order[k2]].m === m0) k2++;
      geo.addGroup(k*3, (k2-k)*3, 0);
      geo.groups[geo.groups.length-1].materialIndex = 0; // set below
      geo.groups[geo.groups.length-1]._dtsMat = m0;
      k = k2;
    }
    const matList = geo.groups.map(g => mats[g._dtsMat] || fallbackMat);
    geo.groups.forEach((g,i)=> g.materialIndex = i);
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, matList);
    mesh.position.set(o.offset[0], o.offset[1], o.offset[2]);
    if (o.node >= 0 && o.node < nodeObjs.length) nodeObjs[o.node].add(mesh);
    else root.add(mesh);
    meshRecs.push({obj:o, mesh, framePos, node:o.node});
  });

  builds.push({M, root, nodeObjs, meshRecs, subtree, inAnyDetail, mats, fallbackMat});
});

function applyTransform(g, tf){
  g.position.set(tf.t[0], tf.t[1], tf.t[2]);
  g.quaternion.copy(dtsQuat(tf.q));
}

// ---------- engine-accurate mounting ----------
// Weapon equip: Player::getImageTransform -> off(mountRotation, mountOffset) @
// player node "dummy hand" (PrimaryMount). Vehicle rider:
// Vehicle::getObjectMountTransform -> vehicle node "dummy pilot" (mountPoint 1).
// Node lookup mirrors getNodeAtCurrentDetail: names carry LOD suffixes
// ("dummy hand36"), so match by prefix preferring the detail-0 subtree.
function findMountNode(build, prefix){
  const p = prefix.toLowerCase();
  let exact = -1, inDetail0 = -1, first = -1;
  build.M.nodes.forEach((n, i) => {
    const nm = n.name.toLowerCase();
    if (nm === p && exact < 0) exact = i;
    if (nm.startsWith(p)){
      if (first < 0) first = i;
      if (inDetail0 < 0 && build.subtree.length && build.subtree[0].has(i)) inDetail0 = i;
    }
  });
  const idx = exact >= 0 ? exact : (inDetail0 >= 0 ? inDetail0 : first);
  return idx >= 0 ? build.nodeObjs[idx] : null;
}

MODELS.forEach((M, mi) => {
  if (!M.attach) return;
  const child = builds[mi];
  const parent = builds[M.attach.parent];
  const nodeObj = parent ? findMountNode(parent, M.attach.node) : null;
  if (!nodeObj){
    console.warn('mount node not found: ' + M.attach.node + ' -- leaving model at origin');
    return;
  }
  scene.remove(child.root);
  nodeObj.add(child.root);
  const a = M.attach;
  child.root.position.set(a.offset[0], a.offset[1], a.offset[2]);
  child.root.rotation.set(a.rot[0], a.rot[1], a.rot[2]);
  console.log('mounted "' + M.label + '" on node "' + nodeObj.name + '"');
});

// ---------- LOD ----------
const lodSel = document.getElementById('lodSel');
const maxDetails = Math.max(...MODELS.map(m => m.details.length), 1);
for (let i=0;i<maxDetails;i++){
  const o = document.createElement('option'); o.value = i;
  o.text = 'detail ' + i + (MODELS[0].details[i] ? ' (size '+MODELS[0].details[i].size+')' : '');
  lodSel.add(o);
}
function applyLOD(){
  const di = parseInt(lodSel.value);
  builds.forEach(b => {
    b.meshRecs.forEach(r => {
      if (b.M.details.length <= 1){ r.mesh.visible = true; return; }
      const set = b.subtree[Math.min(di, b.subtree.length-1)];
      const inThis = set && set.has(r.node);
      const common = !b.inAnyDetail.has(r.node);
      r.mesh.visible = inThis || common;
    });
  });
}
lodSel.onchange = applyLOD; applyLOD();

// ---------- sequences ----------
const seqSel = document.getElementById('seqSel');
{
  const o0 = document.createElement('option'); o0.value = -1; o0.text = '(bind pose)'; seqSel.add(o0);
  (MODELS[0].sequences||[]).forEach((s,i)=>{
    const o = document.createElement('option'); o.value = i;
    o.text = s.name + ' ('+s.duration+'s'+(s.cyclic?', cyclic':'')+')';
    seqSel.add(o);
  });
}
let playing = false, t = 0, lastTs = 0;
const playBtn = document.getElementById('playBtn'), scrub = document.getElementById('scrub'),
      timeLabel = document.getElementById('timeLabel');
playBtn.onclick = ()=>{ playing = !playing; playBtn.classList.toggle('on', playing); };
scrub.oninput = ()=>{ t = parseFloat(scrub.value); applyPose(); };
seqSel.onchange = ()=>{ t = 0; scrub.value = 0; applyPose(); };

function sampleTrack(kfs, t){
  if (!kfs || !kfs.length) return null;
  let a = kfs[0], b = kfs[0];
  for (const k of kfs){ if (k.p <= t) a = k; }
  for (let i=kfs.length-1;i>=0;i--){ if (kfs[i].p >= t) b = kfs[i]; }
  const span = b.p - a.p;
  const f = span > 1e-6 ? (t - a.p)/span : 0;
  return {a, b, f};
}
function applyPose(){
  const si = parseInt(seqSel.value);
  builds.forEach(bd => {
    // Mounted models with an engine pose hold their OWN pose sequence,
    // mirroring Player::pickAnimation when mounted: mountPoint 1 plays the
    // vehicle datablock's driverPose (Scout: 22 = "flyer root", the
    // lean-forward pose; APC: 23 = "apc root"). Whitespace-insensitive match
    // because DTS sequence names have spaces ("flyer root") while the engine
    // enum doesn't (flyerroot).
    let esi = si;
    if (bd.M.attach && bd.M.attach.pose){
      const key = bd.M.attach.pose.replace(/\s+/g,'').toLowerCase();
      esi = bd.M.sequences.findIndex(s => s.name.replace(/\s+/g,'').toLowerCase() === key);
    }
    bd.M.nodes.forEach((n,i)=>{
      const g = bd.nodeObjs[i];
      const kfs = esi >= 0 ? n.tracks[esi] : null;
      if (!kfs || !kfs.length){ applyTransform(g, bd.M.transforms[n.dt]); return; }
      const s = sampleTrack(kfs, t);
      const A = bd.M.transforms[s.a.v], B = bd.M.transforms[s.b.v];
      const qa = dtsQuat(A.q), qb = dtsQuat(B.q);
      g.quaternion.copy(qa.slerp(qb, s.f));
      g.position.set(A.t[0]+(B.t[0]-A.t[0])*s.f, A.t[1]+(B.t[1]-A.t[1])*s.f, A.t[2]+(B.t[2]-A.t[2])*s.f);
    });
    bd.meshRecs.forEach(r => {
      const kfs = esi >= 0 ? r.obj.tracks[esi] : null;
      let frame = 0;
      if (kfs && kfs.length){
        const s = sampleTrack(kfs, t);
        if (s.a.ft) frame = s.a.v;           // frame track: step to keyed morph frame
      }
      if (r.framePos.length > 1){
        const arr = r.framePos[Math.min(frame, r.framePos.length-1)];
        r.mesh.geometry.attributes.position.array.set(arr);
        r.mesh.geometry.attributes.position.needsUpdate = true;
        r.mesh.geometry.computeVertexNormals();
      }
    });
  });
  timeLabel.textContent = 't=' + t.toFixed(2);
}

// ---------- toggles ----------
let wire = false, ds = false, spin = false;
document.getElementById('wireBtn').onclick = function(){
  wire = !wire; this.classList.toggle('on', wire);
  builds.forEach(b => b.mats.concat([b.fallbackMat]).forEach(m => m.wireframe = wire));
};
document.getElementById('dsBtn').onclick = function(){
  ds = !ds; this.classList.toggle('on', ds);
  builds.forEach(b => b.mats.concat([b.fallbackMat]).forEach(m => { m.side = ds ? THREE.DoubleSide : THREE.FrontSide; m.needsUpdate = true; }));
};
document.getElementById('spinBtn').onclick = function(){
  spin = !spin; this.classList.toggle('on', spin);
};

// ---------- labels ----------
const labels = document.getElementById('labels');
MODELS.forEach(m => {
  const s = document.createElement('span'); s.className = 'mlabel';
  s.textContent = m.label + (m.attach ? ' (mounted: ' + m.attach.node + ')' : '');
  labels.appendChild(s);
});

// ---------- camera fit ----------
{
  let r = 1;
  FREE.forEach(m => r = Math.max(r, m.radius || 1));
  const span = spacing * Math.max(1, FREE.length-1) / 2 + r;
  camera.position.set(span*0.8, -span*2.0, r*0.9);
  controls.target.set(0, 0, r*0.4);
}

function tick(ts){
  requestAnimationFrame(tick);
  const dt = Math.min(0.05, (ts - lastTs)/1000 || 0); lastTs = ts;
  if (playing){
    const si = parseInt(seqSel.value);
    const dur = si >= 0 ? (MODELS[0].sequences[si].duration || 1) : 1;
    t += dt / Math.max(0.05, dur);
    if (t > 1) t = (si >= 0 && MODELS[0].sequences[si].cyclic) ? t % 1 : 1;
    scrub.value = t;
    applyPose();
  }
  if (spin) builds.forEach(b => b.root.rotation.z += dt*0.6);
  controls.update();
  renderer.render(scene, camera);
}
addEventListener('resize', ()=>{ camera.aspect = innerWidth/innerHeight;
  camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
applyPose();
tick(0);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('inputs', nargs='+', help='.dts files to view')
    ap.add_argument('-o', '--output', default=None, help='output HTML path')
    ap.add_argument('--equip', action='append', default=[],
                    help='.dts to mount on model 0\'s "dummy hand" (weapon equip)')
    ap.add_argument('--pilot', action='append', default=[],
                    help='.dts to mount on model 0\'s "dummy pilot" (vehicle rider)')
    ap.add_argument('--pilot-pose', default='flyerroot',
                    help='pose sequence the rider holds (engine driverPose; '
                         'Scout=flyerroot, APC=apcroot). Default: flyerroot')
    ap.add_argument('--attach', action='append', default=[],
                    help='file.dts:nodePrefix[:ox,oy,oz[:rx,ry,rz]]')
    ap.add_argument('--texdir', action='append', default=[],
                    help='extra folder(s) to search for textures (repeatable); '
                         'e.g. a game skins dir for shapes whose BMPs live in .vol archives')
    ap.add_argument('--vol', action='append', default=[],
                    help='.vol archive(s) to search for textures (repeatable)')
    ap.add_argument('--voldir', action='append', default=[],
                    help='folder(s) whose *.vol archives are all searched '
                         '(e.g. C:\\Dynamix\\Tribes\\base)')
    ap.add_argument('--ppl', default=None,
                    help='world palette: a .ppl file path, or a name to find '
                         'in the vols (default: auto, prefers lush.day.ppl)')
    args = ap.parse_args()

    vol_paths = list(args.vol)
    for d_ in args.voldir:
        try:
            for root, _dirs, files in os.walk(d_):
                vol_paths.extend(
                    os.path.join(root, fn) for fn in sorted(files)
                    if fn.lower().endswith('.vol'))
        except OSError as e:
            print(f"WARNING: cannot scan voldir {d_}: {e}")
    vols = {}
    for vp in vol_paths:
        try:
            idx = read_vol_index(vp)
            if idx:
                vols[vp] = idx
        except Exception as e:
            print(f"WARNING: failed to index {vp}: {e}")
    if vols:
        total = sum(len(v) for v in vols.values())
        print(f"indexed {len(vols)} vol archive(s), {total} files")

    # World multipalette: PBMP textures carry only a palette index; the
    # colors live in a .ppl. Use --ppl, else auto-discover one in the vols
    # (prefer the lush day palette, the KoK world's).
    ppl_tables = None
    ppl_src = args.ppl
    if ppl_src and os.path.isfile(ppl_src):
        with open(ppl_src, 'rb') as f:
            ppl_tables = parse_ppl(f.read())
        print(f"palette: {ppl_src} ({len(ppl_tables) - 1} tables)")
    else:
        want = (ppl_src or '').lower() or None
        best = None
        for volpath, index in vols.items():
            for name, (off, size) in index.items():
                if not name.endswith('.ppl'):
                    continue
                if want and name != want:
                    continue
                rank = 0 if 'lush.day' in name else 1
                if best is None or rank < best[0]:
                    best = (rank, volpath, name, off, size)
        if best:
            _, volpath, name, off, size = best
            with open(volpath, 'rb') as f:
                f.seek(off)
                ppl_tables = parse_ppl(f.read(size))
            print(f"palette: {name} from {os.path.basename(volpath)} "
                  f"({len(ppl_tables) - 1} tables)")

    # (path, attach-spec or None); attach = {parent, node, offset, rot}
    jobs = [(p, None) for p in args.inputs]
    for p in args.equip:
        jobs.append((p, {'parent': 0, 'node': 'dummy hand',
                         'offset': [0, 0, 0], 'rot': [0, 0, 0]}))
    for p in args.pilot:
        jobs.append((p, {'parent': 0, 'node': 'dummy pilot',
                         'offset': [0, 0, 0], 'rot': [0, 0, 0],
                         'pose': args.pilot_pose}))
    for spec in args.attach:
        parts = spec.split(':')
        path, node = parts[0], parts[1]
        offset = [float(x) for x in parts[2].split(',')] if len(parts) > 2 else [0, 0, 0]
        rot = [float(x) for x in parts[3].split(',')] if len(parts) > 3 else [0, 0, 0]
        jobs.append((path, {'parent': 0, 'node': node,
                            'offset': offset, 'rot': rot}))

    models = []
    for p, attach in jobs:
        print(f"parsing {p} ...")
        models.append(extract_model(p, texdirs=args.texdir, vols=vols,
                                    ppl_tables=ppl_tables))
        m = models[-1]
        if attach:
            m['attach'] = attach
            print(f"  -> mounted on model 0 node '{attach['node']}'")
        print(f"  {len(m['objects'])} visible meshes, {len(m['nodes'])} nodes, "
              f"{len(m['sequences'])} sequences, {len(m['materials'])} materials "
              f"({sum(1 for x in m['materials'] if x['uri'])} textured)")

    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.inputs[0])), 'dts_viewer.html')
    html = HTML_TEMPLATE.replace('/*__MODELS__*/', json.dumps(models))
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"wrote {out} ({os.path.getsize(out)//1024} KB)")


if __name__ == '__main__':
    main()
