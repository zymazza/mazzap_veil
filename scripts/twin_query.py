#!/usr/bin/env python3
"""Query layer over the twin store, for the MCP server.

Everything the MCP tools answer is computed here, so the logic is testable
without the MCP runtime (scripts/twin_query_test.py runs it against the real
data/twin.gpkg). Store access goes through scripts/twin_store.py (the Store's
sqlite connection is reused for the read-only SQL the store API doesn't
cover, the same way scripts/canopy_density.py queries it).

The store is strictly read-only here. The one thing this module writes is
<data>/annotations.json — ephemeral map drawings (draw_polygon / draw_point /
clear_drawings) that the viewer polls and renders in orange so an LLM can
point at places instead of dictating coordinates. Annotations never touch
the store or the journal.

Conventions (the documented ones — no second convention):
  * Store/scene coordinates are scene-local meters: x = east, y = north,
    i.e. the twin's projected CRS minus origin_utm. The CRS comes from the
    store's meta table (falling back to data/georef.json) — never from a
    constant here. Geographic conversion is pyproj, projected CRS <-> its
    own geodetic CRS, so round-trips are exact and lon/lat matches the
    viewer's proj4js conversion to <1e-4 m.
  * Tool inputs accept points as {"lat","lon"} or {"x","y"}; outputs always
    echo both. Polygons accept [lon,lat] or scene-local [x,y] vertex pairs
    (auto-detected: a polygon whose every vertex falls inside the twin's
    own geographic window — extent plus a pad — is treated as lon/lat).
  * Every factual answer carries provenance: source / confidence / run_id /
    observed_at from the observations table, or acquisition / service from
    the layers table for atlas facts.

The point-identify logic (point-in-polygon, line distance, grid sampling
with legends, GAP species bitmask rows) is a direct port of the viewer's
click-to-identify in public/app.js.
"""

import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import twin_store  # noqa: E402

PROJECT = twin_store.PROJECT
DATA = twin_store.DATA_DIR
ATLAS_LOCAL = os.path.join(DATA, "atlas", "local")
VIEWER_LAYERS = os.path.join(ATLAS_LOCAL, "viewer-layers.json")
AOI_GEOJSON = os.path.join(DATA, "terrain", "aoi_local.geojson")
TERRAIN_GRID = os.path.join(DATA, "terrain", "grid.json")
APRON_GRID = os.path.join(DATA, "terrain", "grid.apron.json")
ANNOTATIONS_PATH = os.path.join(DATA, "annotations.json")
# Survey companion (docs/survey.md): the viewer catalog of uploaded field
# layers + the scene-local GeoJSON each references.
SURVEY_CATALOG = os.path.join(DATA, "surveys", "survey-layers.json")

# Pad (degrees) added around the twin's extent to form the geographic window
# used to auto-detect lon/lat polygon vertices. Scene-local meters never look
# like coordinates inside that window unless the polygon is a few meters
# across at one pathological spot.
GEO_WINDOW_PAD_DEG = 0.5

# Entity kind -> the gpkg spatial layer that carries its geometry.
POINT_KINDS = {"tree": "trees", "shrub": "shrubs"}
VECTOR_KINDS = {
    "building": "building_footprints",
    "parcel": "parcels",
    "stream": "streams",
    "road": "roads",
}
# building_model has no spatial layer; its position is the latest "placement"
# observation (scene-local x/y written by the viewer editor).

# Same hidden-property set as the viewer's identify cards (app.js HIDE_PROPS).
HIDE_PROPS = {"__label", "OBJECTID", "Shape_Length", "Shape_Area",
              "Shape__Area", "Shape__Length", "SHAPE.AREA", "SHAPE.LEN",
              "SPATIALVER", "GlobalID"}

LINE_HIT_DISTANCE_M = 8.0  # app.js identify: line features hit within 8 m

# The richness raster the GAP per-species habitat bitmasks attach to; filtering
# it by species renders a habitat mask instead of the richness gradient.
GAP_SPECIES_LAYER = "gap_species_richness"
DRAPE_TYPES = ("raster", "polygon", "line", "point")


class TwinQueryError(Exception):
    """A structured, caller-visible error (never a stack trace)."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.payload = {"error": message}
        if details:
            self.payload.update(details)


# --------------------------------------------------------------- georef

class Georef:
    """Scene-local meters <-> lon/lat, bound to the store's projected origin
    and CRS (no module-level CRS constants — the CRS arrives from the store's
    meta / data/georef.json via the caller)."""

    def __init__(self, origin_utm, projected_crs):
        import twin_georef
        from pyproj import Transformer
        self.ox = float(origin_utm[0])
        self.oy = float(origin_utm[1])
        self.crs = projected_crs
        geographic = twin_georef.geographic_crs(projected_crs)
        self._fwd = Transformer.from_crs(projected_crs, geographic, always_xy=True)
        self._inv = Transformer.from_crs(geographic, projected_crs, always_xy=True)
        # lon/lat auto-detection window; refined to extent+pad by TwinQuery
        self._window_provider = None
        self._window = None

    def to_lonlat(self, x, y):
        lon, lat = self._fwd.transform(self.ox + x, self.oy + y)
        return lon, lat

    def to_scene(self, lon, lat):
        e, n = self._inv.transform(lon, lat)
        return e - self.ox, n - self.oy

    def set_window_provider(self, provider):
        """provider() -> (minx, miny, maxx, maxy) scene-local extent used to
        derive the lon/lat detection window."""
        self._window_provider = provider
        self._window = None

    def geo_window(self):
        """((lon_min, lon_max), (lat_min, lat_max)) — the twin's extent in
        degrees plus GEO_WINDOW_PAD_DEG, used to recognize lon/lat input."""
        if self._window is None:
            if self._window_provider is not None:
                minx, miny, maxx, maxy = self._window_provider()
            else:  # standalone Georef: a nominal 2 km box around the origin
                minx, miny, maxx, maxy = -1000, -1000, 1000, 1000
            lons, lats = [], []
            for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
                lon, lat = self.to_lonlat(x, y)
                lons.append(lon)
                lats.append(lat)
            p = GEO_WINDOW_PAD_DEG
            self._window = ((min(lons) - p, max(lons) + p),
                            (min(lats) - p, max(lats) + p))
        return self._window

    def echo(self, x, y):
        lon, lat = self.to_lonlat(x, y)
        # 9 decimals ~ 0.1 mm: returned lat/lon must round-trip within 1e-4 m
        return {"x": round(x, 3), "y": round(y, 3),
                "lat": round(lat, 9), "lon": round(lon, 9)}


def resolve_point(point, georef):
    """Accept {"lat","lon"} or {"x","y"}; return (x, y) scene-local meters."""
    if not isinstance(point, dict):
        raise TwinQueryError(
            "point must be an object with lat/lon (degrees) or x/y (scene-local meters)")
    has_geo = "lat" in point and "lon" in point
    has_scene = "x" in point and "y" in point
    if has_geo == has_scene:
        raise TwinQueryError(
            "point must carry exactly one coordinate pair: {lat, lon} in degrees "
            "or {x, y} in scene-local meters",
            got=sorted(point.keys()))
    try:
        if has_geo:
            return georef.to_scene(float(point["lon"]), float(point["lat"]))
        return float(point["x"]), float(point["y"])
    except (TypeError, ValueError):
        raise TwinQueryError("point coordinates must be numbers", got=point)


# ----------------------------------------------------- geometry helpers

def point_in_rings(rings, x, y):
    """Even-odd test across all rings (port of app.js pointInRings)."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def polygon_rings(geometry):
    """All rings of a Polygon/MultiPolygon geojson geometry."""
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    if geometry["type"] == "MultiPolygon":
        return [ring for poly in geometry["coordinates"] for ring in poly]
    return []


def line_paths(geometry):
    """Coordinate paths for line-distance tests (port of app.js eachLine:
    polygons contribute their outlines too)."""
    if not geometry:
        return []
    t = geometry["type"]
    if t == "LineString":
        return [geometry["coordinates"]]
    if t == "MultiLineString":
        return list(geometry["coordinates"])
    if t == "Polygon":
        return list(geometry["coordinates"])
    if t == "MultiPolygon":
        return [ring for poly in geometry["coordinates"] for ring in poly]
    return []


def dist_to_paths(paths, x, y):
    """Min distance (m) from a point to a set of polylines (app.js distToLine)."""
    best = math.inf
    for line in paths:
        for i in range(1, len(line)):
            x1, y1 = line[i - 1][0], line[i - 1][1]
            x2, y2 = line[i][0], line[i][1]
            dx, dy = x2 - x1, y2 - y1
            len2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / len2))
            best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def shoelace_area(ring):
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i - 1][0], ring[i - 1][1]
        x2, y2 = ring[i][0], ring[i][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def sample_grid(grid, bounds, x, y):
    """Nearest-cell raster sample (port of app.js sampleGrid).
    Returns (row, col, value) or None when outside the bounds."""
    minx, miny, maxx, maxy = bounds
    if x < minx or x > maxx or y < miny or y > maxy:
        return None
    col = min(grid["width"] - 1, int((x - minx) / (maxx - minx) * grid["width"]))
    row = min(grid["height"] - 1, int((maxy - y) / (maxy - miny) * grid["height"]))
    return row, col, grid["values"][row][col]


def sample_terrain_elevation(grid, x, y):
    """Bilinear DEM sample, absolute meters (port of viewer/terrain.js
    sampleTerrainHeightAtLocal, without the minElevation offset).
    Returns None outside the grid or over nodata."""
    if not (grid["minX"] <= x <= grid["maxX"] and grid["minY"] <= y <= grid["maxY"]):
        return None
    w = max(1e-9, grid["maxX"] - grid["minX"])
    h = max(1e-9, grid["maxY"] - grid["minY"])
    xr = min(max((x - grid["minX"]) / w, 0.0), 0.999999)
    yr = min(max((y - grid["minY"]) / h, 0.0), 0.999999)
    xi = xr * (grid["width"] - 1)
    yi = (1 - yr) * (grid["height"] - 1)
    x0, y0 = int(xi), int(yi)
    x1 = min(grid["width"] - 1, x0 + 1)
    y1 = min(grid["height"] - 1, y0 + 1)
    tx, ty = xi - x0, yi - y0
    heights = grid["heights"]
    cells = [
        (heights[y0 * grid["width"] + x0], (1 - tx) * (1 - ty)),
        (heights[y0 * grid["width"] + x1], tx * (1 - ty)),
        (heights[y1 * grid["width"] + x0], (1 - tx) * ty),
        (heights[y1 * grid["width"] + x1], tx * ty),
    ]
    valid = [(v, wgt) for v, wgt in cells if isinstance(v, (int, float))]
    if not valid:
        return None
    total = sum(wgt for _, wgt in valid)
    if total <= 0:
        return valid[0][0]
    return sum(v * wgt for v, wgt in valid) / total


def parse_gpkg_geometry(blob):
    """GeoPackage geometry blob -> geojson dict (scene-local coords).
    Header: 'GP', version, flags (bit 1-3 = envelope size code), srs_id."""
    from osgeo import ogr
    flags = blob[3]
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
    geom = ogr.CreateGeometryFromWkb(bytes(blob[8 + envelope_bytes:]))
    if geom is None:
        return None
    return json.loads(geom.ExportToJson())


def geometry_centroid_and_bbox(geometry):
    """Centroid (vertex average is enough for locating entities) and bbox."""
    xs, ys = [], []

    def collect(coords):
        if coords and isinstance(coords[0], (int, float)):
            xs.append(coords[0])
            ys.append(coords[1])
        else:
            for c in coords:
                collect(c)

    collect(geometry["coordinates"])
    if not xs:
        return None, None
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    return (cx, cy), (min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------------- region

class Region:
    """One region abstraction, four shapes (aoi / bbox / within_m / polygon).
    `contains(x, y)` takes scene-local meters; `bounds` is the scene-local
    bounding box used to prefilter before the exact test."""

    def __init__(self, shape, bounds, contains, area_m2, description):
        self.shape = shape
        self.bounds = bounds
        self.contains = contains
        self.area_m2 = area_m2
        self.description = description

    def describe(self):
        return {"shape": self.shape, "bounds_scene_m": [round(v, 3) for v in self.bounds],
                "area_m2": round(self.area_m2, 1) if self.area_m2 else None,
                "description": self.description}


def _looks_geographic(pairs, georef):
    (lon0, lon1), (lat0, lat1) = georef.geo_window()
    return all(lon0 <= p[0] <= lon1 and lat0 <= p[1] <= lat1 for p in pairs)


def _rings_region(shape, rings, description):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    area = sum(shoelace_area(r) for r in rings if shoelace_area(r) > 0)
    # even-odd handles holes; approximate area as outer-minus-holes per polygon
    # is not derivable from a flat ring list, so report the even-odd area by
    # summing signed contributions: outer rings dominate in this dataset.
    return Region(shape, bounds, lambda x, y: point_in_rings(rings, x, y),
                  area, description)


def _aoi_rings():
    with open(AOI_GEOJSON) as fh:
        gj = json.load(fh)
    features = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    rings = []
    for f in features:
        rings.extend(polygon_rings(f.get("geometry") or f))
    if not rings:
        raise TwinQueryError("AOI boundary has no polygon rings", file=AOI_GEOJSON)
    return rings


def resolve_region(region, georef):
    """The single region resolver every spatial tool uses (decision 6).
    Accepts exactly one of:
      {"aoi": true}
      {"bbox": [minx, miny, maxx, maxy]}            (scene-local meters)
      {"within_m": r, "point": {lat,lon} | {x,y}}   (radius in meters)
      {"polygon": [[lon,lat], ...] | [[x,y], ...]}  (ring auto-closed)
    Returns a Region, or None when region is None (no spatial filter).
    """
    if region is None:
        return None
    if not isinstance(region, dict):
        raise TwinQueryError("region must be an object", got=region)
    shapes = [k for k in ("aoi", "bbox", "within_m", "polygon") if k in region]
    if len(shapes) != 1:
        raise TwinQueryError(
            "region must carry exactly one of: aoi, bbox, within_m (+point), polygon",
            got=sorted(region.keys()))
    extra = set(region) - {shapes[0], "point"}
    if extra or ("point" in region and shapes[0] != "within_m"):
        raise TwinQueryError("unexpected region keys", got=sorted(region.keys()))
    shape = shapes[0]

    if shape == "aoi":
        if region["aoi"] is not True:
            raise TwinQueryError('the aoi region is {"aoi": true}', got=region)
        return _rings_region("aoi", _aoi_rings(), "parcel AOI boundary")

    if shape == "bbox":
        b = region["bbox"]
        if (not isinstance(b, (list, tuple)) or len(b) != 4
                or not all(isinstance(v, (int, float)) for v in b)):
            raise TwinQueryError(
                "bbox must be [minx, miny, maxx, maxy] in scene-local meters", got=b)
        minx, miny, maxx, maxy = map(float, b)
        if minx >= maxx or miny >= maxy:
            raise TwinQueryError("bbox min must be < max on both axes", got=b)
        return Region(
            "bbox", (minx, miny, maxx, maxy),
            lambda x, y: minx <= x <= maxx and miny <= y <= maxy,
            (maxx - minx) * (maxy - miny),
            f"bbox ({minx:g},{miny:g})..({maxx:g},{maxy:g}) scene-local m")

    if shape == "within_m":
        if "point" not in region:
            raise TwinQueryError('within_m region needs a center: {"within_m": r, "point": {...}}')
        r = region["within_m"]
        if not isinstance(r, (int, float)) or r <= 0:
            raise TwinQueryError("within_m must be a positive number of meters", got=r)
        cx, cy = resolve_point(region["point"], georef)
        r = float(r)
        r2 = r * r
        return Region(
            "within_m", (cx - r, cy - r, cx + r, cy + r),
            lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r2,
            math.pi * r2,
            f"within {r:g} m of ({cx:.1f},{cy:.1f}) scene-local m")

    # polygon
    poly = region["polygon"]
    if (not isinstance(poly, (list, tuple)) or len(poly) < 3
            or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in poly)):
        raise TwinQueryError(
            "polygon must be a list of at least 3 [lon,lat] or [x,y] vertex pairs", got=poly)
    pts = [(float(p[0]), float(p[1])) for p in poly]
    geographic = _looks_geographic(pts, georef)
    if geographic:
        pts = [georef.to_scene(lon, lat) for lon, lat in pts]
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]  # auto-close
    coords = "lon/lat" if geographic else "scene-local m"
    return _rings_region("polygon", [pts], f"polygon with {len(pts) - 1} vertices ({coords})")


# --------------------------------------------------- map drawings (viewer)
# LLM-drawn polygons/points the viewer renders in orange. They live in one
# flat JSON file inside the twin's data dir (so the static server serves it
# and any process pointed at the same twin shares it) — never in the store.
# Scene-local meters only, matching every other viewer payload.

ANNOTATION_LABEL_MAX = 80


def _utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_view_doc():
    """The viewer-directive document: drawings the agent placed (`annotations`)
    and layer-view overrides it set (`layer_views`). One file the viewer polls;
    both lists are returned so a write to one never drops the other."""
    try:
        with open(ANNOTATIONS_PATH) as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}
    anns = doc.get("annotations")
    views = doc.get("layer_views")
    return (anns if isinstance(anns, list) else [],
            views if isinstance(views, list) else [])


def _save_view_doc(annotations, layer_views):
    doc = {"version": 1, "updated_at": _utc_now(),
           "annotations": annotations, "layer_views": layer_views}
    tmp = ANNOTATIONS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, ANNOTATIONS_PATH)


def _load_annotations():
    return _load_view_doc()[0]


def _next_annotation_id(annotations):
    high = 0
    for a in annotations:
        m = re.fullmatch(r"drawing:(\d+)", str(a.get("id", "")))
        if m:
            high = max(high, int(m.group(1)))
    return f"drawing:{high + 1:04d}"


def _clean_label(label):
    if label is None:
        return None
    label = str(label).strip()
    return label[:ANNOTATION_LABEL_MAX] or None


_DRAWN_NOTE = ("now visible on the user's 3D map in orange; refer to it by its "
               "label/color instead of reciting coordinates. The user can remove "
               "drawings with the viewer's \"Clear drawings\" button, or call "
               "clear_drawings.")

_LAYER_NOTE = ("The drape conforms to the terrain so the user sees exactly "
               "which ground it covers. Overrides take effect within a few "
               "seconds and persist until you change them; call "
               "reset_layer_views to hand layer control back to the user.")


# -------------------------------------------------------------- the store

class TwinQuery:
    """All query functions, over one Store connection, with per-process
    caches invalidated when data/twin.gpkg changes on disk."""

    def __init__(self, store_path=twin_store.STORE_PATH):
        if not os.path.exists(store_path):
            raise TwinQueryError(
                "twin store not found — run `npm run rebuild-store` first",
                path=store_path)
        self.store = twin_store.Store(store_path, journal=False)
        self.conn = self.store.conn
        origin = self.store.get_meta("origin_utm")
        if not origin:
            raise TwinQueryError("store has no origin_utm in meta; not a twin store?")
        import twin_georef
        crs_meta = self.store.get_meta("crs") or {}
        projected = crs_meta.get("analysis_crs") or twin_georef.crs()
        self.georef = Georef(origin, projected)
        self.georef.set_window_provider(self._extent)
        self._store_path = store_path
        self._cache_stamp = None
        self._caches = {}

    # -- caching -----------------------------------------------------------

    def _cache(self, key, build):
        stamp = os.path.getmtime(self._store_path)
        if stamp != self._cache_stamp:
            self._caches = {}
            self._cache_stamp = stamp
        if key not in self._caches:
            self._caches[key] = build()
        return self._caches[key]

    # -- low-level reads ----------------------------------------------------

    def kinds(self):
        return self._cache("kinds", lambda: [
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT kind FROM entities ORDER BY kind")])

    def _require_kind(self, kind):
        if kind not in self.kinds():
            raise TwinQueryError(f"unknown entity kind: {kind!r}", valid_kinds=self.kinds())

    def _runs_by_id(self):
        return self._cache("runs", lambda: {
            r[0]: {"run_id": r[0], "script": r[1], "started_at": r[2],
                   "finished_at": r[3], "inputs_hash": r[4], "notes": r[5]}
            for r in self.conn.execute(
                "SELECT run_id, script, started_at, finished_at, inputs_hash, notes"
                " FROM pipeline_runs")})

    def _alive_ids(self, kind):
        return self._cache(("alive", kind), lambda: set(self.store.alive_entities(kind)))

    def _latest_full(self, kind):
        """{entity_id: {attr: (encoded_value, observed_at, run_id, source,
        confidence)}} — latest observation per (entity, attr), one ordered
        scan (no N+1)."""
        def build():
            out = {}
            for eid, attr, value, at, run_id, source, conf in self.conn.execute(
                    "SELECT o.entity_id, o.attr, o.value, o.observed_at, o.run_id,"
                    " o.source, o.confidence"
                    " FROM observations o JOIN entities e ON e.entity_id = o.entity_id"
                    " WHERE e.kind = ? ORDER BY o.obs_id", (kind,)):
                out.setdefault(eid, {})[attr] = (value, at, run_id, source, conf)
            return out
        return self._cache(("latest", kind), build)

    def _vector_table(self, kind):
        """The spatial table carrying a kind's geometry: the static map for
        the base kinds, plus survey kinds (docs/survey.md), whose table name
        is the kind itself (survey_trails etc.)."""
        table = VECTOR_KINDS.get(kind)
        if table is None and kind.startswith("survey_"):
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (kind,)).fetchone()
            table = kind if row else None
        return table

    def _positions(self, kind):
        """{entity_id: (x, y)} — point layers directly; vector layers by
        centroid; building_model by its latest placement observation."""
        def build():
            if kind in POINT_KINDS:
                return {eid: (x, y) for eid, (x, y, _s)
                        in self.store.points(POINT_KINDS[kind]).items()}
            if self._vector_table(kind):
                out = {}
                for eid, blob in self.conn.execute(
                        f"SELECT entity_id, geom FROM {self._vector_table(kind)}"):
                    gj = parse_gpkg_geometry(blob)
                    if gj:
                        centroid, _bbox = geometry_centroid_and_bbox(gj)
                        if centroid:
                            out[eid] = centroid
                return out
            if kind == "building_model":
                out = {}
                for eid, attrs in self._latest_full(kind).items():
                    if "placement" in attrs:
                        p = twin_store.decode_value(attrs["placement"][0])
                        out[eid] = (p["x"], p["y"])
                return out
            return {}
        return self._cache(("positions", kind), build)

    def _entity_row(self, eid):
        row = self.conn.execute(
            "SELECT entity_id, kind, created_run_id, created_at, retired_run_id,"
            " retired_at FROM entities WHERE entity_id = ?", (eid,)).fetchone()
        if row is None:
            raise TwinQueryError(f"unknown entity_id: {eid!r}",
                                 hint="find_entities(kind=...) lists valid IDs",
                                 valid_kinds=self.kinds())
        return row

    def _attrs_with_provenance(self, kind, eid, only=None):
        runs = self._runs_by_id()
        out = {}
        for attr, (value, at, run_id, source, conf) in self._latest_full(kind).get(eid, {}).items():
            if attr == "id" or (only is not None and attr not in only):
                continue  # "id" duplicates entity_id
            out[attr] = {
                "value": twin_store.decode_value(value),
                "observed_at": at,
                "run_id": run_id,
                "run_script": runs.get(run_id, {}).get("script"),
                "source": source,
                "confidence": conf,
            }
        return out

    def _entity_position(self, eid):
        kind = self._entity_row(eid)[1]
        pos = self._positions(kind).get(eid)
        if pos is None:
            raise TwinQueryError(f"entity {eid} has no position/geometry")
        return kind, pos

    # -- atlas data ----------------------------------------------------------

    def _atlas_catalog(self):
        """Viewer-ready atlas layers (the ones with local data files), merged
        with their provenance row from the store's layers table."""
        def build():
            try:
                with open(VIEWER_LAYERS) as fh:
                    viewer = json.load(fh)
            except OSError:
                raise TwinQueryError("atlas catalog missing — run `npm run build-atlas`",
                                     path=VIEWER_LAYERS)
            table = self._layers_table()
            catalog = {}
            for layer in viewer.get("layers", []):
                merged = dict(layer)
                # the viewer entry wins (friendly labels); the table row only
                # contributes what the viewer file lacks (acquisition etc.)
                for k, v in table.get(layer["id"], {}).items():
                    if v is not None and merged.get(k) in (None, ""):
                        merged[k] = v
                catalog[layer["id"]] = merged
            catalog["__species_grids__"] = viewer.get("gap_species_grids")
            return catalog
        return self._cache("atlas", build)

    def _layers_table(self):
        return self._cache("layers_table", lambda: {
            r[0]: {"layer_id": r[0], "label": r[1], "kind": r[2], "acquisition": r[3],
                   "service": r[4], "source_path": r[5], "fetched_at": r[6],
                   "feature_count": r[7], "status": r[8], "content_sha1": r[9]}
            for r in self.conn.execute(
                "SELECT layer_id, label, kind, acquisition, service, source_path,"
                " fetched_at, feature_count, status, content_sha1 FROM layers")})

    def _atlas_layers(self):
        return [v for k, v in self._atlas_catalog().items() if k != "__species_grids__"]

    def _layer_data(self, layer):
        """Lazily loaded layer payload: geojson features (scene-local) for
        vectors, the value grid for rasters."""
        def build():
            if layer["type"] == "raster":
                with open(os.path.join(DATA, layer["grid"])) as fh:
                    return {"grid": json.load(fh)}
            with open(os.path.join(DATA, layer["file"])) as fh:
                return json.load(fh)
        return self._cache(("layer_data", layer["id"]), build)

    def _species_grids(self):
        def build():
            rel = self._atlas_catalog().get("__species_grids__")
            if not rel:
                return None
            with open(os.path.join(DATA, rel)) as fh:
                return json.load(fh)
        return self._cache("species_grids", build)

    def _terrain_grids(self):
        def build():
            grids = []
            for path in (TERRAIN_GRID, APRON_GRID):
                try:
                    with open(path) as fh:
                        grids.append(json.load(fh))
                except OSError:
                    pass
            return grids
        return self._cache("terrain_grids", build)

    def _extent(self):
        """The twin's queryable extent: union of the raster atlas bounds and
        the terrain grids (scene-local meters)."""
        def build():
            boxes = [l["bounds_local"] for l in self._atlas_layers()
                     if l["type"] == "raster" and l.get("bounds_local")]
            for g in self._terrain_grids():
                boxes.append([g.get("outerMinX", g["minX"]), g.get("outerMinY", g["minY"]),
                              g.get("outerMaxX", g["maxX"]), g.get("outerMaxY", g["maxY"])])
            return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
        return self._cache("extent", build)

    def _layer_provenance(self, layer):
        return {k: layer.get(k) for k in
                ("layer_id", "label", "acquisition", "service", "source_path", "fetched_at")
                if layer.get(k) is not None}

    # -- survey companion (docs/survey.md) ------------------------------------

    def _survey_catalog(self):
        """The survey-layers.json catalog (one entry per uploaded survey
        layer), or [] when nothing has been surveyed yet."""
        def build():
            try:
                with open(SURVEY_CATALOG) as fh:
                    return json.load(fh).get("layers", [])
            except (OSError, ValueError):
                return []
        return self._cache("survey_catalog", build)

    def _survey_features(self, layer):
        """The scene-local GeoJSON features for one survey layer."""
        def build():
            try:
                with open(os.path.join(DATA, layer["file"])) as fh:
                    return json.load(fh).get("features", [])
            except (OSError, ValueError):
                return []
        return self._cache(("survey_features", layer["id"]), build)

    # -- attr filters ---------------------------------------------------------

    _FILTER_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(>=|<=|!=|=|>|<)\s*(.+?)\s*$")

    def _parse_filters(self, attr_filters):
        if attr_filters is None:
            return []
        if isinstance(attr_filters, str):
            attr_filters = [attr_filters]
        parsed = []
        for f in attr_filters:
            m = self._FILTER_RE.match(f) if isinstance(f, str) else None
            if not m:
                raise TwinQueryError(
                    'attr_filters entries look like "height > 20" or "type = evergreen"'
                    " (ops: = != > >= < <=)", got=f)
            attr, op, raw = m.groups()
            raw = raw.strip("'\"")
            try:
                value = float(raw)
            except ValueError:
                value = {"true": True, "false": False}.get(raw.lower(), raw)
            parsed.append((attr, op, value))
        return parsed

    @staticmethod
    def _filter_match(actual, op, expected):
        if actual is None:
            return False
        if isinstance(expected, float):
            try:
                a = float(actual)
            except (TypeError, ValueError):
                return False
            return {"=": a == expected, "!=": a != expected, ">": a > expected,
                    ">=": a >= expected, "<": a < expected, "<=": a <= expected}[op]
        if op not in ("=", "!="):
            raise TwinQueryError(
                f"ordering comparison needs a numeric value, got {expected!r}")
        equal = (str(actual).lower() == str(expected).lower()
                 if isinstance(expected, str) else actual == expected)
        return equal if op == "=" else not equal

    # ======================================================== public queries

    def describe_twin(self):
        """Origin, CRS, extent, entity-kind counts, run history — orientation."""
        crs = self.store.get_meta("crs")
        counts = {kind: {"alive": 0, "total": 0} for kind in self.kinds()}
        for kind, retired, n in self.conn.execute(
                "SELECT kind, retired_run_id IS NOT NULL, COUNT(*)"
                " FROM entities GROUP BY 1, 2"):
            counts[kind]["total"] += n
            if not retired:
                counts[kind]["alive"] += n
        minx, miny, maxx, maxy = self._extent()
        aoi = _rings_region("aoi", _aoi_rings(), "aoi")
        ax0, ay0, ax1, ay1 = aoi.bounds
        layer_rows = list(self._layers_table().values())
        return {
            "name": self.store.get_meta("twin_name") or "VEIL digital twin",
            "crs": crs,
            "origin_utm": self.store.get_meta("origin_utm"),
            "schema_version": self.store.get_meta("schema_version"),
            "coordinate_convention": (
                f"scene-local meters: x = east, y = north ({self.georef.crs} minus "
                "origin_utm). Tools accept {lat,lon} degrees or {x,y} meters; "
                "results echo both."),
            "extent_scene_m": [round(v, 1) for v in (minx, miny, maxx, maxy)],
            "extent_corners": {
                "southwest": self.georef.echo(minx, miny),
                "northeast": self.georef.echo(maxx, maxy)},
            "aoi": {
                "area_m2": round(aoi.area_m2, 1),
                "bounds_scene_m": [round(v, 1) for v in aoi.bounds],
                "southwest": self.georef.echo(ax0, ay0),
                "northeast": self.georef.echo(ax1, ay1)},
            "entity_counts": counts,
            "pipeline_runs": sorted(self._runs_by_id().values(),
                                    key=lambda r: r["run_id"]),
            "layers": {
                "total": len(layer_rows),
                "with_data": sum(1 for r in layer_rows if r["status"] == "ok"),
                "empty_for_parcel": sum(1 for r in layer_rows if r["status"] == "empty"),
                "viewer_ready": len(self._atlas_layers())},
            "vegetation_metadata": self.store.get_meta("vegetation_metadata"),
        }

    def find_entities(self, kind, near=None, within_m=None, region=None,
                      attr_filters=None, limit=50):
        """Spatially + attribute-filtered entity search. `near`+`within_m` is
        sugar for the within_m region shape; `near` may also be
        {"entity_id": ...} to center on another entity."""
        self._require_kind(kind)
        if near is not None:
            if region is not None:
                raise TwinQueryError("pass either near+within_m or region, not both")
            if within_m is None:
                raise TwinQueryError("near needs within_m (meters)")
            if isinstance(near, dict) and "entity_id" in near:
                _, (nx, ny) = self._entity_position(near["entity_id"])
                near = {"x": nx, "y": ny}
            region = {"within_m": within_m, "point": near}
        reg = resolve_region(region, self.georef)
        filters = self._parse_filters(attr_filters)
        limit = max(1, min(int(limit or 50), 1000))

        positions = self._positions(kind)
        alive = self._alive_ids(kind)
        latest = self._latest_full(kind)
        center = None
        if reg is not None and reg.shape == "within_m":
            b = reg.bounds
            center = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

        matches = []
        for eid, (x, y) in positions.items():
            if eid not in alive:
                continue
            if reg is not None:
                bx0, by0, bx1, by1 = reg.bounds
                if not (bx0 <= x <= bx1 and by0 <= y <= by1):
                    continue
                if not reg.contains(x, y):
                    continue
            if filters:
                attrs = latest.get(eid, {})
                ok = True
                for attr, op, expected in filters:
                    actual = (twin_store.decode_value(attrs[attr][0])
                              if attr in attrs else None)
                    if not self._filter_match(actual, op, expected):
                        ok = False
                        break
                if not ok:
                    continue
            matches.append((eid, x, y))

        if center is not None:
            matches.sort(key=lambda m: (m[1] - center[0]) ** 2 + (m[2] - center[1]) ** 2)
        else:
            matches.sort(key=lambda m: m[0])

        entities = []
        for eid, x, y in matches[:limit]:
            entry = {
                "entity_id": eid,
                "kind": kind,
                "position": self.georef.echo(x, y),
                "attrs": self._attrs_with_provenance(kind, eid),
            }
            if kind not in POINT_KINDS and kind != "building_model":
                entry["position_is"] = "centroid"
            if center is not None:
                entry["distance_m"] = round(math.hypot(x - center[0], y - center[1]), 2)
            entities.append(entry)
        return {
            "kind": kind,
            "region": reg.describe() if reg else None,
            "attr_filters": attr_filters,
            "total_matched": len(matches),
            "returned": len(entities),
            "entities": entities,
        }

    def get_entity(self, entity_id):
        """Full current state of one entity: latest attrs with provenance,
        geometry, created/retired runs."""
        eid, kind, created_run, created_at, retired_run, retired_at = \
            self._entity_row(entity_id)
        runs = self._runs_by_id()
        out = {
            "entity_id": eid,
            "kind": kind,
            "created": {"run": runs.get(created_run), "at": created_at},
            "retired": ({"run": runs.get(retired_run), "at": retired_at}
                        if retired_run is not None else None),
            "attrs": self._attrs_with_provenance(kind, eid),
        }
        pos = self._positions(kind).get(eid)
        if pos:
            out["position"] = self.georef.echo(*pos)
        if self._vector_table(kind):
            row = self.conn.execute(
                f"SELECT geom FROM {self._vector_table(kind)} WHERE entity_id = ?",
                (eid,)).fetchone()
            if row:
                out["geometry_scene_m"] = parse_gpkg_geometry(row[0])
                out["position_is"] = "centroid"
        return out

    def entity_history(self, entity_id, attr=None):
        """The observation timeline for one entity, oldest first."""
        self._entity_row(entity_id)
        runs = self._runs_by_id()
        rows = self.store.history(entity_id, attr)
        for r in rows:
            r["run_script"] = runs.get(r["run_id"], {}).get("script")
        return {"entity_id": entity_id, "attr": attr,
                "observations": rows, "count": len(rows)}

    # -- point identify --------------------------------------------------------

    def identify_at(self, point):
        """Everything true at one point, across all atlas + entity layers —
        the server-side port of the viewer's click-to-identify."""
        x, y = resolve_point(point, self.georef)
        minx, miny, maxx, maxy = self._extent()
        echo = self.georef.echo(x, y)
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return {
                "point": echo,
                "outside_extent": True,
                "message": "point is outside the twin extent — no data here",
                "extent_scene_m": [round(v, 1) for v in (minx, miny, maxx, maxy)],
                "extent_corners": {
                    "southwest": self.georef.echo(minx, miny),
                    "northeast": self.georef.echo(maxx, maxy)},
            }

        results = []
        for layer in self._atlas_layers():
            data = self._layer_data(layer)
            if layer["type"] == "raster":
                grid = data["grid"]
                s = sample_grid(grid, layer["bounds_local"], x, y)
                if s is None or s[2] is None or s[2] == grid.get("nodata"):
                    continue
                legend = (grid.get("legend") or {}).get(str(s[2]))
                results.append({
                    "layer_id": layer["id"], "layer_label": layer["label"],
                    "value": s[2],
                    "name": legend["name"] if legend else str(s[2]),
                    "provenance": self._layer_provenance(layer),
                })
                continue
            for f in data.get("features", []):
                g = f.get("geometry")
                if not g:
                    continue
                if layer["type"] == "polygon":
                    hit = point_in_rings(polygon_rings(g), x, y)
                else:
                    hit = dist_to_paths(line_paths(g), x, y) < LINE_HIT_DISTANCE_M
                if hit:
                    props = {k: v for k, v in (f.get("properties") or {}).items()
                             if k not in HIDE_PROPS and v not in (None, "", " ")}
                    results.append({
                        "layer_id": layer["id"], "layer_label": layer["label"],
                        "name": (f.get("properties") or {}).get("__label") or layer["label"],
                        "properties": props,
                        "provenance": self._layer_provenance(layer),
                    })

        species = None
        sg = self._species_grids()
        if sg:
            bx0, by0, bx1, by1 = sg["bounds_local"]
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                col = min(sg["width"] - 1, int((x - bx0) / (bx1 - bx0) * sg["width"]))
                row = min(sg["height"] - 1, int((by1 - y) / (by1 - by0) * sg["height"]))
                names = sorted(
                    s["common_name"] for s in sg["species"].values()
                    if row < len(s["rows"]) and col < len(s["rows"][row])
                    and s["rows"][row][col] == "1")
                gap_row = self._layers_table().get("gap_species_richness", {})
                species = {"count": len(names), "common_names": names,
                           "provenance": {k: gap_row.get(k) for k in
                                          ("layer_id", "acquisition", "service")}}

        containing = []
        for kind in ("parcel", "building"):
            table = VECTOR_KINDS[kind]
            for eid, blob in self.conn.execute(
                    f"SELECT entity_id, geom FROM {table}"):
                gj = parse_gpkg_geometry(blob)
                if gj and gj["type"].endswith("Polygon") \
                        and point_in_rings(polygon_rings(gj), x, y):
                    containing.append({
                        "entity_id": eid, "kind": kind,
                        "attrs": self._attrs_with_provenance(kind, eid)})

        survey = self._survey_hits(x, y)

        elevation = None
        for grid in self._terrain_grids():
            elevation = sample_terrain_elevation(grid, x, y)
            if elevation is not None:
                break

        return {
            "point": echo,
            "elevation_m": round(elevation, 2) if elevation is not None else None,
            "atlas": results,
            "species_habitat": species,
            "survey": survey,
            "entities_here": containing,
        }

    def _survey_hits(self, x, y):
        """Survey-companion features at a point (docs/survey.md): polygons by
        containment, lines within 8 m, points within 8 m — the click-to-identify
        coverage atlas layers get, now extended to field uploads (photo and
        status included). [] when nothing has been surveyed here."""
        hits = []
        for layer in self._survey_catalog():
            for f in self._survey_features(layer):
                g = f.get("geometry") or {}
                gtype = g.get("type", "")
                if gtype.endswith("Polygon"):
                    hit = point_in_rings(polygon_rings(g), x, y)
                elif "Line" in gtype:
                    hit = dist_to_paths(line_paths(g), x, y) < LINE_HIT_DISTANCE_M
                elif gtype in ("Point", "MultiPoint"):
                    coords = [g["coordinates"]] if gtype == "Point" else g["coordinates"]
                    hit = any(math.hypot(c[0] - x, c[1] - y) < LINE_HIT_DISTANCE_M
                              for c in coords)
                else:
                    hit = False
                if hit:
                    props = {k: v for k, v in (f.get("properties") or {}).items()
                             if k not in HIDE_PROPS and v not in (None, "", " ")}
                    hits.append({
                        "kind": layer["id"], "layer_label": layer.get("label"),
                        "name": (f.get("properties") or {}).get("__label")
                        or layer.get("label"),
                        "properties": props,
                        "provenance": {"acquisition": layer.get("acquisition",
                                                                 "qfield_survey")},
                    })
        return hits

    def sample_raster(self, layer_id, point):
        """One raster layer's value + legend entry at a point."""
        layer = self._atlas_catalog().get(layer_id)
        rasters = [l["id"] for l in self._atlas_layers() if l["type"] == "raster"]
        if not layer or layer.get("type") != "raster":
            raise TwinQueryError(f"unknown raster layer: {layer_id!r}",
                                 valid_raster_layers=rasters)
        x, y = resolve_point(point, self.georef)
        grid = self._layer_data(layer)["grid"]
        s = sample_grid(grid, layer["bounds_local"], x, y)
        echo = self.georef.echo(x, y)
        if s is None:
            return {"layer_id": layer_id, "point": echo, "value": None,
                    "message": "point is outside this layer's bounds",
                    "bounds_scene_m": layer["bounds_local"]}
        legend = (grid.get("legend") or {}).get(str(s[2]))
        return {
            "layer_id": layer_id, "layer_label": layer["label"],
            "point": echo,
            "value": s[2],
            "name": legend["name"] if legend else None,
            "nodata": s[2] == grid.get("nodata"),
            "provenance": self._layer_provenance(layer),
        }

    # -- catalog ---------------------------------------------------------------

    def list_layers(self, kind=None):
        """The layer catalog (atlas layers and registered inputs) with
        acquisition provenance. Layers with status 'empty' legitimately have
        no features on this parcel."""
        rows = sorted(self._layers_table().values(), key=lambda r: r["layer_id"])
        valid = sorted({r["kind"] for r in rows if r["kind"]})
        if kind is not None:
            if kind not in valid:
                raise TwinQueryError(f"unknown layer kind: {kind!r}", valid_kinds=valid)
            rows = [r for r in rows if r["kind"] == kind]
        queryable = {l["id"]: l["type"] for l in self._atlas_layers()}
        out = []
        for r in rows:
            entry = dict(r)
            entry.pop("content_sha1", None)
            if r["layer_id"] in queryable:
                entry["queryable_as"] = queryable[r["layer_id"]]
            out.append(entry)
        return {"count": len(out), "kinds": valid, "layers": out}

    def layer_summary(self, layer_id):
        """One layer in depth: fields and labels for vectors, the legend and
        per-class cell breakdown for categorical rasters."""
        table_row = self._layers_table().get(layer_id)
        layer = self._atlas_catalog().get(layer_id)
        if table_row is None and layer is None:
            raise TwinQueryError(f"unknown layer_id: {layer_id!r}",
                                 valid_layer_ids=sorted(self._layers_table().keys()))
        out = {"layer_id": layer_id, "provenance": table_row or self._layer_provenance(layer)}
        if layer is None:
            out["note"] = ("registered in the store but not viewer-queryable "
                           "(input file, imagery, or empty for this parcel)")
            return out
        out["type"] = layer["type"]
        data = self._layer_data(layer)
        if layer["type"] == "raster":
            grid = data["grid"]
            counts = {}
            for row in grid["values"]:
                for v in row:
                    if v is not None and v != grid.get("nodata"):
                        counts[v] = counts.get(v, 0) + 1
            total = sum(counts.values()) or 1
            legend = grid.get("legend") or {}
            classes = [{
                "value": v,
                "name": (legend.get(str(v)) or {}).get("name", str(v)),
                "cells": n,
                "share": round(n / total, 4),
            } for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
            b = layer["bounds_local"]
            out.update({
                "width": grid["width"], "height": grid["height"],
                "bounds_scene_m": b,
                "bounds_corners": {"southwest": self.georef.echo(b[0], b[1]),
                                   "northeast": self.georef.echo(b[2], b[3])},
                "classes": classes,
            })
            # the GAP richness grid carries per-species habitat masks: list the
            # species so the agent knows what filter_layer(..., field="species")
            # can reveal.
            sg = self._species_grids() if layer_id == GAP_SPECIES_LAYER else None
            if sg:
                out["filterable_species"] = sorted(
                    {s.get("common_name") for s in sg["species"].values()
                     if s.get("common_name")})
        else:
            features = data.get("features", [])
            geom_types = {}
            prop_keys = set()
            labels = []
            for f in features:
                g = f.get("geometry") or {}
                geom_types[g.get("type")] = geom_types.get(g.get("type"), 0) + 1
                props = f.get("properties") or {}
                prop_keys.update(k for k in props if k not in HIDE_PROPS)
                lbl = props.get("__label")
                if lbl and lbl not in labels:
                    labels.append(lbl)
            out.update({
                "feature_count": len(features),
                "geometry_types": geom_types,
                "attribute_fields": sorted(prop_keys),
                "labels": labels[:25],
            })
        return out

    # -- region summary -----------------------------------------------------------

    def _region_samples(self, reg, target=3000):
        """Evenly spaced sample points inside a region (used to estimate
        raster class shares and polygon-layer overlap)."""
        minx, miny, maxx, maxy = reg.bounds
        w, h = maxx - minx, maxy - miny
        step = max(1.0, math.sqrt(max(w * h, 1.0) / target))
        pts = []
        ny = max(1, int(h / step))
        nx = max(1, int(w / step))
        for iy in range(ny):
            yv = miny + (iy + 0.5) * (h / ny)
            for ix in range(nx):
                xv = minx + (ix + 0.5) * (w / nx)
                if reg.contains(xv, yv):
                    pts.append((xv, yv))
        if not pts:
            cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
            if reg.contains(cx, cy):
                pts = [(cx, cy)]
        return pts, step

    def _raster_breakdown(self, layer_id, samples):
        layer = self._atlas_catalog().get(layer_id)
        if not layer or layer.get("type") != "raster":
            return None
        grid = self._layer_data(layer)["grid"]
        legend = grid.get("legend") or {}
        counts = {}
        hit = 0
        for x, y in samples:
            s = sample_grid(grid, layer["bounds_local"], x, y)
            if s is None or s[2] is None or s[2] == grid.get("nodata"):
                continue
            hit += 1
            counts[s[2]] = counts.get(s[2], 0) + 1
        if not hit:
            return None
        classes = [{
            "value": v,
            "name": (legend.get(str(v)) or {}).get("name", str(v)),
            "share": round(n / hit, 4),
        } for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return {"classes": classes, "dominant": classes[0],
                "provenance": self._layer_provenance(layer)}

    def _polygon_overlap(self, layer_id, samples, name_props=("__label",)):
        """Which features of a polygon atlas layer cover the region, with the
        sampled share of the region they cover."""
        layer = self._atlas_catalog().get(layer_id)
        if not layer or layer.get("type") not in ("polygon", "line"):
            return None
        data = self._layer_data(layer)
        found = {}
        for f in data.get("features", []):
            rings = polygon_rings(f.get("geometry"))
            if not rings:
                continue
            inside = sum(1 for x, y in samples if point_in_rings(rings, x, y))
            if not inside:
                continue
            props = f.get("properties") or {}
            name = next((props[p] for p in name_props if props.get(p)), layer["label"])
            entry = found.setdefault(name, {"name": name, "samples_inside": 0,
                                            "properties": {k: v for k, v in props.items()
                                                           if k not in HIDE_PROPS}})
            entry["samples_inside"] += inside
        if not found:
            return None
        total = len(samples) or 1
        features = sorted(found.values(), key=lambda e: -e["samples_inside"])
        for e in features:
            e["share_of_region"] = round(e["samples_inside"] / total, 4)
            del e["samples_inside"]
        return {"features": features, "provenance": self._layer_provenance(layer)}

    def summarize_region(self, region):
        """The headline call: everything happening inside a region, with
        provenance per fact — shaped for an LLM to narrate directly."""
        reg = resolve_region(region, self.georef)
        if reg is None:
            raise TwinQueryError(
                "summarize_region needs a region "
                '({"aoi":true} | {"bbox":[...]} | {"within_m":r,"point":{...}} | {"polygon":[...]})')
        samples, spacing = self._region_samples(reg)
        if not samples:
            return {"region": reg.describe(),
                    "message": "region contains no sampleable area inside the twin extent"}

        runs = self._runs_by_id()

        def veg_stats(kind):
            positions = self._positions(kind)
            alive = self._alive_ids(kind)
            latest = self._latest_full(kind)
            bx0, by0, bx1, by1 = reg.bounds
            stats = {"count": 0}
            heights, crown, types, species, sources, run_ids = [], 0.0, {}, {}, {}, set()
            for eid, (x, y) in positions.items():
                if eid not in alive or not (bx0 <= x <= bx1 and by0 <= y <= by1):
                    continue
                if not reg.contains(x, y):
                    continue
                stats["count"] += 1
                attrs = latest.get(eid, {})
                for name, bucket in (("type", types), ("species", species),
                                     ("source", sources)):
                    if name in attrs:
                        v = twin_store.decode_value(attrs[name][0])
                        bucket[v] = bucket.get(v, 0) + 1
                if "height" in attrs:
                    heights.append(float(twin_store.decode_value(attrs["height"][0])))
                if "radius" in attrs:
                    r = float(twin_store.decode_value(attrs["radius"][0]))
                    crown += math.pi * r * r
                for rec in attrs.values():
                    run_ids.add(rec[2])
            if heights:
                stats["mean_height_m"] = round(sum(heights) / len(heights), 2)
                stats["max_height_m"] = round(max(heights), 2)
            if crown:
                stats["crown_area_m2"] = round(crown, 1)
            if types:
                stats["type_split"] = types
            if species:
                stats["top_species"] = dict(sorted(species.items(),
                                                   key=lambda kv: -kv[1])[:8])
            if sources:
                stats["sources"] = sources
            if run_ids:
                stats["provenance"] = {
                    "store": "latest observations per entity",
                    "runs": sorted({runs[r]["script"] for r in run_ids if r in runs}),
                }
            return stats

        entity_counts = {}
        for kind in self.kinds():
            positions = self._positions(kind)
            alive = self._alive_ids(kind)
            bx0, by0, bx1, by1 = reg.bounds
            n = sum(1 for eid, (x, y) in positions.items()
                    if eid in alive and bx0 <= x <= bx1 and by0 <= y <= by1
                    and reg.contains(x, y))
            if n:
                entity_counts[kind] = n

        richness = None
        layer = self._atlas_catalog().get("gap_species_richness")
        if layer:
            grid = self._layer_data(layer)["grid"]
            vals = []
            for x, y in samples:
                s = sample_grid(grid, layer["bounds_local"], x, y)
                if s and s[2] is not None:
                    vals.append(s[2])
            if vals:
                richness = {"min": min(vals), "max": max(vals),
                            "mean": round(sum(vals) / len(vals), 1),
                            "provenance": self._layer_provenance(layer)}

        # parcel entities live in the store, not the atlas: report which
        # parcel polygons cover the region's samples (subsampled — coverage,
        # not share, is the question here).
        parcel_hits = {}
        for eid, blob in self.conn.execute("SELECT entity_id, geom FROM parcels"):
            gj = parse_gpkg_geometry(blob)
            rings = polygon_rings(gj) if gj else []
            if rings and any(point_in_rings(rings, x, y) for x, y in samples[::7] or samples):
                props = self._attrs_with_provenance("parcel", eid).get("properties", {})
                p = props.get("value") or {}
                parcel_hits[eid] = {"entity_id": eid, "owner": p.get("owner"),
                                    "parcel_address": p.get("parcel_address"),
                                    "calc_acres": p.get("calc_acres")}

        # Every atlas layer the twin has, summarized inside the region —
        # the engine names no layers; whatever the twin's atlas holds is
        # what gets reported (rasters as class breakdowns, vectors as
        # covering features with their share of the region).
        atlas_rasters = {}
        atlas_vectors = {}
        for lid, layer in self._atlas_catalog().items():
            if lid in ("__species_grids__", "gap_species_richness") \
                    or not isinstance(layer, dict):
                continue
            if layer.get("type") == "raster":
                b = self._raster_breakdown(lid, samples)
                if b:
                    atlas_rasters[lid] = b
            elif layer.get("type") in ("polygon", "line"):
                o = self._polygon_overlap(lid, samples)
                if o:
                    atlas_vectors[lid] = o

        return {
            "region": reg.describe(),
            "sampling": {"points": len(samples), "spacing_m": round(spacing, 1)},
            "entity_counts": entity_counts,
            "trees": veg_stats("tree"),
            "shrubs": {"count": entity_counts.get("shrub", 0)},
            "parcels": list(parcel_hits.values()),
            "atlas_rasters": atlas_rasters,
            "atlas_vector_overlaps": atlas_vectors,
            "gap_species_richness": richness,
        }

    # -- aggregates / temporal ------------------------------------------------------

    def aggregate_entities(self, kind, metric, group_by=None, where=None, region=None):
        """Aggregate latest-state values over entities of one kind.
        metric: "count", "crown_area" (sum of pi*radius^2), or
        "<sum|mean|min|max>:<numeric attr>" e.g. "mean:height"."""
        self._require_kind(kind)
        reg = resolve_region(region, self.georef)
        filters = self._parse_filters(where)

        m = re.match(r"^(count|crown_area|(sum|mean|min|max):([A-Za-z_]\w*))$",
                     str(metric))
        if not m:
            raise TwinQueryError(
                'metric must be "count", "crown_area", or "<sum|mean|min|max>:<attr>"',
                got=metric)
        agg, attr = (m.group(2), m.group(3)) if m.group(2) else (m.group(1), None)
        if agg == "crown_area":
            agg, attr = "crown_area", "radius"

        positions = self._positions(kind)
        alive = self._alive_ids(kind)
        latest = self._latest_full(kind)
        runs = self._runs_by_id()

        groups = {}
        for eid, (x, y) in positions.items():
            if eid not in alive:
                continue
            if reg is not None:
                bx0, by0, bx1, by1 = reg.bounds
                if not (bx0 <= x <= bx1 and by0 <= y <= by1) or not reg.contains(x, y):
                    continue
            attrs = latest.get(eid, {})
            if filters:
                skip = False
                for fattr, op, expected in filters:
                    actual = (twin_store.decode_value(attrs[fattr][0])
                              if fattr in attrs else None)
                    if not self._filter_match(actual, op, expected):
                        skip = True
                        break
                if skip:
                    continue
            key = "all"
            if group_by:
                key = (twin_store.decode_value(attrs[group_by][0])
                       if group_by in attrs else None)
            g = groups.setdefault(key, {"n": 0, "values": [], "sources": {},
                                        "run_ids": set()})
            g["n"] += 1
            if attr and attr in attrs:
                try:
                    g["values"].append(float(twin_store.decode_value(attrs[attr][0])))
                except (TypeError, ValueError):
                    pass
                g["run_ids"].add(attrs[attr][2])
                src = attrs[attr][3]
                g["sources"][src] = g["sources"].get(src, 0) + 1
            elif "source" in attrs:
                src = twin_store.decode_value(attrs["source"][0])
                g["sources"][src] = g["sources"].get(src, 0) + 1

        def finish(g):
            vals = g["values"]
            if agg == "count":
                value = g["n"]
            elif agg == "crown_area":
                value = round(sum(math.pi * v * v for v in vals), 1)
            elif not vals:
                value = None
            elif agg == "sum":
                value = round(sum(vals), 3)
            elif agg == "mean":
                value = round(sum(vals) / len(vals), 3)
            elif agg == "min":
                value = round(min(vals), 3)
            else:
                value = round(max(vals), 3)
            out = {"value": value, "entity_count": g["n"]}
            prov = {"sources": g["sources"]} if g["sources"] else {}
            if g["run_ids"]:
                prov["runs"] = sorted({runs[r]["script"] for r in g["run_ids"]
                                       if r in runs})
            if prov:
                out["provenance"] = prov
            return out

        return {
            "kind": kind, "metric": metric, "group_by": group_by,
            "where": where, "region": reg.describe() if reg else None,
            "groups": {str(k): finish(g) for k, g in
                       sorted(groups.items(), key=lambda kv: -kv[1]["n"])},
        }

    def canopy_change(self, region=None, member="member_parcel"):
        """Tree count + summed crown area as of each pipeline run, in time
        order — "when did canopy density change here". member: which
        population ('member_parcel', 'member_surrounding', or 'any')."""
        if member not in ("member_parcel", "member_surrounding", "any"):
            raise TwinQueryError("member must be member_parcel, member_surrounding, or any",
                                 got=member)
        reg = resolve_region(region, self.georef)
        params = {"minx": -1e9, "miny": -1e9, "maxx": 1e9, "maxy": 1e9}
        id_join = ""
        if reg is not None:
            bx0, by0, bx1, by1 = reg.bounds
            params.update(minx=bx0, miny=by0, maxx=bx1, maxy=by1)
            if reg.shape != "bbox":
                # exact predicate -> temp table of candidate ids (no N+1)
                cand = [eid for eid, (x, y) in self._positions("tree").items()
                        if bx0 <= x <= bx1 and by0 <= y <= by1 and reg.contains(x, y)]
                self.conn.execute("DROP TABLE IF EXISTS temp.region_trees")
                self.conn.execute("CREATE TEMP TABLE region_trees (entity_id TEXT PRIMARY KEY)")
                self.conn.executemany("INSERT INTO temp.region_trees VALUES (?)",
                                      [(c,) for c in cand])
                id_join = "JOIN temp.region_trees ri ON ri.entity_id = e.entity_id"

        def member_subselect(attr):
            return (f"(SELECT o.value FROM observations o"
                    f" WHERE o.entity_id = e.entity_id AND o.attr = '{attr}'"
                    f" AND o.run_id <= r.run_id ORDER BY o.obs_id DESC LIMIT 1)")

        if member == "any":
            member_cols = (f"{member_subselect('member_parcel')} AS mp,"
                           f" {member_subselect('member_surrounding')} AS ms")
            member_where = "(s.mp = 'true' OR s.ms = 'true')"
        else:
            member_cols = f"{member_subselect(member)} AS mp"
            member_where = "s.mp = 'true'"

        sql = f"""
        WITH runs AS (SELECT run_id, script, started_at FROM pipeline_runs),
        state AS (
          SELECT r.run_id, e.entity_id, {member_cols},
            (SELECT CAST(o.value AS REAL) FROM observations o
              WHERE o.entity_id = e.entity_id AND o.attr = 'radius'
                AND o.run_id <= r.run_id
              ORDER BY o.obs_id DESC LIMIT 1) AS radius
          FROM runs r
          JOIN entities e ON e.kind = 'tree'
            AND e.created_run_id <= r.run_id
            AND (e.retired_run_id IS NULL OR e.retired_run_id > r.run_id)
          JOIN trees t ON t.entity_id = e.entity_id
          {id_join}
          WHERE t.x BETWEEN :minx AND :maxx AND t.y BETWEEN :miny AND :maxy
        )
        SELECT r.run_id, r.script, r.started_at,
               COUNT(*) AS tree_count,
               CAST(ROUND(SUM(3.14159265 * radius * radius), 0) AS INTEGER)
        FROM state s JOIN runs r USING (run_id)
        WHERE {member_where}
        GROUP BY r.run_id
        ORDER BY r.started_at
        """
        rows = []
        prev = None
        for run_id, script, started, count, area in self.conn.execute(sql, params):
            rows.append({
                "run_id": run_id, "script": script, "started_at": started,
                "tree_count": count, "crown_area_m2": area,
                "tree_delta": None if prev is None else count - prev,
            })
            prev = count
        return {
            "member": member,
            "region": reg.describe() if reg else None,
            "runs": rows,
            "provenance": {
                "store": "per-run liveness over entities + latest-attr-as-of-run "
                         "over observations, joined to pipeline_runs "
                         "(same query shape as scripts/canopy_density.py)"},
        }

    # -- survey companion query surface (docs/survey.md) ----------------------

    def list_survey_layers(self):
        """The field-survey catalog: one entry per uploaded QField layer
        (trails, stream_centerlines, photo_points, observations), each with
        its store kind (survey_<layer> — queryable via find_entities /
        summarize_region / aggregate_entities / identify_at), geometry type,
        live feature count, the attribute fields present, and whether any
        feature carries a photo. Empty list (with a note) when no survey has
        been uploaded yet."""
        layers = []
        for layer in self._survey_catalog():
            feats = self._survey_features(layer)
            fields = sorted({k for f in feats
                             for k in (f.get("properties") or {})
                             if k not in HIDE_PROPS and k != "__label"})
            layers.append({
                "kind": layer["id"],
                "label": layer.get("label"),
                "geometry_type": layer.get("type"),
                "feature_count": layer.get("feature_count", len(feats)),
                "fields": fields,
                "has_photos": any((f.get("properties") or {}).get("photo")
                                  for f in feats),
                "acquisition": layer.get("acquisition", "qfield_survey"),
            })
        out = {"count": len(layers), "layers": layers}
        if not layers:
            out["note"] = ("no field surveys uploaded yet — the Survey companion "
                           "write path (docs/survey.md) is empty for this twin")
        return out

    # -- map drawings (viewer annotations) -----------------------------------

    def _within_extent(self, xs, ys):
        try:
            minx, miny, maxx, maxy = self._extent()
        except Exception:
            return None
        return all(minx <= x <= maxx and miny <= y <= maxy
                   for x, y in zip(xs, ys))

    def draw_polygon(self, polygon, label=None):
        if (not isinstance(polygon, (list, tuple)) or len(polygon) < 3
                or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in polygon)):
            raise TwinQueryError(
                "polygon must be a list of at least 3 [lon,lat] or [x,y] vertex pairs",
                got=polygon)
        try:
            pts = [(float(p[0]), float(p[1])) for p in polygon]
        except (TypeError, ValueError):
            raise TwinQueryError("polygon vertices must be numbers", got=polygon)
        geographic = _looks_geographic(pts, self.georef)
        if geographic:
            pts = [self.georef.to_scene(lon, lat) for lon, lat in pts]
        if pts[0] == pts[-1]:
            pts = pts[:-1]  # store the open ring; the viewer closes it
        if len(pts) < 3:
            raise TwinQueryError("polygon needs at least 3 distinct vertices", got=polygon)
        pts = [(round(x, 2), round(y, 2)) for x, y in pts]

        annotations, views = _load_view_doc()
        ann = {"id": _next_annotation_id(annotations), "type": "polygon",
               "label": _clean_label(label), "vertices": [[x, y] for x, y in pts],
               "created_at": _utc_now()}
        annotations.append(ann)
        _save_view_doc(annotations, views)

        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        result = {
            "drawn": {"id": ann["id"], "type": "polygon", "label": ann["label"],
                      "vertex_count": len(pts),
                      "area_m2": round(shoelace_area(pts), 1),
                      "centroid": self.georef.echo(cx, cy),
                      "vertices_scene_m": ann["vertices"]},
            "annotations_total": len(annotations),
            "note": f"Polygon {_DRAWN_NOTE}",
        }
        inside = self._within_extent([p[0] for p in pts], [p[1] for p in pts])
        if inside is False:
            result["warning"] = "some vertices fall outside the twin's extent"
        return result

    def draw_point(self, point, label=None):
        x, y = resolve_point(point, self.georef)
        x, y = round(x, 2), round(y, 2)
        annotations, views = _load_view_doc()
        ann = {"id": _next_annotation_id(annotations), "type": "point",
               "label": _clean_label(label), "x": x, "y": y,
               "created_at": _utc_now()}
        annotations.append(ann)
        _save_view_doc(annotations, views)
        result = {
            "drawn": {"id": ann["id"], "type": "point", "label": ann["label"],
                      "position": self.georef.echo(x, y)},
            "annotations_total": len(annotations),
            "note": f"Point marker {_DRAWN_NOTE}",
        }
        if self._within_extent([x], [y]) is False:
            result["warning"] = "the point falls outside the twin's extent"
        return result

    def clear_drawings(self):
        annotations, views = _load_view_doc()
        _save_view_doc([], views)
        return {"cleared": len(annotations),
                "note": "all drawings removed from the user's 3D map "
                        "(layer views left untouched — use reset_layer_views "
                        "to restore the user's layer toggles)"}

    # -- layer views (atlas map-layer control) -------------------------------

    def _drape_layer(self, layer_id):
        """The atlas layer the agent can show/filter, or a structured error
        listing the drape-able ids."""
        layer = self._atlas_catalog().get(layer_id)
        if layer is None or layer.get("type") not in DRAPE_TYPES:
            raise TwinQueryError(
                f"unknown or non-drape-able layer_id: {layer_id!r}",
                valid_layer_ids=sorted(
                    l["id"] for l in self._atlas_layers()
                    if l.get("type") in DRAPE_TYPES))
        return layer

    def _filter_options(self, layer):
        """How a layer can be filtered: its drape `kind` and the values a
        filter may select (legend class names for rasters, modeled-habitat
        species for the GAP grid, per-attribute distinct values for vectors)."""
        lid = layer["id"]
        sg = self._species_grids()
        if lid == GAP_SPECIES_LAYER and sg:
            names = sorted({s.get("common_name") for s in sg["species"].values()
                            if s.get("common_name")})
            return {"kind": "species", "field": "species",
                    "fields": {"species": names}}
        data = self._layer_data(layer)
        if layer["type"] == "raster":
            legend = (data.get("grid") or {}).get("legend") or {}
            names = []
            for meta in legend.values():
                nm = (meta or {}).get("name")
                if nm and nm not in names:
                    names.append(nm)
            return {"kind": "raster", "field": "class", "fields": {"class": names}}
        fields = {}
        labels = []
        for f in data.get("features", []):
            props = f.get("properties") or {}
            # __label is the feature's friendly name and the primary filter
            # target; it lives in HIDE_PROPS (hidden from identify cards) so it
            # must be collected explicitly, not through the property loop below.
            lbl = props.get("__label")
            if lbl not in (None, "", " ") and str(lbl) not in labels:
                labels.append(str(lbl))
            for k, v in props.items():
                if k in HIDE_PROPS or v in (None, "", " "):
                    continue
                vals = fields.setdefault(k, [])
                if str(v) not in vals:
                    vals.append(str(v))
        fields["__label"] = labels
        return {"kind": "vector", "field": "__label", "fields": fields}

    def _upsert_layer_view(self, directive):
        annotations, views = _load_view_doc()
        views = [v for v in views if v.get("layer_id") != directive["layer_id"]]
        views.append(directive)
        _save_view_doc(annotations, views)
        return views

    def set_layer_visibility(self, layer_id, visible=True):
        """Show or hide one atlas map layer on the user's live 3D terrain,
        without filtering it."""
        layer = self._drape_layer(layer_id)
        directive = {"layer_id": layer_id, "visible": bool(visible),
                     "filter": None, "created_at": _utc_now()}
        views = self._upsert_layer_view(directive)
        verb = "shown on" if visible else "hidden from"
        return {
            "layer": {"id": layer_id, "label": layer.get("label"),
                      "type": layer.get("type")},
            "visible": bool(visible),
            "layer_views_total": len(views),
            "note": f"{layer.get('label', layer_id)} is now {verb} the user's "
                    "3D map. " + _LAYER_NOTE,
        }

    def filter_layer(self, layer_id, values, field=None):
        """Reveal only the selected features/regions of an atlas layer (and turn
        the layer on). values are legend class names for rasters, modeled-habitat
        species common-names for the GAP species grid, or — for vector layers —
        the distinct values of `field` (default the feature label). Everything
        else in the layer is hidden until the filter is cleared."""
        layer = self._drape_layer(layer_id)
        if not isinstance(values, (list, tuple)) or not values:
            raise TwinQueryError(
                "values must be a non-empty list of names/classes to reveal",
                got=values)
        values = [str(v) for v in values]
        opts = self._filter_options(layer)
        kind = opts["kind"]
        field = field or opts["field"]
        available = opts["fields"].get(field)
        if available is None:
            raise TwinQueryError(
                f"layer {layer_id!r} cannot be filtered on field {field!r}",
                filterable_fields=sorted(opts["fields"].keys()))
        by_lower = {a.lower(): a for a in available}
        matched, unmatched = [], []
        for v in values:
            hit = by_lower.get(v.lower())
            (matched if hit else unmatched).append(hit or v)
        if not matched:
            raise TwinQueryError(
                f"none of the requested values exist in {layer_id!r} "
                f"(field {field!r})",
                requested=values, available_values=available[:60])
        flt = {"field": field, "values": matched}
        directive = {"layer_id": layer_id, "visible": True, "filter": flt,
                     "kind": kind, "created_at": _utc_now()}
        views = self._upsert_layer_view(directive)
        result = {
            "layer": {"id": layer_id, "label": layer.get("label"),
                      "type": layer.get("type"), "filter_kind": kind},
            "filter": flt,
            "matched_values": matched,
            "layer_views_total": len(views),
            "note": f"{layer.get('label', layer_id)} now reveals only "
                    f"{', '.join(matched)} on the user's 3D map; everything else "
                    "in the layer is hidden. " + _LAYER_NOTE,
        }
        if unmatched:
            result["unmatched_values"] = unmatched
            result["warning"] = ("these values matched nothing in the layer and "
                                 "were ignored — see layer_summary for the valid "
                                 "names")
        return result

    def reset_layer_views(self):
        """Drop every agent layer override, returning the user's manual layer
        toggles to control. Leaves drawn polygons/points in place."""
        annotations, views = _load_view_doc()
        _save_view_doc(annotations, [])
        return {"cleared": len(views),
                "note": "all agent layer overrides removed; the user's manual "
                        "layer toggles are back in control"}


# ----------------------------------------------------------- CLI for demos

def main(argv):
    """python3 scripts/twin_query.py <function> ['<json kwargs>'] — run one
    query function directly (used for demos; the MCP server is the product)."""
    if len(argv) < 2:
        names = [n for n in dir(TwinQuery) if not n.startswith("_")]
        print(f"usage: twin_query.py <function> ['<json kwargs>']\nfunctions: {names}")
        return 2
    fn_name = argv[1]
    kwargs = json.loads(argv[2]) if len(argv) > 2 else {}
    tq = TwinQuery()
    fn = getattr(tq, fn_name, None)
    if fn is None or fn_name.startswith("_"):
        print(f"unknown function: {fn_name}")
        return 2
    try:
        print(json.dumps(fn(**kwargs), indent=1, default=str))
    except TwinQueryError as e:
        print(json.dumps(e.payload, indent=1))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
