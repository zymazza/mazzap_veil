#!/usr/bin/env python3
"""Set each building's z_offset so the building base sits on the terrain.

The photogrammetry tiles have skirts that extend well below the ground
surface; the viewer anchors the model's bbox bottom on the terrain, which
leaves the building floating on a plinth. For sample points just outside the
footprint ring (mapped into model plan coordinates via the current placement),
find the local ground level (lowest nearby vertex z) and shift the model down
by the median ground-above-bbox-bottom distance.
"""
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'data' / 'buildings' / 'models'
FOOTPRINTS = ROOT / 'data' / 'buildings' / 'footprints.geojson'


def read_positions(path):
    data = path.read_bytes()
    offset = 12
    gltf = None
    binary = b''
    length = struct.unpack_from('<I', data, 8)[0]
    while offset < length:
        clen, ctype = struct.unpack_from('<II', data, offset)
        offset += 8
        chunk = data[offset:offset + clen]
        offset += clen
        if ctype == 0x4E4F534A:
            gltf = json.loads(chunk)
        elif ctype == 0x004E4942:
            binary = chunk
    pts = []
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            acc = gltf['accessors'][prim['attributes']['POSITION']]
            view = gltf['bufferViews'][acc['bufferView']]
            start = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
            arr = np.frombuffer(binary, dtype='<f4',
                                count=acc['count'] * 3, offset=start)
            pts.append(arr.reshape(-1, 3))
    return np.concatenate(pts)


def main():
    manifest_path = OUT_DIR / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    footprints = json.loads(FOOTPRINTS.read_text())
    by_oid = {f['properties']['OBJECTID']: f for f in footprints['features']}

    for entry in manifest['buildings']:
        bid = entry['id']
        pts = read_positions(OUT_DIR / f'{bid}.glb').astype(np.float64)
        p = entry['placement']
        ring = np.array(by_oid[entry['footprint_objectid']]
                        ['geometry']['coordinates'][0])[:, :2]
        centroid = ring.mean(axis=0)
        # sample points near the ring: prefer just outside (adjacent lawn),
        # fall back inward when the model tile barely exceeds the footprint
        dirs = (ring - centroid) / np.linalg.norm(
            ring - centroid, axis=1, keepdims=True)
        candidates = [ring + dirs * 1.5, ring, ring - dirs * 1.0]

        # world plan -> model plan (model is Z-up: plan = x,y)
        bbox = entry['model_bbox']
        bc = np.array([(bbox['min'][0] + bbox['max'][0]) / 2,
                       (bbox['min'][1] + bbox['max'][1]) / 2])
        rad = math.radians(-p['yaw_deg'])
        c, s = math.cos(rad), math.sin(rad)
        rot = np.array([[c, -s], [s, c]])
        plan = pts[:, :2]
        grounds = []
        for out in candidates:
            local = (out - np.array([p['x'], p['y']])) @ rot.T / p['scale'] + bc
            for q in local:
                d2 = ((plan - q) ** 2).sum(axis=1)
                near = pts[d2 < 1.0 ** 2]
                if len(near) > 20:
                    grounds.append(np.percentile(near[:, 2], 2))
            if len(grounds) >= 4:
                break
            grounds = []
        if not grounds:
            print(f'{bid}: no ground samples found, skipping')
            continue
        local = candidates[0]
        ground_z = float(np.median(grounds))
        zmin = bbox['min'][2]
        z_offset = -(ground_z - zmin) * p['scale']
        print(f'{bid}: ground at {ground_z:+.2f} m (model z), bbox min '
              f'{zmin:+.2f} -> z_offset {z_offset:+.2f} m '
              f'({len(grounds)}/{len(local)} samples)')
        p['z_offset'] = round(z_offset, 3)

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f'updated {manifest_path}')


if __name__ == '__main__':
    sys.exit(main())
