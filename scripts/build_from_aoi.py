#!/usr/bin/env python3
"""Build a whole twin from just an AOI, by querying national data live.

Give it an area of interest (a shapefile / GeoJSON / GeoPackage polygon) and it
fetches everything else for that footprint — no big data ships in the repo:

  1. 3DEP elevation   -> terrain + georeferencing   (scripts/ingest_dem.py)
  2. NAIP imagery     -> aerial drape               (scripts/ingest_imagery.py)
  3. LANDFIRE EVT     -> a displayed land-cover layer + vegetation typing
                         (packs/us-national/fetch_landfire.py)
  4. vegetation       -> typed trees (TWIN_PACK=us-national)

The result is a complete, viewable twin in --data-dir, built from one small
committed AOI file plus live national queries. This is also how the bundled
demo (packs/us-national/demo/) is produced.

Usage:
  python3 scripts/build_from_aoi.py --aoi area.shp --data-dir ./twins/mine/data \
      --name "My Place"

Needs internet. CONUS only (the national services cover the lower 48). To use
your OWN data instead of fetching, skip this and run ingest_dem / ingest_imagery
/ add_layer against your files directly (see docs/make-a-twin.md).
"""

import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def run(cmd, env=None):
    print("  $", " ".join(os.path.relpath(c, PROJECT) if c.startswith("/") and os.path.exists(c)
                          else c for c in cmd))
    subprocess.run(cmd, check=True, env=env)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aoi", required=True, help="AOI polygon (shapefile/GeoJSON/…)")
    ap.add_argument("--data-dir", required=True, help="output twin data dir")
    ap.add_argument("--name", default="VEIL twin", help="twin display name")
    ap.add_argument("--dem-res", type=float, default=4.0, help="terrain cell size (m)")
    ap.add_argument("--no-vegetation", action="store_true",
                    help="skip the vegetation build")
    ap.add_argument("--force", action="store_true", help="overwrite an existing twin")
    args = ap.parse_args()

    import national_fetch
    import ingest_dem
    from osgeo import osr
    from pyproj import Transformer

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="veil_aoi_")

    # --- AOI ring + WGS84 bbox -------------------------------------------
    ring, aoi_crs = ingest_dem.ring_from_aoi(args.aoi, "EPSG:4326")
    to_wgs = Transformer.from_crs(aoi_crs, "EPSG:4326", always_xy=True)
    lons, lats = zip(*[to_wgs.transform(p[0], p[1]) for p in ring])
    bbox_wgs = (min(lons), min(lats), max(lons), max(lats))
    print(f"AOI: {os.path.basename(args.aoi)}  bbox(WGS84)="
          f"{tuple(round(v, 5) for v in bbox_wgs)}")

    # --- 1. 3DEP DEM -> terrain ------------------------------------------
    print("\n[1/4] fetching 3DEP elevation…")
    dem_tif = os.path.join(tmp, "dem.tif")
    # Fetch in WGS84 (geographic); ingest_dem reprojects to the UTM working CRS
    # it picks. The bbox is in degrees, so the sampling step is too (~3 m).
    deg_per_m = 1.0 / 111320.0
    national_fetch.fetch_3dep_dem(bbox_wgs, 4326, dem_tif, resolution_m=3.0 * deg_per_m)
    cmd = [sys.executable, os.path.join(HERE, "ingest_dem.py"), dem_tif,
           "--aoi", os.path.abspath(args.aoi), "--name", args.name,
           "--data-dir", data_dir, "--resolution", str(args.dem_res)]
    if args.force:
        cmd.append("--force")
    run(cmd)

    # --- 2. NAIP -> imagery ----------------------------------------------
    print("\n[2/4] fetching NAIP imagery…")
    import json
    import twin_georef
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef_path)
    epsg = twin_georef.epsg_number(georef_path)
    foot = (grid["outerMinX"] + ox, grid["outerMinY"] + oy,
            grid["outerMaxX"] + ox, grid["outerMaxY"] + oy)
    naip_tif = os.path.join(tmp, "naip.tif")
    try:
        national_fetch.fetch_naip(foot, epsg, naip_tif, resolution_m=1.0)
        run([sys.executable, os.path.join(HERE, "ingest_imagery.py"), naip_tif,
             "--data-dir", data_dir])
    except Exception as e:  # noqa: BLE001
        print(f"  NAIP unavailable here ({str(e)[:80]}); continuing without imagery")

    # --- 3. LANDFIRE EVT -> displayed layer + typing ---------------------
    print("\n[3/4] fetching LANDFIRE EVT…")
    run([sys.executable, os.path.join(PROJECT, "packs", "us-national", "fetch_landfire.py"),
         "--data-dir", data_dir])

    # --- 4. vegetation ----------------------------------------------------
    if not args.no_vegetation:
        print("\n[4/4] building vegetation (us-national typing)…")
        env = dict(os.environ, TWIN_PACK="us-national", TWIN_DATA_DIR=data_dir)
        run([sys.executable, os.path.join(HERE, "analyze_vegetation.py")], env=env)
    # mark the twin's pack so the viewer/agent use national typing by default
    with open(os.path.join(data_dir, "pack.txt"), "w") as fh:
        fh.write("us-national\n")

    print(f"\nDone. Serve it:\n  TWIN_DATA_DIR={os.path.relpath(data_dir, os.getcwd())} "
          "PORT=4174 npm start   # -> http://127.0.0.1:4174")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
