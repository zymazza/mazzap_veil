#!/usr/bin/env python3
"""Build the QField survey package — the field-side half of the Survey
companion (docs/survey.md).

Produces data/surveys/package/: a QGIS project (project.qgs) over an empty
survey.gpkg with the fixed v1 survey schema (trails, stream_centerlines,
photo_points, observations — see SURVEY_LAYERS), an aerial basemap GeoTIFF
when the twin has georeferenced imagery, and an empty DCIM/ for photos. The
whole folder is zipped to survey-package.zip, downloadable from the viewer
at /data/surveys/package/survey-package.zip for sideloading into QField.

The .qgs bakes the survey form behaviors in: hidden auto-uuid (the natural
key scripts/ingest_survey.py builds entity IDs from), a status value map
(active/retired/removed — retirement is an explicit field act, never
inferred), captured_at defaulting to now(), accuracy_m defaulting to
QField's @position_horizontal_accuracy, and a camera attachment widget on
the point layers. Everything CRS-related comes from data/georef.json — no
region knowledge here.

Usage:
  python3 scripts/build_survey_package.py [--data-dir DIR] [--name LABEL]
"""

import argparse
import json
import os
import sys
import zipfile
from xml.sax.saxutils import escape

from osgeo import gdal, ogr, osr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_georef
import twin_store

gdal.UseExceptions()
ogr.UseExceptions()

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The fixed v1 survey schema. ingest_survey.py consumes exactly these layers;
# change them in both places (and bump the package, not the ingester's
# expectations mid-flight).
COMMON_FIELDS = ("uuid", "name", "status", "notes", "captured_at", "accuracy_m")
SURVEY_LAYERS = [
    {"name": "trails", "geom": ogr.wkbLineString, "wkb": "LineString",
     "kind": "Line", "photo": False, "color": "224,168,75,255"},
    {"name": "stream_centerlines", "geom": ogr.wkbLineString, "wkb": "LineString",
     "kind": "Line", "photo": False, "color": "78,168,222,255"},
    {"name": "photo_points", "geom": ogr.wkbPoint, "wkb": "Point",
     "kind": "Point", "photo": True, "color": "242,95,92,255"},
    {"name": "observations", "geom": ogr.wkbPoint, "wkb": "Point",
     "kind": "Point", "photo": True, "color": "111,207,151,255"},
]


def _resolve_dirs(data_dir):
    data = os.path.abspath(data_dir or os.environ.get("TWIN_DATA_DIR")
                           or os.path.join(PROJECT, "data"))
    return data, os.path.join(data, "surveys", "package")


# ------------------------------------------------------------------ survey.gpkg

def create_survey_gpkg(path, epsg):
    if os.path.exists(path):
        os.remove(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    for spec in SURVEY_LAYERS:
        lyr = ds.CreateLayer(spec["name"], srs=srs, geom_type=spec["geom"])
        for fname in COMMON_FIELDS:
            if fname == "captured_at":
                f = ogr.FieldDefn(fname, ogr.OFTDateTime)
            elif fname == "accuracy_m":
                f = ogr.FieldDefn(fname, ogr.OFTReal)
            else:
                f = ogr.FieldDefn(fname, ogr.OFTString)
            lyr.CreateField(f)
        if spec["photo"]:
            lyr.CreateField(ogr.FieldDefn("photo", ogr.OFTString))
    ds = None


# -------------------------------------------------------------------- basemap

def build_basemap(data_dir, out_tif, epsg, extent_abs):
    """Georeference the twin's aerial PNG into a GeoTIFF QField can drape.
    The imagery is aligned to the grid's outer footprint by invariant
    (ingest_imagery.py / docs/grid-contract.md), so its bounds are the
    grid extent in the twin's projected CRS — no separate metadata needed.
    Optional: a twin without imagery just gets a package without a basemap."""
    png = os.path.join(data_dir, "imagery", "naip_rgb.png")
    if not os.path.exists(png):
        return False
    x0, y0, x1, y1 = extent_abs
    gdal.Translate(out_tif, png,
                   outputBounds=[x0, y1, x1, y0],  # ulx uly lrx lry
                   outputSRS=f"EPSG:{epsg}",
                   creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    return True


# ---------------------------------------------------------------- project.qgs

def srs_xml(epsg, proj4):
    """The exact shape QGIS itself writes (verified against a pyqgis-written
    project): nativeFormat="Wkt" with a WKT2 definition and nothing else.
    Extra children (srsid/authid/proj4) make the reader bail to an invalid CRS."""
    del proj4  # carried in the WKT2 definition
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    wkt2 = srs.ExportToWkt(["FORMAT=WKT2_2019"])
    return f'<spatialrefsys nativeFormat="Wkt"><wkt>{escape(wkt2)}</wkt></spatialrefsys>'


def _widget(field, spec):
    """The editWidget block per field — this is where the QField form UX lives."""
    if field == "uuid":
        return '<editWidget type="Hidden"><config><Option/></config></editWidget>'
    if field == "status":
        opts = "".join(
            f'<Option type="Map"><Option type="QString" value="{v}" name="{label}"/></Option>'
            for label, v in (("Active", "active"), ("Retired", "retired"),
                             ("Removed", "removed")))
        return ('<editWidget type="ValueMap"><config><Option type="Map">'
                f'<Option type="List" name="map">{opts}</Option>'
                "</Option></config></editWidget>")
    if field == "captured_at":
        return ('<editWidget type="DateTime"><config><Option type="Map">'
                '<Option type="bool" value="true" name="allow_null"/>'
                '<Option type="bool" value="false" name="calendar_popup"/>'
                '<Option type="QString" value="yyyy-MM-dd HH:mm:ss" name="display_format"/>'
                '<Option type="QString" value="yyyy-MM-dd HH:mm:ss" name="field_format"/>'
                '<Option type="bool" value="false" name="field_iso_format"/>'
                "</Option></config></editWidget>")
    if field == "photo":
        return ('<editWidget type="ExternalResource"><config><Option type="Map">'
                '<Option type="int" value="1" name="DocumentViewer"/>'
                '<Option type="int" value="0" name="DocumentViewerHeight"/>'
                '<Option type="int" value="0" name="DocumentViewerWidth"/>'
                '<Option type="bool" value="true" name="FileWidget"/>'
                '<Option type="bool" value="true" name="FileWidgetButton"/>'
                '<Option type="QString" value="" name="FileWidgetFilter"/>'
                '<Option type="int" value="1" name="RelativeStorage"/>'
                '<Option type="int" value="0" name="StorageMode"/>'
                "</Option></config></editWidget>")
    multiline = "true" if field == "notes" else "false"
    return ('<editWidget type="TextEdit"><config><Option type="Map">'
            f'<Option type="bool" value="{multiline}" name="IsMultiline"/>'
            '<Option type="bool" value="false" name="UseHtml"/>'
            "</Option></config></editWidget>")


def _renderer(spec):
    if spec["kind"] == "Line":
        props = (f'<Option type="QString" value="{spec["color"]}" name="line_color"/>'
                 '<Option type="QString" value="solid" name="line_style"/>'
                 '<Option type="QString" value="0.8" name="line_width"/>'
                 '<Option type="QString" value="MM" name="line_width_unit"/>')
        sym_type, cls = "line", "SimpleLine"
    else:
        props = (f'<Option type="QString" value="{spec["color"]}" name="color"/>'
                 '<Option type="QString" value="circle" name="name"/>'
                 '<Option type="QString" value="255,255,255,255" name="outline_color"/>'
                 '<Option type="QString" value="0.4" name="outline_width"/>'
                 '<Option type="QString" value="MM" name="outline_width_unit"/>'
                 '<Option type="QString" value="3" name="size"/>'
                 '<Option type="QString" value="MM" name="size_unit"/>')
        sym_type, cls = "marker", "SimpleMarker"
    return (f'<renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0"'
            f' forceraster="0" referencescale="-1"><symbols>'
            f'<symbol type="{sym_type}" name="0" alpha="1" clip_to_extent="1"'
            f' force_rhr="0" frame_rate="10" is_animated="0">'
            f'<layer class="{cls}" enabled="1" locked="0" pass="0">'
            f'<Option type="Map">{props}</Option>'
            f"</layer></symbol></symbols><rotation/><sizescale/></renderer-v2>")


def vector_maplayer_xml(spec, srs_block, extent_xml):
    name = spec["name"]
    fields = list(COMMON_FIELDS) + (["photo"] if spec["photo"] else [])
    field_cfg = "".join(
        f'<field name="{f}" configurationFlags="NoFlag">{_widget(f, spec)}</field>'
        for f in fields)
    # The baked-in form defaults: auto uuid (entity identity), active status,
    # capture time, and QField's live GPS accuracy (NULL on desktop).
    defaults = ("<default expression=\"regexp_replace(uuid(),'[{}]','')\""
                ' applyOnUpdate="0" field="uuid"/>'
                '<default expression="\'active\'" applyOnUpdate="0" field="status"/>'
                '<default expression="now()" applyOnUpdate="0" field="captured_at"/>'
                '<default expression="@position_horizontal_accuracy"'
                ' applyOnUpdate="0" field="accuracy_m"/>')
    return f"""<maplayer type="vector" geometry="{spec["kind"]}" wkbType="{spec["wkb"]}" autoRefreshEnabled="0" autoRefreshTime="0" readOnly="0" refreshOnNotifyEnabled="0" simplifyDrawingHints="1" simplifyAlgorithm="0" simplifyDrawingTol="1" simplifyLocal="1" simplifyMaxScale="1" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="100000000" styleCategories="AllStyleCategories" legendPlaceholderImage="">
    {extent_xml}
    <id>{name}_survey</id>
    <datasource>./survey.gpkg|layername={name}</datasource>
    <layername>{name}</layername>
    <srs>{srs_block}</srs>
    <provider encoding="UTF-8">ogr</provider>
    {_renderer(spec)}
    <fieldConfiguration>{field_cfg}</fieldConfiguration>
    <aliases/>
    <defaults>{defaults}</defaults>
    <constraints/>
    <constraintExpressions/>
    <expressionfields/>
    <attributeactions/>
    <editform tolerant="1"></editform>
    <editforminit/>
    <featformsuppress>0</featformsuppress>
    <editorlayout>generatedlayout</editorlayout>
  </maplayer>"""


def raster_maplayer_xml(srs_block, extent_xml):
    return f"""<maplayer type="raster" autoRefreshEnabled="0" autoRefreshTime="0" refreshOnNotifyEnabled="0" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories" legendPlaceholderImage="">
    {extent_xml}
    <id>basemap_survey</id>
    <datasource>./basemap.tif</datasource>
    <layername>basemap</layername>
    <srs>{srs_block}</srs>
    <provider>gdal</provider>
    <pipe>
      <provider><resampling enabled="false" zoomedInResamplingMethod="nearestNeighbour" zoomedOutResamplingMethod="nearestNeighbour" maxOversampling="2"/></provider>
      <rasterrenderer type="multibandcolor" opacity="1" alphaBand="-1" redBand="1" greenBand="2" blueBand="3" nodataColor="">
        <rasterTransparency/>
        <minMaxOrigin><limits>None</limits><extent>WholeRaster</extent><statAccuracy>Estimated</statAccuracy><cumulativeCutLower>0.02</cumulativeCutLower><cumulativeCutUpper>0.98</cumulativeCutUpper><stdDevFactor>2</stdDevFactor></minMaxOrigin>
      </rasterrenderer>
      <brightnesscontrast brightness="0" gamma="1" contrast="0"/>
      <huesaturation colorizeBlue="128" colorizeGreen="128" colorizeOn="0" colorizeRed="255" colorizeStrength="100" grayscaleMode="0" invertColors="0" saturation="0"/>
      <rasterresampler maxOversampling="2"/>
      <resamplingStage>resamplingFilter</resamplingStage>
    </pipe>
  </maplayer>"""


def build_qgs(out_path, title, epsg, proj4, extent_abs, with_basemap):
    srs_block = srs_xml(epsg, proj4)
    x0, y0, x1, y1 = extent_abs
    extent_xml = (f"<extent><xmin>{x0}</xmin><ymin>{y0}</ymin>"
                  f"<xmax>{x1}</xmax><ymax>{y1}</ymax></extent>")
    tree, layers, order = [], [], []
    for spec in SURVEY_LAYERS:
        lid, name = f'{spec["name"]}_survey', spec["name"]
        tree.append(
            f'<layer-tree-layer expanded="1" legend_exp="" legend_split_behavior="0"'
            f' providerKey="ogr" checked="Qt::Checked" patch_size="-1,-1" id="{lid}"'
            f' source="./survey.gpkg|layername={name}" name="{name}">'
            f"<customproperties><Option/></customproperties></layer-tree-layer>")
        layers.append(vector_maplayer_xml(spec, srs_block, extent_xml))
        order.append(f'<layer id="{lid}"/>')
    if with_basemap:
        tree.append(
            '<layer-tree-layer expanded="0" legend_exp="" legend_split_behavior="0"'
            ' providerKey="gdal" checked="Qt::Checked" patch_size="-1,-1"'
            ' id="basemap_survey" source="./basemap.tif" name="basemap">'
            "<customproperties><Option/></customproperties></layer-tree-layer>")
        layers.append(raster_maplayer_xml(srs_block, extent_xml))
        order.append('<layer id="basemap_survey"/>')

    doc = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.40.0" projectname="{escape(title)}" saveUser="" saveUserFull="">
  <homePath path=""/>
  <title>{escape(title)}</title>
  <transaction mode="Disabled"/>
  <projectFlags set=""/>
  <projectCrs>{srs_block}</projectCrs>
  <layer-tree-group>
    <customproperties><Option/></customproperties>
    {"".join(tree)}
  </layer-tree-group>
  <mapcanvas annotationsVisible="1" name="theMapCanvas">
    <units>meters</units>
    {extent_xml}
    <rotation>0</rotation>
    <destinationsrs>{srs_block}</destinationsrs>
    <rendermaptile>0</rendermaptile>
  </mapcanvas>
  <projectlayers>{"".join(layers)}</projectlayers>
  <layerorder>{"".join(order)}</layerorder>
  <properties>
    <Paths><Absolute type="bool">false</Absolute></Paths>
    <!-- without ProjectionsEnabled QGIS never reads the projectCrs node -->
    <SpatialRefSys><ProjectionsEnabled type="int">1</ProjectionsEnabled></SpatialRefSys>
  </properties>
  <visibility-presets/>
  <transformContext/>
  <Annotations/>
  <Layouts/>
</qgis>
"""
    with open(out_path, "w") as fh:
        fh.write(doc)


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--data-dir", default=None,
                    help="the twin's data dir (default: ./data or $TWIN_DATA_DIR)")
    ap.add_argument("--name", default=None,
                    help="project title (default: the twin's scene name)")
    args = ap.parse_args()
    data_dir, out_dir = _resolve_dirs(args.data_dir)
    georef = os.path.join(data_dir, "georef.json")
    if not os.path.exists(georef):
        raise SystemExit(f"no georef.json in {data_dir} — not a twin data dir")
    epsg = twin_georef.epsg_number(georef)
    proj4 = twin_georef.proj4_string(path=georef)
    ox, oy = twin_georef.origin(georef)

    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    extent_abs = (grid.get("outerMinX", grid["minX"]) + ox,
                  grid.get("outerMinY", grid["minY"]) + oy,
                  grid.get("outerMaxX", grid["maxX"]) + ox,
                  grid.get("outerMaxY", grid["maxY"]) + oy)

    title = args.name
    if not title:
        scene = os.path.join(data_dir, "scene.json")
        twin_name = (json.load(open(scene)).get("name") if os.path.exists(scene)
                     else None)
        title = f"{twin_name or 'Twin'} survey"

    os.makedirs(out_dir, exist_ok=True)
    create_survey_gpkg(os.path.join(out_dir, "survey.gpkg"), epsg)
    with_basemap = build_basemap(data_dir, os.path.join(out_dir, "basemap.tif"),
                                 epsg, extent_abs)
    if not with_basemap:
        print("no imagery (data/imagery/naip_rgb.png) — package built"
              " without a basemap")
    build_qgs(os.path.join(out_dir, "project.qgs"), title, epsg, proj4,
              extent_abs, with_basemap)
    dcim = os.path.join(out_dir, "DCIM")
    os.makedirs(dcim, exist_ok=True)
    keep = os.path.join(dcim, "README.txt")
    if not os.path.exists(keep):
        with open(keep, "w") as fh:
            fh.write("QField saves survey photos here.\n")

    zip_path = os.path.join(out_dir, "survey-package.zip")
    members = ["project.qgs", "survey.gpkg", "DCIM/README.txt"]
    if with_basemap:
        members.insert(2, "basemap.tif")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in members:
            zf.write(os.path.join(out_dir, m), arcname=f"survey/{m}")

    # Record the package build in the store (skipped on a bare terrain twin).
    try:
        store_path = os.path.join(data_dir, "twin.gpkg")
        if not os.path.exists(store_path):
            raise FileNotFoundError("no twin store yet")
        store = twin_store.Store(store_path)
        run = store.begin_run("build_survey_package.py",
                              notes=f"survey package: {title}")
        store.set_meta("survey_package", {
            "created_at": twin_store.utcnow(), "title": title,
            "layers": [s["name"] for s in SURVEY_LAYERS],
            "basemap": with_basemap, "epsg": epsg,
        })
        store.finish_run(run)
        store.close()
    except Exception as e:  # noqa: BLE001
        print(f"(store registration skipped: {e})")

    print(f"survey package -> {os.path.relpath(zip_path, PROJECT)}"
          f" ({len(SURVEY_LAYERS)} layers, basemap={'yes' if with_basemap else 'no'})")


if __name__ == "__main__":
    main()
