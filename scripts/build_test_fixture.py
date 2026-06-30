#!/usr/bin/env python3
"""Generate the tiny committed test fixture twin (tests/fixtures/mini-twin/data).

This is a *synthetic* twin — no DEM, no imagery, no network — built purely so
`npm test` (scripts/twin_query_test.py) can run offline and deterministically in
CI, without the demo's live 3DEP/NAIP/LANDFIRE fetch. It seeds a small,
hand-made grid + AOI + a lattice of trees through the normal twin_store write
path, so the resulting twin.gpkg and journal are real store artifacts (the test
asserts against them exactly as it would against any twin).

Run once (needs GDAL only to bootstrap the empty GeoPackage container, like any
store write — no internet):

    python3 scripts/build_test_fixture.py            # default fixture dir
    python3 scripts/build_test_fixture.py --data-dir /tmp/mini

The output is committed (see .gitignore's tests/fixtures/ exception). The
deterministic layout below is the contract the test derives its expectations
from; keep it stable. Regenerate after a schema change, then commit the result.
"""

import argparse
import json
import os
import shutil
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DEFAULT_DIR = os.path.join(PROJECT, "tests", "fixtures", "mini-twin", "data")

# A real projected CRS so georef round-trips and the viewer's proj4js agrees
# with pyproj (the test cross-checks both). UTM 13N over Colorado-ish ground.
ANALYSIS_CRS = "EPSG:32613"
ORIGIN_UTM = [476000.0, 4428000.0]      # scene origin in the projected CRS
GEOGRAPHIC_CRS = "EPSG:4326"

# Inner terrain bounds (scene-local meters) and a 9x9 sample lattice.
GRID_W = GRID_H = 9
INNER = 200.0                            # inner half-span: [-200, 200]
AOI_HALF = 180.0                         # AOI square, comfortably inside outer

# Tree lattice: dense enough that any anchor-centered test stand holds >5 stems
# and a mix of heights/types, small enough to stay a few-KB gpkg.
TREE_SPAN = 150.0
TREE_STEP = 30.0


def proj4_for(crs):
    from pyproj import CRS
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="You will likely lose important projection information.*",
            category=UserWarning,
            module="pyproj.crs.crs",
        )
        return CRS(crs).to_proj4().replace(" +type=crs", "")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)


def build_grid():
    xs = [(-INNER) + i * (2 * INNER) / (GRID_W - 1) for i in range(GRID_W)]
    ys = [(-INNER) + j * (2 * INNER) / (GRID_H - 1) for j in range(GRID_H)]
    heights = []
    # A smooth synthetic surface; deterministic, gently sloped so identify_at
    # elevations land inside [minElevation, maxElevation].
    for j in range(GRID_H):
        for i in range(GRID_W):
            x, y = xs[i], ys[j]
            heights.append(round(1700.0 + 0.05 * x + 0.03 * y, 3))
    xstep = (2 * INNER) / (GRID_W - 1)
    ystep = (2 * INNER) / (GRID_H - 1)
    return {
        "width": GRID_W, "height": GRID_H, "heights": heights,
        "minX": -INNER, "maxX": INNER, "minY": -INNER, "maxY": INNER,
        "outerMinX": -INNER - xstep / 2, "outerMaxX": INNER + xstep / 2,
        "outerMinY": -INNER - ystep / 2, "outerMaxY": INNER + ystep / 2,
        "minElevation": min(heights), "maxElevation": max(heights),
    }


def aoi_feature_collection():
    h = AOI_HALF
    ring = [[-h, -h], [h, -h], [h, h], [-h, h], [-h, -h]]
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": "fixture AOI"},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }],
    }


def tree_items():
    """A deterministic lattice of stems with varied height/type/radius."""
    coords = []
    v = -TREE_SPAN
    while v <= TREE_SPAN + 1e-9:
        coords.append(round(v, 1))
        v += TREE_STEP
    items = []
    for i, x in enumerate(coords):
        for j, y in enumerate(coords):
            n = i * len(coords) + j
            height = round(5.0 + (n % 20) * 0.7, 2)       # ~5.0 .. 18.3, varied
            radius = round(1.5 + height / 10.0, 2)
            ttype = "evergreen" if (i + j) % 2 == 0 else "deciduous"
            items.append({
                "x": x, "y": y, "source": "fixture",
                "height": height, "radius": radius, "type": ttype,
            })
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=DEFAULT_DIR)
    args = ap.parse_args()
    data = os.path.abspath(args.data_dir)

    # Clean prior artifacts so the rebuild is deterministic (the committed
    # journal/gpkg should reflect exactly this script, nothing left over).
    for rel in ("twin.gpkg", "twin.gpkg.bak", "journal", "terrain",
                "atlas", "georef.json", "annotations.json"):
        p = os.path.join(data, rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
    os.makedirs(data, exist_ok=True)

    write_json(os.path.join(data, "georef.json"), {
        "analysis_crs": ANALYSIS_CRS,
        "geographic_crs": GEOGRAPHIC_CRS,
        "origin_utm": ORIGIN_UTM,
        "proj4": proj4_for(ANALYSIS_CRS),
        "note": "synthetic test fixture — see scripts/build_test_fixture.py",
    })
    write_json(os.path.join(data, "terrain", "grid.json"), build_grid())
    write_json(os.path.join(data, "terrain", "aoi_local.geojson"),
               aoi_feature_collection())
    # Minimal atlas catalog so the query layer's identify/list paths run with
    # zero atlas layers (those test sections skip cleanly).
    write_json(os.path.join(data, "atlas", "local", "viewer-layers.json"),
               {"layers": []})

    # The store write must target this dir. twin_store/twin_georef resolve
    # TWIN_DATA_DIR at import, so set it before importing.
    os.environ["TWIN_DATA_DIR"] = data
    sys.path.insert(0, HERE)
    import twin_store  # noqa: E402

    store = twin_store.open_store()
    try:
        run_id = store.begin_run("build_test_fixture.py",
                                 notes="synthetic fixture vegetation")
        items = tree_items()
        ids, stats = store.bulk_upsert_vegetation(
            "tree", "trees", items, run_id, member_attr="member_parcel",
            source_default="fixture")
        store.reconcile_membership("tree", "member_parcel", set(ids), run_id)
        store.finish_run(run_id, notes=f"{stats['created']} stems")
    finally:
        store.close()

    print(f"fixture twin built at {os.path.relpath(data, PROJECT)}: "
          f"{stats['created']} trees, {stats['observations']} observations")


if __name__ == "__main__":
    main()
