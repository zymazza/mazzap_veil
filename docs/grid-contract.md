# The terrain grid contract (`data/terrain/grid.json`)

This is the public interface between terrain generation and everything else.
The field set below is **frozen** — `public/viewer/terrain.js`,
`scripts/analyze_vegetation.py`, `scripts/build_surrounding_vegetation.py`,
`scripts/clear_vegetation_around_buildings.py` and `scripts/twin_query.py`
all consume it. If a change here ever seems necessary, stop and flag it; do
not silently extend or reinterpret the interface.

## Required fields

| field | meaning |
|---|---|
| `width`, `height` | grid dimensions in cells (vertices) |
| `heights` | flat **row-major** array, `width*height` long, `heights[row*width + col]`; **row 0 is the northern edge** (`maxY`); values are absolute elevation in meters, `null` for nodata |
| `minX`, `maxX`, `minY`, `maxY` | **cell-center** bounds in scene-local meters: vertex `(col,row)` sits at `(minX + col*xStep, maxY - row*yStep)` with `xStep = (maxX-minX)/(width-1)` |
| `outerMinX`, `outerMaxX`, `outerMinY`, `outerMaxY` | the **cell-edge footprint**: inner bounds extended by half a cell on each side. Imagery and atlas drapes are aligned to this rectangle |
| `minElevation`, `maxElevation` | `minElevation` is the **scene's vertical datum** (`world.y = elevation − minElevation`). For the primary grid it equals min over valid cells (and `maxElevation` the max); companion grids rendered in the same scene (e.g. `grid.apron.json`) **inherit the primary grid's datum** so their meshes stack correctly — their own height range may exceed it |

`xStep`, `yStep` (the cell size in meters) and `source` are conventional
extras — consumers may not rely on anything beyond the table above
(terrain.js recomputes the steps from the bounds).

## Semantics that are easy to get wrong

- **Half-cell offset.** The DEM is sampled at *cell centers* (that's
  `minX..maxX`), while imagery/drape rasters cover the *cell-edge footprint*
  (`outerMinX..outerMaxX`). terrain.js compensates with a half-pixel UV
  offset; `ingest_dem.py` derives `inner = outer + cellSize/2`. Never set
  the two bound pairs equal.
- **Nodata shapes the mesh.** terrain.js only triangulates where all three
  vertices are non-null — masking `heights` to the AOI polygon is what gives
  the terrain its parcel shape. `analyze_vegetation.py` uses the same
  null-check to keep synthetic stems on valid terrain.
- **Imagery alignment is load-bearing.** `analyze_vegetation.py` maps scene
  coordinates to image pixels assuming the imagery covers *exactly*
  `outerMinX..outerMaxX / outerMinY..outerMaxY`. `ingest_imagery.py`
  preserves this (and writes integer pixels-per-meter); if imagery is ever
  produced another way, keep the footprint identical.

## Validating a grid

`scripts/ingest_dem.py` is the genesis path for new twins and emits this
shape; `scripts/ingest_dem.py --validate <grid.json>` checks any grid
against the contract, and `scripts/twin_query_test.py` validates the test
twin's grid on every run.
