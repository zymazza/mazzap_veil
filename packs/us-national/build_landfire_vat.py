#!/usr/bin/env python3
"""Give every LANDFIRE atlas raster natural-language class names.

The viewer's click-to-identify shows ``grid.legend[value].name`` for a raster,
else the bare value ("value 176"). EVT is named by the us-national vegetation
pack (fetch_landfire.py + landfire_vat.py); the other LANDFIRE products a twin
carries (EVC, EVH, BPS, FRG, fuel, canopy, succession, …) fall through to the
engine default and read "value N".

This downloads each product's LANDFIRE attribute CSV, writes a generic
value->{name,color} sidecar at ``<data>/atlas/vat/<id>.json`` (consumed by
build_viewer_layers.load_vat), and refreshes that layer's legend + PNG in place
from its already-exported grid — no raster re-fetch, no GDAL warp, no store
write (legends/PNGs are pure viewer exports). Only layers the twin actually has
are touched.

Usage:
  python3 packs/us-national/build_landfire_vat.py --data-dir ./twins/mine/data

Needs internet (one-time CSV snapshot per product).
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))  # packs/<name>/ -> repo root
sys.path.insert(0, os.path.join(PROJECT, "scripts"))

# LANDFIRE products beyond EVT (EVT is named by the vegetation pack). The
# service name encodes the edition + code used to derive the attribute CSV URL;
# BPS/FRG serve *recoded* VALUE indices (not native codes) and their wide CSVs
# live at the off-pattern CSV/LF2016/ path, so they pin the URL + name columns.
LANDFIRE_PRODUCTS = [
    {"id": "landfire_evc_2024", "service": "LF2024_EVC_CONUS"},
    {"id": "landfire_evh_2024", "service": "LF2024_EVH_CONUS"},
    {"id": "landfire_sclass_2024", "service": "LF2024_SClass_CONUS"},
    {"id": "landfire_vcc_2024", "service": "LF2024_VCC_CONUS"},
    {"id": "landfire_vdep_2024", "service": "LF2024_VDep_CONUS"},
    {"id": "landfire_fbfm13_2024", "service": "LF2024_FBFM13_CONUS"},
    {"id": "landfire_fbfm40_2024", "service": "LF2024_FBFM40_CONUS"},
    {"id": "landfire_cc_2024", "service": "LF2024_CC_CONUS"},
    {"id": "landfire_ch_2024", "service": "LF2024_CH_CONUS"},
    {"id": "landfire_cbh_2024", "service": "LF2024_CBH_CONUS"},
    {"id": "landfire_cbd_2024", "service": "LF2024_CBD_CONUS"},
    {"id": "landfire_fdist_2024", "service": "LF2024_FDist_CONUS"},
    {
        "id": "landfire_bps_2020", "service": "LF2020_BPS_CONUS",
        "vat_csv": "https://landfire.gov/sites/default/files/CSV/LF2016/LF16_BPS.csv",
        "vat_name_fields": ["BPS_NAME"],
    },
    {
        "id": "landfire_frg_2016", "service": "LF2016_FRG_CONUS",
        "vat_csv": "https://landfire.gov/sites/default/files/CSV/LF2016/LF2016_FRG.csv",
        "vat_name_fields": ["FRG_NEW", "FRG_DESC"],
    },
]


def vat_csv_url(product):
    if product.get("vat_csv"):
        return product["vat_csv"]
    m = re.search(r"LF(\d{4})_([A-Za-z0-9]+)_CONUS", product["service"])
    if not m:
        return None
    year, code = m.group(1), m.group(2)
    return f"https://landfire.gov/sites/default/files/CSV/{year}/LF{year}_{code}.csv"


def parse_vat_csv(text, name_fields=None):
    """LANDFIRE VAT CSV -> {value_str: {name[, color]}}.

    Class name joins the descriptive columns (de-duplicated, dropping empty /
    'NA' / 'Fill-' filler and the integer 'x10/x100' scaled-encoding column);
    R,G,B become the official color. ``name_fields`` pins the descriptive
    columns by header (in order) for wide CSVs that also carry codes/zones."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return {}
    header = [h.strip().lstrip("﻿") for h in rows[0]]
    up = [h.upper() for h in header]
    if "VALUE" not in up:
        return {}  # not a VAT CSV (e.g. an HTML error page served as 200)
    vi = up.index("VALUE")

    def idx(name):
        return up.index(name) if name in up else None

    ri, gi, bi = idx("R"), idx("G"), idx("B")
    color_idx = {i for i in (ri, gi, bi, idx("RED"), idx("GREEN"), idx("BLUE")) if i is not None}
    if name_fields:
        want = [f.upper() for f in name_fields]
        desc_idx = [up.index(f) for f in want if f in up]
    else:
        desc_idx = [i for i in range(len(header)) if i != vi and i not in color_idx]

    table = {}
    for row in rows[1:]:
        if len(row) <= vi:
            continue
        vs = row[vi].strip()
        if not vs.lstrip("-").isdigit():
            continue
        parts = []
        for i in desc_idx:
            if i >= len(row):
                continue
            t = re.sub(r"^Fill-", "", row[i].strip()).strip()
            if not t or t.upper() == "NA":
                continue
            low = t.lower()
            if any(low in p.lower() for p in parts):
                continue
            parts = [p for p in parts if p.lower() not in low]
            parts.append(t)
        if len(parts) > 1:
            cleaned = [p for p in parts if not re.search(r"x[\s_]*\d{2,}", p, re.I)]
            if cleaned:
                parts = cleaned
        entry = {}
        name = " — ".join(parts)
        if name:
            entry["name"] = name
        if None not in (ri, gi, bi) and len(row) > max(ri, gi, bi):
            try:
                entry["color"] = [int(float(row[ri])), int(float(row[gi])), int(float(row[bi]))]
            except ValueError:
                pass
        if entry:
            table[vs] = entry
    return table


def fetch_vat(product, vat_dir):
    """Download + write the attribute sidecar. Returns a status dict; never
    raises (a missing/!VAT CSV is recorded, not fatal)."""
    url = vat_csv_url(product)
    if not url:
        return {"vat": "no_csv_url"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "veil/1.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as err:  # noqa: BLE001 - network/HTTP errors are non-fatal
        return {"vat": "unavailable", "vat_url": url, "vat_error": str(err)[:200]}
    table = parse_vat_csv(text, product.get("vat_name_fields"))
    if not table:
        return {"vat": "empty", "vat_url": url}
    os.makedirs(vat_dir, exist_ok=True)
    with open(os.path.join(vat_dir, product["id"] + ".json"), "w") as fh:
        json.dump(table, fh)
    return {"vat": "ok", "vat_classes": len(table)}


def rerender_legend_from_grid(data_dir, layer_id):
    """Refresh a layer's legend + PNG from its exported grid values and vat
    sidecar, in-process (no GDAL warp, no store write). Returns True if the
    layer's local grid was found and rewritten."""
    import numpy as np
    import build_viewer_layers as bvl

    local = os.path.join(data_dir, "atlas", "local")
    gpath = os.path.join(local, layer_id + ".grid.json")
    if not os.path.exists(gpath):
        return False
    grid = json.load(open(gpath))
    nodata = grid.get("nodata")
    fill = nodata if nodata is not None else 0
    arr = np.array([[fill if v is None else v for v in row] for row in grid["values"]])
    vat = bvl.load_vat(layer_id, os.path.join(data_dir, "atlas"))
    rgba, legend = bvl.generic_render_raster(arr, nodata, vat)
    bvl.write_png(rgba, os.path.join(local, layer_id + ".png"))
    grid["legend"] = legend
    json.dump(grid, open(gpath, "w"))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir",
                    default=os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    vat_dir = os.path.join(data_dir, "atlas", "vat")
    local = os.path.join(data_dir, "atlas", "local")
    named = 0
    for product in LANDFIRE_PRODUCTS:
        if not os.path.exists(os.path.join(local, product["id"] + ".grid.json")):
            continue  # this twin doesn't carry the layer
        status = fetch_vat(product, vat_dir)
        ok = rerender_legend_from_grid(data_dir, product["id"])
        note = status.get("vat") + (
            f" ({status['vat_classes']} classes)" if status.get("vat_classes") else "")
        print(f"[{ 'vat' if ok else 'skip'}] {product['id']}: {note}")
        if status.get("vat") == "ok" and ok:
            named += 1
    print(f"[done] named {named} LANDFIRE atlas layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
