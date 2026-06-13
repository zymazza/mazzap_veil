#!/usr/bin/env python3
"""MCP server over a VEIL digital-twin store — stdio transport.

This is the agent-facing surface of the twin: every tool is a thin wrapper
around scripts/twin_query.py (where all logic lives and is tested). The
store is read-only; the only writes are the draw_polygon / draw_point /
clear_drawings tools, which maintain <data>/annotations.json — ephemeral
orange drawings the 3D viewer polls and renders so an agent can point at
places on the map instead of reciting coordinates. Drawings never touch
the store or the journal.

Run it:
    python3 scripts/mcp_server.py

Register it with Claude Code:
    claude mcp add veil-twin -- python3 /abs/path/to/scripts/mcp_server.py

Conventions every tool follows:
  * Points are {"lat": deg, "lon": deg} or {"x": m, "y": m} in scene-local
    meters (x = east, y = north, the twin's projected CRS minus its origin —
    describe_twin reports which CRS). Results always echo both forms.
  * All distances/areas are meters / square meters. Heights are meters.
  * `region` arguments take exactly one of four shapes:
      {"aoi": true}                                    — the parcel AOI
      {"bbox": [minx, miny, maxx, maxy]}               — scene-local meters
      {"within_m": r, "point": {lat,lon} | {x,y}}      — circle, r in meters
      {"polygon": [[lon,lat], ...] or [[x,y], ...]}    — ring auto-closed
  * Every factual value carries provenance: source / confidence / run_id /
    observed_at from the store's observations, or acquisition / service for
    atlas layers.
  * Errors come back as {"error": ...} objects with the valid alternatives
    listed — never a stack trace.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import twin_query  # noqa: E402
from twin_query import TwinQueryError  # noqa: E402

mcp = FastMCP(
    "veil-twin",
    instructions=(
        "A georeferenced 3D digital twin of a real place: terrain, trees and "
        "shrubs, buildings, parcels, streams, roads, plus a local atlas of "
        "map layers (land cover, soils, hydrology — whatever this twin "
        "holds) and any field-survey uploads. The store is read-only. "
        "Call describe_twin first to learn where this twin is and what it holds. "
        "Points are {lat,lon} degrees or {x,y} scene-local meters; every result "
        "echoes both. "
        "For field observations use list_survey_layers then the survey_* kinds. "
        "When an answer is about a place, show it on the user's live 3D map "
        "instead of reciting coordinates: draw_polygon / draw_point put orange "
        "shapes on it, and set_layer_visibility / filter_layer turn the twin's "
        "own atlas layers on and reveal just the regions that matter (e.g. only "
        "the GAP cells where a species occurs, or one soil class). "
        "reset_layer_views hands layer control back to the user."),
)

_tq = None


def _query():
    global _tq
    if _tq is None:
        _tq = twin_query.TwinQuery()
    return _tq


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TwinQueryError as e:
        return e.payload


@mcp.tool()
def describe_twin() -> dict:
    """Orient yourself: the twin's UTM origin and CRS, the coordinate
    convention, the queryable extent and parcel-AOI bounds (scene-local
    meters and lat/lon), entity counts per kind (alive/total), the full
    pipeline-run history, and layer-catalog counts. Call this first."""
    return _run(_query().describe_twin)


@mcp.tool()
def find_entities(kind: str, near: dict | None = None,
                  within_m: float | None = None, region: dict | None = None,
                  attr_filters: list[str] | None = None, limit: int = 50) -> dict:
    """Find entities of one kind, spatially and/or by attribute.

    kind: tree | shrub | building | building_model | parcel | stream | road.
    near + within_m: center {lat,lon}|{x,y}|{"entity_id": id} and a radius in
      METERS (sugar for the within_m region shape; results sorted nearest
      first with distance_m). region: the four-shape region object (see
      server description) — pass either near+within_m or region, not both.
    attr_filters: strings like "height > 20", "type = evergreen",
      "source = lidar" (ops = != > >= < <=; numbers compared numerically,
      strings case-insensitively) evaluated against latest observations.
      Tree attrs: height (m), radius (crown m), z (elevation m), type
      (evergreen|deciduous), species, community, source (lidar|canopy_fill),
      confidence; building_model attrs include name.
    limit: max entities returned (default 50, cap 1000); total_matched is
      always the full count. Each entity returns its position in both
      coordinate systems and all latest attrs with provenance
      (source/confidence/run_id/observed_at). Only alive (non-retired)
      entities are returned."""
    return _run(_query().find_entities, kind, near=near, within_m=within_m,
                region=region, attr_filters=attr_filters, limit=limit)


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Full current state of one entity by ID (e.g. "tree:000a8cc17eb6",
    "building_model:B-4"): every latest attribute with provenance, position
    in both coordinate systems, geometry (scene-local GeoJSON for parcels /
    buildings / streams / roads), and the runs that created/retired it."""
    return _run(_query().get_entity, entity_id)


@mcp.tool()
def entity_history(entity_id: str, attr: str | None = None) -> dict:
    """The append-only observation timeline of one entity, oldest first —
    how its attributes changed across pipeline runs (a tree keeps its ID
    across rebuilds; moved stems become new entities by design). attr
    restricts to one attribute (e.g. "height"). Each observation carries
    value, observed_at, run_id + script, source, confidence."""
    return _run(_query().entity_history, entity_id, attr=attr)


@mcp.tool()
def identify_at(point: dict) -> dict:
    """Everything true at a single point — the server-side equivalent of
    clicking the viewer: terrain elevation (m), soil map unit with drainage /
    hydrologic group / slope / farmland class, surficial geology, ecoregions,
    LANDFIRE community, NLCD land cover, wetland and protected-species areas,
    GAP species richness and the list of species with modeled habitat there,
    plus the parcel and any building footprint containing the point. point:
    {"lat","lon"} degrees or {"x","y"} scene-local
    meters. Outside the twin you get a clear outside_extent result. Every
    fact carries the source layer's acquisition provenance."""
    return _run(_query().identify_at, point)


@mcp.tool()
def sample_raster(layer_id: str, point: dict) -> dict:
    """Sample one raster atlas layer at a point: raw cell value plus its
    legend name. Valid layer_ids: the raster entries from
    list_layers(kind="raster"). point: {"lat","lon"} or {"x","y"}
    (scene-local meters)."""
    return _run(_query().sample_raster, layer_id, point)


@mcp.tool()
def list_layers(kind: str | None = None) -> dict:
    """The layer catalog: every atlas layer and registered input file with
    its acquisition provenance (local_source_clip vs api_snapshot, service
    URL, fetch time, feature count, status). status="empty" means the layer
    legitimately has nothing on this parcel. kind filters (e.g. "vector",
    "raster", "imagery", "wetlands"); invalid kinds list the valid ones.
    queryable_as marks layers identify_at/sample_raster can read."""
    return _run(_query().list_layers, kind=kind)


@mcp.tool()
def layer_summary(layer_id: str) -> dict:
    """One layer in depth. Vectors: feature count, geometry types, attribute
    fields, distinct labels. Categorical rasters: dimensions, bounds (both
    coordinate systems), and the legend with per-class cell counts and
    shares (e.g. the LANDFIRE community breakdown). Plus the layer's
    acquisition provenance."""
    return _run(_query().layer_summary, layer_id)


@mcp.tool()
def summarize_region(region: dict) -> dict:
    """What's happening inside a shape — the one call for "summarize this
    area". region: {"aoi": true} | {"bbox":[minx,miny,maxx,maxy] meters} |
    {"within_m": r, "point": {...}} | {"polygon": [[lon,lat],...] or
    [[x,y],...]}. Returns region area (m2), entity counts by kind, tree
    statistics (count, type split, mean/max height, summed crown area, top
    species, sources), the parcels covering it, a class breakdown with
    shares for every raster atlas layer, covering features with shares for
    every vector atlas layer, and the species-richness range if the twin
    has a richness grid — every block with its provenance. Shares are
    estimated from evenly spaced sample points (count and spacing
    reported)."""
    return _run(_query().summarize_region, region)


@mcp.tool()
def aggregate_entities(kind: str, metric: str, group_by: str | None = None,
                       where: list[str] | None = None,
                       region: dict | None = None) -> dict:
    """Aggregate latest-state values over entities of one kind. metric:
    "count", "crown_area" (summed pi*radius^2 in m2), or
    "<sum|mean|min|max>:<attr>" for numeric attrs, e.g. "mean:height".
    group_by: a categorical attr ("type", "species", "source", "community")
    — e.g. kind=tree, metric=count, group_by=type is the evergreen/deciduous
    split. where: attr_filters strings (see find_entities). region: the
    four-shape region object. Groups come back with entity_count and
    source/run provenance."""
    return _run(_query().aggregate_entities, kind, metric, group_by=group_by,
                where=where, region=region)


@mcp.tool()
def canopy_change(region: dict | None = None,
                  member: str = "member_parcel") -> dict:
    """When did canopy density change: tree count and summed crown area (m2)
    as of EACH pipeline run, in time order, with per-run deltas — computed
    from the append-only observation history, optionally scoped to a region
    (the four-shape object; a polygon makes "the north field" literal).
    member selects the population: member_parcel (default), member_surrounding
    (the terrain apron outside the parcel), or any."""
    return _run(_query().canopy_change, region=region, member=member)


@mcp.tool()
def list_survey_layers() -> dict:
    """The field-survey catalog (Survey companion, docs/survey.md): one entry
    per uploaded QField layer (trails, stream_centerlines, photo_points,
    observations) with its store kind `survey_<layer>`, geometry type, live
    feature count, attribute fields, and whether photos are attached. The
    survey_* kinds are first-class entities: query them with find_entities,
    summarize_region, aggregate_entities, get_entity and identify_at. Returns
    an empty list (with a note) when no survey has been uploaded yet."""
    return _run(_query().list_survey_layers)


@mcp.tool()
def draw_polygon(polygon: list, label: str | None = None) -> dict:
    """Draw an orange polygon on the user's live 3D map — use this whenever
    an answer points at an area (a stand, a wet corner, a recommended site)
    instead of listing coordinates in text. polygon: at least 3 [lon,lat] or
    scene-local [x,y] vertex pairs (auto-detected; ring auto-closed). label:
    short name shown on the map (e.g. "Densest evergreen stand"). Drawings
    are presentation-only annotations — they appear immediately, persist
    until cleared, and never modify the twin. The user can remove them with
    the viewer's "Clear drawings" button."""
    return _run(_query().draw_polygon, polygon, label=label)


@mcp.tool()
def draw_point(point: dict, label: str | None = None) -> dict:
    """Drop an orange marker on the user's live 3D map — use this whenever
    an answer points at a spot (a specific tree, the highest point, where to
    dig) instead of listing coordinates in text. point: {"lat","lon"} degrees
    or {"x","y"} scene-local meters. label: short name shown on the map.
    Presentation-only — appears immediately, persists until cleared, never
    modifies the twin. The user can remove drawings with the viewer's
    "Clear drawings" button."""
    return _run(_query().draw_point, point, label=label)


@mcp.tool()
def clear_drawings() -> dict:
    """Remove every drawn polygon and point marker from the user's 3D map.
    Call it when the user asks, or before drawing a fresh set for a new
    question so stale shapes don't pile up. Layer-view overrides
    (set_layer_visibility / filter_layer) are left untouched — clear those
    with reset_layer_views."""
    return _run(_query().clear_drawings)


@mcp.tool()
def set_layer_visibility(layer_id: str, visible: bool = True) -> dict:
    """Show or hide one of the twin's atlas map layers on the user's live 3D
    terrain — use this to bring up the layer your answer is about (land cover,
    soils, geology, hydrology, GAP species richness — whatever this twin holds)
    instead of describing it in text. layer_id: a drape-able atlas layer (the
    valid ids are listed by list_layers and echoed in any error). The layer is
    draped onto the terrain so it conforms to topography. visible=False hides
    it again. The override persists until you change it or call
    reset_layer_views."""
    return _run(_query().set_layer_visibility, layer_id, visible=visible)


@mcp.tool()
def filter_layer(layer_id: str, values: list,
                 field: str | None = None) -> dict:
    """Reveal ONLY the selected regions of an atlas layer (and turn the layer
    on) — the precise way to point at "where X is". Everything else in the
    layer is hidden until you clear the filter (call set_layer_visibility, or
    reset_layer_views). Use it for questions like "where could I find wild
    turkey": filter the GAP species-richness layer to that species and the map
    lights up only its modeled habitat.

    layer_id: a drape-able atlas layer. values: the regions to reveal —
      * raster categorical layers (e.g. LANDFIRE land cover): legend class
        names, from layer_summary(layer_id).classes[].name.
      * the GAP species-richness layer: species common-names (field defaults to
        "species"), from layer_summary(...).filterable_species or identify_at.
      * vector layers: the distinct values of `field` (defaults to the feature
        label) — see layer_summary(layer_id).labels / .attribute_fields.
    field: which attribute to match on (vectors only; ignored otherwise).
    Matching is case-insensitive; the result reports matched_values and any
    unmatched ones so you can correct a name. Combine with draw_polygon /
    draw_point when a single spot also helps."""
    return _run(_query().filter_layer, layer_id, values, field=field)


@mcp.tool()
def reset_layer_views() -> dict:
    """Undo every layer override you made with set_layer_visibility /
    filter_layer, handing the twin's layer toggles back to the user. Drawn
    polygons and points are left in place (clear those with clear_drawings).
    Call it when switching topics so stale layer state doesn't linger."""
    return _run(_query().reset_layer_views)


if __name__ == "__main__":
    mcp.run()  # stdio transport
