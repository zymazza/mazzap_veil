#!/usr/bin/env python3
"""Align aerial imagery to the twin's terrain grid.

Companion to ingest_dem.py (separate script on purpose: imagery is optional
and often arrives later). Reprojects/resamples an aerial image to **exactly**
the grid's outer cell-edge footprint (outerMinX..outerMaxX / outerMinY..
outerMaxY) at an integer pixels-per-meter — the alignment
analyze_vegetation.py depends on (see docs/grid-contract.md).

Writes:
  data/imagery/naip_rgb.png     RGB drape (bands 1-3)
  data/imagery/drape.png        copy of the RGB drape (the viewer's ortho URL)
  data/imagery/false_color.png  (NIR, R, G) when the image has a 4th band
and updates the imagery section of data/scene.json when present.

Usage:
  python3 scripts/ingest_imagery.py image.tif [--px-per-m N]

px-per-m defaults to the image's native resolution rounded to the nearest
integer >= 1 (NAIP at 0.6 m -> 2 px/m).
"""

import argparse
import json
import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("image", help="aerial GeoTIFF (RGB or RGBN), any CRS")
    ap.add_argument("--px-per-m", type=int, help="output pixels per meter (integer)")
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    args = ap.parse_args()

    from osgeo import gdal, osr
    from pyproj import CRS
    import twin_georef
    gdal.UseExceptions()

    georef_path = os.path.join(args.data_dir, "georef.json")
    grid = json.load(open(os.path.join(args.data_dir, "terrain", "grid.json")))
    working = twin_georef.crs(georef_path)
    ox, oy = twin_georef.origin(georef_path)

    src = gdal.Open(args.image)
    src_srs = osr.SpatialReference(wkt=src.GetProjection())
    gt = src.GetGeoTransform()
    native = abs(gt[1])
    if not src_srs.IsProjected():
        import math as m
        lat = twin_georef.load(georef_path)["origin_wgs84"]["lat"]
        native *= 111320 * m.cos(m.radians(lat))
    ppm = args.px_per_m or max(1, round(1 / native))

    # exact outer footprint in absolute projected coords
    bounds = (grid["outerMinX"] + ox, grid["outerMinY"] + oy,
              grid["outerMaxX"] + ox, grid["outerMaxY"] + oy)
    ext_x = bounds[2] - bounds[0]
    ext_y = bounds[3] - bounds[1]
    w = round(ext_x * ppm)
    h = round(ext_y * ppm)
    if abs(w - ext_x * ppm) > 1e-6 or abs(h - ext_y * ppm) > 1e-6:
        print(f"note: outer extent {ext_x:g}x{ext_y:g} m is not integral at "
              f"{ppm} px/m; pixel grid rounds to {w}x{h} (still exactly "
              "footprint-aligned, fractionally off integer px/m)")

    warped = gdal.Warp("", src, format="MEM", dstSRS=CRS(working).to_wkt(),
                       outputBounds=bounds, width=w, height=h,
                       resampleAlg="bilinear", outputType=gdal.GDT_Byte)
    out_dir = os.path.join(args.data_dir, "imagery")
    os.makedirs(out_dir, exist_ok=True)
    png = gdal.GetDriverByName("PNG")

    nbands = warped.RasterCount
    rgb = gdal.Translate("", warped, format="MEM", bandList=[1, 2, 3]) \
        if nbands >= 3 else warped
    rgb_path = os.path.join(out_dir, "naip_rgb.png")
    png.CreateCopy(rgb_path, rgb)
    shutil.copyfile(rgb_path, os.path.join(out_dir, "drape.png"))
    print(f"wrote imagery/naip_rgb.png + drape.png ({w}x{h} px, {ppm} px/m, "
          f"{nbands} source bands)")

    wrote_fc = False
    if nbands >= 4:
        fc = gdal.Translate("", warped, format="MEM", bandList=[4, 1, 2])
        png.CreateCopy(os.path.join(out_dir, "false_color.png"), fc)
        wrote_fc = True
        print("wrote imagery/false_color.png (NIR, R, G)")
    else:
        print("no NIR band — skipping false_color.png "
              "(vegetation type classification will degrade to 'unknown')")

    scene_path = os.path.join(args.data_dir, "scene.json")
    if os.path.exists(scene_path):
        scene = json.load(open(scene_path))
        scene["imagery"] = {
            "drape_url": "/data/imagery/drape.png",
            **({"false_color_url": "/data/imagery/false_color.png"} if wrote_fc else {}),
            **({"hillshade_url": scene.get("imagery", {}).get("hillshade_url")}
               if scene.get("imagery", {}).get("hillshade_url") else {}),
            "status": "ready",
        }
        json.dump(scene, open(scene_path, "w"), indent=2)
        print(f"updated {scene_path} imagery")
    # clean GDAL PNG sidecar files
    for aux in ("naip_rgb.png.aux.xml", "drape.png.aux.xml", "false_color.png.aux.xml"):
        p = os.path.join(out_dir, aux)
        if os.path.exists(p):
            os.remove(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
