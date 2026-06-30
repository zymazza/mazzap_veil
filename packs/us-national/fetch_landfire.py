#!/usr/bin/env python3
"""Fetch LANDFIRE 2024 EVT for any CONUS twin's footprint.

LANDFIRE Existing Vegetation Type is a national (continental US) 30 m product,
so this works for any twin in the lower 48 — no regional data needed. It pulls
the EVT raster for the twin's terrain footprint from the USGS LANDFIRE
ImageServer, names each code via the bundled national VAT
(landfire_evt_vat.json), and writes the scene-local grid the viewer + the
us-national vegetation hook consume:

  <data>/atlas/local/landfire_evt_2024.grid.json   value grid + bounds + legend

After this, vegetation typing works:
  TWIN_PACK=us-national TWIN_DATA_DIR=<data> npm run build-vegetation

Usage:
  python3 packs/us-national/fetch_landfire.py --data-dir ./twins/mine/data

Needs internet (one-time snapshot). Outside CONUS the service returns nodata.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))  # packs/<name>/ -> repo root
sys.path.insert(0, os.path.join(PROJECT, "scripts"))
sys.path.insert(0, HERE)
from landfire_vat import load_vat  # noqa: E402

SERVICE = ("https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2024/"
           "LF2024_EVT_CONUS/ImageServer/exportImage")
NATIVE_M = 30.0  # LANDFIRE resolution
LAYER_ID = "landfire_evt_2024"
LAYER_LABEL = "LANDFIRE Vegetation (EVT)"


def _stable_color(code):
    import numpy as np
    rng = np.random.default_rng(int(code) * 2654435761 % (2 ** 32))
    return [int(rng.integers(60, 230)) for _ in range(3)]


def _write_png(rgba, out_path):
    from osgeo import gdal
    h, w, _ = rgba.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    for b in range(4):
        mem.GetRasterBand(b + 1).WriteArray(rgba[:, :, b])
    gdal.GetDriverByName("PNG").CreateCopy(out_path, mem)
    aux = out_path + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)


def _register_layer(data_dir, entry):
    """Add/replace the layer in the twin's viewer-layers.json (the viewer reads
    this to build toggles, the drape, and identify)."""
    path = os.path.join(data_dir, "atlas", "local", "viewer-layers.json")
    if os.path.exists(path):
        catalog = json.load(open(path))
    else:
        import twin_georef
        catalog = {"origin_utm": list(twin_georef.origin(
            os.path.join(data_dir, "georef.json"))), "layers": []}
    catalog["layers"] = [l for l in catalog.get("layers", []) if l["id"] != entry["id"]]
    catalog["layers"].append(entry)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(catalog, open(path, "w"), indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--resolution", type=float, default=NATIVE_M,
                    help="sample spacing in meters (default 30 = LANDFIRE native)")
    args = ap.parse_args()

    import numpy as np
    from osgeo import gdal
    import twin_georef
    gdal.UseExceptions()

    data_dir = os.path.abspath(args.data_dir)
    georef_path = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    epsg = twin_georef.epsg_number(georef_path)
    ox, oy = twin_georef.origin(georef_path)

    # the terrain outer footprint, in the twin's projected CRS
    x0, y0 = grid["outerMinX"] + ox, grid["outerMinY"] + oy
    x1, y1 = grid["outerMaxX"] + ox, grid["outerMaxY"] + oy
    w = max(2, round((x1 - x0) / args.resolution))
    h = max(2, round((y1 - y0) / args.resolution))

    params = {
        "bbox": "%f,%f,%f,%f" % (x0, y0, x1, y1),
        "bboxSR": str(epsg), "imageSR": str(epsg), "size": "%d,%d" % (w, h),
        "format": "tiff", "pixelType": "U16",
        "interpolation": "RSP_NearestNeighbor", "f": "image",
    }
    url = SERVICE + "?" + urllib.parse.urlencode(params)
    print(f"fetching LANDFIRE EVT for the twin footprint ({w}x{h} @ "
          f"{args.resolution:g} m, EPSG:{epsg})…")
    req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0"})
    tif = os.path.join(data_dir, "atlas", "landfire_evt_2024.tif")
    os.makedirs(os.path.dirname(tif), exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp:
        open(tif, "wb").write(resp.read())

    arr = gdal.Open(tif).ReadAsArray()
    vat = load_vat()  # {code: (name, phys)}

    # scene-local bounds (the grid the viewer + vegetation hook expect)
    bounds_local = [round(x0 - ox, 2), round(y0 - oy, 2),
                    round(x1 - ox, 2), round(y1 - oy, 2)]
    legend = {}
    colors = {}
    present = np.unique(arr)
    for v in present.tolist():
        v = int(v)
        if v <= 0:
            continue
        name = vat.get(v, (None, None))[0] or "LANDFIRE EVT %d" % v
        c = _stable_color(v)
        legend[v] = {"name": name, "color": c}
        colors[v] = c

    out_dir = os.path.join(data_dir, "atlas", "local")
    os.makedirs(out_dir, exist_ok=True)
    grid_out = {
        "bounds_local": bounds_local, "width": int(arr.shape[1]), "height": int(arr.shape[0]),
        "nodata": 0,
        "values": [[int(v) if int(v) > 0 else None for v in row] for row in arr.tolist()],
        "legend": legend,
    }
    json.dump(grid_out, open(os.path.join(out_dir, LAYER_ID + ".grid.json"), "w"))

    # colored PNG drape + register as a displayed, clickable atlas layer
    h, w_ = arr.shape
    rgba = np.zeros((h, w_, 4), dtype=np.uint8)
    for v, c in colors.items():
        rgba[arr == v] = c + [200]
    _write_png(rgba, os.path.join(out_dir, LAYER_ID + ".png"))
    _register_layer(data_dir, {
        "id": LAYER_ID, "label": LAYER_LABEL, "type": "raster",
        "image": "atlas/local/%s.png" % LAYER_ID,
        "grid": "atlas/local/%s.grid.json" % LAYER_ID,
        "bounds_local": bounds_local, "acquisition": "api_snapshot",
        "service": "USGS LANDFIRE LF2024 EVT",
    })

    # forest vs non-forest summary so the run is legible
    forest = {"Conifer", "Hardwood", "Conifer-Hardwood", "Riparian"}
    by_phys = {}
    vals, counts = np.unique(arr, return_counts=True)
    for v, c in zip(vals.tolist(), counts.tolist()):
        phys = vat.get(int(v), (None, "?"))[1] or "?"
        by_phys[phys] = by_phys.get(phys, 0) + int(c)
    total = int(counts.sum()) or 1
    top = sorted(by_phys.items(), key=lambda kv: -kv[1])[:6]
    print("wrote atlas/local/landfire_evt_2024.grid.json "
          f"({len(legend)} EVT types present)")
    print("physiognomy: " + ", ".join("%s %d%%" % (p, round(100 * c / total))
                                       for p, c in top))
    forest_pct = round(100 * sum(c for p, c in by_phys.items() if p in forest) / total)
    print(f"forest/woodland cover: {forest_pct}% "
          "(trees will be planted there; the rest stays open)")
    # clean GDAL sidecar
    aux = tif + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
