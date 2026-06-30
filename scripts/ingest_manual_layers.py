#!/usr/bin/env python3
"""Ingest user-downloaded manual/downloadable layers from a drop directory.

Place GeoTIFFs, GeoJSON, Shapefiles, GeoPackages, KML/KMZ, GPX, FileGDB
directories, or GDAL-readable zip archives in manual_layers/, then run:

  npm run ingest-manual-layers -- --data-dir ./twins/mine/data

Each source is routed through scripts/add_layer.py, so reprojection, clipping,
styling, viewer registration, and store provenance stay in one place.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

gdal.UseExceptions()

DEFAULT_SOURCE_DIR = os.path.join(PROJECT, "manual_layers")
SKIP_EXTS = {
    ".aux", ".cpg", ".dbf", ".idx", ".lock", ".ovr", ".prj", ".qix",
    ".sbn", ".sbx", ".shx", ".tfw", ".xml",
}
VECTOR_EXTS = {".csv", ".geojson", ".gdb", ".gpkg", ".gpx", ".json", ".kml", ".kmz", ".shp"}
RASTER_EXTS = {".img", ".tif", ".tiff", ".vrt"}
ARCHIVE_EXTS = {".zip"}


def slug(value):
    text = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return text or "manual_layer"


def display_label(value):
    return re.sub(r"[_-]+", " ", value).strip().title() or "Manual Layer"


def source_for(path):
    if path.lower().endswith(".zip"):
        return "/vsizip/" + os.path.abspath(path)
    return os.path.abspath(path)


def layer_names(path):
    ds = gdal.OpenEx(source_for(path), gdal.OF_VECTOR)
    if ds is None:
        return []
    try:
        return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
    finally:
        ds = None


def is_raster(path):
    ds = gdal.OpenEx(source_for(path), gdal.OF_RASTER)
    if ds is None:
        return False
    try:
        return ds.RasterCount > 0
    finally:
        ds = None


def candidates(source_dir):
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for dirname in list(dirs):
            if dirname.lower().endswith(".gdb"):
                path = os.path.join(root, dirname)
                yield path
                dirs.remove(dirname)
        for filename in files:
            if filename.startswith(".") or filename.lower() == "readme.md":
                continue
            path = os.path.join(root, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext in SKIP_EXTS:
                continue
            if ext in VECTOR_EXTS or ext in RASTER_EXTS or ext in ARCHIVE_EXTS:
                yield path


def run_add_layer(path, data_dir, layer_id, label, layer_name=None):
    cmd = [
        sys.executable,
        os.path.join(PROJECT, "scripts", "add_layer.py"),
        source_for(path),
        "--id", layer_id,
        "--label", label,
        "--data-dir", os.path.abspath(data_dir),
    ]
    if layer_name:
        cmd.extend(["--layer", layer_name])
    subprocess.run(cmd, check=True, cwd=PROJECT)


def ingest(path, data_dir, force_multi=False):
    stem = slug(os.path.splitext(os.path.basename(path.rstrip(os.sep)))[0])
    layers = layer_names(path)
    if layers:
        if len(layers) == 1:
            run_add_layer(path, data_dir, stem, display_label(stem))
            return [stem]
        if not force_multi:
            raise RuntimeError(
                f"{path} has multiple vector layers ({', '.join(layers)}); "
                "rerun with --all-layers to ingest each one"
            )
        imported = []
        for layer in layers:
            layer_id = slug(f"{stem}_{layer}")
            run_add_layer(path, data_dir, layer_id, display_label(layer_id), layer_name=layer)
            imported.append(layer_id)
        return imported
    if is_raster(path):
        run_add_layer(path, data_dir, stem, display_label(stem))
        return [stem]
    raise RuntimeError(f"GDAL could not open {path} as a vector or raster")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR,
                    help="directory of downloaded user layers (default: ./manual_layers)")
    ap.add_argument("--data-dir", default=os.environ.get("TWIN_DATA_DIR"),
                    help="target twin data dir (default: $TWIN_DATA_DIR or ./data)")
    ap.add_argument("--all-layers", action="store_true",
                    help="for multi-layer vector sources, ingest every layer with derived ids")
    args = ap.parse_args()

    data_dir = args.data_dir or os.path.join(PROJECT, "data")
    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        raise SystemExit(f"manual layer directory does not exist: {source_dir}")

    found = list(candidates(source_dir))
    if not found:
        print(f"no GDAL-readable files found in {source_dir}")
        return 0

    imported = []
    failed = []
    for path in found:
        rel = os.path.relpath(path, source_dir)
        try:
            ids = ingest(path, data_dir, force_multi=args.all_layers)
            imported.extend(ids)
            print(f"[ok] {rel} -> {', '.join(ids)}")
        except Exception as err:  # noqa: BLE001
            failed.append((rel, str(err)))
            print(f"[error] {rel}: {err}", file=sys.stderr)

    print(f"imported {len(imported)} layer(s) from {source_dir}")
    if failed:
        print(f"{len(failed)} source(s) failed; fix or remove them and rerun", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
