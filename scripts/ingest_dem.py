#!/usr/bin/env python3
"""Genesis: turn an arbitrary DEM GeoTIFF + an AOI into a viewable twin.

This is the entry point for pointing the engine at a new place. It emits the
three georeferencing artifacts everything else builds on, conforming to the
frozen grid contract (docs/grid-contract.md):

  data/terrain/grid.json          terrain grid (heights, inner/outer bounds)
  data/georef.json                projected CRS + proj4 string + scene origin
  data/terrain/aoi_local.geojson  the AOI ring in scene-local meters
  data/scene.json                 a minimal viewer scene (only when absent,
                                  or with --force; migrate/export overwrite
                                  it later once a store exists)

Decisions (documented, not configurable by accident):
  * Working CRS: --crs if given; else the DEM's native CRS when projected;
    else (geographic DEM) the WGS84 UTM zone of the AOI centroid.
  * Scene origin = the center of the outer grid footprint, rounded to 0.01 m,
    so scene-local coordinates stay small and roughly symmetric.
  * Cell size: --resolution if given; else the smallest whole-meter size
    >= the DEM's native resolution that keeps the grid under ~600k cells.
    Whole-meter cells keep the outer footprint a whole number of meters,
    which keeps imagery at an exact integer px/m (see ingest_imagery.py).
  * A polygon AOI masks heights outside the ring to null (the terrain mesh
    takes the AOI's shape); a bbox AOI keeps the full rectangle.

Usage:
  python3 scripts/ingest_dem.py dem.tif --bbox MINX MINY MAXX MAXY [--bbox-crs EPSG:n]
  python3 scripts/ingest_dem.py dem.tif --aoi boundary.geojson [--aoi-crs EPSG:n]
  python3 scripts/ingest_dem.py --validate data/terrain/grid.json
"""

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

NODATA = -99999.0
MAX_CELLS = 600_000
REQUIRED_FIELDS = ("width", "height", "heights", "minX", "maxX", "minY", "maxY",
                   "outerMinX", "outerMaxX", "outerMinY", "outerMaxY",
                   "minElevation", "maxElevation")


def validate_grid(grid, primary=True):
    """Assert a grid dict honors docs/grid-contract.md. Returns error list.
    primary=False for companion grids (the apron), whose min/maxElevation is
    the scene datum inherited from the primary grid, not their own range."""
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in grid:
            errors.append(f"missing field: {f}")
    if errors:
        return errors
    w, h = grid["width"], grid["height"]
    if len(grid["heights"]) != w * h:
        errors.append(f"heights length {len(grid['heights'])} != width*height {w * h}")
    if not (grid["minX"] < grid["maxX"] and grid["minY"] < grid["maxY"]):
        errors.append("inner bounds not ordered")
    xstep = (grid["maxX"] - grid["minX"]) / max(1, w - 1)
    ystep = (grid["maxY"] - grid["minY"]) / max(1, h - 1)
    for name, inner, outer, half in (
            ("outerMinX", grid["minX"], grid["outerMinX"], xstep / 2),
            ("outerMaxX", grid["maxX"], grid["outerMaxX"], xstep / 2),
            ("outerMinY", grid["minY"], grid["outerMinY"], ystep / 2),
            ("outerMaxY", grid["maxY"], grid["outerMaxY"], ystep / 2)):
        if abs(abs(outer - inner) - half) > 0.51 * max(xstep, ystep):
            errors.append(f"{name} is not ~half a cell beyond the inner bound "
                          f"(|{outer} - {inner}| vs half-cell {half:.3f})")
    valid = [v for v in grid["heights"] if v is not None]
    if not valid:
        errors.append("no valid heights")
    elif primary:
        if abs(min(valid) - grid["minElevation"]) > 0.01:
            errors.append(f"minElevation {grid['minElevation']} != min(heights) {min(valid)}")
        if abs(max(valid) - grid["maxElevation"]) > 0.01:
            errors.append(f"maxElevation {grid['maxElevation']} != max(heights) {max(valid)}")
    return errors


def utm_zone_crs(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def srs_to_epsg_string(srs):
    code = srs.GetAuthorityCode(None)
    auth = srs.GetAuthorityName(None)
    if code and auth == "EPSG":
        return f"EPSG:{code}"
    return None


def ring_from_aoi(path, source_crs):
    """Largest polygon ring from any OGR-readable AOI (GeoJSON, Shapefile,
    GeoPackage, KML…). Returns (ring, crs) — crs from the file when it carries
    one, else the passed source_crs."""
    from osgeo import ogr, osr
    ds = ogr.Open(path)
    if ds is None:  # not OGR-openable (or plain GeoJSON dict) — parse as JSON
        gj = json.load(open(path))
        feats = gj.get("features") if gj.get("type") == "FeatureCollection" else None
        geom = (feats[0]["geometry"] if feats else
                gj.get("geometry", gj))
        coords = geom["coordinates"]
        ring = coords[0] if geom["type"] == "Polygon" else \
            max(coords, key=lambda p: len(p[0]))[0]
        return ring, source_crs
    layer = ds.GetLayer(0)
    ref = layer.GetSpatialRef()
    crs = source_crs
    if ref is not None:
        code = ref.GetAuthorityCode(None)
        if code:
            crs = f"EPSG:{code}"
    biggest, best = None, -1
    for feat in layer:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        gj = json.loads(g.ExportToJson())
        polys = ([gj["coordinates"]] if gj["type"] == "Polygon"
                 else gj["coordinates"] if gj["type"] == "MultiPolygon" else [])
        for poly in polys:
            if len(poly[0]) > best:
                best, biggest = len(poly[0]), poly[0]
    if biggest is None:
        raise SystemExit(f"AOI {path} has no polygon geometry")
    return biggest, crs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("dem", nargs="?", help="DEM GeoTIFF")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("MINX", "MINY", "MAXX", "MAXY"),
                    help="AOI bbox (default: in the DEM's CRS; see --bbox-crs)")
    ap.add_argument("--bbox-crs", help="CRS of --bbox values (e.g. EPSG:4326)")
    ap.add_argument("--aoi", help="AOI polygon GeoJSON (assumed EPSG:4326 per spec)")
    ap.add_argument("--aoi-crs", default="EPSG:4326", help="CRS of --aoi coordinates")
    ap.add_argument("--crs", help="override the projected working CRS (EPSG:n)")
    ap.add_argument("--resolution", type=float, help="grid cell size in meters")
    ap.add_argument("--name", default="digital twin", help="scene display name")
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing georef.json / grid.json / scene.json")
    ap.add_argument("--validate", metavar="GRID_JSON",
                    help="only validate an existing grid against the contract")
    args = ap.parse_args()

    if args.validate:
        errors = validate_grid(json.load(open(args.validate)))
        for e in errors:
            print("contract violation:", e)
        print("valid" if not errors else f"{len(errors)} violations")
        return 1 if errors else 0

    if not args.dem or (args.bbox is None) == (args.aoi is None):
        ap.error("need a DEM plus exactly one of --bbox / --aoi")

    import numpy as np
    from osgeo import gdal, ogr, osr
    from pyproj import CRS, Transformer
    gdal.UseExceptions()

    data_dir = args.data_dir
    terrain_dir = os.path.join(data_dir, "terrain")
    georef_path = os.path.join(data_dir, "georef.json")
    grid_path = os.path.join(terrain_dir, "grid.json")
    if not args.force and (os.path.exists(georef_path) or os.path.exists(grid_path)):
        raise SystemExit("data/georef.json or data/terrain/grid.json already exists — "
                         "this is genesis for a new twin; pass --force to overwrite")

    dem = gdal.Open(args.dem)
    dem_srs = osr.SpatialReference(wkt=dem.GetProjection())
    dem_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dem_crs = srs_to_epsg_string(dem_srs)

    # ---- AOI ring in its source CRS
    if args.bbox:
        x0, y0, x1, y1 = args.bbox
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        aoi_crs = args.bbox_crs or dem_crs or "EPSG:4326"
        aoi_is_polygon = False
    else:
        ring, aoi_crs = ring_from_aoi(args.aoi, args.aoi_crs)
        aoi_is_polygon = True

    # centroid in lon/lat for UTM-zone selection
    to_wgs = Transformer.from_crs(aoi_crs, "EPSG:4326", always_xy=True)
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    c_lon, c_lat = to_wgs.transform(cx, cy)

    # ---- working CRS
    if args.crs:
        working = args.crs
    elif dem_srs.IsProjected():
        working = dem_crs
        if not working:
            raise SystemExit("DEM has a projected CRS without an EPSG code — pass --crs")
    else:
        working = utm_zone_crs(c_lon, c_lat)
    wcrs = CRS(working)
    print(f"working CRS: {working} ({wcrs.name})")

    to_working = Transformer.from_crs(aoi_crs, working, always_xy=True)
    wring = [list(to_working.transform(p[0], p[1])) for p in ring]
    minx = min(p[0] for p in wring)
    maxx = max(p[0] for p in wring)
    miny = min(p[1] for p in wring)
    maxy = max(p[1] for p in wring)

    # ---- resolution: whole meters keeps the outer footprint integral
    gt = dem.GetGeoTransform()
    native = abs(gt[1])
    if not dem_srs.IsProjected():
        native *= 111320 * math.cos(math.radians(c_lat))  # deg -> m at this latitude
    res = args.resolution
    if res is None:
        res = max(1.0, math.ceil(native))
        while ((maxx - minx) / res) * ((maxy - miny) / res) > MAX_CELLS:
            res += 1.0
    width = max(2, math.ceil((maxx - minx) / res))
    height = max(2, math.ceil((maxy - miny) / res))
    if width * height > 4 * MAX_CELLS:
        raise SystemExit(f"grid would be {width}x{height} cells at {res} m — "
                         "pass a coarser --resolution")
    # center the AOI inside the snapped outer footprint
    pad_x = (width * res - (maxx - minx)) / 2
    pad_y = (height * res - (maxy - miny)) / 2
    outer_abs = (minx - pad_x, miny - pad_y, minx - pad_x + width * res,
                 miny - pad_y + height * res)
    print(f"grid: {width}x{height} cells @ {res:g} m "
          f"(DEM native ~{native:.2f} m)")

    # ---- scene origin: center of the outer footprint, rounded to cm
    ox = round((outer_abs[0] + outer_abs[2]) / 2, 2)
    oy = round((outer_abs[1] + outer_abs[3]) / 2, 2)

    # ---- resample the DEM (row 0 = north, matching the contract)
    warped = gdal.Warp("", dem, format="MEM", dstSRS=wcrs.to_wkt(),
                       outputBounds=outer_abs, xRes=res, yRes=res,
                       resampleAlg="bilinear", dstNodata=NODATA,
                       outputType=gdal.GDT_Float32)
    arr = warped.GetRasterBand(1).ReadAsArray().astype(float)
    arr[arr == NODATA] = np.nan

    if aoi_is_polygon:
        # mask to the AOI ring so the mesh takes the AOI's shape
        drv = ogr.GetDriverByName("Memory")
        vds = drv.CreateDataSource("aoi")
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wcrs.to_wkt())
        layer = vds.CreateLayer("aoi", srs)
        wkt = "POLYGON ((" + ", ".join(f"{p[0]} {p[1]}" for p in wring + [wring[0]]) + "))"
        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        layer.CreateFeature(feat)
        mask_ds = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
        mask_ds.SetGeoTransform(warped.GetGeoTransform())
        mask_ds.SetProjection(wcrs.to_wkt())
        gdal.RasterizeLayer(mask_ds, [1], layer, burn_values=[1])
        arr[mask_ds.ReadAsArray() == 0] = np.nan

    valid = arr[~np.isnan(arr)]
    if valid.size == 0:
        raise SystemExit("no valid DEM cells inside the AOI — wrong bbox/CRS?")
    heights = [None if math.isnan(v) else round(float(v), 2) for v in arr.ravel()]

    outer = [outer_abs[0] - ox, outer_abs[1] - oy, outer_abs[2] - ox, outer_abs[3] - oy]
    grid = {
        "width": width, "height": height,
        "minX": outer[0] + res / 2, "maxX": outer[2] - res / 2,
        "minY": outer[1] + res / 2, "maxY": outer[3] - res / 2,
        "minElevation": float(valid.min()), "maxElevation": float(valid.max()),
        "xStep": res, "yStep": res,
        "heights": heights,
        "outerMinX": outer[0], "outerMaxX": outer[2],
        "outerMinY": outer[1], "outerMaxY": outer[3],
        "source": f"ingest_dem.py: {os.path.basename(args.dem)} -> {working} @ {res:g} m",
    }
    errors = validate_grid(grid)
    if errors:
        raise SystemExit("generated grid violates the contract (bug!): " + "; ".join(errors))

    # ---- georef.json
    geodetic = wcrs.geodetic_crs
    proj4 = wcrs.to_proj4().replace(" +type=crs", "")
    o_lon, o_lat = Transformer.from_crs(working, "EPSG:4326", always_xy=True) \
        .transform(ox, oy)
    georef = {
        "description": f"Georeferencing anchor for {args.name}.",
        "analysis_crs": working,
        "crs_name": wcrs.name,
        "proj4": proj4,
        "geographic_crs": f"EPSG:{geodetic.to_epsg()}" if geodetic.to_epsg() else None,
        "origin_utm": [ox, oy, 0],
        "origin_wgs84": {"lon": o_lon, "lat": o_lat},
        "scene_axes": {
            "x": "+east  (meters) = projected_easting  - origin_easting",
            "z": "-north (meters); projected_northing = origin_northing - world.z",
            "y": "+up (meters) = elevation - grid.minElevation",
        },
        "grid_min_elevation_m": grid["minElevation"],
        "note": "Generated by ingest_dem.py. Origin = center of the outer grid "
                "footprint. The viewer converts coordinates with proj4js from "
                "the proj4 string above; Python uses pyproj on analysis_crs.",
    }

    os.makedirs(terrain_dir, exist_ok=True)
    json.dump(grid, open(grid_path, "w"))
    json.dump(georef, open(georef_path, "w"), indent=2)
    aoi_local = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {"source": "ingest_dem.py"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[round(p[0] - ox, 3), round(p[1] - oy, 3)] for p in wring + [wring[0]]]]},
        }],
    }
    json.dump(aoi_local, open(os.path.join(terrain_dir, "aoi_local.geojson"), "w"))
    print(f"wrote {os.path.relpath(grid_path, PROJECT)}, "
          f"{os.path.relpath(georef_path, PROJECT)}, terrain/aoi_local.geojson")

    scene_path = os.path.join(data_dir, "scene.json")
    if args.force or not os.path.exists(scene_path):
        scene = {
            "name": args.name,
            "analysis_crs": working,
            "origin_utm": [ox, oy, 0],
            "aoi_extent_utm": [outer_abs[0], outer_abs[1], outer_abs[2], outer_abs[3]],
            "terrain": {"grid_url": "/data/terrain/grid.json", "status": "ready"},
            "aoi_boundary": {"geojson_url": "/data/terrain/aoi_local.geojson",
                             "source": "ingest_dem.py"},
            "imagery": {"status": "none"},
            "vegetation": {"status": "none"},
            # empty stubs: scene.js reads parcels.features_url unguarded
            "parcels": {"status": "none"},
            "soils": {"status": "none"},
            "hydrology": {"status": "none"},
            "roads_trails": {"status": "none"},
            "buildings": {"status": "none"},
        }
        json.dump(scene, open(scene_path, "w"), indent=2)
        print(f"wrote {os.path.relpath(scene_path, PROJECT) if scene_path.startswith(PROJECT) else scene_path} (minimal; migrate/export will replace it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
