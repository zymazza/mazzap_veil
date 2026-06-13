#!/usr/bin/env python3
"""One-shot migration of the flat data/ bundle into the twin store
(data/twin.gpkg). Idempotent: re-running adds a pipeline_runs row but no
duplicate entities, observations, or spatial features.

Ingests:
  * data/vegetation/tree_instances.json + shrub_points.json (member_parcel)
  * data/vegetation/surrounding_*.json (member_surrounding)
  * data/buildings/footprints.geojson, data/parcels|hydrology|roads/features.geojson
  * data/buildings/models/manifest.json (placements as observations)
  * data/atlas/atlas-manifest.json -> layers table; localized atlas vectors
    (data/atlas/local/*.geojson, scene-local meters) -> atlas_<id> gpkg layers
  * data/georef.json origin/CRS + data/scene.json skeleton -> meta

Run:  python3 scripts/migrate_to_store.py
"""

import json
import os
import sys
from datetime import datetime, timezone

from osgeo import gdal, ogr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_placements
import twin_store
from twin_store import Store, entity_id

gdal.UseExceptions()

PROJECT = twin_store.PROJECT
D = twin_store.DATA_DIR


def read_json(*parts):
    path = os.path.join(D, *parts)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def feature_entity_id(kind, source, geom, assigned):
    """Deterministic ID for a vector feature from its centroid, with the same
    position-free suffix probing used for vegetation (in practice unused:
    centroids never collide in this data)."""
    c = geom.Centroid()
    base = entity_id(kind, source, round(c.GetX(), 3), round(c.GetY(), 3))
    eid, n = base, 1
    while eid in assigned:
        n += 1
        eid = f"{base}-{n}"
    assigned.add(eid)
    return eid


def ingest_vector_file(store, run, path, kind, layer, source_label):
    """GeoJSON features (already scene-local meters) -> entities + spatial rows
    + a 'properties' observation per feature."""
    with open(path) as fh:
        fc = json.load(fh)
    assigned = set()
    created = 0
    for feat in fc.get("features", []):
        geom = ogr.CreateGeometryFromJson(json.dumps(feat["geometry"]))
        eid = feature_entity_id(kind, source_label, geom, assigned)
        if store.upsert_entity(eid, kind, run):
            created += 1
        store.insert_feature(layer, eid, geom.ExportToWkb(), feat.get("properties") or {})
        store.observe(eid, "properties", feat.get("properties") or {}, run,
                      source=source_label)
    store.conn.commit()
    return len(fc.get("features", [])), created


def file_mtime_utc(path):
    return datetime.fromtimestamp(
        os.path.getmtime(path), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register_input_files(store):
    """Register the non-atlas inputs (terrain grids, imagery, the LiDAR seed)
    in the layers table with content hashes, like the building model_file
    observations — so 'did an input change' is queryable."""
    n = 0
    for layer_id, rel, kind, label in [
        ("terrain_grid", "terrain/grid.json", "terrain_grid", "Parcel terrain grid"),
        ("terrain_grid_apron", "terrain/grid.apron.json", "terrain_grid",
         "Surrounding terrain apron grid (USGS 3DEP)"),
        ("imagery_drape", "imagery/drape.png", "imagery", "NAIP ortho drape"),
        ("imagery_false_color", "imagery/false_color.png", "imagery",
         "NAIP color-infrared"),
        ("imagery_naip_rgb", "imagery/naip_rgb.png", "imagery", "NAIP RGB"),
        ("imagery_hillshade", "imagery/hillshade.png", "imagery", "Hillshade"),
        ("imagery_hillshade_surrounding", "imagery/hillshade_surrounding.png",
         "imagery", "Hillshade (surrounding)"),
        ("lidar_tree_seed", "vegetation/tree_instances.lidar.json", "lidar_seed",
         "Pristine LiDAR stem population (immutable seed input)"),
        ("building_clearance_hulls", "buildings/models/clearance.json", "derived",
         "Building plan outlines for vegetation clearance"),
    ]:
        path = os.path.join(D, rel)
        if not os.path.exists(path):
            continue
        store.upsert_layer(
            layer_id, label=label, kind=kind, acquisition="local_input",
            source_path="data/" + rel, fetched_at=file_mtime_utc(path),
            content_sha1=twin_store.sha1_file(path),
        )
        n += 1
    return n


def ingest_atlas(store, run):
    manifest = read_json("atlas", "atlas-manifest.json")
    viewer_layers = read_json("atlas", "local", "viewer-layers.json") or {}
    labels = {l["id"]: l.get("label") for l in viewer_layers.get("layers", [])}

    n_layers = 0
    for entry in manifest.get("layers", []):
        path = os.path.join(D, entry.get("file") or "")
        fetched = sha1 = None
        if entry.get("file") and os.path.exists(path):
            fetched = file_mtime_utc(path)
            sha1 = twin_store.sha1_file(path)
        store.upsert_layer(
            entry["name"],
            label=labels.get(entry["name"], entry["name"]),
            kind=entry.get("kind"),
            acquisition=entry.get("acquisition"),
            service=entry.get("service"),
            source_path=entry.get("file") or entry.get("source"),
            fetched_at=fetched,
            feature_count=entry.get("feature_count"),
            status=entry.get("status"),
            content_sha1=sha1,
        )
        n_layers += 1
    for entry in manifest.get("drape_layers", []):
        store.upsert_layer(
            entry["name"], label=entry["name"], kind="drape",
            source_path=entry.get("file"), feature_count=entry.get("feature_count"),
            status="ok", acquisition="derived")
        n_layers += 1
    for entry in manifest.get("api_queried_empty_or_error", []):
        store.upsert_layer(
            entry["name"], label=entry["name"], kind=entry.get("category"),
            service=entry.get("service"), status=entry.get("status"),
            acquisition="api_snapshot")
        n_layers += 1
    for entry in manifest.get("skipped", []):
        store.upsert_layer(
            entry["name"], label=entry["name"], status=entry.get("status", "skipped"),
            source_path=entry.get("source"), acquisition="local_source_clip")
        n_layers += 1

    # Localized atlas vectors (scene-local meters) -> one gpkg layer each.
    n_features = 0
    for entry in manifest.get("layers", []):
        if entry.get("kind") != "vector" or entry.get("status") != "ok":
            continue
        local_path = os.path.join(D, "atlas", "local", entry["name"] + ".geojson")
        if not os.path.exists(local_path):
            continue
        table = "atlas_" + entry["name"]
        store.ensure_spatial_layer(table, "GEOMETRY")
        count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count:  # already migrated
            continue
        with open(local_path) as fh:
            fc = json.load(fh)
        for feat in fc.get("features", []):
            geom = ogr.CreateGeometryFromJson(json.dumps(feat["geometry"]))
            store.insert_feature(table, None, geom.ExportToWkb(),
                                 feat.get("properties") or {})
            n_features += 1
        store.conn.commit()
    return n_layers, n_features


def ingest_building_models(store, run):
    manifest = read_json("buildings", "models", "manifest.json")
    for b in manifest.get("buildings", []):
        eid = f"building_model:{b['id']}"
        store.upsert_entity(eid, "building_model", run)
        for attr in ("name", "url", "up_axis", "footprint_objectid",
                     "footprint_rect", "model_bbox", "placement"):
            if attr in b:
                store.observe(eid, attr, b[attr], run, source="models/manifest.json")
    store.conn.commit()
    return len(manifest.get("buildings", []))


def main():
    store = Store()

    georef = read_json("georef.json")
    scene = read_json("scene.json")
    store.set_meta("schema_version", twin_store.SCHEMA_VERSION)
    store.set_meta("origin_utm", georef["origin_utm"])
    store.set_meta("crs", {
        "analysis_crs": georef["analysis_crs"],
        "convention": "store coordinates are scene-local meters: "
                      "x = easting - origin, y = northing - origin",
    })
    store.set_meta("scene_template", scene)
    veg_meta = read_json("vegetation", "metadata.json")
    if veg_meta:
        store.set_meta("vegetation_metadata", veg_meta)
    surr_meta = read_json("vegetation", "surrounding_metadata.json")
    if surr_meta:
        store.set_meta("surrounding_vegetation_metadata", surr_meta)

    inputs = [
        os.path.join(D, "vegetation", "tree_instances.json"),
        os.path.join(D, "vegetation", "shrub_points.json"),
        os.path.join(D, "buildings", "footprints.geojson"),
        os.path.join(D, "buildings", "models", "manifest.json"),
        os.path.join(D, "atlas", "atlas-manifest.json"),
    ]
    run = store.begin_run("migrate_to_store.py", inputs=inputs)

    # ---- vegetation
    trees = read_json("vegetation", "tree_instances.json") or []
    ids, stats = store.bulk_upsert_vegetation("tree", "trees", trees, run,
                                              "member_parcel")
    print(f"parcel trees: {len(trees)} -> {stats}")

    surr_trees = read_json("vegetation", "surrounding_tree_instances.json") or []
    if surr_trees:
        ids, stats = store.bulk_upsert_vegetation("tree", "trees", surr_trees, run,
                                                  "member_surrounding")
        print(f"surrounding trees: {len(surr_trees)} -> {stats}")

    shrubs = read_json("vegetation", "shrub_points.json") or []
    ids, stats = store.bulk_upsert_vegetation("shrub", "shrubs", shrubs, run,
                                              "member_parcel")
    print(f"parcel shrubs: {len(shrubs)} -> {stats}")

    surr_shrubs = read_json("vegetation", "surrounding_shrub_points.json") or []
    if surr_shrubs:
        ids, stats = store.bulk_upsert_vegetation("shrub", "shrubs", surr_shrubs, run,
                                                  "member_surrounding")
        print(f"surrounding shrubs: {len(surr_shrubs)} -> {stats}")

    # ---- vector entities
    for path, kind, layer, label in [
        (os.path.join(D, "parcels", "features.geojson"), "parcel", "parcels", "parcels"),
        (os.path.join(D, "hydrology", "features.geojson"), "stream", "streams", "hydrology"),
        (os.path.join(D, "roads", "features.geojson"), "road", "roads", "roads"),
        (os.path.join(D, "buildings", "footprints.geojson"), "building",
         "building_footprints", "footprints"),
    ]:
        total, created = ingest_vector_file(store, run, path, kind, layer, label)
        print(f"{label}: {total} features ({created} new entities)")

    # ---- building models + their placement history
    n = ingest_building_models(store, run)
    print(f"building models: {n}")
    ingest_placements.ingest(store)

    # ---- atlas + input registration
    n_layers, n_features = ingest_atlas(store, run)
    print(f"atlas: {n_layers} layer records, {n_features} vector features migrated")
    n_inputs = register_input_files(store)
    print(f"input files registered with content hashes: {n_inputs}")

    store.finish_run(run, notes="initial migration of flat data/ bundle")
    n_entities = store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    print(f"store: {n_entities} entities, {n_obs} observations (run {run})")
    store.close()


if __name__ == "__main__":
    main()
