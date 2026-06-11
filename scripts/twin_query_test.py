#!/usr/bin/env python3
"""Tests for scripts/twin_query.py against a real twin store.

No mocks, no test framework: twin builds are deterministic (seeded RNGs,
journaled history), so real-data assertions are cheap and meaningful.

By default the suite runs against the bundled Flatirons demo twin
(twins/demo/data), building it first with scripts/build_from_aoi.py if it
isn't there (one-time; needs internet + GDAL). Every expectation is derived
from the twin under test, never hardcoded to a place, so the same suite runs
against any twin:

    python3 scripts/twin_query_test.py            # demo twin (or: npm test)
    TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query_test.py
"""

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# Pick the twin BEFORE importing the engine modules — they resolve
# TWIN_DATA_DIR at import time.
DEMO_DATA = os.path.join(PROJECT, "twins", "demo", "data")
if not os.environ.get("TWIN_DATA_DIR"):
    os.environ["TWIN_DATA_DIR"] = DEMO_DATA
DATA = os.path.abspath(os.environ["TWIN_DATA_DIR"])

if not os.path.exists(os.path.join(DATA, "twin.gpkg")):
    if DATA != os.path.abspath(DEMO_DATA):
        sys.exit(f"no twin store at {DATA}/twin.gpkg — build that twin first")
    print("demo twin not found — building it (one-time; needs internet + GDAL)…")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "build_from_aoi.py"),
         "--aoi", os.path.join(PROJECT, "packs", "us-national", "demo",
                               "flatirons_aoi.shp"),
         "--data-dir", DATA, "--name", "Flatirons demo", "--force"],
        check=True)

import twin_georef  # noqa: E402
import twin_query  # noqa: E402
import twin_store  # noqa: E402
from twin_query import TwinQuery, TwinQueryError, resolve_region  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def skip(name, why):
    print(f"  skip  {name} ({why})")


def expect_error(name, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except TwinQueryError as e:
        check(name, True)
        return e.payload
    check(name, False, "expected TwinQueryError")
    return {}


tq = TwinQuery()
G = tq.georef
conn = sqlite3.connect(twin_store.STORE_PATH)
KINDS = set(tq.kinds())

# Derived fixtures: an anchor tree and a test polygon centered on it, sized
# from the population's own bounding box — works for any twin with trees.
HAS_TREES = "tree" in KINDS
if HAS_TREES:
    tx0, ty0, tx1, ty1 = conn.execute(
        "SELECT MIN(t.x), MIN(t.y), MAX(t.x), MAX(t.y) FROM trees t"
        " JOIN entities e ON e.entity_id = t.entity_id"
        " WHERE e.retired_run_id IS NULL").fetchone()
    ax, ay = conn.execute(
        "SELECT t.x, t.y FROM trees t JOIN entities e"
        " ON e.entity_id = t.entity_id WHERE e.retired_run_id IS NULL"
        " ORDER BY t.entity_id LIMIT 1").fetchone()
    hx = max(20.0, (tx1 - tx0) * 0.25)
    hy = max(20.0, (ty1 - ty0) * 0.25)
    STAND = [[ax - hx, ay - hy], [ax + hx, ay - hy],
             [ax + hx, ay + hy], [ax - hx, ay + hy]]
    STAND_REGION = {"polygon": STAND}
    FAR = (max(abs(tx0), abs(tx1)) + max(abs(ty0), abs(ty1))) * 10 + 1e4

print(f"twin under test: {DATA}")

print("== georef ==")
# round-trip: scene -> lat/lon -> scene within 1e-4 m
for x, y in [(0.0, 0.0), (50.0, 100.0), (-287.5, -393.1), (700.0, 808.0)]:
    e = G.echo(x, y)
    x2, y2 = G.to_scene(e["lon"], e["lat"])
    check(f"round-trip ({x},{y}) within 1e-4 m",
          math.hypot(x2 - x, y2 - y) < 1e-4, f"err={math.hypot(x2 - x, y2 - y)}")

# agreement with the viewer: proj4js (vendored, fed the proj4 string from
# the twin's georef.json — same path georef.js uses in the browser) must
# agree with the pyproj transform used here to better than 1e-4 m.
node = shutil.which("node")
if node:
    out = subprocess.run(
        [node, "-e",
         "const g=require(process.argv[1]);"
         "for (const [e,n] of JSON.parse(process.argv[2])) {"
         "const r=g.projectedToGeographic(e,n);"
         "console.log(r.lon.toPrecision(17), r.lat.toPrecision(17));}",
         os.path.join(PROJECT, "public", "viewer", "georef.js"),
         json.dumps([[G.ox, G.oy], [G.ox + 287, G.oy - 393], [G.ox - 287, G.oy + 393]])],
        capture_output=True, text=True, check=True,
        env={**os.environ, "TWIN_DATA_DIR": DATA})
    worst = 0.0
    for line, (dx, dy) in zip(out.stdout.splitlines(), [(0, 0), (287, -393), (-287, 393)]):
        lon_js, lat_js = map(float, line.split())
        x_js, y_js = G.to_scene(lon_js, lat_js)
        worst = max(worst, math.hypot(x_js - dx, y_js - dy))
    check("viewer proj4js agrees with pyproj (<1e-4 m)", worst < 1e-4, f"worst={worst}")
else:
    skip("georef.js comparison", "node not found")

# a lat/lon for scene point (50,100), derived from this twin's own georef
_lon, _lat = G.to_lonlat(50, 100)
p = twin_query.resolve_point({"lat": _lat, "lon": _lon}, G)
check("lat/lon point resolves near (50,100)",
      math.hypot(p[0] - 50, p[1] - 100) < 0.05, str(p))
expect_error("point with both coordinate pairs rejected",
             twin_query.resolve_point, {"lat": 1, "lon": 2, "x": 3, "y": 4}, G)
expect_error("point with neither pair rejected",
             twin_query.resolve_point, {"lat": 1}, G)


print("== resolve_region ==")
r = resolve_region({"aoi": True}, G)
check("aoi region has positive area", r.area_m2 > 0, str(r.area_m2))
if HAS_TREES:
    check("aoi contains the anchor tree", r.contains(ax, ay))
    check("aoi excludes far point", not r.contains(FAR, FAR))

r = resolve_region({"bbox": [-10, -20, 10, 20]}, G)
check("bbox area", abs(r.area_m2 - 800) < 1e-6)
check("bbox contains/excludes", r.contains(0, 0) and not r.contains(11, 0))

r = resolve_region({"within_m": 50, "point": {"x": 100, "y": 100}}, G)
check("circle contains center+edge", r.contains(100, 100) and r.contains(149, 100))
check("circle excludes outside", not r.contains(151, 100))

if HAS_TREES:
    r = resolve_region(STAND_REGION, G)
    check("scene polygon contains its center", r.contains(ax, ay))
    check("scene polygon excludes just-outside point",
          not r.contains(ax + hx * 1.01, ay))
    check("scene polygon area (shoelace)",
          abs(r.area_m2 - (2 * hx) * (2 * hy)) < 1)

    geo_poly = [[G.echo(x, y)["lon"], G.echo(x, y)["lat"]] for x, y in STAND]
    rg = resolve_region({"polygon": geo_poly}, G)
    check("lon/lat polygon auto-detected and matches scene polygon",
          abs(rg.area_m2 - r.area_m2) < 1.0 and rg.contains(ax, ay)
          and not rg.contains(ax + hx * 1.01, ay))

expect_error("region with two shapes rejected", resolve_region,
             {"aoi": True, "bbox": [0, 0, 1, 1]}, G)
expect_error("bbox min>=max rejected", resolve_region, {"bbox": [1, 0, 0, 1]}, G)
expect_error("within_m without point rejected", resolve_region, {"within_m": 5}, G)
expect_error("two-vertex polygon rejected", resolve_region,
             {"polygon": [[0, 0], [1, 1]]}, G)
check("region=None means no filter", resolve_region(None, G) is None)


print("== grid contract ==")
import ingest_dem  # noqa: E402
for grid_file, primary in (("grid.json", True), ("grid.apron.json", False)):
    grid_path = os.path.join(DATA, "terrain", grid_file)
    if not os.path.exists(grid_path):
        if primary:
            check(f"{grid_file} exists", False, "primary grid missing")
        else:
            skip(grid_file, "twin has no apron grid")
        continue
    errors = ingest_dem.validate_grid(json.load(open(grid_path)), primary=primary)
    check(f"{grid_file} honors docs/grid-contract.md", not errors, "; ".join(errors))
GRID = json.load(open(os.path.join(DATA, "terrain", "grid.json")))


print("== describe_twin ==")
d = tq.describe_twin()
for kind in sorted(KINDS):
    alive = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE kind=? AND retired_run_id IS NULL",
        (kind,)).fetchone()[0]
    check(f"{kind} alive count matches store",
          d["entity_counts"][kind]["alive"] == alive,
          f"{d['entity_counts'][kind]['alive']} != {alive}")
check("store crs matches georef.json",
      d["crs"]["analysis_crs"] == twin_georef.crs())
check("origin matches georef", d["origin_utm"][:2] == list(twin_georef.origin()))
check("run history present", len(d["pipeline_runs"]) >= 1)
check("extent corners carry lat/lon",
      "lat" in d["extent_corners"]["southwest"])


if HAS_TREES:
    print("== find_entities ==")
    hmin, hmax = conn.execute(
        "SELECT MIN(CAST(o.value AS REAL)), MAX(CAST(o.value AS REAL)) FROM"
        " observations o JOIN entities e ON e.entity_id = o.entity_id"
        " WHERE e.kind='tree' AND e.retired_run_id IS NULL AND o.attr='height'"
    ).fetchone()
    near = tq.find_entities("tree", near={"x": ax, "y": ay}, within_m=50)
    check("trees found near the anchor stem", near["total_matched"] >= 1)
    check("near results sorted by distance",
          [e["distance_m"] for e in near["entities"]]
          == sorted(e["distance_m"] for e in near["entities"]))
    check("all within 50 m", all(e["distance_m"] <= 50 for e in near["entities"]))
    check("height provenance present", all(
        e["attrs"]["height"]["source"] and e["attrs"]["height"]["run_id"]
        and e["attrs"]["height"]["observed_at"] for e in near["entities"]
        if "height" in e["attrs"]))

    anchor_id = near["entities"][0]["entity_id"]
    near_eid = tq.find_entities("tree", near={"entity_id": anchor_id},
                                within_m=50)
    check("near accepts entity_id",
          near_eid["total_matched"] == near["total_matched"])

    stand = tq.find_entities("tree", region=STAND_REGION, limit=100000)
    reg = resolve_region(STAND_REGION, G)
    check("polygon region returns trees", stand["total_matched"] >= 1)
    check("every returned tree is inside the polygon", all(
        reg.contains(e["position"]["x"], e["position"]["y"])
        for e in stand["entities"]))

    if hmin is not None and hmax > hmin:
        mid = (hmin + hmax) / 2
        tall = tq.find_entities("tree", region=STAND_REGION,
                                attr_filters=[f"height > {mid}"], limit=100000)
        check("numeric filter is a strict subset",
              tall["total_matched"] < stand["total_matched"])
        check("all heights pass the filter", all(
            e["attrs"]["height"]["value"] > mid for e in tall["entities"]))
    else:
        skip("numeric height filter", "all tree heights identical")

    a_type = stand["entities"][0]["attrs"].get("type", {}).get("value")
    if a_type:
        sub = tq.find_entities("tree", region=STAND_REGION,
                               attr_filters=[f"type = {str(a_type).upper()}"],
                               limit=1)
        check("string filter case-insensitive subset",
              0 < sub["total_matched"] <= stand["total_matched"])

    if stand["total_matched"] > 5:
        lim = tq.find_entities("tree", region=STAND_REGION, limit=5)
        check("limit caps returned, not total",
              lim["returned"] == 5
              and lim["total_matched"] == stand["total_matched"])

    expect_error("near+region together rejected", tq.find_entities, "tree",
                 near={"x": 0, "y": 0}, within_m=5, region=STAND_REGION)
    expect_error("unknown kind rejected", tq.find_entities, "dragon")
    expect_error("bad filter syntax rejected", tq.find_entities, "tree",
                 attr_filters=["height >>"])

    print("== get_entity / entity_history ==")
    tree_id = stand["entities"][0]["entity_id"]
    e = tq.get_entity(tree_id)
    check("tree entity has position + attrs + creation run",
          "position" in e and e["attrs"] and e["created"]["run"])
    check("tree attrs carry provenance", all(
        "source" in a and "run_id" in a and "observed_at" in a
        for a in e["attrs"].values()))

    h = tq.entity_history(tree_id)
    check("tree has history", h["count"] >= 1)
    check("history is oldest-first",
          [o["obs_id"] for o in h["observations"]]
          == sorted(o["obs_id"] for o in h["observations"]))
    check("history rows carry run script",
          all(o["run_script"] for o in h["observations"]))
    some_attr = h["observations"][0]["attr"]
    hh = tq.entity_history(tree_id, attr=some_attr)
    check("attr filter narrows history",
          0 < hh["count"] <= h["count"]
          and all(o["attr"] == some_attr for o in hh["observations"]))
    expect_error("unknown entity rejected", tq.get_entity, "tree:nope")

if "parcel" in KINDS:
    parcel_id = conn.execute("SELECT entity_id FROM parcels LIMIT 1").fetchone()[0]
    pe = tq.get_entity(parcel_id)
    check("parcel entity has scene-local geometry",
          pe.get("geometry_scene_m", {}).get("type", "").endswith("Polygon"))
else:
    skip("parcel geometry", "twin has no parcel entities")


print("== identify_at / sample_raster ==")
PX, PY = (ax, ay) if HAS_TREES else (0.0, 0.0)
a = tq.identify_at({"x": PX, "y": PY})
check("identify returns an atlas list", isinstance(a.get("atlas"), list))
check("identify provenance on every atlas fact",
      all(r["provenance"].get("acquisition") for r in a["atlas"]))
if a.get("elevation_m") is not None:
    check("elevation within the grid's range",
          GRID["minElevation"] - 1 <= a["elevation_m"] <= GRID["maxElevation"] + 1,
          str(a["elevation_m"]))
if a.get("species_habitat"):
    check("species habitat list well-formed", a["species_habitat"]["count"] >= 0)
if "parcel" in KINDS:
    check("containing parcel reported",
          any(c["kind"] == "parcel" for c in a["entities_here"]))

b = tq.identify_at({"lat": a["point"]["lat"], "lon": a["point"]["lon"]})
check("identify identical via lat/lon and x/y",
      [r["name"] for r in a["atlas"]] == [r["name"] for r in b["atlas"]])

out = tq.identify_at({"x": 1e6, "y": 1e6})
check("outside extent is a structured result", out.get("outside_extent") is True
      and "extent_scene_m" in out)

raster_ids = [r["layer_id"] for r in a["atlas"]
              if r.get("value") is not None and r.get("layer_id")]
if raster_ids:
    rid = raster_ids[0]
    s = tq.sample_raster(rid, {"x": PX, "y": PY})
    check("sample_raster value matches identify",
          s["value"] == next(r["value"] for r in a["atlas"]
                             if r["layer_id"] == rid))
    check("sample_raster has legend name", bool(s["name"]))
else:
    skip("sample_raster vs identify", "no raster value at the test point")
bad = expect_error("unknown raster lists valid ones", tq.sample_raster,
                   "nope", {"x": 0, "y": 0})
check("error payload lists raster ids", "valid_raster_layers" in bad)


print("== list_layers / layer_summary ==")
ll = tq.list_layers()
n_layers = conn.execute("SELECT COUNT(*) FROM layers").fetchone()[0]
check("catalog covers the layers table", ll["count"] == n_layers)
check("acquisition provenance present on ok layers", all(
    l["acquisition"] for l in ll["layers"] if l["status"] == "ok"))
kinds_in_table = {l["kind"] for l in ll["layers"] if l["kind"]}
if kinds_in_table:
    some_kind = sorted(kinds_in_table)[0]
    filtered = tq.list_layers(kind=some_kind)
    check("kind filter works", 0 < filtered["count"] <= n_layers
          and all(l["kind"] == some_kind for l in filtered["layers"]))
else:
    skip("layer kind filter", "no layer kinds recorded in this twin")
expect_error("unknown layer kind rejected", tq.list_layers, kind="nope")

atlas_vectors = [l for l in tq._atlas_layers()
                 if l.get("type") in ("polygon", "line")]
if atlas_vectors:
    ls = tq.layer_summary(atlas_vectors[0]["id"])
    check("vector summary has fields + count",
          ls["attribute_fields"] and ls["feature_count"] >= 1)
else:
    skip("vector layer_summary", "twin has no vector atlas layers")
atlas_rasters = [l for l in tq._atlas_layers() if l.get("type") == "raster"]
if atlas_rasters:
    lr = tq.layer_summary(atlas_rasters[0]["id"])
    check("raster summary has classes with shares",
          lr["classes"] and abs(sum(c["share"] for c in lr["classes"]) - 1.0) < 0.01)
    check("raster summary names classes", all(c["name"] for c in lr["classes"]))
else:
    skip("raster layer_summary", "twin has no raster atlas layers")
expect_error("unknown layer_id rejected", tq.layer_summary, "nope")


if HAS_TREES:
    print("== summarize_region ==")
    sm = tq.summarize_region(STAND_REGION)
    check("tree count matches find_entities on the same polygon",
          sm["trees"]["count"] == stand["total_matched"],
          f"{sm['trees']['count']} != {stand['total_matched']}")
    if "type_split" in sm["trees"]:
        check("type split sums to count",
              sum(sm["trees"]["type_split"].values()) == sm["trees"]["count"])
    for lid, block in sm["atlas_rasters"].items():
        check(f"raster {lid} shares sum to ~1",
              abs(sum(c["share"] for c in block["classes"]) - 1.0) < 0.01)
        check(f"raster {lid} carries provenance",
              bool(block["provenance"].get("acquisition")))
    check("region area reported", abs(sm["region"]["area_m2"] - reg.area_m2) < 1)
    expect_error("summarize_region requires a region", tq.summarize_region, None)

    print("== aggregate_entities ==")
    ag = tq.aggregate_entities("tree", "count", group_by="type",
                               region=STAND_REGION)
    if "type_split" in sm["trees"]:
        check("count by type matches summarize split",
              {k: v["value"] for k, v in ag["groups"].items()}
              == sm["trees"]["type_split"])
    mh = tq.aggregate_entities("tree", "mean:height", region=STAND_REGION)
    if "mean_height_m" in sm["trees"]:
        check("mean height matches summarize", abs(
            mh["groups"]["all"]["value"] - sm["trees"]["mean_height_m"]) < 0.01)
    ca = tq.aggregate_entities("tree", "crown_area", region=STAND_REGION)
    if "crown_area_m2" in sm["trees"]:
        check("crown area matches summarize", abs(
            ca["groups"]["all"]["value"] - sm["trees"]["crown_area_m2"]) < 1)
    if hmin is not None and hmax > hmin:
        wh = tq.aggregate_entities("tree", "count",
                                   where=[f"height > {(hmin + hmax) / 2}"],
                                   region=STAND_REGION)
        check("where filter reduces count",
              wh["groups"]["all"]["value"] < sm["trees"]["count"])
    check("aggregate carries source provenance", all(
        "sources" in g["provenance"] for g in ag["groups"].values()))
    expect_error("bad metric rejected", tq.aggregate_entities,
                 "tree", "median:height")

    print("== canopy_change ==")
    cc = tq.canopy_change()
    check("at least one run with trees", len(cc["runs"]) >= 1)
    check("rows in time order", [r["started_at"] for r in cc["runs"]]
          == sorted(r["started_at"] for r in cc["runs"]))
    check("counts and crown area positive", all(
        r["tree_count"] > 0 and r["crown_area_m2"] > 0 for r in cc["runs"]))
    check("deltas consistent", all(
        cc["runs"][i]["tree_delta"]
        == cc["runs"][i]["tree_count"] - cc["runs"][i - 1]["tree_count"]
        for i in range(1, len(cc["runs"]))))

    # cross-check the unscoped query against the reference consumer's SQL
    import canopy_density  # noqa: E402
    ref = conn.execute(canopy_density.QUERY, {
        "member": "member_parcel", "minx": -1e9, "maxx": 1e9,
        "miny": -1e9, "maxy": 1e9}).fetchall()
    check("matches scripts/canopy_density.py exactly",
          [(r["run_id"], r["tree_count"], r["crown_area_m2"]) for r in cc["runs"]]
          == [(r[0], r[3], r[4]) for r in ref])

    ccr = tq.canopy_change(region=STAND_REGION)
    check("region scopes canopy history", all(
        rr["tree_count"] <= ar["tree_count"]
        for rr, ar in zip(ccr["runs"], cc["runs"])))
    expect_error("bad member rejected", tq.canopy_change, member="nope")
else:
    skip("find_entities/summarize/aggregate/canopy sections", "twin has no trees")

conn.close()
print(f"\n{PASS} passed, {FAIL} failed")
if FAILURES:
    print("failures:", FAILURES)
sys.exit(1 if FAIL else 0)
