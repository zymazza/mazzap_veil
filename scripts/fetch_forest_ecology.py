#!/usr/bin/env python3
"""Fetch additional LANDFIRE forest/fire ecology rasters for a VEIL twin.

These are public CONUS ImageServer products used as atlas layers. Each product
is clipped to the twin terrain footprint, saved as a small GeoTIFF under
<data>/atlas/raw/forest_ecology/, then registered through scripts/add_layer.py
so the viewer gets PNG drapes + grid identify data.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef  # noqa: E402

gdal.UseExceptions()

LF_ROOT = "https://lfps.usgs.gov/arcgis/rest/services"
UA = {"User-Agent": "veil/1.0"}

LANDFIRE_PRODUCTS = [
    {
        "id": "landfire_evc_2024",
        "label": "LANDFIRE Existing Vegetation Cover (EVC)",
        "service": "Landfire_LF2024/LF2024_EVC_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_evh_2024",
        "label": "LANDFIRE Existing Vegetation Height (EVH)",
        "service": "Landfire_LF2024/LF2024_EVH_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_bps_2020",
        "label": "LANDFIRE Biophysical Settings (BPS)",
        "service": "Landfire_LF2020/LF2020_BPS_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_sclass_2024",
        "label": "LANDFIRE Succession Class",
        "service": "Landfire_LF2024/LF2024_SClass_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_vcc_2024",
        "label": "LANDFIRE Vegetation Condition Class (VCC)",
        "service": "Landfire_LF2024/LF2024_VCC_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_vdep_2024",
        "label": "LANDFIRE Vegetation Departure (VDep)",
        "service": "Landfire_LF2024/LF2024_VDep_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_fbfm13_2024",
        "label": "LANDFIRE Fire Behavior Fuel Model 13",
        "service": "Landfire_LF2024/LF2024_FBFM13_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_fbfm40_2024",
        "label": "LANDFIRE Fire Behavior Fuel Model 40",
        "service": "Landfire_LF2024/LF2024_FBFM40_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_cc_2024",
        "label": "LANDFIRE Canopy Cover (CC)",
        "service": "Landfire_LF2024/LF2024_CC_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_ch_2024",
        "label": "LANDFIRE Canopy Height (CH)",
        "service": "Landfire_LF2024/LF2024_CH_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_cbh_2024",
        "label": "LANDFIRE Canopy Base Height (CBH)",
        "service": "Landfire_LF2024/LF2024_CBH_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_cbd_2024",
        "label": "LANDFIRE Canopy Bulk Density (CBD)",
        "service": "Landfire_LF2024/LF2024_CBD_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_fdist_2024",
        "label": "LANDFIRE Fuel Disturbance",
        "service": "Landfire_LF2024/LF2024_FDist_CONUS",
        "pixel_type": "U16",
    },
    {
        "id": "landfire_frg_2016",
        "label": "LANDFIRE Fire Regime Group (FRG)",
        "service": "Landfire_LF2016/LF2016_FRG_CONUS",
        "pixel_type": "U16",
    },
]

KNOWN_UNAVAILABLE = [
    {
        "id": "landfire_esp",
        "label": "LANDFIRE Environmental Site Potential (ESP)",
        "status": "unavailable",
        "reason": "No ESP CONUS ImageServer was found in the current public LANDFIRE service catalog.",
        "checked_services": [
            "Landfire_LF2016", "Landfire_LF2020", "Landfire_LF2022",
            "Landfire_LF2023", "Landfire_LF2024", "Landfire_LF2025",
        ],
    },
]


def grid_projected_bounds(data_dir):
    georef = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef)
    return (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    )


def export_image(service, bbox, epsg, width, height, out_path, pixel_type):
    url = f"{LF_ROOT}/{service}/ImageServer/exportImage"
    params = {
        "bbox": "%f,%f,%f,%f" % bbox,
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": "%d,%d" % (width, height),
        "format": "tiff",
        "pixelType": pixel_type,
        "interpolation": "RSP_NearestNeighbor",
        "f": "image",
    }
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers=UA)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    with open(out_path, "wb") as fh:
        fh.write(data)
    if gdal.Open(out_path) is None:
        raise RuntimeError(f"GDAL could not read exported raster {out_path}")
    return out_path


def add_layer(data_dir, tif, layer_id, label):
    subprocess.run([
        sys.executable,
        os.path.join(PROJECT, "scripts", "add_layer.py"),
        tif,
        "--id", layer_id,
        "--label", label,
        "--data-dir", data_dir,
    ], check=True, cwd=PROJECT, env={**os.environ, "TWIN_DATA_DIR": data_dir})


def write_provenance(data_dir, records):
    path = os.path.join(data_dir, "atlas", "additional-forest-ecology-layers.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"layers": records, "known_unavailable": KNOWN_UNAVAILABLE},
              open(path, "w"), indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    ap.add_argument("--resolution", type=float, default=30.0)
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    bbox = grid_projected_bounds(data_dir)
    epsg = twin_georef.epsg_number(os.path.join(data_dir, "georef.json"))
    width = max(2, round((bbox[2] - bbox[0]) / args.resolution))
    height = max(2, round((bbox[3] - bbox[1]) / args.resolution))
    raw_dir = os.path.join(data_dir, "atlas", "raw", "forest_ecology")
    records = []

    for product in LANDFIRE_PRODUCTS:
        tif = os.path.join(raw_dir, product["id"] + ".tif")
        print(f"[fetch] {product['id']} from {product['service']} ({width}x{height})")
        export_image(product["service"], bbox, epsg, width, height, tif, product["pixel_type"])
        add_layer(data_dir, tif, product["id"], product["label"])
        records.append({
            **product,
            "status": "ok",
            "source_path": os.path.relpath(tif, data_dir),
            "service_url": f"{LF_ROOT}/{product['service']}/ImageServer",
        })

    write_provenance(data_dir, records)
    print(f"[done] added {len(records)} forest/fire ecology atlas layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
