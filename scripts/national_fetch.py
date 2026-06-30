"""Live-fetch national US base layers (terrain + imagery) for an AOI.

These are the CONUS-wide products every US twin can pull on demand, so nothing
big ships in the repo. Each is a USGS ArcGIS ImageServer exportImage call:

  * 3DEP elevation  (1 m where flown, else 1/3 arc-sec) -> a DEM GeoTIFF
  * NAIP Plus ortho (~0.6-1 m, RGB+NIR where available) -> an aerial GeoTIFF

LANDFIRE EVT (vegetation/land-cover) is fetched by the us-national pack
(packs/us-national/fetch_landfire.py). gSSURGO / NLCD / GAP follow the same
exportImage pattern and can be added here as more national sources.
"""

import os
import urllib.parse
import urllib.request

DEP_ELEV = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
            "3DEPElevation/ImageServer/exportImage")
NAIP_PLUS = ("https://imagery.nationalmap.gov/arcgis/rest/services/"
             "USGSNAIPPlus/ImageServer/exportImage")
UA = {"User-Agent": "veil/1.0"}


def _export(service, bbox, sr, w, h, out_path, pixel_type="F32",
            fmt="tiff", interpolation="RSP_BilinearInterpolation"):
    params = {
        "bbox": "%f,%f,%f,%f" % tuple(bbox),
        "bboxSR": str(sr), "imageSR": str(sr), "size": "%d,%d" % (w, h),
        "format": fmt, "pixelType": pixel_type,
        "interpolation": interpolation, "f": "image",
    }
    url = service + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path


def fetch_3dep_dem(bbox, sr, out_path, resolution_m=1.0):
    """Fetch a 3DEP DEM covering bbox (in CRS `sr`) at ~resolution_m."""
    w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
    h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    # cap request size so huge AOIs don't ask for gigapixel rasters
    while w * h > 4_000_000:
        resolution_m *= 1.5
        w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
        h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    return _export(DEP_ELEV, bbox, sr, w, h, out_path, pixel_type="F32"), resolution_m


def fetch_naip(bbox, sr, out_path, resolution_m=1.0):
    """Fetch USGS NAIP Plus orthoimagery covering bbox at ~resolution_m.

    The service is NAIP plus high-resolution orthoimagery (HRO) in gaps. Where
    source imagery has NIR, ingest_imagery.py uses it for false color and NDVI.
    """
    w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
    h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    while w * h > 16_000_000:
        resolution_m *= 1.5
        w = max(2, round((bbox[2] - bbox[0]) / resolution_m))
        h = max(2, round((bbox[3] - bbox[1]) / resolution_m))
    return _export(NAIP_PLUS, bbox, sr, w, h, out_path, pixel_type="U8",
                   interpolation="RSP_BilinearInterpolation")
