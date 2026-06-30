#!/usr/bin/env python3
"""Fetch USGS 3DEP lidar point-cloud tiles for a twin and derive DSM/DTM rasters.

This is a best-effort enhancement for the AOI builder. The base terrain already
comes from the 3DEP elevation ImageServer; this script pulls the raw 3DEP Lidar
Point Cloud (LPC) LAZ tiles where USGS has coverage, clips them to the twin
footprint, and writes:

  <data>/terrain/dsm.tif    highest return surface, for canopy/building tops
  <data>/terrain/dtm.tif    ground-classified surface, for CHM stem detection
  <data>/terrain/lidar_fetch.json

`scripts/analyze_vegetation.py` already consumes dsm.tif + dtm.tif as its
second-best vegetation rung, ahead of the NDVI fallback. This script intentionally
does not fail the build when lidar is unavailable: 3DEP coverage is broad but not
identical to "every AOI", and point-cloud downloads can be large.

Needs the PDAL command-line tool with readers.las, filters.reprojection,
filters.crop, filters.range, and writers.gdal.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

from osgeo import gdal
from pyproj import Transformer
gdal.UseExceptions()

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ingest_dem  # noqa: E402
import twin_georef  # noqa: E402

TNM_PRODUCTS = "https://tnmaccess.nationalmap.gov/api/v1/products"
DATASET = "Lidar Point Cloud (LPC)"
UA = {"User-Agent": "veil/1.0"}
NODATA = -9999.0


def _json_url(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _aoi_bbox_wgs(aoi):
    ring, aoi_crs = ingest_dem.ring_from_aoi(aoi, "EPSG:4326")
    to_wgs = Transformer.from_crs(aoi_crs, "EPSG:4326", always_xy=True)
    pts = [to_wgs.transform(p[0], p[1]) for p in ring]
    lons, lats = zip(*pts)
    return (min(lons), min(lats), max(lons), max(lats))


def _footprint_abs(data_dir):
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef_path)
    epsg = twin_georef.epsg_number(georef_path)
    return epsg, (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    )


def _query_lpc(bbox_wgs, max_items):
    params = {
        "datasets": DATASET,
        "bbox": "%f,%f,%f,%f" % bbox_wgs,
        "max": str(max_items),
        "outputFormat": "JSON",
    }
    url = TNM_PRODUCTS + "?" + urllib.parse.urlencode(params)
    data = _json_url(url)
    return data.get("items", [])


def _safe_name(item, index):
    title = item.get("title") or f"tile-{index:04d}"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in title)
    return safe[:180] + ".laz"


def _select_items(items, max_tiles, max_bytes):
    selected = []
    total = 0
    for item in sorted(items, key=lambda it: it.get("bestFitIndex", 999999)):
        url = item.get("downloadLazURL") or item.get("downloadURL")
        size = int(item.get("sizeInBytes") or 0)
        if not url or not url.lower().endswith(".laz"):
            continue
        if len(selected) >= max_tiles:
            break
        if max_bytes and size and total + size > max_bytes and selected:
            break
        selected.append(item)
        total += size
    return selected, total


def _pdal_summary(path):
    proc = subprocess.run(
        ["pdal", "info", "--summary", path],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(proc.stdout)


def _summary_bounds(summary):
    bounds = summary.get("summary", {}).get("bounds", {})
    if "minx" in bounds and "maxx" in bounds:
        return (
            float(bounds["minx"]),
            float(bounds["miny"]),
            float(bounds["maxx"]),
            float(bounds["maxy"]),
        )
    return None


def _summary_srs(summary):
    srs = summary.get("summary", {}).get("srs", {})
    if not isinstance(srs, dict):
        return None
    for key in ("compoundwkt", "wkt", "proj4"):
        value = srs.get(key)
        if value:
            return value
    auth = srs.get("authority")
    code = srs.get("horizontal")
    if auth and code:
        return f"{auth}:{code}"
    return None


def _bounds_overlap(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _infer_in_srs(laz_paths, target_epsg, target_bounds):
    inferred = set()
    for path in laz_paths:
        summary = _pdal_summary(path)
        srs = _summary_srs(summary)
        if srs:
            continue
        b = _summary_bounds(summary)
        if b and -180 <= b[0] <= 180 and -180 <= b[2] <= 180 \
                and -90 <= b[1] <= 90 and -90 <= b[3] <= 90:
            inferred.add("EPSG:4326")
        elif b and _bounds_overlap(b, target_bounds):
            inferred.add(f"EPSG:{target_epsg}")
        else:
            detail = f" bounds={tuple(round(v, 3) for v in b)}" if b else ""
            raise RuntimeError(
                "lidar LAZ has no embedded spatial reference and VEIL could "
                f"not infer one for {os.path.basename(path)}.{detail}"
            )
    return inferred.pop() if len(inferred) == 1 else None


def _run_pdal(laz_paths, out_path, epsg, bounds, resolution, ground_only=False, in_srs=None):
    stages = [{"type": "readers.las", "filename": p} for p in laz_paths]
    if len(stages) > 1:
        stages.append({"type": "filters.merge"})
    reprojection = {"type": "filters.reprojection", "out_srs": f"EPSG:{epsg}"}
    if in_srs:
        reprojection["in_srs"] = in_srs
    stages.extend([
        reprojection,
        {"type": "filters.crop",
         "bounds": "([%.3f,%.3f],[%.3f,%.3f])" % (bounds[0], bounds[2], bounds[1], bounds[3])},
    ])
    if ground_only:
        stages.append({"type": "filters.range", "limits": "Classification[2:2]"})
    stages.append({
        "type": "writers.gdal",
        "filename": out_path,
        "resolution": resolution,
        "output_type": "mean" if ground_only else "max",
        "data_type": "float32",
        "nodata": NODATA,
        "bounds": "([%.3f,%.3f],[%.3f,%.3f])" % (bounds[0], bounds[2], bounds[1], bounds[3]),
        "window_size": 3,
    })
    pipeline = {"pipeline": stages}
    tmp = out_path + ".pdal.json"
    json.dump(pipeline, open(tmp, "w"), indent=2)
    try:
        proc = subprocess.run(
            ["pdal", "pipeline", tmp],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.stdout:
            print(proc.stdout.rstrip())
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr)
    except subprocess.CalledProcessError as e:
        details = "\n".join(s for s in (e.stdout, e.stderr) if s)
        raise RuntimeError(
            f"PDAL failed while writing {os.path.basename(out_path)}"
            + (f":\n{details.rstrip()}" if details else "")
        ) from e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _nanify(path):
    ds = gdal.Open(path, gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"PDAL did not write {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype("float32")
    arr[arr <= NODATA + 1] = float("nan")
    band.WriteArray(arr)
    band.SetNoDataValue(float("nan"))
    ds.FlushCache()
    ds = None


def _write_status(data_dir, status):
    path = os.path.join(data_dir, "terrain", "lidar_fetch.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(status, open(path, "w"), indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aoi", required=True, help="AOI polygon used to query TNM")
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--resolution", type=float, default=1.0,
                    help="DSM/DTM raster cell size in meters")
    ap.add_argument("--max-tiles", type=int, default=24,
                    help="maximum LAZ tiles to download")
    ap.add_argument("--max-gb", type=float, default=3.0,
                    help="soft cap for LAZ downloads; already-selected first tile is kept")
    ap.add_argument("--keep-laz", action="store_true",
                    help="keep downloaded LAZ files under terrain/lidar/laz")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    if shutil.which("pdal") is None:
        status = {"status": "skipped", "reason": "PDAL is not installed"}
        _write_status(data_dir, status)
        print("lidar skipped — PDAL is not installed")
        return 0

    bbox_wgs = _aoi_bbox_wgs(args.aoi)
    epsg, bounds = _footprint_abs(data_dir)
    items = _query_lpc(bbox_wgs, max(args.max_tiles * 3, 50))
    selected, total_bytes = _select_items(
        items, args.max_tiles, int(args.max_gb * 1024 * 1024 * 1024))
    if not selected:
        status = {"status": "unavailable", "reason": "no TNM LPC LAZ tiles matched AOI",
                  "bbox_wgs84": bbox_wgs, "items_seen": len(items)}
        _write_status(data_dir, status)
        print("lidar unavailable — no TNM LPC LAZ tiles matched AOI")
        return 0

    laz_dir = os.path.join(data_dir, "terrain", "lidar", "laz")
    os.makedirs(laz_dir, exist_ok=True)
    laz_paths = []
    print("fetching %d lidar LAZ tile(s), %.2f GB advertised…" %
          (len(selected), total_bytes / (1024 ** 3)))
    for i, item in enumerate(selected, 1):
        url = item.get("downloadLazURL") or item.get("downloadURL")
        path = os.path.join(laz_dir, _safe_name(item, i))
        if not os.path.exists(path):
            print("  [%d/%d] %s" % (i, len(selected), item.get("title", "LAZ tile")))
            _download(url, path)
        laz_paths.append(path)

    terrain_dir = os.path.join(data_dir, "terrain")
    dsm = os.path.join(terrain_dir, "dsm.tif")
    dtm = os.path.join(terrain_dir, "dtm.tif")
    print("deriving DSM/DTM with PDAL @ %.2f m…" % args.resolution)
    try:
        in_srs = _infer_in_srs(laz_paths, epsg, bounds)
        if in_srs:
            print(f"lidar LAZ lacks embedded CRS; using inferred {in_srs}")
        _run_pdal(laz_paths, dsm, epsg, bounds, args.resolution,
                  ground_only=False, in_srs=in_srs)
        _run_pdal(laz_paths, dtm, epsg, bounds, args.resolution,
                  ground_only=True, in_srs=in_srs)
    except Exception as e:  # noqa: BLE001
        status = {
            "status": "failed",
            "reason": str(e),
            "dataset": DATASET,
            "source": "USGS TNMAccess",
            "tile_count": len(selected),
            "advertised_bytes": total_bytes,
            "bbox_wgs84": bbox_wgs,
            "epsg": epsg,
            "bounds": bounds,
            "resolution_m": args.resolution,
            "tiles": [{
                "title": item.get("title"),
                "publicationDate": item.get("publicationDate"),
                "sizeInBytes": item.get("sizeInBytes"),
                "downloadURL": item.get("downloadLazURL") or item.get("downloadURL"),
            } for item in selected],
        }
        _write_status(data_dir, status)
        print(f"lidar skipped — {e}")
        if not args.keep_laz:
            shutil.rmtree(laz_dir, ignore_errors=True)
        return 0
    _nanify(dsm)
    _nanify(dtm)

    if not args.keep_laz:
        shutil.rmtree(laz_dir, ignore_errors=True)

    status = {
        "status": "ok",
        "dataset": DATASET,
        "source": "USGS TNMAccess",
        "tile_count": len(selected),
        "advertised_bytes": total_bytes,
        "bbox_wgs84": bbox_wgs,
        "epsg": epsg,
        "bounds": bounds,
        "resolution_m": args.resolution,
        "dsm": "terrain/dsm.tif",
        "dtm": "terrain/dtm.tif",
        "tiles": [{
            "title": item.get("title"),
            "publicationDate": item.get("publicationDate"),
            "sizeInBytes": item.get("sizeInBytes"),
            "downloadURL": item.get("downloadLazURL") or item.get("downloadURL"),
        } for item in selected],
    }
    _write_status(data_dir, status)
    print("wrote terrain/dsm.tif + terrain/dtm.tif from lidar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
