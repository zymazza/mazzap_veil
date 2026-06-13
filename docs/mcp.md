# MCP server over the twin store

`scripts/mcp_server.py` exposes the twin store (`data/twin.gpkg`) to LLM
agents over MCP (stdio). The store is **read-only**: no tool mutates it.
The one writable surface is the viewer-directive set — the map-drawing trio
(`draw_polygon` / `draw_point` / `clear_drawings`) plus the layer-view trio
(`set_layer_visibility` / `filter_layer` / `reset_layer_views`), which
together maintain `<data>/annotations.json` — ephemeral orange shapes and
atlas-layer overrides the 3D viewer polls and applies so an agent can point
at places (and reveal the exact map layer/region) instead of reciting
coordinates (see "Drawing on the map" below). All logic lives in
`scripts/twin_query.py` (tested by
`scripts/twin_query_test.py` against the real store); the MCP layer is thin
wrappers.

## Setup & run

```bash
pip install -r requirements.txt    # the mcp SDK (pyproj usually already present)
npm run rebuild-store              # if data/twin.gpkg doesn't exist yet
python3 scripts/mcp_server.py      # stdio server (for a client to spawn)
```

Register with Claude Code:

```bash
claude mcp add veil-twin -- python3 /ABS/PATH/TO/scripts/mcp_server.py
```

Or in a client's JSON config:

```json
{ "mcpServers": { "veil-twin": {
    "command": "python3",
    "args": ["/ABS/PATH/TO/scripts/mcp_server.py"] } } }
```

Quick sanity check without a client:

```bash
python3 scripts/twin_query.py describe_twin
# points are {x,y} scene-local meters or {lat,lon} degrees; describe_twin
# reports the extent so you can pick an in-bounds point
python3 scripts/twin_query.py identify_at '{"point":{"x":50,"y":100}}'
python3 scripts/twin_query_test.py
```

## Conventions

- **Coordinates.** Scene-local meters (x = east, y = north; the twin's
  projected CRS from `georef.json` minus `origin_utm`) — the store
  convention. Every point input accepts `{"lat","lon"}` degrees **or**
  `{"x","y"}` meters; every output echoes both. Conversion is pyproj
  between the twin's projected and geographic CRS, which agrees with the
  viewer's `georef.js` to ~0.1 mm. All distances are meters.
- **Regions.** Spatial tools take one `region` object, exactly one of:
  `{"aoi": true}` · `{"bbox": [minx,miny,maxx,maxy]}` (scene-local meters) ·
  `{"within_m": r, "point": {…}}` · `{"polygon": [[lon,lat],…] or [[x,y],…]}`
  (ring auto-closed; lon/lat vertices auto-detected).
- **Provenance.** Every entity attribute returns
  `source / confidence / run_id / observed_at`; every atlas fact returns the
  layer's `acquisition` (`local_source_clip` | `api_snapshot`) and service.
- **Errors are structured** — `{"error": …}` with the valid alternatives
  (kinds, layer ids) listed, never a stack trace. Out-of-extent points get
  an `outside_extent` result with the extent.

## Tools

| Tool | Question it answers |
|---|---|
| `describe_twin()` | "What is this place?" — origin, CRS, extent, entity counts, run history |
| `find_entities(kind, near?, within_m?, region?, attr_filters?, limit?)` | "Trees over 20 m within 50 m of this building" |
| `get_entity(entity_id)` | Full current state + geometry of one entity |
| `entity_history(entity_id, attr?)` | One entity's observation timeline across runs |
| `identify_at(point)` | Everything true at a point (the viewer's click-to-identify) — atlas layers, GAP species habitat, containing parcel/building, **and** survey features (`survey_*`, photo + status) |
| `sample_raster(layer_id, point)` | One raster value + legend name at a point |
| `list_layers(kind?)` | The atlas/input catalog with acquisition provenance |
| `layer_summary(layer_id)` | Fields/labels of a vector; legend + class shares of a raster |
| `summarize_region(region)` | "What's happening inside this shape?" — the headline call |
| `aggregate_entities(kind, metric, group_by?, where?, region?)` | Counts, mean height, crown area, splits |
| `canopy_change(region?, member?)` | "When did canopy density change here?" — per-run history |
| `list_survey_layers()` | The field-survey catalog (`survey_*` kinds, counts, fields, photos) |
| `draw_polygon(polygon, label?)` | Draw a labeled orange polygon on the user's live 3D map |
| `draw_point(point, label?)` | Drop a labeled orange marker on the user's live 3D map |
| `clear_drawings()` | Remove every drawn shape from the map |
| `set_layer_visibility(layer_id, visible?)` | Show/hide one atlas map layer on the user's terrain |
| `filter_layer(layer_id, values, field?)` | Reveal ONLY the selected regions of a layer (turns it on) |
| `reset_layer_views()` | Drop every agent layer override (user toggles back in control) |

## Drawing on the map

When an answer points at a place — "the densest stand is here", "put the
trail along this line of parcels", "the wettest corner is this one" — the
agent should **draw it** with `draw_polygon` / `draw_point` (vertices in
`[lon,lat]` or scene-local `[x,y]`, auto-detected like region polygons; a
short `label` shows on the map) rather than listing coordinates in text.

Mechanics: the tools append to `<data>/annotations.json` (scene-local
meters, atomic rewrite) and the viewer (`public/annotations.js`) polls that
file every few seconds, rendering terrain-hugging orange outlines, orange
point markers, and label sprites. This works identically for the built-in
chat panel and for any external MCP client pointed at the same twin — the
viewer doesn't care who wrote the file. Drawings are presentation-only:
they never enter the store or the journal, and they persist until cleared.
The user clears them with the chat panel's **Clear drawings** button
(`POST /api/annotations/clear`); the agent can also call `clear_drawings`,
e.g. before drawing a fresh set for a new question.

## Controlling map layers

Beyond drawing its own shapes, the agent can drive the twin's *own* atlas
layers. `set_layer_visibility(layer_id, visible)` toggles a layer's drape on
the terrain — bring up the layer the answer is about (land cover, soils,
geology, hydrology, GAP species richness — whatever the twin holds) instead
of describing it. `filter_layer(layer_id, values, field?)` goes further: it
turns the layer on but reveals **only** the selected regions, hiding the
rest. The `values` are:

- **raster categorical layers** (e.g. LANDFIRE land cover): legend class
  names from `layer_summary(layer_id).classes[].name` — the drape re-renders
  from the value grid keeping only those classes, in their legend colors;
- **the GAP species-richness grid**: species common-names (default
  `field="species"`) from `layer_summary(...).filterable_species` or
  `identify_at` — the viewer paints an orange habitat mask over the cells
  where any chosen species has modeled habitat. *"Where could I find wild
  turkey?"* → `filter_layer("gap_species_richness", ["Wild Turkey"])` and the
  map lights up exactly that range;
- **vector layers**: the distinct values of `field` (default the feature
  label `__label`) from `layer_summary(layer_id).labels` / `.attribute_fields`.

Matching is case-insensitive; the result reports `matched_values` and any
`unmatched_values` so the agent can correct a name. Mechanics mirror the
drawings: directives are written into the same `<data>/annotations.json`
(under `layer_views`), polled by `public/annotations.js`, and applied by
`public/app.js` (which owns the drape) — moving the layer toggles and drape
filters to match. They are **edge-triggered**: between directive changes the
user's own manual toggles win, and a manual toggle reclaims a layer from the
agent. `reset_layer_views` drops every override (the **Clear drawings**
button leaves layer views untouched — they have their own reset). Layer
control, like drawings, never touches the store or the journal.

## Example session

Natural-language questions and the tool calls they become:

1. **"How tall are the trees right around this building?"**
   `find_entities(kind="building_model", attr_filters=["name = Workshop"])` →
   the building's position and entity id, then
   `find_entities(kind="tree", near={"entity_id":"building_model:W-1"}, within_m=50, attr_filters=["height > 20"])`
   → the matching trees, nearest-first, each with height, species (if the
   active pack supplies any), source, and confidence.

2. **"What's the land cover and habitat at this spot?"** (a point in
   scene-local meters, or `{lat,lon}`)
   `identify_at(point={"x":50,"y":100})` → every atlas fact true there:
   raster values with legend names (e.g. the LANDFIRE community, NLCD land
   cover), the vector features containing the point (soils, geology — whatever
   the twin's atlas holds), and species-habitat grids if present.

3. **"Summarize this stand."** (a polygon from the viewer's draw tool, in
   scene-local meters or a lon/lat ring)
   `summarize_region(region={"polygon":[[-50,22],[113,22],[113,190],[-50,190]]})`
   → region area, tree count + type split + mean/max height, dominant
   land-cover community and its share, vector features covering it — each
   fact with provenance.

4. **"When did canopy density change here?"**
   `canopy_change(region={"polygon": …})` → tree count + crown area per
   pipeline run, exposing clearance passes and rebuilds over time.

5. **"What data does this twin actually have?"**
   `describe_twin()` + `list_layers()` → entity kinds and counts, the layer
   catalog with acquisition provenance, extent and CRS — the right first
   calls in any session.

6. **"What did the field survey find here?"**
   `list_survey_layers()` → the uploaded layers and their `survey_*` kinds,
   then `find_entities(kind="survey_observations", region={…})` or
   `identify_at(point=…)` for the features (with photos) at a spot.

## Survey companion

Field uploads (QField, docs/survey.md) land as `survey_*` store entities and
were already queryable via `find_entities` / `summarize_region` /
`aggregate_entities`. Two additions complete the exposure:

- `list_survey_layers()` is the discovery call — the uploaded layers, their
  store kinds, geometry types, feature counts, attribute fields, and whether
  photos are attached. Empty (with a note) until something is uploaded.
- `identify_at(point)` now includes survey features (polygons by containment,
  lines and points within 8 m), with each feature's photo and status — the
  click-to-identify gap is closed.

## The viewer chat panel

The viewer ships an "Ask the land" panel (above the coordinate readout) that
exposes this MCP server through an LLM: `POST /api/chat` in `server.js`
spawns `scripts/mcp_server.py` once, speaks MCP over stdio, and hands the
tool catalog to OpenAI `gpt-5.5` (Responses API function calling; override
with `OPENAI_MODEL` / `OPENAI_REASONING_EFFORT`). The key comes from
`OPENAI_API_KEY` or the gitignored `.openai_key` file.

Three question scopes:

- **Whole land** (default) — the model calls tools as it pleases.
- **Drawn region** — "Draw region", click 3+ points on the terrain, finish;
  the polygon goes to the model with a pre-loaded `summarize_region`, and
  spatial tool calls are scoped to it. "Clear" removes it.
- **Picked point** — "Pick point", click the terrain (same raycast as the
  GPS readout); the model gets `identify_at` plus `summarize_region` within
  100 m as pre-loaded context.

The transcript shows each tool call the model made (⚙ lines), so every
answer is auditable against the store. When the model draws on the map
mid-answer (draw_polygon / draw_point), the shapes appear in orange as soon
as the reply lands; **Clear drawings** removes them.

## Phase boundaries

No store-mutating tools (drawings and layer views touch only
`annotations.json`), no
sensors/actuators, no HTTP transport, no auth — later phases. The MCP
server reads `data/twin.gpkg` (plus the atlas files under
`data/atlas/local/` and terrain grids) directly via `scripts/twin_store.py`;
the Node viewer server is involved only in serving/clearing the annotations
file for the browser.
