#!/usr/bin/env python3
"""The one place Python scripts read the twin's georeferencing from.

data/georef.json is the anchor: analysis_crs (projected EPSG), a proj4
string (consumed by the viewer's vendored proj4js), origin_utm, and the
geographic CRS used for lon/lat output (the projected CRS's own datum, so
projected <-> geographic round-trips exactly).

No script should carry a module-level CRS or origin constant — call
load() / origin() / crs() / transformers() here instead. ingest_dem.py
writes this file when a twin is created.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))
GEOREF_PATH = os.path.join(DATA_DIR, "georef.json")

_cache = {"path": None, "data": None}


def load(path=GEOREF_PATH):
    if _cache["path"] != path:
        with open(path) as fh:
            _cache["data"] = json.load(fh)
        _cache["path"] = path
    return _cache["data"]


def crs(path=GEOREF_PATH):
    """The projected working CRS, e.g. 'EPSG:26918'."""
    return load(path)["analysis_crs"]


def epsg_number(path=GEOREF_PATH):
    return int(crs(path).split(":")[1])


def origin(path=GEOREF_PATH):
    """(easting, northing) scene origin in the projected CRS."""
    o = load(path)["origin_utm"]
    return float(o[0]), float(o[1])


def geographic_crs(projected_crs=None, path=GEOREF_PATH):
    """The geographic CRS lon/lat is expressed in: the explicit
    geographic_crs from georef.json, else the projected CRS's own datum."""
    g = load(path).get("geographic_crs") if projected_crs is None else None
    if g:
        return g
    from pyproj import CRS
    geodetic = CRS(projected_crs or crs(path)).geodetic_crs
    code = geodetic.to_epsg()
    return f"EPSG:{code}" if code else geodetic


def proj4_string(projected_crs=None, path=GEOREF_PATH):
    """The proj4 definition the viewer feeds to proj4js."""
    p = load(path).get("proj4") if projected_crs is None else None
    if p:
        return p
    from pyproj import CRS
    s = CRS(projected_crs or crs(path)).to_proj4()
    return s.replace(" +type=crs", "")


def transformers(path=GEOREF_PATH):
    """(projected->geographic, geographic->projected) pyproj Transformers,
    always_xy (lon/lat order)."""
    from pyproj import Transformer
    p, g = crs(path), geographic_crs(path=path)
    return (Transformer.from_crs(p, g, always_xy=True),
            Transformer.from_crs(g, p, always_xy=True))


def from_wgs84_transformer(path=GEOREF_PATH):
    """WGS84 lon/lat -> projected CRS (for localizing WGS84 GeoJSON sources)."""
    from pyproj import Transformer
    return Transformer.from_crs("EPSG:4326", crs(path), always_xy=True)
