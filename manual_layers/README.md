# Manual/downloadable VEIL layers

Drop user-downloaded geospatial files in this directory, then ingest them into a
twin with:

```bash
npm run ingest-manual-layers -- --data-dir ./twins/mine/data
```

Supported inputs are whatever GDAL/OGR can read, including GeoTIFF, GeoJSON,
Shapefile (`.shp` plus its sidecars), GeoPackage, KML/KMZ, GPX, FileGDB
directories, and many zip archives. Multi-layer files can be ingested with:

```bash
npm run ingest-manual-layers -- --data-dir ./twins/mine/data --all-layers
```

VEIL clips each layer to the target twin's terrain footprint, reprojects it,
styles it, and registers it in the viewer.
