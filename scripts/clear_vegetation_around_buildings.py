#!/usr/bin/env python3
"""Clear vegetation that intersects the placed 3D building models.

For each building, uses the model's plan outline (clearance.json if the
twin has one — a convex hull of the model's wall cross-section, which covers
ground tiles and additions that extend beyond the mapped footprint; falls
back to the footprint ring) and pushes nearby trees/shrubs
out to a clearance distance from the polygon (crown-aware for trees);
instances whose trunk lands deep inside are deleted instead of teleported
across the building. Relocated instances get their elevation resampled from
the terrain grid.

Writes the cleared population to the twin store (moved stems become new
entities at their new position — entity IDs are position-derived — and the
old positions are reconciled out of member_parcel), then re-exports the
viewer payloads. Note: re-running the vegetation build regenerates the
uncleared population, so run this after `npm run build-vegetation`.
"""
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_viewer_payloads
from twin_store import SHRUB_ATTRS, TREE_ATTRS, Store

ROOT = Path(__file__).resolve().parent.parent
VEG = ROOT / 'data' / 'vegetation'
FOOTPRINTS = ROOT / 'data' / 'buildings' / 'footprints.geojson'
GRID = ROOT / 'data' / 'terrain' / 'grid.json'

CLEARANCE = ROOT / 'data' / 'buildings' / 'models' / 'clearance.json'

# Extra clearance beyond the building outline, metres (per building id).
MARGIN = {}
MARGIN_DEFAULT = 1.0
DELETE_DEPTH = 1.5   # trunk this far inside the outline -> delete


def point_in_ring(ring, x, y):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][:2]
        xj, yj = ring[j][:2]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def nearest_on_ring(ring, x, y):
    best = None
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][:2]
        x2, y2 = ring[i + 1][:2]
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1e-9)))
        px, py = x1 + t * dx, y1 + t * dy
        d = math.hypot(x - px, y - py)
        if best is None or d < best[0]:
            best = (d, px, py)
    return best


def sample_height(grid, x, y):
    w = max(1e-9, grid['maxX'] - grid['minX'])
    h = max(1e-9, grid['maxY'] - grid['minY'])
    xr = min(max((x - grid['minX']) / w, 0), 0.999999)
    yr = min(max((y - grid['minY']) / h, 0), 0.999999)
    xi = xr * (grid['width'] - 1)
    yi = (1 - yr) * (grid['height'] - 1)
    x0, y0 = int(xi), int(yi)
    x1 = min(grid['width'] - 1, x0 + 1)
    y1 = min(grid['height'] - 1, y0 + 1)
    tx, ty = xi - x0, yi - y0
    hs = grid['heights']
    vals = [
        (hs[y0 * grid['width'] + x0], (1 - tx) * (1 - ty)),
        (hs[y0 * grid['width'] + x1], tx * (1 - ty)),
        (hs[y1 * grid['width'] + x0], (1 - tx) * ty),
        (hs[y1 * grid['width'] + x1], tx * ty),
    ]
    vals = [(v, wt) for v, wt in vals if isinstance(v, (int, float))]
    total = sum(wt for _, wt in vals) or 1.0
    return sum(v * wt for v, wt in vals) / total


def process(instances, rings, grid, min_elev, crown_aware):
    kept = []
    moved = deleted = 0
    for inst in instances:
        x, y = inst['x'], inst['y']
        action = None
        # Re-sweep all rings after every push: clearing one building can land
        # the stem inside another's clearance. A stem that can't be cleared in
        # 8 sweeps is squeezed between buildings -> delete.
        for _sweep in range(8):
            swept = None
            for bid, ring, margin in rings:
                d, px, py = nearest_on_ring(ring, x, y)
                inside = point_in_ring(ring, x, y)
                # full crown radius so canopies don't overhang the roofs
                clearance = margin + (inst.get('radius', 0) if crown_aware else 0.3)
                if inside and d > DELETE_DEPTH:
                    swept = 'delete'
                    break
                if inside or d < clearance:
                    ux, uy = x - px, y - py
                    norm = math.hypot(ux, uy) or 1e-9
                    if inside:
                        ux, uy = -ux, -uy
                    # overshoot by 5 mm so the rounded position stays strictly
                    # outside the clearance and re-runs are a fixpoint
                    nx = px + ux / norm * (clearance + 0.005)
                    ny = py + uy / norm * (clearance + 0.005)
                    inst['x'] = round(nx, 3)
                    inst['y'] = round(ny, 3)
                    inst['z'] = round(sample_height(grid, nx, ny) + min_elev, 2)
                    x, y = nx, ny
                    swept = 'move'
            if swept == 'delete':
                action = 'delete'
                break
            if swept != 'move':
                break  # clear of every ring
            action = 'move'
        else:
            action = 'delete'
        if action == 'delete':
            deleted += 1
        else:
            moved += 1 if action == 'move' else 0
            kept.append(inst)
    return kept, moved, deleted


def main():
    footprints = json.loads(FOOTPRINTS.read_text())
    by_oid = {f['properties']['OBJECTID']: f for f in footprints['features']}
    grid = json.loads(GRID.read_text())
    min_elev = grid['minElevation']

    store = Store()
    # Building IDs/footprint links come from the store, not the exported manifest.
    models = {eid.split(':', 1)[1]: attrs
              for eid, attrs in store.latest_attrs('building_model').items()}

    hulls = json.loads(CLEARANCE.read_text()) if CLEARANCE.exists() else {}
    rings = []
    for bid in sorted(models):
        ring = hulls.get(bid) or \
            by_oid[models[bid]['footprint_objectid']]['geometry']['coordinates'][0]
        rings.append((bid, ring, MARGIN.get(bid, MARGIN_DEFAULT)))

    run = store.begin_run('clear_vegetation_around_buildings.py',
                          inputs=[str(CLEARANCE), str(FOOTPRINTS)])
    # Read the parcel populations from the store, not the exported JSON.
    for kind, layer, attrs, crown_aware in [
        ('tree', 'trees', TREE_ATTRS, True),
        ('shrub', 'shrubs', SHRUB_ATTRS, False),
    ]:
        instances = store.instances(kind, layer, 'member_parcel', attrs,
                                    include_id=False)
        kept, moved, deleted = process(instances, rings, grid, min_elev, crown_aware)
        print(f'{kind}s: {len(instances)} -> {len(kept)} '
              f'({moved} pushed clear, {deleted} deleted)')
        ids, stats = store.bulk_upsert_vegetation(kind, layer, kept, run, 'member_parcel')
        left, retired = store.reconcile_membership(
            kind, 'member_parcel', ids, run,
            other_member_attrs=('member_surrounding',))
        print(f'  store: {stats["created"]} created, {stats["reactivated"]} reactivated, '
              f'{left} left parcel ({retired} retired)')
    store.finish_run(run)
    store.close()
    export_viewer_payloads.export_all()


if __name__ == '__main__':
    sys.exit(main())
