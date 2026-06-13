# VEIL — a georeferenced 3D digital-twin engine



https://github.com/user-attachments/assets/5f383d21-384b-4097-9977-d754a1557969



VEIL turns a patch of real ground into a standalone, fully **georeferenced** 3D
digital twin you can open in a browser, click to read true GPS coordinates, drape
your own map layers onto the terrain, and ask questions about in natural language.

No database, no cloud, no build step at view time: one tiny zero-dependency Node
static server serves a Three.js viewer over a self-contained bundle of geospatial
data. Nothing is fetched from the network when you view it.

```bash
npm start          # -> http://127.0.0.1:4173
```

Requires Node ≥ 18 (the server uses the built-in `fetch`); the data pipeline
scripts need Python 3 with GDAL (`osgeo`), numpy, pyproj, and Pillow. The
MCP/chat path also needs the Python `mcp` SDK from `requirements.txt`. If you'd
rather not assemble that toolchain yourself, **[run it in a container](#run-with-docker)** —
GDAL, numpy, Node, and the rest come pinned and pre-built.

Point it at **your own** DEM and imagery (see "Build your own twin" below) — the
engine is region-agnostic. The coordinate system, the vegetation knowledge, the
map-layer styling, and any source-acquisition scripts all live in data and in an
optional **regional pack**, never hardcoded in the engine.

## What you get

- **3D terrain** from any DEM (a LiDAR DTM, USGS 3DEP, a national survey grid),
  rendered as a lit mesh with selectable surface modes (aerial / false-color /
  hillshade / elevation).
- **Aerial imagery** draped on the terrain, aligned to the grid so it conforms
  to the topography instead of floating.
- **Click-to-identify + GPS readout** — click anywhere to pin the real-world
  lat/lon (validated against proj4js), elevation, and every map feature true at
  that spot.
- **Atlas layers** — import any vector or raster geospatial file and it is
  reprojected to the scene, clipped to the terrain footprint, auto-styled, and
  made clickable. Bring soils, wetlands, zoning, habitat, hydrology, land cover —
  whatever you have.
- **Vegetation** — a capability ladder that derives trees from the best signal
  available (LiDAR segmentation stems, a canopy-height model, or an NDVI canopy),
  rendered as low-LOD 3D forms. Anywhere in the continental US, the bundled
  **`us-national`** pack types them evergreen/deciduous and names their
  community from LANDFIRE — no regional setup (see "Vegetation" under
  [docs/make-a-twin.md](docs/make-a-twin.md)).
- **"Ask the land" chat** — an in-viewer panel that talks to an LLM wired to the
  twin's read-only query tools (the MCP server), scoped to the whole twin, a
  polygon you draw, or a point you pick. See [docs/mcp.md](docs/mcp.md).
- **QField survey companion** — generate a QField project from the active twin,
  collect trails / stream centerlines / photo points / observations in the
  field, then upload the zipped project folder back through the viewer. Uploads
  become journaled `survey_*` entities, rendered as survey layers and queryable
  through MCP. See [docs/survey.md](docs/survey.md).

## Run with Docker

The static server has **zero npm dependencies**, but the data pipeline needs
Python with GDAL, numpy, pyproj, and Pillow — fiddly to assemble by hand because
GDAL's Python (`osgeo`) bindings must match the system GDAL. The container does
it for you: one image with Node and the whole Python pipeline, all versions
pinned (GDAL via `GDAL_VERSION`, the pip deps in `requirements.txt`, Node via
`NODE_MAJOR`). It's built on the official [OSGeo GDAL](https://github.com/OSGeo/gdal)
image, so GDAL + bindings + numpy already line up.

```bash
docker compose up --build                     # build + serve ./data at http://127.0.0.1:4173

# the same image runs every pipeline step — twins persist to ./twins on the host:
docker compose run --rm veil npm run demo     # build the Flatirons demo twin
TWIN_DATA_DIR=/app/twins/demo/data docker compose up   # serve it

# build your own twin (see "Build your own twin" below for the commands):
docker compose run --rm -e TWIN_DATA_DIR=/app/twins/mine/data \
  veil npm run build-from-aoi -- --aoi /app/twins/mine/my_area.shp --name "My Place"
```

`./data` and `./twins` are bind-mounted, so anything the pipeline builds inside
the container lands on the host and stays private/gitignored. For the **"Ask the
land"** chat panel, pass `OPENAI_API_KEY` (env var or a `.env` file) — see the
chat section below. For an editable dev environment with the same pinned
toolchain, open the repo in a **Dev Container** (`.devcontainer/`) in VS Code or
any devcontainer-aware editor.

Prefer a local toolchain? Everything below works the same with `npm` and
`python3` directly.

## Build your own twin

**The repo ships only code** (plus one ~600-byte demo AOI). You either fetch
national data live for your area, or bring your own files. One checkout hosts
many twins — point `--data-dir` / `TWIN_DATA_DIR` at a folder and your other
twins are untouched. Full walkthrough: [docs/make-a-twin.md](docs/make-a-twin.md).

**US, fetch everything from one AOI** — hand it a small AOI polygon (shapefile,
GeoJSON, GeoPackage…) and it queries 3DEP elevation, NAIP imagery, and LANDFIRE
land cover/vegetation for that footprint and builds a complete, typed twin:

```bash
npm run build-from-aoi -- --aoi my_area.shp --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data PORT=4174 npm start    # -> http://127.0.0.1:4174

# the bundled demo is exactly this, from one committed shapefile:
npm run demo && npm run serve-demo
```

**Bring your own data** — anywhere, any format. Drop files in and ingest them:

```bash
# terrain + georeferencing from any DEM (the CRS is the DEM's, or the AOI's UTM zone)
npm run ingest-dem -- mydem.tif --aoi boundary.geojson --data-dir ./twins/mine/data
# aerial imagery, aligned to the terrain footprint
npm run ingest-imagery -- myaerial.tif --data-dir ./twins/mine/data
# any vector/raster layer, any format, any CRS
npm run add-layer -- soils.shp     --id soils    --label "Soils"   --data-dir ./twins/mine/data
npm run add-layer -- wetlands.gpkg --id wetlands --layer NWI       --data-dir ./twins/mine/data
npm run add-layer -- landcover.tif --id landcover                  --data-dir ./twins/mine/data
```

`add_layer` accepts anything GDAL/OGR can read — GeoJSON, Shapefile (`.shp`),
GeoPackage (`.gpkg`), KML/KMZ, GPX, CSV with coordinates, File Geodatabase,
GeoTIFF and other rasters — in any coordinate system. Multi-layer sources take a
`--layer NAME` selector. National layers (LANDFIRE today; NLCD/gSSURGO/GAP follow
the same exportImage pattern in `scripts/national_fetch.py` /
`packs/us-national/`) fetch straight into a twin and register themselves as
draped, clickable atlas layers.

The terrain grid that `ingest-dem` writes conforms to a frozen interface — see
[docs/grid-contract.md](docs/grid-contract.md). It is the one contract every
terrain consumer depends on; don't change it silently.

## QField survey companion

VEIL has a built-in field loop for QField: the app generates the survey package
from the current twin, QField records edits against that package, and the viewer
uploads the finished package back into the twin store.

Build a package for the twin you are serving:

```bash
npm run build-survey-package -- --data-dir ./twins/mine/data --name "My Place"
TWIN_DATA_DIR=./twins/mine/data npm start
```

Then open the viewer's **Survey companion** panel and download
`survey-package.zip`. Unzip or sideload the `survey/` project folder into
QField and open `project.qgs`. The generated package contains:

- `survey.gpkg` with four editable layers: `trails`, `stream_centerlines`,
  `photo_points`, and `observations`.
- `project.qgs`, with forms wired for stable UUIDs, active / retired / removed
  status, capture time, GPS accuracy, notes, and camera attachments.
- `basemap.tif` when the twin has georeferenced imagery.

After fieldwork, zip the whole QField project folder, including its `DCIM/`
photos, and upload it from the same **Survey companion** panel. The server saves
the raw zip under `<data>/surveys/incoming/`, logs it in
`<data>/surveys/uploads.log.jsonl`, runs `scripts/ingest_survey.py --pending`,
and refreshes the viewer's survey layers. If Python ingest is unavailable, the
upload is kept and `npm run export` will process pending uploads later.

Survey features are stored as ordinary twin-store entities with stable IDs like
`survey_trails:<uuid>`. Re-uploading the same project is safe: unchanged
features are skipped, moved features keep identity, and retirement is explicit
through the `status` field rather than inferred from a missing feature. To gate
uploads on a shared LAN, create a gitignored `.survey_token`; the viewer prompts
for the token and sends it as `X-Survey-Token`.

Full details: [docs/survey.md](docs/survey.md).

## MCP server and app chat

`scripts/mcp_server.py` is the MCP surface over the twin store. It exposes
the same facts the viewer uses — terrain, entities, atlas layers, survey
layers, provenance, and region summaries — as structured tools for LLM
agents. The store itself is read-only; the only write tools are
`draw_polygon` / `draw_point` / `clear_drawings`, which let the LLM put
labeled orange shapes on the live 3D map (a flat `annotations.json` the
viewer polls — never the store). It reads the active twin from
`TWIN_DATA_DIR` or `./data`.

Install the Python side once:

```bash
pip install -r requirements.txt
TWIN_DATA_DIR=./twins/mine/data npm run rebuild-store      # only if twin.gpkg is missing/stale
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py describe_twin
```

You do **not** start the MCP server separately for the browser app. Open **Ask
the land**, and the Node server lazily spawns `scripts/mcp_server.py` on the
first chat request. Provide an OpenAI key either way:

```bash
# Bring-your-own-key (recommended, esp. for a shared/public twin): start with no
# key, then click "Key" in the chat panel and paste yours. It is stored only in
# your browser (localStorage) and sent per request as X-OpenAI-Key — never on the
# server or in the repo.
TWIN_DATA_DIR=./twins/mine/data npm start

# Server-side key (single-user/local convenience): the server uses this for any
# request that doesn't bring its own.
OPENAI_API_KEY=sk-... TWIN_DATA_DIR=./twins/mine/data npm start
# or put the key in a gitignored .openai_key file

# Public deployment: forbid the server-key fallback so every request must BYOK.
OPENAI_REQUIRE_USER_KEY=1 TWIN_DATA_DIR=./twins/mine/data npm start

# Fully local, no key, nothing leaves the machine: point the chat at an Ollama
# model. `npm run start:local` is the shortcut (CHAT_PROVIDER=ollama,
# OLLAMA_MODEL=gpt-oss:20b); CHAT_PROVIDER=ollama is also inferred whenever
# OLLAMA_MODEL is set. Needs `ollama serve` running with a tool-calling model
# pulled. gpt-oss:20b is a good 24 GB-GPU default (~16 GB on the GPU at the 96k
# default context — its KV cache is cheap; even the full 131072 fits ~17 GB).
TWIN_DATA_DIR=./twins/mine/data npm run start:local
# tune with OLLAMA_HOST (default http://127.0.0.1:11434), OLLAMA_NUM_CTX (default
# 98304), OLLAMA_TEMPERATURE (default 0), OLLAMA_MAX_TOOL_ROUNDS (default 16)
```

In the app, questions can target:

- **Whole land** — the default scope.
- **Drawn region** — click **Draw region**, place 3+ terrain points, then ask
  about "this area".
- **Picked point** — click **Pick point**, choose a terrain point, then ask
  about "here" with nearby context preloaded.

The chat transcript shows the MCP tools the model called, so answers can be
checked against store data and layer provenance.

For an external MCP client, register the stdio server directly:

```bash
claude mcp add veil-twin -- env TWIN_DATA_DIR=/ABS/PATH/TO/twins/mine/data \
  python3 /ABS/PATH/TO/veil/scripts/mcp_server.py
```

Useful direct sanity checks:

```bash
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py describe_twin
TWIN_DATA_DIR=./twins/mine/data python3 scripts/twin_query.py identify_at '{"point":{"x":50,"y":100}}'
npm test
```

Core MCP tools include `describe_twin`, `find_entities`, `get_entity`,
`entity_history`, `identify_at`, `sample_raster`, `list_layers`,
`layer_summary`, `summarize_region`, `aggregate_entities`,
`canopy_change`, and `list_survey_layers`, plus the map-drawing trio
`draw_polygon` / `draw_point` / `clear_drawings` — answers can point at places
with labeled orange shapes in the viewer (built-in chat and external MCP
clients alike; the chat panel's **Clear drawings** button removes them).
Survey uploads appear automatically as `survey_*` kinds for `find_entities`,
`aggregate_entities`, and `summarize_region`, are catalogued by
`list_survey_layers`, and are now included in point `identify_at` (with photo
and status). Full tool semantics and examples: [docs/mcp.md](docs/mcp.md).

## Architecture: engine vs. regional pack

VEIL is a **region-agnostic engine** (`scripts/`, `public/`, `server.js`) plus an
optional **regional content pack** (`packs/<name>/`). Nothing in the engine names
a CRS, a layer, or a species.

- **Coordinates are data.** `<data>/georef.json` carries the projected CRS (EPSG +
  a proj4 string), the geographic CRS, and the scene origin. Python reads it
  through `scripts/twin_georef.py`; the viewer through vendored **proj4js**
  (`public/vendor/proj4.js`) in `public/viewer/georef.js`. Scene coordinates are
  local meters (x = east, y = north) offset from the origin; the store keeps the
  same convention.
- **Packs** load via `scripts/twin_pack.py` (chosen by `TWIN_PACK`, else
  `<data>/pack.txt`, else none). A pack is a folder with `pack.json` and optional
  `load(context)` hook modules: `vegetation.py` (species/community/type knowledge)
  and `layers.py` (atlas styles, attribute enrichment, named raster renderings),
  plus its own source-acquisition scripts. Without a pack the engine auto-styles
  every layer and emits trees with `type:"unknown"` — it never guesses local
  botany.
- **`packs/us-national`** is the region-agnostic exception: it encodes no single
  place, just CONUS-wide LANDFIRE EVT. Any US twin can use it
  (`TWIN_PACK=us-national`) to type vegetation evergreen/deciduous and name
  communities, after fetching LANDFIRE for the twin's footprint
  (`packs/us-national/fetch_landfire.py --data-dir <data>`). A single-region
  pack adds what no national dataset has: curated local species, your own atlas
  styling, regional attribute enrichment. National datasets
  (3DEP/NAIP/NLCD/LANDFIRE/GAP/gSSURGO) belong in `us-national`-style packs,
  distinct from both the engine core and any one region.

```
server.js                 zero-dependency static server (+ /api/chat, /api/* )
public/
  index.html  app.js  chat.js        UI, boot, click-to-identify, chat panel
  viewer/  scene.js terrain.js vegetation.js overlays.js buildings3d.js
           georef.js                 scene-local meters <-> lon/lat (proj4js)
  vendor/  three.min.js  OrbitControls.js  proj4.js
scripts/                  the region-agnostic engine
  twin_georef.py  twin_pack.py        read georef.json / load the active pack
  ingest_dem.py  ingest_imagery.py    genesis: DEM + imagery -> a twin
  add_layer.py                        import any geospatial file as a layer
  analyze_vegetation.py  veg_detect.py   capability-gated vegetation
  build_viewer_layers.py              generic atlas localization + styling
  twin_store.py  migrate_to_store.py  rebuild_store.py   the twin store
  mcp_server.py  twin_query.py        read-only query tools for the chat/agent
packs/<name>/             optional regional content pack
  pack.json  vegetation.py  layers.py   knowledge + styling hooks
  *.py                                  its own source-acquisition scripts
data/  (or any --data-dir)             one twin instance: georef, terrain,
                                       imagery, vegetation, atlas, store
```

## The twin store

The system of record is the **twin store** — an append-only write journal in
`<data>/journal/`, materialized as a GeoPackage at `<data>/twin.gpkg`. The
journal lives inside the twin's data dir (private, gitignored along with the
rest of it); `npm run rebuild-store` reconstructs the GeoPackage from it
exactly. The flat JSON the viewer loads is an *export* of the store. Origin
and CRS live in the store's `meta`; coordinates are scene-local meters. See
`CLAUDE.md` and `docs/mcp.md` for the store model and the query/agent layer.

## Tests

```bash
npm test            # offline: the committed fixture twin
npm run test:demo   # real data: the Flatirons demo twin
```

`npm test` runs `scripts/twin_query_test.py` against a tiny **committed fixture
twin** (`tests/fixtures/mini-twin/data`) — a synthetic, network-free twin built
by `scripts/build_test_fixture.py`. It needs no internet and no GDAL (just
Python + `pyproj`; Node for the proj4js cross-check), so CI runs the full query
suite offline. Every expectation is derived from the twin under test, never
hardcoded to a place.

`npm run test:demo` runs the same suite against the **Flatirons demo twin**
(`twins/demo/data`), building it first from live national services if it isn't
there (needs internet + GDAL once) — the real-data check. Point `TWIN_DATA_DIR`
at any other twin to run the suite against it. Regenerate the fixture with
`npm run build-test-fixture` after a store schema change, then commit it.

## Privacy

A twin's `data/` (and any `--data-dir`) holds real coordinates, parcel/owner
attributes, building footprints, and imagery for a specific place — it is **not**
tracked by git in this repo (see `.gitignore`). Share the engine and your pack;
keep your ground to yourself. If you previously committed a twin and want it gone
from history (not just future commits), rewrite history with `git filter-repo` —
or, safer, start a fresh repo from the current tree, since a rewritten history
can still leave reachable objects in clones and forks.

## Security posture

The server is meant for localhost or a trusted LAN/Tailscale bind, not the
open internet:

- **`POST /api/chat` spends on an OpenAI key.** Each request uses the caller's
  own key if the viewer supplies one (the chat panel's **"Key"** button stores it
  in the browser's `localStorage` and sends it as the `X-OpenAI-Key` header — it
  never touches the repo or the server's disk); otherwise it falls back to the
  server's `OPENAI_API_KEY` / `.openai_key`. That fallback is **unauthenticated
  spend**: anyone who can reach the port can call OpenAI on the server's key. For
  a public deployment set **`OPENAI_REQUIRE_USER_KEY=1`** so the server never uses
  its own key and every request must bring its own.
- Survey uploads can be token-gated (a `.survey_token` file at the repo root
  enforces an `X-Survey-Token` header); without the file the route is open.
- There is no TLS, no rate limiting, and no auth on the building-placement
  endpoint — by design, for a single-user posture.
