# Tribes interior (.dis/.dig/.vol) import/export for Blender.
#
# A Tribes interior lives inside a PVOL .vol archive as a family of files:
#   .dis  ITRShape manifest naming the detail levels      (tag block 'ITRs')
#   .dig  ITRGeometry -- the actual BSP mesh, one per LOD ('PERS' ITRGeometry)
#   .dil  precomputed lighting
#   .dml  TS::MaterialList -- texture filename per material slot
#
# IMPORT parses .vol/.dis/.dig directly (byte layouts verified against the
# engine source: Interior/code/itrgeometry.cpp ITRGeometry::read) and builds
# textured Blender meshes, resolving bitmaps + PL98 world palettes from the
# game's .vol archives automatically.
#
# EXPORT writes the interior family back into a .vol. The hard part -- a real
# BSP the engine can render/cull -- is produced by objbuild.js, an Emscripten
# build of the ENGINE'S OWN compiler (ITRBSPBuild::buildTree + ITRPortal PVS +
# lighting), driven via Node.js. Without Node an empty-BSP .dig can be written
# for round-trip/testing only (the live engine won't render it).

import bpy
import os
import re
import glob
import struct
import subprocess
import tempfile
import zlib

from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

# --------------------------------------------------------------------------
# PVOL archives
# --------------------------------------------------------------------------

class Vol:
    """Seek-based PVOL reader (mirrors Core/code/volstrm.cpp openVolume)."""

    ITEM_SIZE = 17  # DWORD id + int32 strOff + DWORD blockOff + uint32 size + uint8

    def __init__(self, path):
        self.path = path
        self.entries = []       # (name, block_offset, size, compress)
        self._by_name = {}
        with open(path, 'rb') as f:
            self._parse(f)

    def _parse(self, f):
        head = f.read(8)
        if len(head) < 8 or head[:4] != b'PVOL':
            raise ValueError('{}: not a PVOL archive'.format(self.path))
        string_block_off = struct.unpack('<I', head[4:])[0]
        f.seek(string_block_off)
        sid, ssize = struct.unpack('<4sI', f.read(8))
        if sid != b'vols':
            raise ValueError('{}: bad string block'.format(self.path))
        strtab = f.read(ssize)
        iid, isize = struct.unpack('<4sI', f.read(8))
        if iid != b'voli':
            f.seek(-7, 1)  # some volumes word-align the string block
            iid, isize = struct.unpack('<4sI', f.read(8))
            if iid != b'voli':
                raise ValueError('{}: bad voli block'.format(self.path))
        items = f.read(isize)
        for i in range(isize // self.ITEM_SIZE):
            _id, str_off, block_off, size, comp = struct.unpack_from(
                '<IiIIB', items, i * self.ITEM_SIZE)
            if str_off == -1:
                continue
            end = strtab.find(b'\x00', str_off)
            name = strtab[str_off:end].decode('latin1')
            self.entries.append((name, block_off, size, comp))
            self._by_name[name.lower()] = (block_off, size, comp)

    def names(self):
        return [e[0] for e in self.entries]

    def read(self, name):
        e = self._by_name.get(name.lower())
        if e is None:
            return None
        block_off, size, comp = e
        if comp != 0:
            raise NotImplementedError('{}: compressed vol entries unsupported'.format(name))
        with open(self.path, 'rb') as f:
            f.seek(block_off + 8)  # skip the VBLK block header
            return f.read(size)


def find_tribes_root(*starts):
    """Walk up from each start dir looking for a Tribes install (base/ with
    .vol files, else any dir holding vols)."""
    chain, seen = [], set()
    for start in starts:
        d = os.path.abspath(start)
        for _ in range(8):
            if d not in seen:
                seen.add(d)
                chain.append(d)
            p = os.path.dirname(d)
            if p == d:
                break
            d = p
    for c in chain:
        if glob.glob(os.path.join(c, 'base', '*.vol')):
            return c
    for c in chain:
        if glob.glob(os.path.join(c, '*.vol')) or glob.glob(os.path.join(c, '*', '*.vol')):
            return c
    return None


# --------------------------------------------------------------------------
# Bitmaps + PL98 world palettes (Dgfx/code/g_bitmap.cpp, g_pal.cpp)
# --------------------------------------------------------------------------

def parse_ppl(data):
    """PL98 multipalette -> {paletteIndex: [(r,g,b)]*256}, plus None fallback."""
    if data[:4] != b'PL98':
        raise ValueError('not a PL98 palette')
    numPal = struct.unpack_from('<i', data, 4)[0]
    off = 4 + 16 + 32  # fourcc + 4 ints + allowedColorMatches bitvector
    tables, first = {}, None
    for _ in range(numPal):
        colors = [tuple(data[off + c * 4: off + c * 4 + 3]) for c in range(256)]
        off += 1024
        pidx, _ptype = struct.unpack_from('<ii', data, off)
        off += 8
        tables[pidx] = colors
        if first is None:
            first = colors
    tables[None] = first
    return tables


def parse_bitmap(data):
    """GFXBitmap (PBMP chunked or MS DIB) -> dict(width,height,stride,indices,
    paletteIndex,embedded_palette)."""
    if data[:2] == b'BM':
        return _parse_msdib(data)
    return _parse_pbmp(data)


def _parse_pbmp(data):
    off = 0
    width = height = bitDepth = stride = 0
    indices = None
    paletteIndex = None
    num_chunks = -1
    while num_chunks:
        num_chunks -= 1
        if off + 8 > len(data):
            break
        cid = data[off:off + 4]
        csize = struct.unpack_from('<I', data, off + 4)[0]
        off += 8
        if cid == b'PBMP':
            continue
        if cid == b'head':
            ver_nc, width, height, bitDepth, _attr = struct.unpack_from('<IIIII', data, off)
            num_chunks = ver_nc & 0x00ffffff
            stride = ((width * bitDepth >> 3) + 3) & ~3
        elif cid == b'data':
            indices = data[off:off + csize]
        elif cid == b'PiDX':
            paletteIndex = struct.unpack_from('<I', data, off)[0]
        off += csize
    if indices is None or bitDepth != 8:
        raise ValueError('unsupported PBMP (bitDepth={})'.format(bitDepth))
    return {'width': width, 'height': height, 'stride': stride,
            'indices': indices[:height * stride],
            'paletteIndex': paletteIndex, 'embedded_palette': None}


def _parse_msdib(data):
    (_t, _sz, bfRes1, bfRes2, bfOffBits) = struct.unpack_from('<HIHHI', data, 0)
    (biSize, biWidth, biHeight, _pl, biBitCount, _cmp, _szi, _x, _y,
     biClrUsed, _imp) = struct.unpack_from('<IiiHHIIiiII', data, 14)
    if biBitCount != 8:
        raise ValueError('MS DIB bitDepth {} unsupported'.format(biBitCount))
    width, height = biWidth, abs(biHeight)
    stride = ((width * biBitCount >> 3) + 3) & ~3
    ncolors = biClrUsed or 256
    pal_off = 14 + biSize
    embedded = []
    for c in range(ncolors):
        b, g, r = data[pal_off + c * 4: pal_off + c * 4 + 3]
        embedded.append((r, g, b))
    embedded += [(0, 0, 0)] * (256 - len(embedded))
    bits = data[bfOffBits:bfOffBits + height * stride]
    rows = [bits[y * stride:(y + 1) * stride] for y in range(height)]
    if biHeight > 0:
        rows.reverse()  # bottom-up DIB -> top-down
    # RPG-mod trick: real world-palette index hides in bfReserved1/2
    paletteIndex = bfRes2 if bfRes1 == 0xf5f7 and bfRes2 != 0xffff else None
    return {'width': width, 'height': height, 'stride': stride,
            'indices': b''.join(rows), 'paletteIndex': paletteIndex,
            'embedded_palette': embedded}


def write_png_bytes(width, height, rgb):
    def chunk(tag, body):
        c = tag + body
        return struct.pack('>I', len(body)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    raw = bytearray()
    rowlen = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * rowlen:(y + 1) * rowlen]
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))


_BMP_INDEX_CACHE = {}
_PALETTE_CACHE = {}


def build_bmp_index(vol_dir):
    """lower 'name.bmp' -> Vol, across every .vol under vol_dir (cached)."""
    if vol_dir in _BMP_INDEX_CACHE:
        return _BMP_INDEX_CACHE[vol_dir]
    index = {}
    for vp in glob.glob(os.path.join(vol_dir, '**', '*.vol'), recursive=True):
        try:
            v = Vol(vp)
        except Exception:
            continue
        for name, _, _, _ in v.entries:
            n = name.lower()
            if n.endswith('.bmp') and n not in index:
                index[n] = v
    _BMP_INDEX_CACHE[vol_dir] = index
    return index


def load_palettes(vol_dir):
    """Merge every world .ppl in *World.vol archives (indices are global)."""
    if vol_dir in _PALETTE_CACHE:
        return _PALETTE_CACHE[vol_dir]
    tables = {}
    for wv in glob.glob(os.path.join(vol_dir, '**', '*World.vol'), recursive=True):
        try:
            v = Vol(wv)
        except Exception:
            continue
        for n in v.names():
            if n.lower().endswith('.ppl'):
                try:
                    t = parse_ppl(v.read(n))
                except Exception:
                    continue
                for k, val in t.items():
                    if k is not None and k not in tables:
                        tables[k] = val
                if tables.get(None) is None and t.get(None):
                    tables[None] = t[None]
    _PALETTE_CACHE[vol_dir] = tables
    return tables


# --------------------------------------------------------------------------
# .dig / .dml / .dis parsing (import)
# --------------------------------------------------------------------------

SZ_SURFACE, SZ_BSPNODE, SZ_LEAFSOLID, SZ_LEAFEMPTY = 20, 8, 12, 44
SZ_VERTEX, SZ_POINT3F, SZ_POINT2F, SZ_TPLANEF = 4, 12, 8, 16


def _parse_pers_header(data):
    """-> (classname, version, offset_past_header)"""
    if data[:4] != b'PERS':
        raise ValueError('not a PERS block')
    namesize = struct.unpack_from('<H', data, 8)[0]
    off = 10 + ((namesize + 1) & ~1)
    name = data[10:10 + namesize].decode('ascii', 'replace')
    version = struct.unpack_from('<i', data, off)[0]
    return name, version, off + 4


def parse_dig_bytes(data, label='<dig>'):
    cls, version, p = _parse_pers_header(data)
    if cls != 'ITRGeometry':
        raise ValueError('{}: expected ITRGeometry, got {!r}'.format(label, cls))
    p += 4 + 4 + 24  # buildId, textureScale, box
    counts = struct.unpack_from('<9i', data, p)
    p += 36
    (n_surface, n_node, n_solid, n_empty, n_bit,
     n_vertex, n_point3, n_point2, n_plane) = counts

    surface_blob = data[p:p + n_surface * SZ_SURFACE]
    p += n_surface * SZ_SURFACE
    p += n_node * SZ_BSPNODE + n_solid * SZ_LEAFSOLID + n_empty * SZ_LEAFEMPTY + n_bit
    vertex_blob = data[p:p + n_vertex * SZ_VERTEX]
    p += n_vertex * SZ_VERTEX
    point3 = [struct.unpack_from('<fff', data, p + i * SZ_POINT3F) for i in range(n_point3)]
    p += n_point3 * SZ_POINT3F
    point2 = [struct.unpack_from('<ff', data, p + i * SZ_POINT2F) for i in range(n_point2)]

    vertices = [struct.unpack_from('<HH', vertex_blob, i * SZ_VERTEX) for i in range(n_vertex)]
    surfaces = []
    for i in range(n_surface):
        off = i * SZ_SURFACE
        bits = surface_blob[off]
        if bits & 1:      # Surface::Link = portal, not renderable
            continue
        vertexCount = surface_blob[off + 16]
        if vertexCount < 3:
            continue
        surfaces.append((
            surface_blob[off + 1],                                  # material
            struct.unpack_from('<I', surface_blob, off + 8)[0],     # vertexIndex
            vertexCount,
            surface_blob[off + 2] + 1, surface_blob[off + 3] + 1,   # textureSize
            surface_blob[off + 4], surface_blob[off + 5],           # textureOffset
        ))
    return {'point3': point3, 'point2': point2,
            'vertices': vertices, 'surfaces': surfaces}


DML_MAT_RECORD, DML_NAME_OFFSET, DML_NAME_LEN = 64, 16, 32


def material_names_from_bytes(data):
    if not data or data[:4] != b'PERS':
        return None
    namesize = struct.unpack_from('<H', data, 8)[0]
    off = 10 + ((namesize + 1) & ~1) + 4
    fnDetails, fnMaterials = struct.unpack_from('<ii', data, off)
    off += 8
    names = []
    for m in range(fnDetails * fnMaterials):
        rec = off + m * DML_MAT_RECORD
        raw = data[rec + DML_NAME_OFFSET: rec + DML_NAME_OFFSET + DML_NAME_LEN]
        names.append(raw.split(b'\x00', 1)[0].decode('latin1'))
    return names


# --------------------------------------------------------------------------
# .dig / .dml / .dis / .vol building (export)
# --------------------------------------------------------------------------

def pers_block(classname, version, body):
    name = classname.encode('ascii')
    fieldlen = (len(name) + 1) & ~1
    payload = (struct.pack('<H', len(name)) + name + b'\x00' * (fieldlen - len(name))
               + struct.pack('<i', version) + body)
    return b'PERS' + struct.pack('<I', len(payload)) + payload


def tag_block(fourcc, version, body):
    payload = struct.pack('<i', version) + body
    return fourcc + struct.pack('<I', len(payload)) + payload


def build_dig_emptybsp(verts, uvs, faces):
    """Structurally valid ITRGeometry with an EMPTY BSP: round-trips through
    this importer, but the live engine cannot render/cull it. The real BSP
    comes from objbuild.js (the engine's compiler ported to WASM)."""
    point3 = verts
    point2 = uvs if uvs else [(0.0, 0.0)]
    vertexList, surfaces, planes = [], [], []

    def face_plane(corners):
        nx = ny = nz = 0.0
        n = len(corners)
        for i in range(n):
            a = verts[corners[i][0]]
            b = verts[corners[(i + 1) % n][0]]
            nx += (a[1] - b[1]) * (a[2] + b[2])
            ny += (a[2] - b[2]) * (a[0] + b[0])
            nz += (a[0] - b[0]) * (a[1] + b[1])
        L = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
        nx, ny, nz = nx / L, ny / L, nz / L
        p0 = verts[corners[0][0]]
        return (nx, ny, nz, -(nx * p0[0] + ny * p0[1] + nz * p0[2]))

    for mat, corners in faces:
        vidx = len(vertexList)
        for (vi, vti) in corners:
            vertexList.append((vi, vti if vti >= 0 else 0))
        plane_index = len(planes)
        planes.append(face_plane(corners))
        rec = bytearray(20)
        rec[0] = 0x20 | 0x40 | 0x80  # applyAmbient | visibleToOutside | planeFront
        rec[1] = mat & 0xFF
        rec[2] = rec[3] = 255        # full-texture rect (stored size+1 = 256)
        struct.pack_into('<H', rec, 6, plane_index & 0xFFFF)
        struct.pack_into('<I', rec, 8, vidx)
        rec[16] = len(corners) & 0xFF
        surfaces.append(bytes(rec))

    xs = [p[0] for p in point3]
    ys = [p[1] for p in point3]
    zs = [p[2] for p in point3]
    out = bytearray()
    out += struct.pack('<i', 1)      # buildId
    out += struct.pack('<f', 16.0)   # textureScale
    out += struct.pack('<6f', min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    out += struct.pack('<9i', len(surfaces), 0, 0, 0, 0,
                       len(vertexList), len(point3), len(point2), len(planes))
    for s in surfaces:
        out += s
    for (pi, ti) in vertexList:
        out += struct.pack('<HH', pi & 0xFFFF, ti & 0xFFFF)
    for (x, y, z) in point3:
        out += struct.pack('<fff', x, y, z)
    for (u, v) in point2:
        out += struct.pack('<ff', u, v)
    for (a, b, c, d) in planes:
        out += struct.pack('<ffff', a, b, c, d)
    out += struct.pack('<i', 0) + struct.pack('<I', 0)  # highestMipLevel, flags
    return pers_block('ITRGeometry', 7, bytes(out))


def build_dml(mats):
    body = bytearray()
    body += struct.pack('<i', 1)          # fnDetails
    body += struct.pack('<i', len(mats))  # fnMaterials
    for name in mats:
        fname = name if name.lower().endswith('.bmp') else name + '.bmp'
        rec = bytearray(64)
        struct.pack_into('<i', rec, 0, 0x03)   # fFlags = MatTexture
        struct.pack_into('<f', rec, 4, 1.0)    # fAlpha
        enc = fname.encode('latin1')[:31]
        rec[16:16 + len(enc)] = enc            # fMapFile[32]
        struct.pack_into('<f', rec, 52, 1.0)   # fElasticity
        struct.pack_into('<f', rec, 56, 1.0)   # fFriction
        struct.pack_into('<I', rec, 60, 1)     # fUseDefaultProps
        body += rec
    return pers_block('TS::MaterialList', 4, bytes(body))


def build_dis(dig_name, dml_name, dil_name=None):
    names = b''
    state_off = len(names); names += b'State0\x00'
    geom_off = len(names);  names += dig_name.encode('latin1') + b'\x00'
    if dil_name:
        dil_off = len(names); names += dil_name.encode('latin1') + b'\x00'
    mat_off = len(names);   names += dml_name.encode('latin1') + b'\x00'
    light_off = len(names); names += b'default\x00'

    body = bytearray()
    body += struct.pack('<I', 1)                       # 1 state
    body += struct.pack('<III', state_off, 0, 1)
    body += struct.pack('<I', 1)                       # 1 lod
    body += struct.pack('<IIII', 250, geom_off, 0, 0xFF)
    if dil_name:                                       # lodLightStates
        body += struct.pack('<I', 1) + struct.pack('<I', dil_off)
    else:
        body += struct.pack('<I', 0)  # round-trip only; engine needs a .dil
    body += struct.pack('<I', 1) + struct.pack('<I', light_off)
    body += struct.pack('<I', len(names)) + names
    body += struct.pack('<I', mat_off)
    body += struct.pack('<B', 0)                       # m_linkedInterior
    return tag_block(b'ITRs', 3, bytes(body))


def _name_id(name):
    h = 0
    for ch in name.lower():
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def build_vol(entries):
    """entries: [(name, bytes)] -> PVOL bytes. Alignment quirks match the
    engine's BlockRWStream exactly (4-byte blocks; the voli block is found at
    vols + 8 + alignSize(strtab, WORD) -- 2-byte, NOT 4-byte, padding)."""
    out = bytearray()
    out += b'PVOL' + struct.pack('<I', 0)
    items = []
    for name, data in entries:
        while len(out) % 4:
            out.append(0)
        block_off = len(out)
        out += b'VBLK' + struct.pack('<I', len(data)) + data
        items.append((name, block_off, len(data)))
    while len(out) % 4:
        out.append(0)
    string_block_off = len(out)
    strtab = bytearray()
    str_offsets = []
    for (name, _, _) in items:
        str_offsets.append(len(strtab))
        strtab += name.encode('latin1') + b'\x00'
    out += b'vols' + struct.pack('<I', len(strtab)) + strtab
    while len(out) % 2:
        out.append(0)
    voli = bytearray()
    for (name, block_off, size), str_off in zip(items, str_offsets):
        voli += struct.pack('<IiIIB', _name_id(name), str_off, block_off, size, 0)
    out += b'voli' + struct.pack('<I', len(voli)) + voli
    struct.pack_into('<I', out, 4, string_block_off)
    return bytes(out)


# --------------------------------------------------------------------------
# Import operator
# --------------------------------------------------------------------------

class ImportDIS(bpy.types.Operator, ImportHelper):
    """Import a Starsiege: Tribes interior (.vol archive, .dis manifest, or a
    single .dig geometry file) with textures from the game's vols"""
    bl_idname = 'import_scene.tribes_dis'
    bl_label = 'Import Tribes Interior'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.vol'
    filter_glob: StringProperty(default='*.vol;*.dis;*.dig', options={'HIDDEN'})

    load_textures: BoolProperty(
        name='Textures',
        description='Decode each material bitmap (with its PL98 world palette) '
                    'from the game vols and assign it',
        default=True,
    )
    legacy_uv: BoolProperty(
        name='Mirror U (Kronos/RPG)',
        description='Rotated mod interiors (Kronos/RPG buildings) store U '
                    'mirrored; enable if textures look flipped left-to-right',
        default=False,
    )
    all_detail_levels: BoolProperty(
        name='All detail levels',
        description='Import every .dig LOD (…-00 highest). Off = highest only',
        default=False,
    )

    def execute(self, context):
        path = self.filepath
        ext = os.path.splitext(path)[1].lower()
        jobs = []  # (label, dig_bytes, dml_bytes)

        try:
            if ext == '.vol':
                v = Vol(path)
                digs = sorted(n for n in v.names() if n.lower().endswith('.dig'))
                if not digs:
                    self.report({'ERROR'}, 'No interior .dig geometry inside this .vol')
                    return {'CANCELLED'}
                # multi-shape vols carry one .dml PER interior (table.dml,
                # tavern.dml, ...) -- match each dig to ITS material list by
                # base name, not just the first .dml in the archive
                dmls = {os.path.splitext(n)[0].lower(): n
                        for n in v.names() if n.lower().endswith('.dml')}
                if not self.all_detail_levels:
                    # keep only each interior's lowest -NN suffix (highest LOD)
                    best = {}
                    for d in digs:
                        m = re.match(r'(.*?)-(\d+)\.dig$', d, re.IGNORECASE)
                        base, lvl = (m.group(1), int(m.group(2))) if m else (d, 0)
                        if base not in best or lvl < best[base][0]:
                            best[base] = (lvl, d)
                    digs = [d for _, d in best.values()]
                for d in digs:
                    base = re.sub(r'-\d+\.dig$', '', d, flags=re.IGNORECASE).lower()
                    dml_n = dmls.get(base)
                    if dml_n is None and len(dmls) == 1:
                        dml_n = next(iter(dmls.values()))
                    dml_bytes = v.read(dml_n) if dml_n else None
                    jobs.append((os.path.splitext(d)[0], v.read(d), dml_bytes))
            elif ext in ('.dis', '.dig'):
                d = os.path.dirname(os.path.abspath(path))
                dig_paths = ([path] if ext == '.dig'
                             else sorted(glob.glob(os.path.join(d, '*.dig'))))
                if not dig_paths:
                    self.report({'ERROR'}, 'No .dig geometry found next to the .dis')
                    return {'CANCELLED'}
                for dp in dig_paths:
                    stem = os.path.basename(dp)
                    base = re.sub(r'-\d+\.dig$', '', stem, flags=re.IGNORECASE)
                    dml_p = os.path.join(d, base + '.dml')
                    dml_bytes = open(dml_p, 'rb').read() if os.path.isfile(dml_p) else None
                    jobs.append((os.path.splitext(stem)[0],
                                 open(dp, 'rb').read(), dml_bytes))
            else:
                self.report({'ERROR'}, 'Pick a .vol, .dis, or .dig file')
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, 'Parse failed: {}'.format(e))
            return {'CANCELLED'}

        vol_dir = None
        if self.load_textures:
            vol_dir = find_tribes_root(os.path.dirname(path))
            if vol_dir is None:
                self.report({'WARNING'},
                            'Tribes install not found above the file; importing untextured')

        count = 0
        for label, dig_bytes, dml_bytes in jobs:
            try:
                geo = parse_dig_bytes(dig_bytes, label)
            except Exception as e:
                self.report({'WARNING'}, '{}: {}'.format(label, e))
                continue
            materials = material_names_from_bytes(dml_bytes) if dml_bytes else None
            self._build_mesh(context, label, geo, materials, vol_dir)
            count += 1

        if count == 0:
            return {'CANCELLED'}
        self.report({'INFO'}, 'Imported {} interior mesh(es)'.format(count))
        return {'FINISHED'}

    def _build_mesh(self, context, label, geo, materials, vol_dir):
        verts = geo['point3']
        pt2 = geo['point2']
        vlist = geo['vertices']

        # resolve textures first: per-surface UVs need each bitmap's dimensions
        images, tex_dims = {}, {}
        if vol_dir and materials:
            bmp_index = build_bmp_index(vol_dir)
            palettes = load_palettes(vol_dir)
            for idx, name in enumerate(materials):
                if not name:
                    continue
                v = bmp_index.get(name.lower())
                if v is None:
                    continue
                try:
                    bmp = parse_bitmap(v.read(name))
                except Exception:
                    continue
                # engine renders via the WORLD multipalette by paletteIndex; an
                # embedded palette may be a grayscale placeholder (RPG mods)
                pal = None
                if bmp['paletteIndex'] is not None:
                    pal = palettes.get(bmp['paletteIndex'])
                if pal is None:
                    pal = bmp['embedded_palette'] or palettes.get(None)
                if pal is None:
                    continue
                w, h, stride = bmp['width'], bmp['height'], bmp['stride']
                rgb = bytearray(w * h * 3)
                o = 0
                idxb = bmp['indices']
                for y in range(h - 1, -1, -1):  # flip: Blender image origin is bottom
                    row = y * stride
                    for x in range(w):
                        r, g, b = pal[idxb[row + x]]
                        rgb[o] = r; rgb[o + 1] = g; rgb[o + 2] = b
                        o += 3
                png = write_png_bytes(w, h, bytes(rgb))
                img_name = os.path.splitext(name)[0] + '.png'
                tmp = os.path.join(tempfile.gettempdir(), img_name)
                with open(tmp, 'wb') as f:
                    f.write(png)
                img = bpy.data.images.load(tmp)
                img.pack()
                img.filepath = ''
                images[idx] = img
                tex_dims[idx] = (w, h)

        # faces: per-corner UVs from the surface's texture sub-rectangle
        # (texel = textureOffset + point2*(textureSize+1); coord = texel/dim)
        faces, face_mats, uvs = [], [], []
        used_mats = sorted(set(s[0] for s in geo['surfaces']))
        mat_slot = {m: i for i, m in enumerate(used_mats)}
        for mat, vi, vc, tsx, tsy, tox, toy in geo['surfaces']:
            tw, th = tex_dims.get(mat, (256, 256))
            corner_idx, corner_uv = [], []
            for k in range(vc):
                pidx, tidx = vlist[vi + k]
                pu, pv = pt2[tidx]
                u = (1.0 - (tox + pu * tsx) / tw) if self.legacy_uv else (tox + pu * tsx) / tw
                corner_idx.append(pidx)
                corner_uv.append((u, (toy + pv * tsy) / th))
            # skip degenerate faces with repeated points (Blender rejects them)
            if len(set(corner_idx)) < 3:
                continue
            faces.append(corner_idx)
            face_mats.append(mat_slot[mat])
            uvs.extend(corner_uv)

        mesh = bpy.data.meshes.new(label)
        mesh.from_pydata(verts, [], faces)
        ob = bpy.data.objects.new(label, mesh)
        ob['dis_interior'] = 1
        context.scene.collection.objects.link(ob)

        # materials
        for m in used_mats:
            name = (os.path.splitext(materials[m])[0]
                    if materials and m < len(materials) and materials[m]
                    else 'material_{}'.format(m))
            mat = bpy.data.materials.get(name)
            if mat is None:
                mat = bpy.data.materials.new(name)
                mat.use_nodes = True
                bsdf = mat.node_tree.nodes.get('Principled BSDF')
                if bsdf:
                    bsdf.inputs['Roughness'].default_value = 1.0
                if m in images and bsdf:
                    tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
                    tex.image = images[m]
                    tex.location = (bsdf.location.x - 300, bsdf.location.y)
                    mat.node_tree.links.new(tex.outputs['Color'],
                                            bsdf.inputs['Base Color'])
            mesh.materials.append(mat)
        for i, poly in enumerate(mesh.polygons):
            poly.material_index = face_mats[i]

        uv_layer = mesh.uv_layers.new(name='UV Map')
        for li, loop_uv in enumerate(uvs):
            uv_layer.data[li].uv = loop_uv
        mesh.validate()


# --------------------------------------------------------------------------
# Export operator
# --------------------------------------------------------------------------

DEFAULT_OBJBUILD = r'C:\Users\Joe\Desktop\Tribes Browser Based\build\objbuild.js'


class ExportDIS(bpy.types.Operator, ExportHelper):
    """Export selected meshes as a Tribes interior .vol (.dis + .dig + .dml).
    A real, engine-loadable BSP requires Node.js + objbuild.js (the engine's
    BSP compiler ported to WASM)"""
    bl_idname = 'export_scene.tribes_dis'
    bl_label = 'Export Tribes Interior'

    filename_ext = '.vol'
    filter_glob: StringProperty(default='*.vol', options={'HIDDEN'})

    collision_mode: EnumProperty(
        name='Collision',
        description='How the engine BSP compiler builds collision',
        items=(
            ('FULL', 'Full BSP', 'Per-face collision. Complex geometry can '
             'exceed the engine\'s 400-node collision clip cap'),
            ('BOX', 'Box (hybrid)', 'Render full detail, collide as a simple '
             'box -- safe for complex props on unmodified servers'),
            ('NOCOLLIDE', 'None (walk-through)', 'Render full detail, no '
             'collision at all (decoration)'),
            ('EMPTY', 'Empty BSP (no Node.js)', 'Structurally valid file that '
             'round-trips in Blender but the game engine will NOT render'),
        ),
        default='FULL',
    )
    objbuild_path: StringProperty(
        name='objbuild.js',
        description='Path to objbuild.js (WASM build of the engine BSP '
                    'compiler); run with Node.js',
        default=DEFAULT_OBJBUILD,
        subtype='FILE_PATH',
    )
    tex_dir: StringProperty(
        name='Textures dir',
        description='Optional folder of <Material>.bmp files to pack into the '
                    '.vol so materials resolve in-game',
        default='',
        subtype='DIR_PATH',
    )

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            objs = [o for o in context.scene.objects
                    if o.type == 'MESH' and o.get('dis_interior')]
        if not objs:
            self.report({'ERROR'}, 'Select the mesh(es) to export')
            return {'CANCELLED'}

        verts, uvs, faces, mats = [], [], [], []
        mat_idx = {}
        depsgraph = context.evaluated_depsgraph_get()
        for ob in objs:
            mesh = ob.evaluated_get(depsgraph).to_mesh()
            base_v = len(verts)
            mw = ob.matrix_world
            for v in mesh.vertices:
                co = mw @ v.co
                verts.append((co.x, co.y, co.z))
            uv_layer = mesh.uv_layers.active
            for poly in mesh.polygons:
                mat_name = 'default'
                if ob.material_slots and poly.material_index < len(ob.material_slots):
                    slot = ob.material_slots[poly.material_index]
                    if slot.material:
                        mat_name = re.sub(r'\.\d+$', '', slot.material.name)
                if mat_name not in mat_idx:
                    mat_idx[mat_name] = len(mats)
                    mats.append(mat_name)
                corners = []
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index + base_v
                    if uv_layer:
                        uv = uv_layer.data[li].uv
                        corners.append((vi, len(uvs)))
                        uvs.append((uv.x, uv.y))
                    else:
                        corners.append((vi, -1))
                faces.append((mat_idx[mat_name], corners))
            ob.evaluated_get(depsgraph).to_mesh_clear()

        name = os.path.splitext(os.path.basename(self.filepath))[0]
        dig_name = '{}-00.dig'.format(name)
        dml_name = '{}.dml'.format(name)
        dis_name = '{}.dis'.format(name)
        dil_name = '{}-000.dil'.format(name)

        dig = dil = None
        if self.collision_mode != 'EMPTY':
            dig, dil, err = self._run_objbuild(name, verts, uvs, faces, mats)
            if err:
                self.report({'ERROR'}, err)
                return {'CANCELLED'}
        else:
            dig = build_dig_emptybsp(verts, uvs, faces)
            self.report({'WARNING'},
                        'Empty BSP: file round-trips in Blender but the game '
                        'engine will not render it')

        entries = [(dis_name, build_dis(dig_name, dml_name,
                                        dil_name if dil else None)),
                   (dig_name, dig), (dml_name, build_dml(mats))]
        if dil:
            entries.append((dil_name, dil))
        if self.tex_dir and os.path.isdir(self.tex_dir):
            for bp in sorted(glob.glob(os.path.join(self.tex_dir, '*.bmp'))):
                with open(bp, 'rb') as f:
                    entries.append((os.path.basename(bp), f.read()))

        with open(self.filepath, 'wb') as f:
            f.write(build_vol(entries))
        self.report({'INFO'},
                    'Exported {} ({} verts, {} faces, {} materials{})'.format(
                        os.path.basename(self.filepath), len(verts), len(faces),
                        len(mats), ', real BSP' if dil else ''))
        return {'FINISHED'}

    def _run_objbuild(self, name, verts, uvs, faces, mats):
        """Write a temp OBJ, run the engine's BSP compiler (objbuild.js via
        Node), return (dig_bytes, dil_bytes, error)."""
        objbuild = bpy.path.abspath(self.objbuild_path)
        if not os.path.isfile(objbuild):
            return None, None, ('objbuild.js not found at {!r} -- set the path, '
                                'or pick "Empty BSP"'.format(objbuild))
        tmpdir = tempfile.mkdtemp(prefix='tribes_dis_')
        obj_path = os.path.join(tmpdir, name + '.obj')
        dig_path = os.path.join(tmpdir, name + '.dig')
        dil_path = os.path.join(tmpdir, name + '.dil')
        with open(obj_path, 'w') as o:
            o.write('# exported by Tribes DTS Blender addon\n')
            for (x, y, z) in verts:
                o.write('v {:.6g} {:.6g} {:.6g}\n'.format(x, y, z))
            for (u, v) in uvs:
                o.write('vt {:.6g} {:.6g}\n'.format(u, v))
            cur = -1
            for mat, corners in faces:
                if mat != cur:
                    o.write('usemtl {}\n'.format(mats[mat]))
                    cur = mat
                o.write('f ' + ' '.join(
                    '{}/{}'.format(vi + 1, ti + 1) if ti >= 0 else str(vi + 1)
                    for (vi, ti) in corners) + '\n')

        cmd = ['node', objbuild, obj_path, dig_path, dil_path]
        if self.collision_mode == 'BOX':
            cmd.append('--box')
        elif self.collision_mode == 'NOCOLLIDE':
            cmd.append('--nocollide')
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                                 cwd=os.path.dirname(objbuild))
        except FileNotFoundError:
            return None, None, 'Node.js not found on PATH (needed to run objbuild.js)'
        except subprocess.TimeoutExpired:
            return None, None, 'objbuild.js timed out (300s)'
        if res.returncode != 0 or not os.path.isfile(dig_path):
            tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
            return None, None, 'objbuild failed: {}'.format(' | '.join(tail) or 'no output')
        with open(dig_path, 'rb') as f:
            dig = f.read()
        dil = None
        if os.path.isfile(dil_path):
            with open(dil_path, 'rb') as f:
                dil = f.read()
        return dig, dil, None
