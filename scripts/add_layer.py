#!/usr/bin/env python3
"""Add one atlas layer from an arbitrary GeoTIFF or GeoJSON — the generic,
region-agnostic path (no pack, no manifest required).

Reprojects the input from its own CRS to scene-local meters (the twin's
projected CRS from data/georef.json, minus the origin), clips to the terrain
grid's outer footprint, auto-styles it (categorical -> stable hash colors,
continuous rasters -> a viridis ramp; vectors get a stable fill/stroke),
auto-detects a label field, appends it to data/atlas/local/viewer-layers.json
(the viewer reads this directly), and registers it in the twin store's layers
table with provenance — the same machinery a pack's atlas build uses.

Usage:
  python3 scripts/add_layer.py LAYER.geojson  --id my_wetlands --label "Wetlands"
  python3 scripts/add_layer.py LAYER.tif      --id soils [--label "Soils"]
                                              [--src-crs EPSG:4326] [--label-field NAME]

With a regional pack active (TWIN_PACK / data/pack.txt) the pack's named
styles still apply; anything the pack doesn't name uses the generic styling.
"""

import argparse
import json
import os
import sys

import numpy as np
from osgeo import gdal, ogr, osr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_viewer_layers as bvl
import twin_georef
import twin_store

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()
PROJECT = bvl.PROJECT

# Resolved from --data-dir in main(); default to the repo's own twin. A twin's
# CRS/origin/grid all come from this dir, so add_layer works on any twin
# (e.g. a scratch one under ./twins/) without touching the default ./data.
DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))
GEOREF_PATH = os.path.join(DATA_DIR, "georef.json")
OUT = os.path.join(DATA_DIR, "atlas", "local")
VIEWER_LAYERS = os.path.join(OUT, "viewer-layers.json")
STORE_PATH = os.path.join(DATA_DIR, "twin.gpkg")


def _set_data_dir(data_dir):
    global DATA_DIR, GEOREF_PATH, OUT, VIEWER_LAYERS, STORE_PATH
    DATA_DIR = os.path.abspath(data_dir)
    GEOREF_PATH = os.path.join(DATA_DIR, "georef.json")
    OUT = os.path.join(DATA_DIR, "atlas", "local")
    VIEWER_LAYERS = os.path.join(OUT, "viewer-layers.json")
    STORE_PATH = os.path.join(DATA_DIR, "twin.gpkg")


def grid_outer_bounds_abs():
    """The terrain grid's outer footprint in absolute projected coords."""
    grid = json.load(open(os.path.join(DATA_DIR, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(GEOREF_PATH)
    return (grid["outerMinX"] + ox, grid["outerMinY"] + oy,
            grid["outerMaxX"] + ox, grid["outerMaxY"] + oy)


def localize_vector_any_crs(src_path, src_crs, name, label, layer_name=None):
    """Reproject ANY OGR-readable vector source (GeoJSON, Shapefile, GeoPackage,
    KML/KMZ, GPX, CSV, File Geodatabase, …) to scene-local meters, clip to the
    grid footprint by bbox, enrich/label, write data/atlas/local/<name>.geojson
    so the viewer (which only knows GeoJSON) can render it.

    The source CRS comes from the dataset's own spatial reference; --src-crs is
    a fallback for formats that carry none (e.g. CSV)."""
    ds = ogr.Open(src_path)
    if ds is None:
        raise SystemExit(f"OGR could not open {src_path} as a vector source")
    if layer_name:
        layer = ds.GetLayerByName(layer_name)
        if layer is None:
            names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
            raise SystemExit(f"layer {layer_name!r} not found; layers: {names}")
    elif ds.GetLayerCount() > 1:
        names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
        raise SystemExit(f"{src_path} has {len(names)} layers; pass --layer NAME "
                         f"(one of: {names})")
    else:
        layer = ds.GetLayer(0)

    working = twin_georef.crs(GEOREF_PATH)
    ox, oy = twin_georef.origin(GEOREF_PATH)
    src_ref = layer.GetSpatialRef()
    if src_ref is None:
        src_ref = _srs(src_crs or "EPSG:4326")
    else:
        src_ref = src_ref.Clone()
        src_ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src_ref, _srs(working))
    bx0, by0, bx1, by1 = (v - o for v, o in
                          zip(grid_outer_bounds_abs(), (ox, oy, ox, oy)))

    def shift(c):  # working-CRS coords -> scene-local meters
        if c and isinstance(c[0], (int, float)):
            return [round(c[0] - ox, 2), round(c[1] - oy, 2)]
        return [shift(x) for x in c]

    fields = [layer.GetLayerDefn().GetFieldDefn(i).GetName()
              for i in range(layer.GetLayerDefn().GetFieldCount())]
    kept = []
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom = geom.Clone()
        if geom.Transform(ct) != 0:
            continue
        gj = json.loads(geom.ExportToJson())
        if gj.get("coordinates") is None:
            continue
        gj["coordinates"] = shift(gj["coordinates"])
        xs, ys = _coords_extent(gj["coordinates"])
        if not xs or (xs[1] < bx0 or xs[0] > bx1 or ys[1] < by0 or ys[0] > by1):
            continue
        props = {f: feat.GetField(f) for f in fields}
        if bvl.PACK:
            bvl.PACK.enrich(name, props)
        props["__label"] = bvl.friendly_label(props, label)
        kept.append({"type": "Feature", "properties": props, "geometry": gj})

    out = {"type": "FeatureCollection", "features": kept}
    os.makedirs(OUT, exist_ok=True)
    json.dump(out, open(os.path.join(OUT, name + ".geojson"), "w"))
    geom_kind = "line"
    for f in kept:
        t = f["geometry"]["type"]
        if "Polygon" in t:
            geom_kind = "polygon"
            break
        if "Point" in t:
            geom_kind = "point"
    return len(kept), geom_kind


def _coords_extent(coords, xs=None, ys=None):
    xs = xs if xs is not None else [float("inf"), float("-inf")]
    ys = ys if ys is not None else [float("inf"), float("-inf")]
    if coords and isinstance(coords[0], (int, float)):
        xs[0], xs[1] = min(xs[0], coords[0]), max(xs[1], coords[0])
        ys[0], ys[1] = min(ys[0], coords[1]), max(ys[1], coords[1])
    else:
        for c in coords:
            _coords_extent(c, xs, ys)
    return (xs if xs[0] != float("inf") else None,
            ys if ys[0] != float("inf") else None)


def _srs(crs):
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _is_vector(src_path):
    """Probe the source with GDAL/OGR: a raster band -> raster, else vector.
    (GeoTIFF and friends open as raster; .shp/.gpkg/.kml/.gpx/.csv as vector.)"""
    def _try(flag):
        try:
            return gdal.OpenEx(src_path, flag)
        except RuntimeError:
            return None
    raster = _try(gdal.OF_RASTER)
    if raster is not None and raster.RasterCount > 0:
        return False
    vector = _try(gdal.OF_VECTOR)
    if vector is not None and vector.GetLayerCount() > 0:
        return True
    raise SystemExit(f"GDAL/OGR could not read {src_path} as raster or vector")


def localize_raster_any_crs(src_path, name):
    """Warp a raster to the working CRS, clip to the grid footprint, render
    via the generic (or pack) styling, write png + grid.json. Returns the
    viewer entry fields."""
    working = twin_georef.crs(GEOREF_PATH)
    bounds = grid_outer_bounds_abs()
    warped = gdal.Warp("", src_path, format="MEM", dstSRS=_srs(working).ExportToWkt(),
                       outputBounds=bounds, resampleAlg="near")
    band = warped.GetRasterBand(1)
    arr = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    ox, oy = twin_georef.origin(GEOREF_PATH)
    blocal = [round(bounds[0] - ox, 2), round(bounds[1] - oy, 2),
              round(bounds[2] - ox, 2), round(bounds[3] - oy, 2)]

    metadata = bvl.load_raster_metadata(name, os.path.join(DATA_DIR, "atlas"))
    rendered = bvl.PACK.render_raster(name, arr, nodata, bvl.RASTER_HELPERS) if bvl.PACK else None
    rgba, legend = rendered if rendered is not None else bvl.generic_render_raster(
        arr, nodata, bvl.load_vat(name, os.path.join(DATA_DIR, "atlas")), metadata)
    if nodata is not None and np.isfinite(nodata):
        rgba[arr == nodata] = [0, 0, 0, 0]
    os.makedirs(OUT, exist_ok=True)
    bvl.write_png(rgba, os.path.join(OUT, name + ".png"))
    is_float = np.issubdtype(arr.dtype, np.floating)

    def cell(v):
        if not np.isfinite(v):
            return None
        return round(float(v), 3) if is_float else int(v)

    grid = {"bounds_local": blocal, "width": arr.shape[1], "height": arr.shape[0],
            "nodata": None if (nodata is None or not np.isfinite(nodata)) else cell(nodata),
            "values": [[cell(v) for v in row] for row in arr.tolist()], "legend": legend}
    for key in ("description", "uses", "value_kind", "value_unit", "value_classification"):
        if metadata.get(key) not in (None, ""):
            grid[key] = metadata[key]
    json.dump(grid, open(os.path.join(OUT, name + ".grid.json"), "w"))
    return {"image": "atlas/local/%s.png" % name,
            "grid": "atlas/local/%s.grid.json" % name, "bounds_local": blocal}


def upsert_viewer_layer(entry):
    catalog = (json.load(open(VIEWER_LAYERS)) if os.path.exists(VIEWER_LAYERS)
               else {"origin_utm": list(twin_georef.origin(GEOREF_PATH)), "layers": []})
    catalog["layers"] = [l for l in catalog["layers"] if l["id"] != entry["id"]]
    catalog["layers"].append(entry)
    json.dump(catalog, open(VIEWER_LAYERS, "w"), indent=2)
    return len(catalog["layers"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("source", help="any GDAL/OGR geospatial file (GeoJSON, "
                    "Shapefile, GeoPackage, KML/KMZ, GPX, CSV, FileGDB, GeoTIFF, …)")
    ap.add_argument("--id", required=True, help="layer id (slug, unique in the atlas)")
    ap.add_argument("--label", help="display label (default: title-cased id)")
    ap.add_argument("--src-crs", help="fallback source CRS when the file carries "
                    "none (e.g. a CSV); otherwise the file's own CRS is used")
    ap.add_argument("--layer", help="layer name for multi-layer sources (.gpkg/.gdb)")
    ap.add_argument("--label-field", help="property to use as the feature label")
    ap.add_argument("--description", help="natural-language layer description")
    ap.add_argument("--uses", help="natural-language summary of useful analysis/workflows")
    ap.add_argument("--value-kind", help="raster value meaning, e.g. class, percent, year")
    ap.add_argument("--value-unit", help="raster value unit, e.g. percent, m, cm, year")
    ap.add_argument("--value-classification", choices=("categorical", "continuous"),
                    help="whether raster cell values are named classes or measurements")
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"),
                    help="the twin's data dir (default: ./data or $TWIN_DATA_DIR) — "
                    "set this to add a layer to a scratch/alternate twin")
    args = ap.parse_args()

    if args.data_dir:
        _set_data_dir(args.data_dir)
    name = args.id
    label = args.label or name.replace("_", " ").title()
    is_vector = _is_vector(args.source)

    if args.label_field:  # honor an explicit label field ahead of detection
        bvl.GENERIC_LABEL_KEYS = (args.label_field,) + bvl.GENERIC_LABEL_KEYS

    sidecar = {k: v for k, v in {
        "description": args.description,
        "uses": args.uses,
        "value_kind": args.value_kind,
        "value_unit": args.value_unit,
        "value_classification": args.value_classification,
    }.items() if v not in (None, "")}
    if sidecar:
        meta_dir = os.path.join(DATA_DIR, "atlas", "metadata")
        os.makedirs(meta_dir, exist_ok=True)
        meta_path = os.path.join(meta_dir, name + ".json")
        existing = {}
        if os.path.exists(meta_path):
            try:
                existing = json.load(open(meta_path))
            except Exception:
                existing = {}
        json.dump({**existing, **sidecar}, open(meta_path, "w"), indent=2)

    if is_vector:
        n, geom = localize_vector_any_crs(args.source, args.src_crs, name, label,
                                          layer_name=args.layer)
        style = bvl.PACK.vector_style(name) if bvl.PACK else None
        _lbl, fill, stroke, geom = style or bvl.auto_style(name, geom)
        entry = {"id": name, "label": label, "type": geom,
                 "file": "atlas/local/%s.geojson" % name,
                 "fill": fill, "stroke": stroke, "feature_count": n,
                 "acquisition": "add_layer"}
        print("[vector] %s: %d features within the grid footprint" % (name, n))
    else:
        r = localize_raster_any_crs(args.source, name)
        entry = {"id": name, "label": label, "type": "raster", **r,
                 "acquisition": "add_layer"}
        print("[raster] %s: %s" % (name, r["bounds_local"]))
    for key in ("description", "uses", "value_kind", "value_unit", "value_classification"):
        if sidecar.get(key) not in (None, ""):
            entry[key] = sidecar[key]

    total = upsert_viewer_layer(entry)

    # register in the store's layers table (provenance), reusing migrate's path.
    # Skipped gracefully when the twin has no store yet (a bare terrain twin).
    try:
        if not os.path.exists(STORE_PATH):
            raise FileNotFoundError("no twin store yet (run migrate to create one)")
        store = twin_store.Store(STORE_PATH)
        content_sha1 = twin_store.sha1_file(args.source) if os.path.isfile(args.source) else None
        store.upsert_layer(name, label=label, kind=entry["type"],
                           acquisition="add_layer", source_path=os.path.abspath(args.source),
                           fetched_at=twin_store.utcnow(),
                           feature_count=entry.get("feature_count"),
                           status="ok",
                           content_sha1=content_sha1)
        store.close()
        registered = "registered in twin store"
    except Exception as e:  # noqa: BLE001
        registered = f"(store registration skipped: {e})"

    print("added '%s' -> %d layers in viewer-layers.json; %s" % (name, total, registered))


if __name__ == "__main__":
    main()
