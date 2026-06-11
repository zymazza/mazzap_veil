# VEIL — a georeferenced 3D digital-twin engine

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
scripts need Python 3 with GDAL (`osgeo`), numpy, and pyproj.

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
npm test
```

Runs `scripts/twin_query_test.py` against the Flatirons demo twin
(`twins/demo/data`), building it first if it isn't there (needs internet +
GDAL once). The build is deterministic (seeded RNGs), so assertions are
stable across rebuilds. Point `TWIN_DATA_DIR` at another twin to run the
same suite against it.

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

- **`POST /api/chat` is unauthenticated OpenAI spend.** Anyone who can reach
  the port can make the server call the OpenAI API on your key. Don't bind a
  twin with a configured key to an address strangers can reach.
- Survey uploads can be token-gated (a `.survey_token` file at the repo root
  enforces an `X-Survey-Token` header); without the file the route is open.
- There is no TLS, no rate limiting, and no auth on the building-placement
  endpoint — by design, for a single-user posture.
