# MCP server over the twin store

`scripts/mcp_server.py` exposes the twin store (`data/twin.gpkg`) to LLM
agents over MCP (stdio). It is **read-only**: every tool queries, none
mutate. All logic lives in `scripts/twin_query.py` (tested by
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
| `identify_at(point)` | Everything true at a point (the viewer's click-to-identify) — atlas layers only; survey features (`survey_*` kinds, docs/survey.md) are reachable via `find_entities`/`summarize_region` but not point-identify yet |
| `sample_raster(layer_id, point)` | One raster value + legend name at a point |
| `list_layers(kind?)` | The atlas/input catalog with acquisition provenance |
| `layer_summary(layer_id)` | Fields/labels of a vector; legend + class shares of a raster |
| `summarize_region(region)` | "What's happening inside this shape?" — the headline call |
| `aggregate_entities(kind, metric, group_by?, where?, region?)` | Counts, mean height, crown area, splits |
| `canopy_change(region?, member?)` | "When did canopy density change here?" — per-run history |

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
answer is auditable against the store.

## Phase boundaries

No write tools, no sensors/actuators, no HTTP transport, no auth — later
phases. The Node viewer server (`server.js`) is untouched and the viewer
needs none of this; the MCP server reads `data/twin.gpkg` (plus the atlas
files under `data/atlas/local/` and terrain grids) directly via
`scripts/twin_store.py`.
