#!/usr/bin/env python3
"""Repack the photogrammetry building GLBs for the viewer and compute initial
footprint placements.

- Downscales embedded PNG textures (some are 8K+, 40-70 MB each) to JPEG so the
  browser can load the models: buildings/assets/{id}/lod0.glb (85-400 MB)
  -> data/buildings/models/{id}.glb (a few MB + geometry).
- Matches each model to its footprint in data/buildings/footprints.geojson
  (scene-local meters) via the projected centroid in the asset metadata
  (crs + footprint_centroid in asset_meta.json), fits a minimum rotated
  rectangle to the footprint, and derives an initial placement (position,
  yaw, uniform scale) for the viewer.

Assets are discovered: every buildings/assets/<id>/ folder with a lod0.glb
and an asset_meta.json is repacked; the display name comes from the metadata
("name"), falling back to the folder id.

Outputs data/buildings/models/manifest.json; the viewer applies (and we then
hand-tune) the placement numbers stored there. The run is recorded in the twin
store: each building_model entity gets its manifest attributes observed plus a
model_file provenance record (output path, byte size, content hash, source
asset). The GLB binaries stay on disk — registered in the store, not embedded.
"""
import io
import json
import math
import os
import struct
import sys
from pathlib import Path

from PIL import Image
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_store

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'buildings' / 'assets'
OUT_DIR = ROOT / 'data' / 'buildings' / 'models'
FOOTPRINTS = ROOT / 'data' / 'buildings' / 'footprints.geojson'

import twin_georef
SCENE_ORIGIN = twin_georef.origin()  # projected CRS origin from data/georef.json
MAX_TEX = 2048
JPEG_QUALITY = 85

def discover_buildings():
    """Every assets/<id>/ folder with a lod0.glb + asset_meta.json is a
    building; the display name is the metadata's "name", else the folder id."""
    found = {}
    for meta_path in sorted(ASSETS.glob('*/asset_meta.json')):
        bid = meta_path.parent.name
        if not (meta_path.parent / 'lod0.glb').exists():
            continue
        meta = json.loads(meta_path.read_text())
        found[bid] = meta.get('name', bid)
    return found


BUILDINGS = discover_buildings()


def read_glb(path):
    data = path.read_bytes()
    magic, version, length = struct.unpack_from('<III', data, 0)
    assert magic == 0x46546C67, f'{path}: not a GLB'
    offset = 12
    gltf = None
    binary = b''
    while offset < length:
        clen, ctype = struct.unpack_from('<II', data, offset)
        offset += 8
        chunk = data[offset:offset + clen]
        offset += clen
        if ctype == 0x4E4F534A:
            gltf = json.loads(chunk)
        elif ctype == 0x004E4942:
            binary = chunk
    return gltf, binary


def write_glb(path, gltf, binary):
    js = json.dumps(gltf, separators=(',', ':')).encode()
    js += b' ' * (-len(js) % 4)
    binary = bytes(binary) + b'\x00' * (-len(binary) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    with open(path, 'wb') as f:
        f.write(struct.pack('<III', 0x46546C67, 2, total))
        f.write(struct.pack('<II', len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack('<II', len(binary), 0x004E4942))
        f.write(binary)


def shrink_image(blob):
    img = Image.open(io.BytesIO(blob))
    img.load()
    has_alpha = img.mode in ('RGBA', 'LA') and img.getextrema()[-1][0] < 250
    if max(img.size) > MAX_TEX:
        ratio = MAX_TEX / max(img.size)
        img = img.resize((round(img.width * ratio), round(img.height * ratio)),
                         Image.LANCZOS)
    out = io.BytesIO()
    if has_alpha:
        img.save(out, 'PNG', optimize=True)
        mime = 'image/png'
    else:
        img.convert('RGB').save(out, 'JPEG', quality=JPEG_QUALITY)
        mime = 'image/jpeg'
    return out.getvalue(), mime


def repack(src, dst):
    gltf, binary = read_glb(src)
    views = gltf.get('bufferViews', [])
    image_view_ids = {img['bufferView']: i for i, img in enumerate(gltf.get('images', []))}

    new_bin = bytearray()
    for vi, view in enumerate(views):
        start = view.get('byteOffset', 0)
        chunk = binary[start:start + view['byteLength']]
        if vi in image_view_ids:
            img = gltf['images'][image_view_ids[vi]]
            chunk, mime = shrink_image(chunk)
            img['mimeType'] = mime
        new_bin += b'\x00' * (-len(new_bin) % 4)
        view['byteOffset'] = len(new_bin)
        view['byteLength'] = len(chunk)
        new_bin += chunk
    gltf['buffers'] = [{'byteLength': len(new_bin)}]
    write_glb(dst, gltf, new_bin)

    # model bbox from POSITION accessors (glTF Y-up)
    mins = [1e9] * 3
    maxs = [-1e9] * 3
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            acc = gltf['accessors'][prim['attributes']['POSITION']]
            mins = [min(a, b) for a, b in zip(mins, acc['min'])]
            maxs = [max(a, b) for a, b in zip(maxs, acc['max'])]
    return mins, maxs


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and (
                (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])
            ) <= 0:
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(reversed(pts))
    return lower[:-1] + upper[:-1]


def min_rotated_rect(points):
    """Returns (cx, cy, long, short, angle_deg of long axis CCW from +x)."""
    hull = convex_hull(points)
    best = None
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        theta = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(-theta), math.sin(-theta)
        rx = [p[0] * c - p[1] * s for p in hull]
        ry = [p[0] * s + p[1] * c for p in hull]
        w = max(rx) - min(rx)
        h = max(ry) - min(ry)
        if best is None or w * h < best[0]:
            cx_r = (max(rx) + min(rx)) / 2
            cy_r = (max(ry) + min(ry)) / 2
            cc, ss = math.cos(theta), math.sin(theta)
            cx = cx_r * cc - cy_r * ss
            cy = cx_r * ss + cy_r * cc
            best = (w * h, cx, cy, w, h, theta)
    _, cx, cy, w, h, theta = best
    if w < h:
        w, h = h, w
        theta += math.pi / 2
    angle = math.degrees(theta) % 180
    return cx, cy, w, h, angle


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    footprints = json.loads(FOOTPRINTS.read_text())

    # placements/up-axes in an existing manifest are hand-tuned in the
    # in-viewer editor — never regenerate those
    manifest_path = OUT_DIR / 'manifest.json'
    tuned = {}
    if manifest_path.exists():
        tuned = {b['id']: b for b in
                 json.loads(manifest_path.read_text())['buildings']}

    manifest = {'buildings': []}
    for bid, name in BUILDINGS.items():
        meta = json.loads((ASSETS / bid / 'asset_meta.json').read_text())
        # The metadata's "crs" is the CRS of the photogrammetry source data,
        # not the twin's; the target CRS comes from georef.json.
        transformer = Transformer.from_crs(meta['crs'], twin_georef.crs(),
                                           always_xy=True)
        centroid_key = ('footprint_centroid' if 'footprint_centroid' in meta
                        else 'footprint_centroid_utm')
        e, n = transformer.transform(*meta[centroid_key])
        local = (e - SCENE_ORIGIN[0], n - SCENE_ORIGIN[1])

        # nearest footprint in scene-local coords
        def centroid(feature):
            ring = feature['geometry']['coordinates'][0]
            return (sum(p[0] for p in ring) / len(ring),
                    sum(p[1] for p in ring) / len(ring))

        feat = min(footprints['features'],
                   key=lambda f: math.dist(centroid(f), local))
        ring = [tuple(p[:2]) for p in feat['geometry']['coordinates'][0]]
        cx, cy, rect_long, rect_short, angle = min_rotated_rect(ring)

        src = ASSETS / bid / 'lod0.glb'
        dst = OUT_DIR / f'{bid}.glb'
        if dst.exists() and '--force' not in sys.argv:
            gltf, _ = read_glb(dst)
            mins = [1e9] * 3
            maxs = [-1e9] * 3
            for mesh in gltf.get('meshes', []):
                for prim in mesh.get('primitives', []):
                    acc = gltf['accessors'][prim['attributes']['POSITION']]
                    mins = [min(a, b) for a, b in zip(mins, acc['min'])]
                    maxs = [max(a, b) for a, b in zip(maxs, acc['max'])]
            print(f'{bid} ({name}): reusing {dst.relative_to(ROOT)}')
        else:
            print(f'{bid} ({name}): repacking {src.name} '
                  f'({src.stat().st_size / 1e6:.0f} MB)...', flush=True)
            mins, maxs = repack(src, dst)
        size = [maxs[i] - mins[i] for i in range(3)]
        # The assets are Z-up (asset_meta placement axes: x=east, y=south,
        # z=up) — ground footprint is XY, height is Z.
        print(f'  model XY {size[0]:.1f}x{size[1]:.1f} m, height {size[2]:.1f} m')

        model_long, model_short = max(size[0], size[1]), min(size[0], size[1])
        scale = ((rect_long / model_long) + (rect_short / model_short)) / 2
        yaw = angle if size[0] >= size[1] else (angle - 90) % 180
        print(f'  footprint rect {rect_long:.1f}x{rect_short:.1f} m @ {angle:.1f} deg '
              f'-> scale {scale:.3f}, yaw {yaw:.1f} deg')

        entry = tuned.get(bid)
        if entry:
            manifest['buildings'].append(entry)
            print(f'  keeping tuned placement: {entry["placement"]}')
            continue
        manifest['buildings'].append({
            'id': bid,
            'name': name,
            'url': f'/data/buildings/models/{bid}.glb',
            'up_axis': 'z',
            'footprint_objectid': feat['properties'].get('OBJECTID'),
            'footprint_rect': {
                'cx': round(cx, 3), 'cy': round(cy, 3),
                'long': round(rect_long, 2), 'short': round(rect_short, 2),
                'angle_deg': round(angle, 2),
            },
            'model_bbox': {'min': [round(v, 3) for v in mins],
                           'max': [round(v, 3) for v in maxs]},
            'placement': {
                'x': round(cx, 3),
                'y': round(cy, 3),
                'yaw_deg': round(yaw, 2),
                'scale': round(scale, 4),
                'z_offset': 0.0,
            },
        })

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f'wrote {manifest_path}')

    # ---- register the run and the model files in the twin store
    store = twin_store.Store()
    run = store.begin_run('build_building_models.py',
                          inputs=[str(ASSETS / bid / 'lod0.glb') for bid in BUILDINGS])
    written = 0
    for entry in manifest['buildings']:
        eid = f"building_model:{entry['id']}"
        store.upsert_entity(eid, 'building_model', run)
        for attr in ('name', 'url', 'up_axis', 'footprint_objectid',
                     'footprint_rect', 'model_bbox', 'placement'):
            if attr in entry:
                written += store.observe(eid, attr, entry[attr], run,
                                         source='build_building_models.py')
        glb = OUT_DIR / f"{entry['id']}.glb"
        src = ASSETS / entry['id'] / 'lod0.glb'
        written += store.observe(eid, 'model_file', {
            'path': str(glb.relative_to(ROOT)),
            'bytes': glb.stat().st_size,
            'sha1': twin_store.hash_inputs([str(glb)]),
            'source_asset': str(src.resolve()),
        }, run, source='build_building_models.py')
    store.finish_run(run, notes=f"{len(manifest['buildings'])} models")
    store.close()
    print(f'store run {run}: {written} new observation(s)')


if __name__ == '__main__':
    sys.exit(main())
