#!/usr/bin/env python3
"""Export every JSON payload the viewer loads from the twin store.

The store (data/twin.gpkg) is authoritative; the viewer stays a window onto
flat JSON. This regenerates, structurally identical to the originals (each
tree/shrub additionally gains a stable "id" — the viewer ignores unknown keys):

  data/vegetation/tree_instances.json            member_parcel trees
  data/vegetation/shrub_points.json              member_parcel shrubs
  data/vegetation/surrounding_tree_instances.json member_surrounding trees
  data/vegetation/surrounding_shrub_points.json   member_surrounding shrubs
  data/vegetation/metadata.json                  meta:vegetation_metadata
  data/vegetation/surrounding_metadata.json      meta:surrounding_vegetation_metadata
  data/scene.json                                meta:scene_template + live counts
  data/buildings/models/manifest.json            building_model entities
  data/surveys/<layer>.geojson                   survey_<layer> entities
  data/surveys/survey-layers.json                survey layer catalog

New viewer placement saves (placements.log.jsonl) and pending survey uploads
(uploads.log.jsonl) are ingested first, so the exports always reflect the
latest field state.

Run:  python3 scripts/export_viewer_payloads.py   (npm run export)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_placements
import ingest_survey
import twin_store
from twin_store import SHRUB_ATTRS, TREE_ATTRS, Store

D = twin_store.DATA_DIR

BUILDING_ATTRS = ("name", "url", "up_axis", "footprint_objectid",
                  "footprint_rect", "model_bbox", "placement")

# Viewer styling for the survey layers (ingest_survey.SURVEY_LAYERS); the
# catalog is data/surveys/survey-layers.json, separate from the atlas catalog
# because build_viewer_layers.py rewrites that one wholesale.
SURVEY_STYLES = {
    "trails": ("line", "rgba(0,0,0,0)", "#e0a84b"),
    "stream_centerlines": ("line", "rgba(0,0,0,0)", "#4ea8de"),
    "photo_points": ("point", "rgba(242,95,92,0.9)", "#ffffff"),
    "observations": ("point", "rgba(111,207,151,0.9)", "#ffffff"),
}


def write_json(path, payload, indent=None):
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=indent)
    return path


def export_vegetation(store, payloads, d):
    os.makedirs(os.path.join(d, "vegetation"), exist_ok=True)
    written = []
    for member, tree_file, shrub_file in [
        ("member_parcel", "tree_instances.json", "shrub_points.json"),
        ("member_surrounding", "surrounding_tree_instances.json",
         "surrounding_shrub_points.json"),
    ]:
        written.append(write_json(os.path.join(d, "vegetation", tree_file),
                                  payloads[("tree", member)]))
        written.append(write_json(os.path.join(d, "vegetation", shrub_file),
                                  payloads[("shrub", member)]))
    for key, name in [("vegetation_metadata", "metadata.json"),
                      ("surrounding_vegetation_metadata", "surrounding_metadata.json")]:
        meta = store.get_meta(key)
        if meta is not None:
            written.append(write_json(os.path.join(d, "vegetation", name), meta, indent=2))
    return written


def export_scene(store, payloads, d):
    # Prefer the store's scene_template; for a twin seeded only by ingest_dem
    # (no migrate yet) fall back to the on-disk scene.json so vegetation can be
    # added to an otherwise bare twin.
    scene = store.get_meta("scene_template")
    if scene is None:
        scene_path = os.path.join(d, "scene.json")
        scene = json.load(open(scene_path)) if os.path.exists(scene_path) else {}
    veg = scene.setdefault("vegetation", {})
    veg["tree_count"] = len(payloads[("tree", "member_parcel")])
    veg["shrub_anchor_count"] = len(payloads[("shrub", "member_parcel")])
    veg["surrounding_tree_count"] = len(payloads[("tree", "member_surrounding")])
    veg["surrounding_shrub_anchor_count"] = len(payloads[("shrub", "member_surrounding")])
    # make sure the viewer knows where to load the populations it now has
    if payloads[("tree", "member_parcel")] or payloads[("shrub", "member_parcel")]:
        veg.setdefault("tree_instances_url", "/data/vegetation/tree_instances.json")
        veg.setdefault("shrub_points_url", "/data/vegetation/shrub_points.json")
        veg["status"] = "ready"
    if payloads[("tree", "member_surrounding")]:
        veg.setdefault("surrounding_tree_instances_url",
                       "/data/vegetation/surrounding_tree_instances.json")
        veg.setdefault("surrounding_shrub_points_url",
                       "/data/vegetation/surrounding_shrub_points.json")
    meta = store.get_meta("vegetation_metadata") or {}
    for src, dst in [("canopy_cover_pct", "canopy_cover_pct"),
                     ("lidar_backed", "lidar_backed"),
                     ("deciduous_evergreen_available", "deciduous_evergreen_available")]:
        if src in meta:
            veg[dst] = meta[src]
    return [write_json(os.path.join(d, "scene.json"), scene, indent=2)]


def export_building_manifest(store, d):
    buildings = []
    attrs = store.latest_attrs("building_model")
    for eid in store.alive_entities("building_model"):
        a = attrs.get(eid, {})
        entry = {"id": eid.split(":", 1)[1]}
        for key in BUILDING_ATTRS:
            if key in a:
                entry[key] = a[key]
        buildings.append(entry)
    buildings.sort(key=lambda b: b["id"])
    models_dir = os.path.join(d, "buildings", "models")
    os.makedirs(models_dir, exist_ok=True)
    return [write_json(os.path.join(models_dir, "manifest.json"),
                       {"buildings": buildings}, indent=2)]


def export_surveys(data_dir=None, store=None):
    """Survey layers -> scene-local GeoJSON + the survey-layers.json catalog.
    Geometry comes from each entity's latest 'geometry' observation (written
    in lockstep with the spatial row). Retired entities are excluded — the
    history stays in the store. Callable standalone (ingest_survey refreshes
    the payloads right after an upload)."""
    d = data_dir or D
    own_store = store is None
    if own_store:
        store = Store(os.path.join(d, "twin.gpkg"))
    written = []
    try:
        catalog = []
        for layer_name in ingest_survey.SURVEY_LAYERS:
            kind = f"survey_{layer_name}"
            attrs = store.latest_attrs(kind)
            if not attrs:
                continue  # never surveyed
            features = []
            for eid in store.alive_entities(kind):
                a = attrs.get(eid, {})
                geometry = a.get("geometry")
                if geometry is None:
                    continue
                props = {"__label": a.get("name") or layer_name.replace("_", " ")}
                for key in ("name", "status", "notes", "accuracy_m"):
                    if a.get(key) is not None:
                        props[key] = a[key]
                if isinstance(a.get("photo"), dict):
                    props["photo"] = a["photo"].get("path")
                    props["photo_captured_at"] = a["photo"].get("captured_at")
                features.append({"type": "Feature", "properties": props,
                                 "geometry": geometry})
            geom_kind, fill, stroke = SURVEY_STYLES.get(
                layer_name, ("line", "rgba(0,0,0,0)", "#cccccc"))
            os.makedirs(os.path.join(d, "surveys"), exist_ok=True)
            written.append(write_json(
                os.path.join(d, "surveys", f"{layer_name}.geojson"),
                {"type": "FeatureCollection", "features": features}))
            catalog.append({
                "id": kind, "label": layer_name.replace("_", " ").title() + " (survey)",
                "type": geom_kind, "file": f"surveys/{layer_name}.geojson",
                "fill": fill, "stroke": stroke, "feature_count": len(features),
                "acquisition": "qfield_survey",
            })
        if catalog:
            written.append(write_json(
                os.path.join(d, "surveys", "survey-layers.json"),
                {"layers": catalog}, indent=2))
    finally:
        if own_store:
            store.close()
    return written


def export_all(data_dir=None, store_path=None):
    d = data_dir or D
    store = Store(store_path) if store_path else Store()
    with store:
        log_path = os.path.join(d, "buildings", "models", "placements.log.jsonl")
        ingested = ingest_placements.ingest(store, log_path)
        if ingested:
            print(f"ingested {ingested} new placement observation(s) from the log")
        surveys = ingest_survey.ingest_pending(store, d)
        if surveys:
            print(f"ingested {len(surveys)} pending survey upload(s) from the log")
        payloads = {
            (kind, member): store.instances(kind, layer, member, attrs)
            for kind, layer, attrs in [("tree", "trees", TREE_ATTRS),
                                       ("shrub", "shrubs", SHRUB_ATTRS)]
            for member in ("member_parcel", "member_surrounding")
        }
        written = export_vegetation(store, payloads, d) + \
            export_scene(store, payloads, d) + \
            export_building_manifest(store, d) + \
            export_surveys(d, store=store)
    base = twin_store.PROJECT if d.startswith(twin_store.PROJECT) else d
    for path in written:
        print("wrote", os.path.relpath(path, base))
    return written


if __name__ == "__main__":
    export_all()
