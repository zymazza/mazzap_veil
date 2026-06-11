#!/usr/bin/env python3
"""Prepare every atlas layer for interactive use in the 3D viewer.

For each layer in data/atlas/atlas-manifest.json this script produces what the
viewer needs to (a) drape the layer onto the terrain surface as colored pixels
and (b) answer click-to-identify queries:

  * vectors  -> data/atlas/local/<name>.geojson in scene-local meters
                (WGS84 -> projected CRS from georef.json -> minus origin),
                with friendly labels
                merged in (legend names, pack-supplied attribute
                enrichment, ...)
  * rasters  -> data/atlas/local/<name>.png (colored RGBA render),
                <name>.grid.json (value grid + local-meter bounds for identify)
  * GAP species -> data/atlas/local/gap_species_grids.json (per-species presence
                grids so a click can list every species with habitat there)

The result is indexed in data/atlas/local/viewer-layers.json which the viewer
reads to build the layer toggles, the draped canvas, and the identify panel.

Run after the active pack's atlas acquisition scripts (or scripts/add_layer.py):
  python3 scripts/build_viewer_layers.py
"""

import glob
import json
import os

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))
ATLAS = os.path.join(DATA_DIR, "atlas")
OUT = os.path.join(ATLAS, "local")

import twin_georef
import twin_pack

ORIGIN = twin_georef.origin()
TO_UTM = twin_georef.from_wgs84_transformer()

# The active regional pack supplies friendly labels, fills/strokes, attribute
# enrichment and named raster renderings; without one, every layer falls
# through to the generic auto-styling below. The engine names no layers.
PACK = twin_pack.load_layers({"data_dir": DATA_DIR})

# Generic label-field detection when no pack supplies its own ordered list.
GENERIC_LABEL_KEYS = ("label", "NAME", "name", "Name", "TYPE", "type", "CLASS",
                      "class", "title", "id")


def to_local(coords):
    if coords and isinstance(coords[0], (int, float)):
        e, n = TO_UTM.transform(coords[0], coords[1])
        return [round(e - ORIGIN[0], 2), round(n - ORIGIN[1], 2)]
    return [to_local(c) for c in coords]


def friendly_label(props, fallback):
    # a pack's label_keys is its complete ordered preference; without a pack,
    # use the generic keys
    keys = tuple(getattr(PACK, "label_keys", None) or GENERIC_LABEL_KEYS) if PACK \
        else GENERIC_LABEL_KEYS
    for k in keys:
        v = props.get(k)
        if v not in (None, "", " "):
            return str(v)
    return fallback


def localize_vector(name, src_path, label_fallback):
    data = json.load(open(src_path))
    feats = data.get("features", [])
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("coordinates") is not None:
            g["coordinates"] = to_local(g["coordinates"])
        p = f.setdefault("properties", {})
        if PACK:
            PACK.enrich(name, p)
        p["__label"] = friendly_label(p, label_fallback)
    out = os.path.join(OUT, name + ".geojson")
    with open(out, "w") as fh:
        json.dump(data, fh)
    return len(feats)


def raster_local_bounds(path):
    """Local-meter bounds of a raster (corners via its own CRS -> UTM - origin)."""
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    srs_wkt = ds.GetProjection()
    from osgeo import osr
    srs = osr.SpatialReference(wkt=srs_wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(twin_georef.epsg_number())
    utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(srs, utm)
    corners = []
    for px, py in ((0, 0), (w, 0), (0, h), (w, h)):
        x = gt[0] + px * gt[1] + py * gt[2]
        y = gt[3] + px * gt[4] + py * gt[5]
        e, n, _ = ct.TransformPoint(x, y)
        corners.append((e - ORIGIN[0], n - ORIGIN[1]))
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def color_ramp(v):  # 0..1 -> viridis-ish RGBA
    stops = [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)]
    x = max(0.0, min(0.999, float(v))) * (len(stops) - 1)
    i = int(x)
    t = x - i
    a, b = stops[i], stops[i + 1]
    return [int(a[c] + (b[c] - a[c]) * t) for c in range(3)] + [200]


def stable_color(code):  # categorical: stable pseudo-random color per code
    rng = np.random.default_rng(int(code) * 2654435761 % (2**32))
    r, g, b = (rng.integers(60, 230) for _ in range(3))
    return [int(r), int(g), int(b), 200]


# shared with the pack's render_raster hook (so packs reuse the engine palettes)
RASTER_HELPERS = {"stable_color": stable_color, "color_ramp": color_ramp}


def write_png(rgba, out_path):
    h, w, _ = rgba.shape
    drv = gdal.GetDriverByName("MEM")
    mem = drv.Create("", w, h, 4, gdal.GDT_Byte)
    for b in range(4):
        mem.GetRasterBand(b + 1).WriteArray(rgba[:, :, b])
    gdal.GetDriverByName("PNG").CreateCopy(out_path, mem)


def generic_render_raster(arr, nodata):
    """Engine default: integer rasters -> stable hash colors per class with a
    'value N' legend; floating rasters -> a viridis ramp over their range."""
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    legend = {}
    if np.issubdtype(arr.dtype, np.floating):
        finite = arr[np.isfinite(arr)]
        lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        span = (hi - lo) or 1.0
        ramp = np.array([color_ramp(x / 99.0) for x in range(100)], dtype=np.uint8)
        idx = np.clip(((np.nan_to_num(arr, nan=lo) - lo) / span * 99).astype(int), 0, 99)
        rgba = ramp[idx]
        rgba[~np.isfinite(arr)] = [0, 0, 0, 0]
        legend = {"min": {"name": "%.3g" % lo, "color": color_ramp(0)[:3]},
                  "max": {"name": "%.3g" % hi, "color": color_ramp(1)[:3]}}
    else:
        for v in np.unique(arr):
            v = int(v)
            if nodata is not None and v == int(nodata):
                continue
            c = stable_color(v)
            rgba[arr == v] = c
            legend[v] = {"name": "value %d" % v, "color": c[:3]}
    return rgba, legend


def build_raster(name):
    tif = os.path.join(ATLAS, name + ".tif")
    if not os.path.exists(tif):
        return None
    ds = gdal.Open(tif)
    arr = ds.GetRasterBand(1).ReadAsArray()
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    bounds = raster_local_bounds(tif)

    rendered = PACK.render_raster(name, arr, nodata, RASTER_HELPERS) if PACK else None
    rgba, legend = rendered if rendered is not None else generic_render_raster(arr, nodata)
    h, w = arr.shape

    if nodata is not None and np.isfinite(nodata):
        rgba[arr == nodata] = [0, 0, 0, 0]
    png = os.path.join(OUT, name + ".png")
    write_png(rgba, png)

    is_float = np.issubdtype(arr.dtype, np.floating)

    def cell(v):
        if not np.isfinite(v):
            return None
        return round(float(v), 3) if is_float else int(v)

    grid = {
        "bounds_local": bounds, "width": w, "height": h,
        "nodata": None if (nodata is None or not np.isfinite(nodata)) else cell(nodata),
        "values": [[cell(v) for v in row] for row in arr.tolist()],
        "legend": legend,
    }
    with open(os.path.join(OUT, name + ".grid.json"), "w") as fh:
        json.dump(grid, fh)
    return {"image": "atlas/local/%s.png" % name,
            "grid": "atlas/local/%s.grid.json" % name, "bounds_local": bounds}


def build_gap_species_grids():
    """Per-species presence grids so a click can list species with habitat there."""
    summary_path = os.path.join(ATLAS, "gap_species_habitat.json")
    if not os.path.exists(summary_path):
        return None
    summary = json.load(open(summary_path))
    species = [s for s in summary.get("species", []) if s.get("present")]
    grids = {}
    bounds = None
    shape = None
    for s in species:
        tif = os.path.join(ATLAS, "gap_species", s["code"] + ".tif")
        if not os.path.exists(tif):
            continue
        ds = gdal.Open(tif)
        arr = ds.GetRasterBand(1).ReadAsArray()
        nd = ds.GetRasterBand(1).GetNoDataValue()
        mask = (arr > 0) if nd is None else ((arr != nd) & (arr > 0))
        if bounds is None:
            bounds = raster_local_bounds(tif)
            shape = arr.shape
        # pack row-major bitmask as hex per row for compactness
        grids[s["code"]] = {
            "common_name": s["common_name"], "scientific_name": s["scientific_name"],
            "rows": ["".join("1" if x else "0" for x in row) for row in mask.tolist()],
        }
    if shape is None:  # habitat listed species but no per-species rasters present
        return None
    out = {"bounds_local": bounds, "height": shape[0], "width": shape[1], "species": grids}
    with open(os.path.join(OUT, "gap_species_grids.json"), "w") as fh:
        json.dump(out, fh)
    return len(grids)


def auto_style(name, geom_hint="polygon"):
    """Generic vector presentation when the pack names no style: a title-cased
    label, a stable hash fill/stroke, the detected/declared geometry kind."""
    label = name.replace("_", " ").title()
    c = stable_color(sum(bytes(name, "utf8")))
    stroke = "#%02x%02x%02x" % tuple(c[:3])
    fill = "rgba(%d,%d,%d,0.35)" % tuple(c[:3]) if geom_hint != "line" else "rgba(0,0,0,0)"
    return label, fill, stroke, geom_hint


def detect_geometry(src_path):
    try:
        for f in json.load(open(src_path)).get("features", []):
            t = (f.get("geometry") or {}).get("type", "")
            if "Line" in t:
                return "line"
            if "Polygon" in t:
                return "polygon"
    except Exception:  # noqa: BLE001
        pass
    return "polygon"


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = json.load(open(os.path.join(ATLAS, "atlas-manifest.json")))
    layers = []

    for entry in manifest.get("layers", []):
        name = entry["name"]
        kind = entry.get("kind", "vector")
        if kind == "vector":
            src = os.path.join(DATA_DIR, entry["file"])
            style = PACK.vector_style(name) if PACK else None
            # A pack's vector_style may append a fifth element: categorical
            # (each feature gets its own stable color in the viewer drape).
            categorical = False
            if style and len(style) == 5:
                label, fill, stroke, geom, categorical = style
            else:
                label, fill, stroke, geom = style or auto_style(
                    name, detect_geometry(src))
            n = localize_vector(name, src, label)
            layers.append({"id": name, "label": label, "type": geom,
                           "file": "atlas/local/%s.geojson" % name,
                           "fill": fill, "stroke": stroke, "feature_count": n,
                           "categorical": bool(categorical),
                           "acquisition": entry.get("acquisition")})
            print("[vector] %-44s %d feats localized" % (name, n))
        else:
            r = build_raster(name)
            if r:
                label = (PACK.raster_label(name) if PACK else None) or name.replace("_", " ").title()
                layers.append({"id": name, "label": label,
                               "type": "raster", **r,
                               "acquisition": entry.get("acquisition")})
                print("[raster] %-44s png+grid (%s)" % (name, r["bounds_local"]))

    # species-richness composite lives outside the manifest's layer list
    r = build_raster("gap_species_richness")
    if r:
        label = (PACK.raster_label("gap_species_richness") if PACK else None) \
            or "Species Richness"
        layers.append({"id": "gap_species_richness", "label": label,
                       "type": "raster", **r, "acquisition": "local_source_clip"})
        print("[raster] %-44s png+grid" % "gap_species_richness")

    n_species = build_gap_species_grids()
    out = {
        "origin_utm": list(ORIGIN),
        "layers": layers,
        "gap_species_grids": "atlas/local/gap_species_grids.json" if n_species else None,
        "gap_species_count": n_species or 0,
    }
    with open(os.path.join(OUT, "viewer-layers.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    print("\n%d layers prepared; %s species grids -> data/atlas/local/viewer-layers.json"
          % (len(layers), n_species))


if __name__ == "__main__":
    main()
