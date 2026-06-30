#!/usr/bin/env python3
"""Build a whole twin from just an AOI, by querying national data live.

Give it an area of interest (a shapefile / GeoJSON / GeoPackage polygon) and it
fetches everything else for that footprint — no big data ships in the repo:

  1. 3DEP elevation   -> terrain + georeferencing   (scripts/ingest_dem.py)
  2. NAIP Plus ortho  -> aerial drape               (scripts/ingest_imagery.py)
  3. 3DEP lidar LPC   -> DSM/DTM canopy-height inputs where available
                         (scripts/fetch_lidar.py; best effort)
  4. LANDFIRE EVT     -> a displayed land-cover layer + vegetation typing
                         (packs/us-national/fetch_landfire.py)
  5. LANDFIRE forest  -> cover, height, fuels, fire regime, departure layers
                         (scripts/fetch_forest_ecology.py; best effort)
  6. gSSURGO soils    -> clipped soil polygons + hydrologic attributes
                         (scripts/fetch_gssurgo.py; best effort)
  7. optional national atlas layers selected by the setup UI
                         (scripts/fetch_national_layers.py; best effort)
  8. vegetation       -> typed trees (TWIN_PACK=us-national)

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
                          else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aoi", required=True, help="AOI polygon (shapefile/GeoJSON/…)")
    ap.add_argument("--data-dir", required=True, help="output twin data dir")
    ap.add_argument("--name", default="VEIL twin", help="twin display name")
    ap.add_argument("--dem-res", type=float, default=4.0, help="terrain cell size (m)")
    ap.add_argument("--no-lidar", action="store_true",
                    help="skip 3DEP point-cloud fetch + DSM/DTM derivation")
    ap.add_argument("--lidar-max-gb", type=float, default=3.0,
                    help="soft cap for 3DEP LAZ downloads (default 3 GB)")
    ap.add_argument("--lidar-max-tiles", type=int, default=24,
                    help="maximum 3DEP LAZ tiles to download")
    ap.add_argument("--no-gssurgo", action="store_true",
                    help="skip USDA SDA gSSURGO/SSURGO soil fetch")
    ap.add_argument("--no-forest-ecology", action="store_true",
                    help="skip extra LANDFIRE forest/fire ecology atlas layers")
    ap.add_argument("--no-vegetation", action="store_true",
                    help="skip the vegetation build")
    ap.add_argument("--no-climate", action="store_true",
                    help="skip the Daymet climate-forcing fetch (snowmelt/storm presets)")
    ap.add_argument("--no-hydrology", action="store_true",
                    help="skip the Tier-1 terrain-hydrology analysis")
    ap.add_argument("--national-layers", default="",
                    help="comma-separated optional national layer ids to fetch after "
                         "terrain/georef exists (see scripts/fetch_national_layers.py catalog)")
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
          f"{tuple(round(v, 5) for v in bbox_wgs)}", flush=True)

    # --- 1. 3DEP DEM -> terrain ------------------------------------------
    national_layer_ids = [s.strip() for s in args.national_layers.split(",") if s.strip()]
    total_steps = (4 + (0 if args.no_lidar else 1)
                   + (0 if args.no_forest_ecology else 1)
                   + (0 if args.no_gssurgo else 1)
                   + (1 if national_layer_ids else 0)
                   + (0 if args.no_climate else 1)
                   + (0 if args.no_hydrology else 1))
    print(f"\n[1/{total_steps}] fetching 3DEP elevation…", flush=True)
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

    # --- 2. NAIP Plus orthoimagery -> imagery -----------------------------
    print(f"\n[2/{total_steps}] fetching NAIP Plus orthoimagery…", flush=True)
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
        print(f"  NAIP Plus orthoimagery unavailable here ({str(e)}); "
              "continuing without imagery", flush=True)

    # --- 3. 3DEP lidar point cloud -> DSM/DTM for CHM vegetation -----------
    if not args.no_lidar:
        print(f"\n[3/{total_steps}] fetching 3DEP lidar point cloud (best effort)…", flush=True)
        try:
            run([sys.executable, os.path.join(HERE, "fetch_lidar.py"),
                 "--aoi", os.path.abspath(args.aoi),
                 "--data-dir", data_dir,
                 "--max-gb", str(args.lidar_max_gb),
                 "--max-tiles", str(args.lidar_max_tiles)])
        except Exception as e:  # noqa: BLE001
            print(f"  lidar unavailable or failed ({str(e)}); "
                  "continuing with NDVI/LANDFIRE vegetation fallback", flush=True)

    # --- LANDFIRE EVT -> displayed layer + typing -------------------------
    landfire_step = 4 if not args.no_lidar else 3
    print(f"\n[{landfire_step}/{total_steps}] fetching LANDFIRE EVT…", flush=True)
    run([sys.executable, os.path.join(PROJECT, "packs", "us-national", "fetch_landfire.py"),
         "--data-dir", data_dir])

    # --- additional LANDFIRE forest/fire ecology atlas layers --------------
    prev_step = landfire_step
    if not args.no_forest_ecology:
        forest_step = prev_step + 1
        prev_step = forest_step
        print(f"\n[{forest_step}/{total_steps}] fetching LANDFIRE forest/fire ecology layers…",
              flush=True)
        try:
            run([sys.executable, os.path.join(HERE, "fetch_forest_ecology.py"),
                 "--data-dir", data_dir])
        except Exception as e:  # noqa: BLE001
            print(f"  LANDFIRE forest/fire layers unavailable or failed ({str(e)}); "
                  "continuing", flush=True)

    # --- gSSURGO soils ----------------------------------------------------
    if not args.no_gssurgo:
        soils_step = prev_step + 1
        prev_step = soils_step
        print(f"\n[{soils_step}/{total_steps}] fetching gSSURGO soils…", flush=True)
        try:
            run([sys.executable, os.path.join(HERE, "fetch_gssurgo.py"),
                 "--data-dir", data_dir])
        except Exception as e:  # noqa: BLE001
            print(f"  gSSURGO unavailable or failed ({str(e)}); continuing", flush=True)

    # --- selected optional national atlas layers --------------------------
    if national_layer_ids:
        national_step = prev_step + 1
        prev_step = national_step
        print(f"\n[{national_step}/{total_steps}] fetching selected national atlas layers…",
              flush=True)
        try:
            run([sys.executable, os.path.join(HERE, "fetch_national_layers.py"),
                 "fetch",
                 "--aoi", os.path.abspath(args.aoi),
                 "--data-dir", data_dir,
                 "--layers", ",".join(national_layer_ids)])
        except Exception as e:  # noqa: BLE001
            print(f"  selected national layers unavailable or failed ({str(e)}); "
                  "continuing", flush=True)

    # --- vegetation -------------------------------------------------------
    climate_enabled = 0 if args.no_climate else 1
    hydro_enabled = 0 if args.no_hydrology else 1
    if not args.no_vegetation:
        veg_step = total_steps - hydro_enabled - climate_enabled
        print(f"\n[{veg_step}/{total_steps}] building vegetation (us-national typing)…", flush=True)
        env = dict(os.environ, TWIN_PACK="us-national", TWIN_DATA_DIR=data_dir)
        run([sys.executable, os.path.join(HERE, "analyze_vegetation.py")], env=env)
    # mark the twin's pack so the viewer/agent use national typing by default
    with open(os.path.join(data_dir, "pack.txt"), "w") as fh:
        fh.write("us-national\n")

    # --- Daymet climate forcing -------------------------------------------
    # Snowmelt/storm presets for the Simulation window's Tier-2 scenarios.
    # North America only; best effort — a failure leaves the window on explicit
    # event depths (terrain geometry is unaffected).
    if not args.no_climate:
        print(f"\n[{total_steps - hydro_enabled}/{total_steps}] fetching Daymet climate forcing…", flush=True)
        try:
            run([sys.executable, os.path.join(PROJECT, "packs", "us-national",
                                              "fetch_climate_forcing.py"),
                 "--data-dir", data_dir])
        except Exception as e:  # noqa: BLE001
            print(f"  climate forcing unavailable or failed ({str(e)}); "
                  "continuing (scenarios use explicit depths)", flush=True)

    # --- Tier-1 terrain hydrology -----------------------------------------
    # Needs only the terrain grid (+ soils if fetched, for seep/CN richness);
    # terrain-only is a clean degrade, so this runs for any twin. Best effort —
    # a failure here never sinks the build.
    if not args.no_hydrology:
        print(f"\n[{total_steps}/{total_steps}] analyzing terrain hydrology (Tier 1)…", flush=True)
        try:
            run([sys.executable, os.path.join(HERE, "analyze_hydrology.py"),
                 "--data-dir", data_dir])
        except Exception as e:  # noqa: BLE001
            print(f"  hydrology analysis failed ({str(e)}); continuing", flush=True)

    print(f"\nDone. Serve it:\n  TWIN_DATA_DIR={os.path.relpath(data_dir, os.getcwd())} "
          "PORT=4174 npm start   # -> http://127.0.0.1:4174", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
