#!/usr/bin/env python3
"""Probe and fetch optional national atlas layers for a VEIL twin.

The base AOI builder already fetches terrain, imagery, LiDAR, LANDFIRE,
gSSURGO, and Daymet. This script handles additional national sources that are
useful often enough to offer in the setup UI, but not cheap enough to fetch
blindly for every twin.

Commands:
  probe --aoi AOI.geojson
      Lightweight AOI-intersection check. Vector services use ArcGIS
      returnCountOnly queries against the AOI polygon. Raster services make a
      tiny ImageServer export probe.

  fetch --aoi AOI.geojson --data-dir DATA --layers id,id,...
      Download selected intersecting services and register them as atlas layers
      through scripts/add_layer.py.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import twin_georef  # noqa: E402

gdal.UseExceptions()
ogr.UseExceptions()

UA = {"User-Agent": "veil/1.0 (+national optional layer probe/fetch)"}
MAX_VECTOR_FEATURES = int(os.environ.get("VEIL_NATIONAL_LAYER_MAX_FEATURES", "5000"))
MANUAL_LAYER_DROP_DIR = "manual_layers"
DIRECT_DOWNLOAD_MAX_BYTES = int(os.environ.get(
    "VEIL_NATIONAL_DIRECT_DOWNLOAD_MAX_BYTES",
    os.environ.get("VEIL_NATIONAL_BIG_DOWNLOAD_MAX_BYTES", str(1_000_000_000)),
))
DIRECT_DOWNLOAD_MIN_FREE_BYTES = int(os.environ.get(
    "VEIL_NATIONAL_DIRECT_DOWNLOAD_MIN_FREE_BYTES",
    os.environ.get("VEIL_NATIONAL_BIG_DOWNLOAD_MIN_FREE_BYTES", str(20_000_000_000)),
))
LIGHT_DOWNLOAD_MAX_BYTES = int(os.environ.get("VEIL_NATIONAL_LIGHT_DOWNLOAD_MAX_BYTES", str(100_000_000)))
LARGE_DOWNLOAD_BYTES = int(os.environ.get("VEIL_NATIONAL_LARGE_DOWNLOAD_BYTES", str(1_000_000_000)))
GAP_HABITAT_PARENT_ITEM = "527d0a83e4b0850ea0518326"
GAP_RASTER_RESOLUTION_M = 30


def downloadable(item):
    """Catalog a known public download path that still needs user choices.

    These are not one-click AOI services, but they are also not research TODOs:
    users can download a raster/vector package, drop it into manual_layers/, and
    run ingest-manual-layers to let VEIL clip/register it locally.
    """
    out = {**item, "kind": "downloadable", "manual_dir": MANUAL_LAYER_DROP_DIR}
    out.setdefault("reason", "Download the source file, place it in manual_layers/, then run ingest-manual-layers.")
    return out


def direct_download(item):
    """A direct, bounded full-file download VEIL can clip locally.

    These are intentionally opt-in in the setup UI: the probe checks advertised
    file size and local disk headroom, then marks the row as a direct download.
    """
    out = {**item, "kind": "file_download"}
    out.setdefault("max_bytes", DIRECT_DOWNLOAD_MAX_BYTES)
    out.setdefault("min_free_bytes", DIRECT_DOWNLOAD_MIN_FREE_BYTES)
    out.setdefault("reason", "Downloads the full source file, clips it locally, then discards the raw download.")
    return out


def manual(item):
    out = {**item, "kind": "manual", "manual_dir": MANUAL_LAYER_DROP_DIR}
    out.setdefault("reason", "Requires a source-specific API/table workflow before it can become a VEIL atlas layer.")
    return out


GAP_SPECIES_OPTIONS = [
    {"id": "gap_bald_eagle", "label": "GAP Bald Eagle habitat", "code": "bBAEAx",
     "common_name": "Bald Eagle", "scientific_name": "Haliaeetus leucocephalus",
     "sciencebase_item": "58fa4517e4b0b7ea54524ca5", "default_checked": True,
     "description": "Predicted suitable habitat for Bald Eagle from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Raptor habitat screening, riparian/open-water context, and conservation review."},
    {"id": "gap_wild_turkey", "label": "GAP Wild Turkey habitat", "code": "bWITUx",
     "common_name": "Wild Turkey", "scientific_name": "Meleagris gallopavo",
     "sciencebase_item": "58fa5d42e4b0b7ea545255df", "default_checked": True,
     "description": "Predicted suitable habitat for Wild Turkey from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Game species habitat screening, forest/opening edge context, and field planning."},
    {"id": "gap_mule_deer", "label": "GAP Mule Deer habitat", "code": "mMUDEx",
     "common_name": "Mule Deer", "scientific_name": "Odocoileus hemionus",
     "sciencebase_item": "58fa7069e4b0b7ea545259c2", "default_checked": True,
     "description": "Predicted suitable habitat for Mule Deer from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Large-mammal habitat screening, forage/cover context, and wildlife planning."},
    {"id": "gap_black_bear", "label": "GAP American Black Bear habitat", "code": "mABBEx",
     "common_name": "American Black Bear", "scientific_name": "Ursus americanus",
     "sciencebase_item": "58fa5f8be4b0b7ea545256a2",
     "description": "Predicted suitable habitat for American Black Bear from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Large-carnivore context, habitat connectivity review, and wildlife-conflict screening."},
    {"id": "gap_coyote", "label": "GAP Coyote habitat", "code": "mCOYOx",
     "common_name": "Coyote", "scientific_name": "Canis latrans",
     "sciencebase_item": "58fa64ece4b0b7ea545257e3",
     "description": "Predicted suitable habitat for Coyote from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Generalist carnivore habitat context and wildlife-use screening."},
    {"id": "gap_gray_fox", "label": "GAP Gray Fox habitat", "code": "mGRFOx",
     "common_name": "Gray Fox", "scientific_name": "Urocyon cinereoargenteus",
     "sciencebase_item": "58fa697be4b0b7ea545258af",
     "description": "Predicted suitable habitat for Gray Fox from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Mesocarnivore habitat screening, woodland/edge context, and species-filtered site selection."},
    {"id": "gap_red_fox", "label": "GAP Red Fox habitat", "code": "mREFOx",
     "common_name": "Red Fox", "scientific_name": "Vulpes vulpes",
     "sciencebase_item": "58fa7684e4b0b7ea54525ab2",
     "description": "Predicted suitable habitat for Red Fox from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Mesocarnivore habitat screening and open/edge habitat context."},
    {"id": "gap_elk", "label": "GAP Elk habitat", "code": "mELK1x",
     "common_name": "Elk", "scientific_name": "Cervus elaphus",
     "sciencebase_item": "58fa6797e4b0b7ea5452585e",
     "description": "Predicted suitable habitat for Elk from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Large-herbivore habitat context, forage/cover review, and wildlife planning."},
    {"id": "gap_whitetail_deer", "label": "GAP White-tailed Deer habitat", "code": "mWTDEx",
     "common_name": "White-tailed Deer", "scientific_name": "Odocoileus virginianus",
     "sciencebase_item": "58fa817ae4b0b7ea54525c2f",
     "description": "Predicted suitable habitat for White-tailed Deer from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Large-herbivore habitat context, browse pressure screening, and wildlife planning."},
    {"id": "gap_bobcat", "label": "GAP Bobcat habitat", "code": "mBOBCx",
     "common_name": "Bobcat", "scientific_name": "Lynx rufus",
     "sciencebase_item": "58fa621be4b0b7ea5452575c",
     "description": "Predicted suitable habitat for Bobcat from the USGS GAP CONUS 2001 habitat model.",
     "uses": "Mesocarnivore habitat screening, cover context, and wildlife corridor review."},
]


CATALOG = [
    {
        "id": "nwi_wetlands",
        "label": "National Wetlands Inventory wetlands",
        "category": "Ecology / hydrology",
        "description": "USFWS wetland and deepwater polygons classified by NWI code.",
        "uses": "Wetland screening, seep validation, habitat context, and land-management constraints.",
        "kind": "arcgis_vector",
        "url": "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0",
        "label_field": "ATTRIBUTE",
        "query_method": "GET",
        "probe_timeout": 10,
    },
    {
        "id": "nhdplus_flowlines",
        "label": "NHDPlus HR flowlines",
        "category": "Hydrology",
        "description": "USGS NHDPlus High Resolution stream and river network flowlines.",
        "uses": "Drainage context, stream proximity, flow-path validation, and riparian analysis.",
        "kind": "arcgis_vector",
        "url": "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3",
        "label_field": "gnis_name",
    },
    {
        "id": "nhdplus_waterbodies",
        "label": "NHDPlus HR waterbodies",
        "category": "Hydrology",
        "description": "USGS NHDPlus High Resolution lakes, ponds, and reservoirs.",
        "uses": "Surface-water inventory, wetness checks, wildlife habitat, and runoff destinations.",
        "kind": "arcgis_vector",
        "url": "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/9",
        "label_field": "gnis_name",
    },
    {
        "id": "nhdplus_catchments",
        "label": "NHDPlus HR catchments",
        "category": "Hydrology",
        "description": "USGS NHDPlus High Resolution catchment polygons linked to NHDPlus IDs.",
        "uses": "Watershed/catchment context for drainage, nutrient, and runoff questions.",
        "kind": "arcgis_vector",
        "url": "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/10",
        "label_field": "nhdplusid",
    },
    {
        "id": "wbd_huc12",
        "label": "Watershed Boundary Dataset HUC12",
        "category": "Hydrology / land management",
        "description": "USGS/NRCS 12-digit subwatershed boundaries.",
        "uses": "Watershed naming, management-unit reporting, and regional hydrologic context.",
        "kind": "arcgis_vector",
        "url": "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/6",
        "label_field": "name",
    },
    {
        "id": "fema_nfhl_flood_zones",
        "label": "FEMA NFHL flood hazard zones",
        "category": "Hazards / hydrology",
        "description": "FEMA National Flood Hazard Layer flood-hazard polygons.",
        "uses": "Floodplain screening, land-use constraints, and parcel/site risk review.",
        "kind": "arcgis_vector",
        "url": "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28",
        "label_field": "FLD_ZONE",
    },
    {
        "id": "critical_habitat_final",
        "label": "USFWS final critical habitat",
        "category": "Ecology / regulatory",
        "description": "Final designated critical habitat polygons for listed species.",
        "uses": "Endangered Species Act screening, habitat review, and conservation planning.",
        "kind": "arcgis_vector",
        "url": "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/USFWS_Critical_Habitat/FeatureServer/0",
        "label_field": "comname",
    },
    {
        "id": "critical_habitat_proposed",
        "label": "USFWS proposed critical habitat",
        "category": "Ecology / regulatory",
        "description": "Proposed critical habitat polygons for listed species.",
        "uses": "Early regulatory awareness and conservation-planning review.",
        "kind": "arcgis_vector",
        "url": "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/USFWS_Critical_Habitat/FeatureServer/2",
        "label_field": "comname",
    },
    {
        "id": "epa_ecoregions_l3",
        "label": "EPA Level III ecoregions",
        "category": "Ecology",
        "description": "EPA Level III ecological regions for the conterminous United States.",
        "uses": "Regional ecological context for vegetation, soils, habitat, and management interpretation.",
        "kind": "arcgis_vector",
        "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Level_III_Ecoregions_in_the_US_v1/FeatureServer/0",
        "label_field": "US_L3NAME",
    },
    {
        "id": "blm_surface_management",
        "label": "BLM Surface Management Agency",
        "category": "Land management",
        "description": "Federal surface-management jurisdiction polygons assembled by BLM.",
        "uses": "Federal land-management responsibility, agency context, and access/permit screening.",
        "kind": "arcgis_vector",
        "url": "https://gis.blm.gov/arcgis/rest/services/lands/BLM_Natl_SMA_Cached_with_PriUnk/MapServer/1",
        "label_field": "ADMIN_UNIT_NAME",
    },
    {
        "id": "blm_grazing_allotments",
        "label": "BLM grazing allotments",
        "category": "Agriculture / rangeland",
        "description": "BLM livestock grazing allotment polygons.",
        "uses": "Rangeland-use context, grazing-allotment screening, and public-land planning.",
        "kind": "arcgis_vector",
        "url": "https://services1.arcgis.com/KbxwQRRfWyEYLgp4/arcgis/rest/services/BLM_Natl_Grazing_Allotment_Polygons/FeatureServer/1",
        "label_field": "ALLOT_NAME",
    },
    {
        "id": "usfs_forest_boundaries",
        "label": "USFS administrative forest boundaries",
        "category": "Forestry / land management",
        "description": "National Forest System administrative forest boundary polygons.",
        "uses": "USFS management context, adjacent public land review, and forest-unit reporting.",
        "kind": "arcgis_vector",
        "url": "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_ForestSystemBoundaries_01/MapServer/0",
        "label_field": "forestname",
    },
    {
        "id": "usfs_roads",
        "label": "National Forest System roads",
        "category": "Access / forestry",
        "description": "USFS National Forest System road centerlines.",
        "uses": "Access planning, management-road context, and field logistics.",
        "kind": "arcgis_vector",
        "url": "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_RoadBasic_01/MapServer/0",
        "label_field": "name",
    },
    {
        "id": "usfs_trails",
        "label": "National Forest System trails",
        "category": "Access / recreation",
        "description": "USFS published National Forest System trail centerlines.",
        "uses": "Access planning, recreation context, and land-management field navigation.",
        "kind": "arcgis_vector",
        "url": "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0",
        "label_field": "trail_name",
    },
    {
        "id": "nps_boundaries",
        "label": "National Park Service boundaries",
        "category": "Land management",
        "description": "NPS authoritative unit boundary polygons.",
        "uses": "Park-adjacency screening, public-land context, and management constraints.",
        "kind": "arcgis_vector",
        "url": "https://services1.arcgis.com/fBc8EJBxQRMcHlei/ArcGIS/rest/services/NPS_Land_Resources_Division_Boundary_and_Tract_Data_Service/FeatureServer/2",
        "label_field": "UNIT_NAME",
    },
    {
        "id": "national_dams",
        "label": "National Inventory of Dams",
        "category": "Hydrology / hazards",
        "description": "USACE National Inventory of Dams point features.",
        "uses": "Upstream/downstream infrastructure context, hydrologic hazard screening, and water-control inventory.",
        "kind": "arcgis_vector",
        "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NID_v1/FeatureServer/0",
        "label_field": "NAME",
    },
    {
        "id": "wfigs_current_perimeters",
        "label": "Current interagency wildfire perimeters",
        "category": "Fire / hazards",
        "description": "WFIGS current interagency wildfire perimeter polygons.",
        "uses": "Current fire awareness, recent disturbance context, and operational screening.",
        "kind": "arcgis_vector",
        "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0",
        "label_field": "poly_IncidentName",
    },
    {
        "id": "wfigs_historic_perimeters",
        "label": "Interagency wildfire perimeter history",
        "category": "Fire / ecology",
        "description": "Historic interagency wildfire perimeter polygons.",
        "uses": "Disturbance history, fuel/vegetation context, and post-fire land-management review.",
        "kind": "arcgis_vector",
        "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0",
        "label_field": "poly_IncidentName",
    },
    {
        "id": "nced_easements",
        "label": "National Conservation Easement Database",
        "category": "Conservation / land management",
        "description": "Conservation easement polygons from the NCED ArcGIS service.",
        "uses": "Conservation restrictions, protected-interest context, and parcel due diligence.",
        "kind": "arcgis_vector",
        "url": "https://services.arcgis.com/F7DSX1DSNSiWmOqh/arcgis/rest/services/National_Conservation_Easement_Database/FeatureServer/1",
        "label_field": "EasementName",
    },
    {
        "id": "usda_cdl_current",
        "label": "USDA Cropland Data Layer",
        "category": "Agriculture",
        "description": "USDA NASS crop-specific land-cover raster from CropScape/CDL.",
        "uses": "Crop identification, field context, rotation clues, and agriculture/land-cover screening.",
        "kind": "arcgis_raster",
        "url": "https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer",
        "pixel_type": "U16",
        "resolution": 30,
        "value_classification": "categorical",
        "value_kind": "crop class",
    },
    # Known public download paths. Sources with stable services or stable
    # bounded source archives are automated. The remaining downloadables need
    # product/year/species/date choices before VEIL can choose the right file.
    downloadable({"id": "nlcd_land_cover", "label": "NLCD / Annual NLCD", "category": "Land cover", "description": "MRLC land cover, imperviousness, tree canopy, and change products.", "uses": "Generic land-cover baseline, imperviousness, canopy cover, and change context.", "download_url": "https://www.mrlc.gov/data", "download_note": "Choose a land-cover/canopy/impervious product and year; download GeoTIFF where offered."}),
    direct_download({"id": "pad_us", "label": "PAD-US protected areas", "category": "Land management", "description": "USGS protected area inventory and spatial-analysis polygons from the PAD-US 4.1 vector analysis file.", "uses": "Protection status, public/private conservation context, public access, manager names, and land-use constraints.", "source_url": "https://www.sciencebase.gov/catalog/file/get/6759b69fd34edfeb8710a3ea?name=PADUS4_1VectorAnalysis_PADUS_Only.zip", "source_filename": "PADUS4_1VectorAnalysis_PADUS_Only.zip", "source_size_bytes": 361476711, "source_member": "PADUS4_1VectorAnalysis_PADUS_Only.gdb", "source_layer": "PADUS4_1VectorAnalysis_PADUS_Only_Simp_SingP", "download_url": "https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-download", "download_note": "Downloads the PAD-US 4.1 vector-analysis geodatabase, clips selected views locally, and discards the raw archive.", "layer_options": [
        {"id": "pad_us_gap_status", "label": "PAD-US protection status", "description": "Protected areas labeled by GAP status code, a coarse indicator of biodiversity protection intent.", "uses": "Conservation screening, biodiversity protection context, and protected-area overlap checks.", "label_field": "GAP_Sts"},
        {"id": "pad_us_public_access", "label": "PAD-US public access", "description": "Protected areas labeled by published public-access status.", "uses": "Access planning, recreation screening, and private/public access context.", "label_field": "Pub_Access"},
        {"id": "pad_us_manager", "label": "PAD-US manager / owner", "description": "Protected areas labeled by manager name or managing entity where available.", "uses": "Identifying responsible agencies, ownership context, and due-diligence review.", "label_field": "Mang_Name"},
        {"id": "pad_us_designation", "label": "PAD-US designation type", "description": "Protected areas labeled by designation type, such as easement, wilderness, park, or other protected designation.", "uses": "Understanding the legal or administrative type of protection affecting a place.", "label_field": "Des_Tp"},
    ]}),
    {"id": "gap_species_habitat", "label": "USGS GAP species habitat", "category": "Ecology", "description": "USGS GAP predicted suitable habitat rasters for selected terrestrial vertebrate species.", "uses": "Species-specific habitat screening, biodiversity context, species-at-point identify results, and species-filtered site recommendations.", "kind": "gap_species", "download_url": "https://www.usgs.gov/programs/gap-analysis-project/science/species-data-download", "download_note": "Automates selected species from ScienceBase habitat-map archives, clips each species locally, discards the national archive, and builds GAP species-richness/species-mask outputs.", "layer_options": GAP_SPECIES_OPTIONS},
    downloadable({"id": "usfs_treemap", "label": "USFS TreeMap", "category": "Forestry", "description": "30 m forest attributes imputed from FIA and remote sensing.", "uses": "Forest type, structure, biomass, carbon, and species-composition context.", "download_url": "https://data.fs.usda.gov/geodata/rastergateway/treemap/index.php", "download_note": "Choose TreeMap year and attribute raster, then ingest the GeoTIFF."}),
    {"id": "usfs_lcms", "label": "USFS Landscape Change Monitoring System", "category": "Forestry / land cover", "description": "USFS LCMS image services for landscape change, land cover, and disturbance timing.", "uses": "Disturbance, vegetation gain/loss, harvest/fire/insect change screening, and broad land-cover context.", "kind": "arcgis_raster", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_AnnualLandcover_CONUS/ImageServer", "pixel_type": "U8", "resolution": 30, "download_url": "https://data.fs.usda.gov/geodata/rastergateway/LCMS/index.php", "download_note": "Fetched automatically from public LCMS image services for the selected default views.", "layer_options": [
        {"id": "lcms_landcover", "label": "LCMS annual land cover", "description": "LCMS modeled land-cover class for the current/default image-service slice.", "uses": "Forest/non-forest context, broad vegetation setting, and land-cover comparison with LANDFIRE or NLCD.", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_AnnualLandcover_CONUS/ImageServer", "pixel_type": "U8", "resolution": 30, "value_classification": "categorical", "value_kind": "land-cover class"},
        {"id": "lcms_annual_change", "label": "LCMS annual change", "description": "LCMS modeled annual landscape change class for the current/default image-service slice.", "uses": "Screening for disturbance, vegetation loss, vegetation gain, and stable areas.", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_AnnualChange_CONUS/ImageServer", "pixel_type": "U8", "resolution": 30, "value_classification": "categorical", "value_kind": "change class"},
        {"id": "lcms_recent_fast_loss", "label": "LCMS recent fast loss", "description": "Most recent year when LCMS detected fast vegetation loss.", "uses": "Recent fire, harvest, clearing, storm, or other abrupt disturbance screening.", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_MostRecentYearFastLoss_CONUS/ImageServer", "pixel_type": "U16", "resolution": 30, "value_classification": "continuous", "value_kind": "year", "value_unit": "year"},
        {"id": "lcms_recent_slow_loss", "label": "LCMS recent slow loss", "description": "Most recent year when LCMS detected slow vegetation loss.", "uses": "Gradual decline, insect/disease, drought stress, or longer-running degradation screening.", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_MostRecentYearSlowLoss_CONUS/ImageServer", "pixel_type": "U16", "resolution": 30, "value_classification": "continuous", "value_kind": "year", "value_unit": "year"},
        {"id": "lcms_highest_gain", "label": "LCMS highest-probability gain year", "description": "Year with the highest LCMS modeled probability of vegetation gain.", "uses": "Regrowth, revegetation, recovery, and restoration context.", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/USFS_EDW_LCMS_YearHighestProbabilityGain_CONUS/ImageServer", "pixel_type": "U16", "resolution": 30, "value_classification": "continuous", "value_kind": "year", "value_unit": "year"},
    ]},
    manual({"id": "fia_datamart", "label": "USFS FIA DataMart", "category": "Forestry", "description": "Forest Inventory and Analysis tables and public plot summaries.", "uses": "Regional forest statistics and biomass/growth/mortality context.", "download_url": "https://apps.fs.usda.gov/fia/datamart/", "download_note": "Tabular/statistical source; not a direct atlas layer without a summarization adapter.", "reason": "Tabular/statistical source, not an AOI feature layer."}),
    downloadable({"id": "usfs_ads", "label": "USFS Aerial Detection Survey", "category": "Forestry / disturbance", "description": "Forest insect, disease, and mortality observations.", "uses": "Forest health and disturbance screening.", "download_url": "https://www.fs.usda.gov/science-technology/data-tools-products/fhp-mapping-reporting/detection-surveys", "download_note": "Download IDS/ADS geospatial data for the region/year, then ingest vector data."}),
    {"id": "wildfire_hazard_potential", "label": "Wildfire Hazard Potential", "category": "Fire", "description": "USFS classified wildfire hazard potential raster from the 2023 public image service.", "uses": "Landscape-scale wildfire hazard screening and broad fuel-treatment prioritization context.", "kind": "arcgis_raster", "url": "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_RMRS_WildfireHazardPotentialClassified/ImageServer", "pixel_type": "U8", "resolution": 270, "value_classification": "categorical", "value_kind": "hazard class", "download_url": "https://research.fs.usda.gov/firelab/products/dataandtools/wildfire-hazard-potential", "download_note": "Fetched automatically from the public 2023 WHP classified image service."},
    downloadable({"id": "mtbs_burn_severity", "label": "MTBS burn severity", "category": "Fire / ecology", "description": "Monitoring Trends in Burn Severity fire perimeters and severity rasters.", "uses": "Burn history and severity context.", "download_url": "https://www.mtbs.gov/direct-download", "download_note": "Choose fire/state/national products; ingest perimeter vectors or severity rasters."}),
    downloadable({"id": "ravg_postfire", "label": "RAVG post-fire vegetation condition", "category": "Fire / forestry", "description": "Rapid post-fire vegetation condition products.", "uses": "Post-fire mortality and restoration context.", "download_url": "https://data.fs.usda.gov/geodata/rastergateway/ravg/index.php", "download_note": "Choose fire/product rasters such as CBI or canopy/basal-area loss, then ingest."}),
    downloadable({"id": "gnatsgo", "label": "gNATSGO soils", "category": "Soils", "description": "National gridded soil survey database.", "uses": "Complete-coverage soil attributes beyond AOI SDA polygons.", "download_url": "https://www.nrcs.usda.gov/resources/data-and-reports/gridded-national-soil-survey-geographic-database-gnatsgo", "download_note": "Download gridded soil rasters/tables if you need them; the build already fetches SDA/gSSURGO polygons."}),
    downloadable({"id": "polaris_soils", "label": "POLARIS 30 m soils", "category": "Soils / hydrology", "description": "Probabilistic 30 m soil properties.", "uses": "Ksat, texture, water retention, and hydrology parameter priors.", "download_url": "http://hydrology.cee.duke.edu/POLARIS/", "download_note": "Choose property, depth, and statistic raster; ingest the GeoTIFF."}),
    downloadable({"id": "solus100", "label": "SOLUS100 soils", "category": "Soils / hydrology", "description": "100 m soil property maps including depth-related properties.", "uses": "Depth-to-restriction and hydrologic soil-property context.", "download_url": "https://www.nrcs.usda.gov/resources/data-and-reports/soil-landscapes-of-the-united-states-solus", "download_note": "Choose SOLUS property/depth raster from the cloud bucket or repository, then ingest."}),
    {"id": "mlra", "label": "NRCS MLRA/LRR", "category": "Soils / land management", "description": "Major Land Resource Area and Land Resource Region boundaries.", "uses": "Regional soil/agroecological context.", "kind": "arcgis_vector", "url": "https://services.arcgis.com/SXbDpmb7xQkk44JV/arcgis/rest/services/Major_Land_Resource_Areas/FeatureServer/0", "label_field": "MLRA_NAME", "download_url": "https://www.nrcs.usda.gov/resources/data-and-reports/major-land-resource-area-mlra", "download_note": "Also available as a 2022 MLRA Geographic Database download."},
    downloadable({"id": "rap_rangelands", "label": "Rangeland Analysis Platform", "category": "Rangeland", "description": "30 m annual rangeland fractional cover and biomass.", "uses": "Rangeland cover, forage, annual herbaceous, and biomass screening.", "download_url": "https://rangelands.app/", "download_note": "Export/download a product for the AOI/year/metric, then ingest the raster or vector output."}),
    {"id": "rcmap", "label": "USGS RCMAP", "category": "Rangeland", "description": "MRLC/USGS RCMAP rangeland component rasters exposed as WCS services.", "uses": "Western rangeland cover trends and condition assessment.", "kind": "wcs_raster", "wcs_url": "https://www.mrlc.gov/geoserver/rcmap_tree/wcs", "coverage": "rcmap_tree__rcmap_tree_2025", "resolution": 30, "download_url": "https://www.mrlc.gov/data/type/rcmap-time-series-trends", "download_note": "Fetched automatically from MRLC WCS for selected component/year defaults.", "layer_options": [
        {"id": "rcmap_tree_2025", "label": "RCMAP tree cover 2025", "description": "Percent tree cover from the 2025 RCMAP component raster.", "uses": "Woodland expansion, canopy context, and rangeland condition screening.", "wcs_url": "https://www.mrlc.gov/geoserver/rcmap_tree/wcs", "coverage": "rcmap_tree__rcmap_tree_2025", "value_classification": "continuous", "value_kind": "tree cover", "value_unit": "%"},
        {"id": "rcmap_shrub_2025", "label": "RCMAP shrub cover 2025", "description": "Percent shrub cover from the 2025 RCMAP component raster.", "uses": "Shrubland condition, brush encroachment, and wildlife habitat context.", "wcs_url": "https://www.mrlc.gov/geoserver/rcmap_shrub/wcs", "coverage": "rcmap_shrub__rcmap_shrub_2025", "value_classification": "continuous", "value_kind": "shrub cover", "value_unit": "%"},
        {"id": "rcmap_litter_2025", "label": "RCMAP litter cover 2025", "description": "Percent litter cover from the 2025 RCMAP component raster.", "uses": "Ground-cover context for erosion risk, soil protection, and rangeland condition.", "wcs_url": "https://www.mrlc.gov/geoserver/rcmap_litter/wcs", "coverage": "rcmap_litter__rcmap_litter_2025", "value_classification": "continuous", "value_kind": "litter cover", "value_unit": "%"},
        {"id": "rcmap_shrub_height_2025", "label": "RCMAP shrub height 2025", "description": "Shrub-height raster from the 2025 RCMAP product.", "uses": "Rangeland structure, wildlife habitat, and fuel-structure screening.", "wcs_url": "https://www.mrlc.gov/geoserver/rcmap_shrub_height/wcs", "coverage": "rcmap_shrub_height__rcmap_shrub_height_2025", "value_classification": "continuous", "value_kind": "shrub height", "value_unit": "cm"},
    ]},
    downloadable({"id": "lanid_irrigation", "label": "LANID irrigation", "category": "Agriculture / water", "description": "30 m Landsat-derived irrigation maps.", "uses": "Irrigated agriculture screening.", "download_url": "https://zenodo.org/records/5548555", "download_note": "Choose annual irrigation or frequency product, then ingest the raster."}),
    downloadable({"id": "mirad_irrigation", "label": "MIrAD-US irrigation", "category": "Agriculture / water", "description": "Moderate-resolution irrigated agriculture maps.", "uses": "Regional irrigation context.", "download_url": "https://data.usgs.gov/datacatalog/data/USGS:5db08e84e4b0b0c58b56e04f", "download_note": "Choose year/resolution raster, then ingest."}),
    manual({"id": "openet", "label": "OpenET evapotranspiration", "category": "Agriculture / water", "description": "Field-scale evapotranspiration estimates.", "uses": "Crop water use, drought, and irrigation demand context.", "download_url": "https://etdata.org/api/", "download_note": "API requires product/date/AOI choices and quotas; export a GeoTIFF/CSV before VEIL ingest.", "reason": "API/product selection and date range required."}),
    downloadable({"id": "prism", "label": "PRISM climate", "category": "Climate", "description": "800 m climate normals and time-series grids.", "uses": "Precipitation/temperature normals and climate context.", "download_url": "https://prism.oregonstate.edu/", "download_note": "Choose variable and period; ingest gridded raster outputs when available. Daymet point forcing is already fetched."}),
    downloadable({"id": "gridmet", "label": "gridMET", "category": "Climate", "description": "Daily gridded surface meteorology.", "uses": "Hydrology, drought, fire-weather, and ag forcing.", "download_url": "https://www.climatologylab.org/gridmet.html", "download_note": "Choose variable/year or export an AOI subset, then ingest raster outputs."}),
    downloadable({"id": "snodas", "label": "SNODAS snow", "category": "Climate / hydrology", "description": "Assimilated daily snowpack variables.", "uses": "Operational snow depth/SWE and snowmelt context.", "download_url": "https://nsidc.org/data/g02158/versions/1", "download_note": "Choose date/product from NSIDC/NOAA archives; convert flat binary to GeoTIFF before ingest."}),
    direct_download({"id": "drought_monitor", "label": "U.S. Drought Monitor", "category": "Climate / agriculture", "description": "Weekly drought-intensity polygons.", "uses": "Drought status and ag/ecology stress context.", "source_url": "https://droughtmonitor.unl.edu/data/shapefiles_m/USDM_current_M.zip", "download_url": "https://droughtmonitor.unl.edu/DmData/GISData.aspx", "download_note": "Downloads the current weekly GIS shapefile zip, clips it locally, and discards the raw zip."}),
    manual({"id": "geology", "label": "USGS national geology", "category": "Geology", "description": "National geologic map data.", "uses": "Bedrock/surficial context for hydrology, soils, and habitat.", "download_url": "https://ngmdb.usgs.gov/mapview/", "download_note": "Choose a map/product in NGMDB MapView and download geospatial files if offered.", "reason": "Service/data model varies by map; needs adapter and scale choice."}),
    manual({"id": "attains", "label": "EPA ATTAINS impaired waters", "category": "Water quality", "description": "Waterbody assessment and impairment data.", "uses": "Impaired-waters context and regulatory water-quality screening.", "download_url": "https://www.epa.gov/waterdata/get-data-access-public-attains-data", "download_note": "API/table/geospatial service source; needs an ATTAINS-specific join adapter for best use.", "reason": "API/table join source; not a direct AOI feature fetch yet."}),
    manual({"id": "water_quality_portal", "label": "Water Quality Portal", "category": "Water quality", "description": "Monitoring locations and sample results.", "uses": "Nearby water-quality observations and monitoring context.", "download_url": "https://www.waterqualitydata.us/", "download_note": "Download station/result CSV/TSV/KML for the AOI; VEIL needs an observation-specific adapter for rich querying.", "reason": "Observation API; needs station/result query adapter."}),
    downloadable({"id": "ccap_land_cover", "label": "NOAA C-CAP land cover", "category": "Coastal ecology", "description": "Coastal land cover and change products.", "uses": "Coastal habitat and land-cover change context.", "download_url": "https://coast.noaa.gov/digitalcoast/data/ccapregional.html", "download_note": "Choose regional/high-resolution product and year; ingest downloaded raster/vector files."}),
    manual({"id": "noaa_efh", "label": "NOAA Essential Fish Habitat", "category": "Marine / coastal ecology", "description": "Essential fish habitat and habitat areas of particular concern.", "uses": "Coastal/marine habitat screening.", "download_url": "https://www.habitat.noaa.gov/application/efhinventory/", "download_note": "Choose council/species/life-stage data from the EFH inventory, then ingest downloaded GIS files.", "reason": "Marine species/management-area product; needs adapter and user selection."}),
    direct_download({"id": "plant_hardiness", "label": "USDA plant hardiness zones", "category": "Agriculture / ecology", "description": "2023 plant hardiness zone map.", "uses": "Plant suitability and horticultural climate context.", "source_url": "https://prism.oregonstate.edu/phzm/data/2023/phzm_us_zones_shp_2023.zip", "download_url": "https://prism.oregonstate.edu/phzm/", "download_note": "Downloads the 2023 CONUS half-zone shapefile zip, clips it locally, and discards the raw zip."}),
    downloadable({"id": "natureserve_mbi", "label": "NatureServe Map of Biodiversity Importance", "category": "Biodiversity", "description": "Biodiversity-importance and at-risk species habitat layers.", "uses": "Biodiversity prioritization and conservation screening.", "download_url": "https://www.natureserve.org/access-data", "download_note": "Download Map of Biodiversity Importance GIS layers from NatureServe Open Data, then ingest."}),
    downloadable({"id": "tnc_resilience", "label": "TNC Resilient and Connected Network", "category": "Conservation", "description": "Resilience and connectivity conservation-priority layers.", "uses": "Climate-resilient conservation planning.", "download_url": "https://crcs.tnc.org/pages/data-terrestrial-resilience", "download_note": "Download state/network component data and ingest the relevant vector/raster files."}),
]


def catalog_public():
    return [{k: v for k, v in item.items()
             if k not in {"url", "pixel_type", "resolution", "label_field"}}
            for item in CATALOG]


def expanded_catalog_items():
    for item in CATALOG:
        yield item
        for option in item.get("layer_options") or []:
            merged = {**item, **option}
            merged["parent_id"] = item["id"]
            merged["id"] = option["id"]
            merged["label"] = option["label"]
            merged["category"] = option.get("category", item.get("category"))
            merged["description"] = option.get("description", item.get("description"))
            merged["uses"] = option.get("uses", item.get("uses"))
            merged["source_layer"] = option.get("source_layer", item.get("source_layer"))
            merged["source_member"] = option.get("source_member", item.get("source_member"))
            yield merged


def read_aoi_wgs84(path):
    ds = ogr.Open(path)
    if ds is None or ds.GetLayerCount() == 0:
        raise RuntimeError(f"could not open AOI {path}")
    layer = ds.GetLayer(0)
    src = layer.GetSpatialRef()
    dst = osr.SpatialReference()
    dst.SetFromUserInput("EPSG:4326")
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = None
    if src is not None:
        src = src.Clone()
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src, dst)
    union = None
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom = geom.Clone()
        if ct:
            geom.Transform(ct)
        union = geom if union is None else union.Union(geom)
    if union is None or union.IsEmpty():
        raise RuntimeError("AOI has no polygon geometry")
    env = union.GetEnvelope()  # minx, maxx, miny, maxy
    return union, (env[0], env[2], env[1], env[3])


def arcgis_polygon_geometry(geom):
    """Return a simple ArcGIS JSON polygon for AOI queries.

    The init AOI is a single polygon. If a user supplies a multipolygon, use its
    convex hull for the lightweight service-side intersection test; the fetch is
    still clipped/intersected by the remote service before VEIL imports it.
    """
    if geom.GetGeometryName() != "POLYGON":
        geom = geom.ConvexHull()
    rings = []
    for ri in range(geom.GetGeometryCount()):
        ring = geom.GetGeometryRef(ri)
        coords = []
        for pi in range(ring.GetPointCount()):
            x, y, *_ = ring.GetPoint(pi)
            coords.append([round(x, 7), round(y, 7)])
        rings.append(coords)
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


def http_json_get(url, params, timeout=20):
    encoded = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "?" + encoded, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    data = json.loads(text)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(err.get("message") or json.dumps(err))
    return data


def http_json(url, params, timeout=20, method="POST"):
    encoded = urllib.parse.urlencode(params)
    if method == "GET":
        return http_json_get(url, params, timeout=timeout)
    req = urllib.request.Request(
        url,
        data=encoded.encode("utf-8"),
        headers={**UA, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        # A few public ArcGIS services intermittently reject POST even for
        # standard /query calls. The AOI geometries from the init UI are small,
        # so GET is a safe fallback for those services.
        return http_json_get(url, params, timeout=timeout)
    data = json.loads(text)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(err.get("message") or json.dumps(err))
    return data


def _first_attr(attrs, names):
    lowered = {str(k).lower(): v for k, v in attrs.items()}
    for name in names:
        if name in attrs:
            return attrs[name]
        v = lowered.get(name.lower())
        if v is not None:
            return v
    return None


def raster_metadata_sidecar(item):
    return {k: item.get(k) for k in (
        "description", "uses", "value_kind", "value_unit", "value_classification")
        if item.get(k) not in (None, "")}


def write_raster_metadata_sidecar(data_dir, item, require_value_metadata=False):
    meta = raster_metadata_sidecar(item)
    if require_value_metadata:
        meta["require_value_metadata"] = True
    if not meta:
        return
    path = os.path.join(data_dir, "atlas", "metadata", item["id"] + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = {}
    if os.path.exists(path):
        try:
            existing = json.load(open(path))
        except Exception:
            existing = {}
    json.dump({**existing, **meta}, open(path, "w"), indent=2)


def _legend_vat_from_sequence(service_meta, legend_doc):
    layers = legend_doc.get("layers") or []
    rows = (layers[0].get("legend") if layers else None) or []
    labels = [str(r.get("label") or "").strip() for r in rows]
    labels = [label for label in labels if label]
    mins = service_meta.get("minValues") or []
    maxs = service_meta.get("maxValues") or []
    if len(mins) != 1 or len(maxs) != 1 or not labels:
        return {}
    lo, hi = int(mins[0]), int(maxs[0])
    if hi < lo or len(labels) < (hi - lo + 1):
        return {}
    vat = {lo + i: {"name": label} for i, label in enumerate(labels[:hi - lo + 1])}
    if lo > 0:
        vat[0] = {"name": "No data / background", "color": [0, 0, 0]}
    return vat


def arcgis_raster_vat(item):
    """Return value -> natural-language class metadata for an ArcGIS ImageServer.

    Prefer the raster attribute table when available because it carries explicit
    numeric values. Some public class services expose only a legend plus
    min/max values; for those, the ArcGIS legend order maps to the contiguous
    value range.
    """
    service_meta = http_json_get(item["url"], {"f": "json"}, timeout=30)
    vat = {}
    try:
        rat = http_json_get(item["url"] + "/rasterAttributeTable", {"f": "json"}, timeout=30)
    except Exception:
        rat = {}
    for row in rat.get("features") or []:
        attrs = row.get("attributes") or {}
        value = _first_attr(attrs, ("Value", "VALUE", "value", "ClassValue", "Class_Code"))
        name = _first_attr(attrs, ("Class_Names", "Class_Name", "ClassName", "Label", "Name", "Class"))
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        entry = {"name": name.strip()}
        r = _first_attr(attrs, ("Red", "RED", "red"))
        g = _first_attr(attrs, ("Green", "GREEN", "green"))
        b = _first_attr(attrs, ("Blue", "BLUE", "blue"))
        try:
            entry["color"] = [int(r), int(g), int(b)]
        except (TypeError, ValueError):
            pass
        vat[value] = entry
    if vat:
        return vat
    try:
        legend = http_json_get(item["url"] + "/legend", {"f": "json"}, timeout=30)
    except Exception:
        legend = {}
    return _legend_vat_from_sequence(service_meta, legend)


def write_vat_sidecar(data_dir, layer_id, vat):
    path = os.path.join(data_dir, "atlas", "vat", layer_id + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({str(k): v for k, v in sorted(vat.items())}, open(path, "w"), indent=2)


def fmt_bytes(n):
    if n is None:
        return "unknown size"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for unit in units:
        if v < 1000 or unit == units[-1]:
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1000
    return f"{n} B"


def head_download(item, timeout=20):
    req = urllib.request.Request(item["source_url"], headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        length = resp.headers.get("Content-Length")
        size = int(length) if length and length.isdigit() else item.get("source_size_bytes")
        return {
            "url": resp.geturl(),
            "bytes": size,
            "content_type": resp.headers.get("Content-Type"),
        }


@functools.lru_cache(maxsize=256)
def sciencebase_item(item_id):
    url = f"https://www.sciencebase.gov/catalog/item/{item_id}?format=json&fields=title,files"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gap_species_source_info(item):
    data = sciencebase_item(item["sciencebase_item"])
    files = data.get("files") or []
    for f in files:
        name = f.get("name") or ""
        if name.lower().endswith(".zip") and "habmap" in name.lower():
            return {"source_url": f["url"], "source_filename": name,
                    "source_size_bytes": f.get("size")}
    raise RuntimeError(f"ScienceBase item {item['sciencebase_item']} has no HabMap zip")


def gap_item_with_source(item):
    info = gap_species_source_info(item)
    return {**item, **info, "max_bytes": item.get("max_bytes", DIRECT_DOWNLOAD_MAX_BYTES),
            "min_free_bytes": item.get("min_free_bytes", DIRECT_DOWNLOAD_MIN_FREE_BYTES)}


def direct_download_status(item, base_dir=PROJECT):
    info = head_download(item)
    size = info.get("bytes")
    max_bytes = int(item.get("max_bytes") or DIRECT_DOWNLOAD_MAX_BYTES)
    min_free = int(item.get("min_free_bytes") or DIRECT_DOWNLOAD_MIN_FREE_BYTES)
    free = shutil.disk_usage(base_dir).free
    info.update({"free_bytes": free, "max_bytes": max_bytes, "min_free_bytes": min_free})
    if size is None:
        info.update({"ok": False, "reason": "download size unavailable"})
    elif size > max_bytes:
        info.update({"ok": False, "reason": f"source is {fmt_bytes(size)}, over {fmt_bytes(max_bytes)} limit"})
    elif free - size < min_free:
        info.update({"ok": False, "reason": f"needs {fmt_bytes(size)} plus {fmt_bytes(min_free)} free headroom"})
    else:
        info.update({"ok": True, "reason": f"{download_class(size)} download, {fmt_bytes(size)}; {fmt_bytes(free)} free"})
    return info


def download_class(n):
    if n is None:
        return "unknown"
    if n < LIGHT_DOWNLOAD_MAX_BYTES:
        return "light"
    if n < LARGE_DOWNLOAD_BYTES:
        return "medium"
    return "heavy"


def is_large_download(n):
    return download_class(n) == "heavy"


def estimate_gap_processed_bytes(bbox, species_count, include_richness=True):
    if not species_count:
        return 0
    minx, miny, maxx, maxy = bbox
    mid_lat = (miny + maxy) / 2
    meters_per_deg_lat = 111_320
    meters_per_deg_lon = max(1, 111_320 * abs(math.cos(math.radians(mid_lat))))
    w = max(1, round(abs(maxx - minx) * meters_per_deg_lon / GAP_RASTER_RESOLUTION_M))
    h = max(1, round(abs(maxy - miny) * meters_per_deg_lat / GAP_RASTER_RESOLUTION_M))
    # One byte per selected species mask plus two bytes for the richness raster.
    richness_bytes = 2 if include_richness else 0
    return int(w * h * (species_count + richness_bytes))


def probe_vector(item, aoi_arcgis):
    params = {
        "f": "json",
        "where": "1=1",
        "returnCountOnly": "true",
        "geometry": json.dumps(aoi_arcgis),
        "geometryType": "esriGeometryPolygon",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
    }
    data = http_json(
        item["url"] + "/query",
        params,
        timeout=int(item.get("probe_timeout") or 14),
        method=item.get("query_method", "POST"),
    )
    return int(data.get("count") or 0)


def probe_raster(item, bbox):
    minx, miny, maxx, maxy = bbox
    params = {
        "f": "image",
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "bboxSR": "4326",
        "imageSR": "4326",
        "size": "8,8",
        "format": "tiff",
        "pixelType": item.get("pixel_type", "U16"),
        "interpolation": "RSP_NearestNeighbor",
    }
    url = item["url"] + "/exportImage?" + urllib.parse.urlencode(params)
    fd, tmp = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp, open(tmp, "wb") as fh:
            fh.write(resp.read())
        ds = gdal.Open(tmp)
        if ds is None or ds.RasterCount == 0:
            return False
        band = ds.GetRasterBand(1)
        stats = band.GetStatistics(False, True)
        nodata = band.GetNoDataValue()
        ds = None
        if not stats:
            return True
        mn, mx = stats[0], stats[1]
        if nodata is not None and mn == nodata and mx == nodata:
            return False
        return True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def wcs_source(item):
    return "WCS:%s?version=2.0.1&coverage=%s" % (
        item["wcs_url"],
        urllib.parse.quote(item["coverage"], safe="_:"),
    )


def probe_wcs_raster(item, bbox):
    fd, tmp = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        ds = gdal.Warp(
            tmp,
            wcs_source(item),
            format="GTiff",
            dstSRS="EPSG:4326",
            outputBounds=bbox,
            width=8,
            height=8,
            resampleAlg="near",
            creationOptions=["COMPRESS=DEFLATE"],
        )
        if ds is None or ds.RasterCount == 0:
            return False
        band = ds.GetRasterBand(1)
        stats = band.GetStatistics(False, True)
        nodata = band.GetNoDataValue()
        ds = None
        if not stats:
            return True
        mn, mx = stats[0], stats[1]
        if nodata is not None and mn == nodata and mx == nodata:
            return False
        return True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def probe(aoi_path, progress=None):
    """Check every catalog layer against the AOI.

    If ``progress`` is given it is called with each layer's public result as
    soon as that probe finishes (out of catalog order), so callers can stream
    per-layer feedback while the rest are still in flight.
    """
    geom, bbox = read_aoi_wgs84(aoi_path)
    aoi_arcgis = arcgis_polygon_geometry(geom)
    def check_item(item):
        public = {k: v for k, v in item.items()
                  if k not in {"url", "pixel_type", "resolution", "label_field"}}
        if item["kind"] in {"manual", "downloadable"}:
            public.update({"status": item["kind"], "intersects": False,
                           "reason": item.get("reason")})
            return public
        try:
            if item["kind"] == "arcgis_vector":
                count = probe_vector(item, aoi_arcgis)
                public.update({"status": "ok", "intersects": count > 0,
                               "feature_count": count})
            elif item["kind"] == "arcgis_raster":
                intersects = probe_raster(item, bbox)
                public.update({"status": "ok", "intersects": intersects,
                               "feature_count": None})
            elif item["kind"] == "wcs_raster":
                intersects = probe_wcs_raster(item, bbox)
                public.update({"status": "ok", "intersects": intersects,
                               "feature_count": None})
            elif item["kind"] == "gap_species":
                processed_each = estimate_gap_processed_bytes(bbox, 1, include_richness=False)
                processed_overhead = estimate_gap_processed_bytes(bbox, 1) - processed_each
                options = []
                total_download = 0
                for option in item.get("layer_options") or []:
                    src_item = gap_item_with_source(option)
                    info = direct_download_status(src_item)
                    size = info.get("bytes")
                    total_download += size or 0
                    options.append({
                        **option,
                        "status": "file_download" if info["ok"] else "downloadable",
                        "download_bytes": size,
                        "download_size": fmt_bytes(size),
                        "download_class": download_class(size),
                        "processed_bytes_estimate": processed_each,
                        "processed_size_estimate": fmt_bytes(processed_each),
                        "processed_group": "gap_species",
                        "processed_overhead_bytes": processed_overhead,
                        "reason": info.get("reason"),
                    })
                total_processed = estimate_gap_processed_bytes(bbox, len(options))
                public.update({
                    "status": "file_download",
                    "intersects": True,
                    "feature_count": None,
                    "download_bytes": total_download,
                    "download_size": fmt_bytes(total_download),
                    "download_class": download_class(total_download),
                    "processed_bytes_estimate": total_processed,
                    "processed_size_estimate": fmt_bytes(total_processed),
                    "layer_options": options,
                    "reason": "Selected GAP species archives are downloaded, clipped to the AOI, and discarded.",
                })
            elif item["kind"] == "file_download":
                info = direct_download_status(item)
                public.update({
                    "status": "file_download" if info["ok"] else "downloadable",
                    "intersects": bool(info["ok"]),
                    "feature_count": None,
                    "download_bytes": info.get("bytes"),
                    "download_size": fmt_bytes(info.get("bytes")),
                    "download_class": download_class(info.get("bytes")),
                    "processed_bytes_estimate": item.get("processed_bytes_estimate"),
                    "processed_size_estimate": fmt_bytes(item.get("processed_bytes_estimate")) if item.get("processed_bytes_estimate") else None,
                    "free_bytes": info.get("free_bytes"),
                    "large_file": is_large_download(info.get("bytes")),
                    "reason": info.get("reason"),
                })
            else:
                public.update({"status": "unsupported", "intersects": False,
                               "reason": f"unsupported kind {item['kind']}"})
        except Exception as err:  # noqa: BLE001
            public.update({"status": "error", "intersects": False,
                           "error": str(err)[:300]})
        return public

    out = [None] * len(CATALOG)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(check_item, item): i for i, item in enumerate(CATALOG)}
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            out[futures[fut]] = res
            if progress is not None:
                progress(res)
    return {"ok": True, "layers": out}


def fetch_vector(item, aoi_arcgis, out_path):
    features = []
    offset = 0
    page_size = 1000
    exceeded = False
    while len(features) < MAX_VECTOR_FEATURES:
        params = {
            "f": "geojson",
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "geometry": json.dumps(aoi_arcgis),
            "geometryType": "esriGeometryPolygon",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "resultOffset": str(offset),
            "resultRecordCount": str(min(page_size, MAX_VECTOR_FEATURES - len(features))),
        }
        data = http_json(item["url"] + "/query", params, timeout=120)
        page = data.get("features") or []
        features.extend(page)
        if not data.get("exceededTransferLimit") or not page:
            break
        offset += len(page)
    if len(features) >= MAX_VECTOR_FEATURES:
        exceeded = True
    doc = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": item["label"],
            "source_url": item["url"],
            "truncated": exceeded,
            "max_features": MAX_VECTOR_FEATURES,
        },
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(doc, open(out_path, "w"))
    return len(features), exceeded


def grid_projected_bounds(data_dir):
    georef = os.path.join(data_dir, "georef.json")
    grid = json.load(open(os.path.join(data_dir, "terrain", "grid.json")))
    ox, oy = twin_georef.origin(georef)
    return (
        grid["outerMinX"] + ox,
        grid["outerMinY"] + oy,
        grid["outerMaxX"] + ox,
        grid["outerMaxY"] + oy,
    )


def fetch_raster(item, data_dir, out_path):
    bbox = grid_projected_bounds(data_dir)
    epsg = twin_georef.epsg_number(os.path.join(data_dir, "georef.json"))
    res = float(item.get("resolution") or 30)
    w = max(2, round((bbox[2] - bbox[0]) / res))
    h = max(2, round((bbox[3] - bbox[1]) / res))
    while w * h > 4_000_000:
        res *= 1.5
        w = max(2, round((bbox[2] - bbox[0]) / res))
        h = max(2, round((bbox[3] - bbox[1]) / res))
    params = {
        "f": "image",
        "bbox": "%f,%f,%f,%f" % bbox,
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": "%d,%d" % (w, h),
        "format": "tiff",
        "pixelType": item.get("pixel_type", "U16"),
        "interpolation": "RSP_NearestNeighbor",
    }
    url = item["url"] + "/exportImage?" + urllib.parse.urlencode(params)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp, open(out_path, "wb") as fh:
        fh.write(resp.read())
    if gdal.Open(out_path) is None:
        raise RuntimeError(f"GDAL could not read exported raster {out_path}")
    return {"resolution_m": res, "width": w, "height": h}


def fetch_wcs_raster(item, data_dir, out_path):
    bbox = grid_projected_bounds(data_dir)
    epsg = twin_georef.epsg_number(os.path.join(data_dir, "georef.json"))
    res = float(item.get("resolution") or 30)
    w = max(2, round((bbox[2] - bbox[0]) / res))
    h = max(2, round((bbox[3] - bbox[1]) / res))
    while w * h > 4_000_000:
        res *= 1.5
        w = max(2, round((bbox[2] - bbox[0]) / res))
        h = max(2, round((bbox[3] - bbox[1]) / res))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ds = gdal.Warp(
        out_path,
        wcs_source(item),
        format="GTiff",
        dstSRS=f"EPSG:{epsg}",
        outputBounds=bbox,
        xRes=res,
        yRes=res,
        resampleAlg="near",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    if ds is None:
        raise RuntimeError(f"GDAL could not fetch WCS raster {item['id']}")
    width, height = ds.RasterXSize, ds.RasterYSize
    ds = None
    if gdal.Open(out_path) is None:
        raise RuntimeError(f"GDAL could not read WCS raster {out_path}")
    return {"resolution_m": res, "width": width, "height": height}


def download_file(item, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    info = direct_download_status(item, base_dir=os.path.dirname(out_path) or PROJECT)
    if not info["ok"]:
        raise RuntimeError(info["reason"])
    max_bytes = int(item.get("max_bytes") or DIRECT_DOWNLOAD_MAX_BYTES)
    req = urllib.request.Request(item["source_url"], headers=UA)
    downloaded = 0
    with urllib.request.urlopen(req, timeout=180) as resp, open(out_path, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise RuntimeError(f"download exceeded {fmt_bytes(max_bytes)} limit")
            fh.write(chunk)
    if info.get("bytes") is not None and downloaded != info["bytes"]:
        raise RuntimeError(f"downloaded {fmt_bytes(downloaded)} but expected {fmt_bytes(info['bytes'])}")
    return {**info, "downloaded_bytes": downloaded}


def fetch_file_download(item, raw_dir, cache):
    source_url = item["source_url"]
    cache_key = (source_url, item.get("source_member"))
    if cache_key in cache:
        return cache[cache_key]["import_path"], cache[cache_key]["meta"]
    parsed = urllib.parse.urlparse(source_url)
    filename = item.get("source_filename") or os.path.basename(parsed.path) or (item["id"] + ".dat")
    suffix = os.path.splitext(filename)[1] or ".dat"
    out_path = os.path.join(raw_dir, item["id"] + suffix)
    cleanup_paths = [out_path]
    try:
        meta = download_file(item, out_path)
        import_path = out_path
        if suffix.lower() == ".zip":
            extract_dir = os.path.join(raw_dir, item["id"] + "_extracted")
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir)
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(out_path) as zf:
                zf.extractall(extract_dir)
            cleanup_paths.append(extract_dir)
            import_path = find_download_source(extract_dir, item)
        try:
            ds = gdal.OpenEx(import_path, gdal.OF_VECTOR | gdal.OF_RASTER)
        except RuntimeError:
            ds = None
        if ds is None:
            raise RuntimeError(f"GDAL could not read downloaded source {import_path}")
        ds = None
        meta = {**meta, "cleanup_paths": cleanup_paths}
        cache[cache_key] = {"import_path": import_path, "meta": meta}
        return import_path, meta
    except Exception:
        for path in cleanup_paths:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError:
                pass
        raise


def find_download_source(root, item):
    preferred = item.get("source_member")
    if preferred:
        path = os.path.join(root, preferred)
        if os.path.exists(path):
            return path
        raise RuntimeError(f"download did not contain expected member {preferred}")
    candidates = []
    priority = {".shp": 0, ".gpkg": 1, ".geojson": 2, ".json": 3, ".tif": 4, ".tiff": 4}
    for dirpath, dirs, files in os.walk(root):
        for dirname in dirs:
            if dirname.lower().endswith(".gdb"):
                candidates.append((-1, os.path.join(dirpath, dirname)))
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in priority:
                candidates.append((priority[ext], os.path.join(dirpath, name)))
    if not candidates:
        raise RuntimeError("download archive did not contain a GDAL-readable vector/raster member")
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][1]


def find_gap_raster_source(root, item):
    code = (item.get("code") or "").lower()
    candidates = []
    for dirpath, dirs, files in os.walk(root):
        names = list(files) + list(dirs)
        for name in names:
            path = os.path.join(dirpath, name)
            lname = name.lower()
            if code and code not in lname and "habmap" not in lname:
                continue
            try:
                ds = gdal.OpenEx(path, gdal.OF_RASTER)
            except RuntimeError:
                ds = None
            if ds is not None and ds.RasterCount > 0:
                ds = None
                rank = 0 if code and lname.startswith(code) else 1
                candidates.append((rank, path))
    if not candidates:
        for dirpath, dirs, files in os.walk(root):
            for name in list(files) + list(dirs):
                path = os.path.join(dirpath, name)
                try:
                    ds = gdal.OpenEx(path, gdal.OF_RASTER)
                except RuntimeError:
                    ds = None
                if ds is not None and ds.RasterCount > 0:
                    ds = None
                    candidates.append((2, path))
    if not candidates:
        raise RuntimeError(f"download archive did not contain a GDAL-readable GAP raster for {item.get('code')}")
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][1]


def gap_species_output_path(data_dir, item):
    return os.path.join(data_dir, "atlas", "gap_species", item["code"] + ".tif")


def fetch_gap_species(item, data_dir, raw_dir, cache):
    src_item = gap_item_with_source(item)
    source_url = src_item["source_url"]
    if source_url in cache:
        source = cache[source_url]
    else:
        filename = src_item.get("source_filename") or (item["code"] + ".zip")
        out_path = os.path.join(raw_dir, item["id"] + os.path.splitext(filename)[1])
        meta = download_file(src_item, out_path)
        extract_dir = os.path.join(raw_dir, item["id"] + "_extracted")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(out_path) as zf:
            zf.extractall(extract_dir)
        source = {
            "archive": out_path,
            "extract_dir": extract_dir,
            "raster": find_gap_raster_source(extract_dir, item),
            "meta": meta,
        }
        cache[source_url] = source

    bounds = grid_projected_bounds(data_dir)
    epsg = twin_georef.epsg_number(os.path.join(data_dir, "georef.json"))
    out_tif = gap_species_output_path(data_dir, item)
    os.makedirs(os.path.dirname(out_tif), exist_ok=True)
    ds = gdal.Warp(
        out_tif,
        source["raster"],
        format="GTiff",
        dstSRS=f"EPSG:{epsg}",
        outputBounds=bounds,
        xRes=GAP_RASTER_RESOLUTION_M,
        yRes=GAP_RASTER_RESOLUTION_M,
        resampleAlg="near",
        dstNodata=0,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    if ds is None:
        raise RuntimeError(f"GDAL could not clip GAP species raster {item['code']}")
    width, height = ds.RasterXSize, ds.RasterYSize
    ds = None
    return {
        "code": item["code"],
        "common_name": item["common_name"],
        "scientific_name": item["scientific_name"],
        "download_bytes": source["meta"].get("downloaded_bytes"),
        "processed_bytes": os.path.getsize(out_tif) if os.path.exists(out_tif) else None,
        "width": width,
        "height": height,
    }


def raster_local_bounds(path, data_dir):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    ox, oy = twin_georef.origin(os.path.join(data_dir, "georef.json"))
    xs = [gt[0], gt[0] + gt[1] * w, gt[0] + gt[2] * h, gt[0] + gt[1] * w + gt[2] * h]
    ys = [gt[3], gt[3] + gt[4] * w, gt[3] + gt[5] * h, gt[3] + gt[4] * w + gt[5] * h]
    ds = None
    return [round(min(xs) - ox, 2), round(min(ys) - oy, 2),
            round(max(xs) - ox, 2), round(max(ys) - oy, 2)]


def write_gap_species_assets(data_dir, species_rows):
    atlas = os.path.join(data_dir, "atlas")
    species_dir = os.path.join(atlas, "gap_species")
    summary_path = os.path.join(atlas, "gap_species_habitat.json")
    existing = []
    if os.path.exists(summary_path):
        try:
            existing = json.load(open(summary_path)).get("species") or []
        except Exception:
            existing = []
    by_code = {row.get("code"): row for row in existing if row.get("code")}
    for row in species_rows:
        by_code[row["code"]] = {**row, "present": True}
    summary = {"source": "USGS GAP Species Habitat Maps CONUS_2001",
               "species": sorted(by_code.values(), key=lambda r: r.get("common_name", ""))}
    os.makedirs(atlas, exist_ok=True)
    json.dump(summary, open(summary_path, "w"), indent=2)

    arrays = []
    first_path = None
    for row in summary["species"]:
        if not row.get("present"):
            continue
        tif = os.path.join(species_dir, row["code"] + ".tif")
        if not os.path.exists(tif):
            continue
        ds = gdal.Open(tif)
        arr = ds.GetRasterBand(1).ReadAsArray()
        nd = ds.GetRasterBand(1).GetNoDataValue()
        mask = (arr > 0) if nd is None else ((arr != nd) & (arr > 0))
        arrays.append(mask.astype(np.uint16))
        if first_path is None:
            first_path = tif
            projection = ds.GetProjection()
            geotransform = ds.GetGeoTransform()
        ds = None
    if not arrays:
        return {"species_count": 0, "processed_bytes": 0}

    richness = np.sum(arrays, axis=0).astype(np.uint16)
    richness_path = os.path.join(atlas, "gap_species_richness.tif")
    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(richness_path, richness.shape[1], richness.shape[0], 1, gdal.GDT_UInt16,
                        options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(geotransform)
    out.SetProjection(projection)
    band = out.GetRasterBand(1)
    band.WriteArray(richness)
    band.SetNoDataValue(0)
    out = None

    local_dir = os.path.join(atlas, "local")
    os.makedirs(local_dir, exist_ok=True)
    grids = {}
    bounds = raster_local_bounds(first_path, data_dir)
    for row in summary["species"]:
        tif = os.path.join(species_dir, row["code"] + ".tif")
        if not os.path.exists(tif):
            continue
        ds = gdal.Open(tif)
        arr = ds.GetRasterBand(1).ReadAsArray()
        nd = ds.GetRasterBand(1).GetNoDataValue()
        mask = (arr > 0) if nd is None else ((arr != nd) & (arr > 0))
        ds = None
        grids[row["code"]] = {
            "common_name": row["common_name"],
            "scientific_name": row["scientific_name"],
            "rows": ["".join("1" if x else "0" for x in line) for line in mask.tolist()],
        }
    json.dump({"bounds_local": bounds, "height": richness.shape[0], "width": richness.shape[1],
               "species": grids},
              open(os.path.join(local_dir, "gap_species_grids.json"), "w"))
    add_layer(data_dir, richness_path, {"id": "gap_species_richness", "label": "GAP Species Richness"})
    viewer_path = os.path.join(local_dir, "viewer-layers.json")
    if os.path.exists(viewer_path):
        viewer = json.load(open(viewer_path))
        viewer["gap_species_grids"] = "atlas/local/gap_species_grids.json"
        viewer["gap_species_count"] = len(grids)
        json.dump(viewer, open(viewer_path, "w"), indent=2)
    return {
        "species_count": len(grids),
        "processed_bytes": sum(os.path.getsize(os.path.join(species_dir, row["code"] + ".tif"))
                               for row in summary["species"]
                               if os.path.exists(os.path.join(species_dir, row["code"] + ".tif")))
        + os.path.getsize(richness_path),
    }


def add_layer(data_dir, src, item):
    cmd = [
        sys.executable,
        os.path.join(PROJECT, "scripts", "add_layer.py"),
        src,
        "--id", item["id"],
        "--label", item["label"],
        "--data-dir", data_dir,
    ]
    if item.get("label_field"):
        cmd.extend(["--label-field", item["label_field"]])
    if item.get("description"):
        cmd.extend(["--description", item["description"]])
    if item.get("uses"):
        cmd.extend(["--uses", item["uses"]])
    if item.get("value_kind"):
        cmd.extend(["--value-kind", item["value_kind"]])
    if item.get("value_unit"):
        cmd.extend(["--value-unit", item["value_unit"]])
    if item.get("value_classification"):
        cmd.extend(["--value-classification", item["value_classification"]])
    if item.get("source_layer"):
        cmd.extend(["--layer", item["source_layer"]])
    subprocess.run(cmd, check=True, cwd=PROJECT,
                   env={**os.environ, "TWIN_DATA_DIR": data_dir})


def fetch_selected(aoi_path, data_dir, layer_ids):
    by_id = {item["id"]: item for item in expanded_catalog_items()}
    geom, _bbox = read_aoi_wgs84(aoi_path)
    aoi_arcgis = arcgis_polygon_geometry(geom)
    raw_dir = os.path.join(data_dir, "atlas", "raw", "national_layers")
    results = []
    download_cache = {}
    gap_cache = {}
    gap_species_rows = []
    try:
        for layer_id in layer_ids:
            item = by_id.get(layer_id)
            if not item:
                results.append({"id": layer_id, "status": "error", "error": "unknown layer id"})
                continue
            if item["kind"] in {"manual", "downloadable"}:
                results.append({"id": layer_id, "status": "skipped", "reason": item.get("reason")})
                continue
            started = time.time()
            try:
                if item["kind"] == "arcgis_vector":
                    src = os.path.join(raw_dir, item["id"] + ".geojson")
                    n, truncated = fetch_vector(item, aoi_arcgis, src)
                    if n == 0:
                        results.append({"id": item["id"], "status": "empty", "feature_count": 0})
                        continue
                    add_layer(data_dir, src, item)
                    results.append({"id": item["id"], "status": "ok", "feature_count": n,
                                    "truncated": truncated, "seconds": round(time.time() - started, 1)})
                elif item["kind"] == "arcgis_raster":
                    src = os.path.join(raw_dir, item["id"] + ".tif")
                    meta = fetch_raster(item, data_dir, src)
                    if item.get("value_classification") == "categorical":
                        vat = arcgis_raster_vat(item)
                        if not vat:
                            raise RuntimeError(f"no value metadata found for categorical raster {item['id']}")
                        write_vat_sidecar(data_dir, item["id"], vat)
                        write_raster_metadata_sidecar(data_dir, item, require_value_metadata=True)
                    else:
                        write_raster_metadata_sidecar(data_dir, item)
                    add_layer(data_dir, src, item)
                    results.append({"id": item["id"], "status": "ok", **meta,
                                    "seconds": round(time.time() - started, 1)})
                elif item["kind"] == "wcs_raster":
                    src = os.path.join(raw_dir, item["id"] + ".tif")
                    meta = fetch_wcs_raster(item, data_dir, src)
                    write_raster_metadata_sidecar(data_dir, item)
                    add_layer(data_dir, src, item)
                    results.append({"id": item["id"], "status": "ok", **meta,
                                    "seconds": round(time.time() - started, 1)})
                elif item["kind"] == "file_download":
                    src, meta = fetch_file_download(item, raw_dir, download_cache)
                    add_layer(data_dir, src, item)
                    results.append({"id": item["id"], "status": "ok",
                                    "download_bytes": meta.get("downloaded_bytes"),
                                    "seconds": round(time.time() - started, 1)})
                elif item["kind"] == "gap_species":
                    meta = fetch_gap_species(item, data_dir, raw_dir, gap_cache)
                    gap_species_rows.append(meta)
                    results.append({"id": item["id"], "status": "ok",
                                    "download_bytes": meta.get("download_bytes"),
                                    "processed_bytes": meta.get("processed_bytes"),
                                    "width": meta.get("width"), "height": meta.get("height"),
                                    "seconds": round(time.time() - started, 1)})
                else:
                    results.append({"id": item["id"], "status": "skipped",
                                    "reason": f"unsupported kind {item['kind']}"})
            except Exception as err:  # noqa: BLE001
                results.append({"id": item["id"], "status": "error", "error": str(err)[:500]})
        if gap_species_rows:
            started = time.time()
            meta = write_gap_species_assets(data_dir, gap_species_rows)
            results.append({"id": "gap_species_richness", "status": "ok", **meta,
                            "seconds": round(time.time() - started, 1)})
    finally:
        for cached in download_cache.values():
            meta = cached.get("meta") or {}
            if meta.get("cleanup_paths"):
                for path in meta["cleanup_paths"]:
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        else:
                            os.remove(path)
                    except OSError:
                        pass
        for cached in gap_cache.values():
            for path in (cached.get("archive"), cached.get("extract_dir")):
                if not path:
                    continue
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except OSError:
                    pass
    provenance = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "selected_layers": layer_ids,
        "results": results,
    }
    os.makedirs(raw_dir, exist_ok=True)
    json.dump(provenance, open(os.path.join(raw_dir, "fetch_results.json"), "w"), indent=2)
    return {"ok": True, "results": results}


def check_downloads():
    results = []
    for item in CATALOG:
        if item.get("kind") != "file_download":
            continue
        try:
            info = direct_download_status(item)
            results.append({
                "id": item["id"],
                "ok": info["ok"],
                "bytes": info.get("bytes"),
                "size": fmt_bytes(info.get("bytes")),
                "download_class": download_class(info.get("bytes")),
                "large_file": is_large_download(info.get("bytes")),
                "content_type": info.get("content_type"),
                "reason": info.get("reason"),
                "url": info.get("url"),
            })
        except Exception as err:  # noqa: BLE001
            results.append({"id": item["id"], "ok": False, "error": str(err)[:500]})
    return {"ok": all(r.get("ok") for r in results), "downloads": results}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    sub.add_parser("check-downloads")
    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--aoi", required=True)
    p_probe.add_argument("--progress", action="store_true",
                         help="stream one NDJSON event per layer to stdout as it resolves")
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--aoi", required=True)
    p_fetch.add_argument("--data-dir", required=True)
    p_fetch.add_argument("--layers", required=True,
                         help="comma-separated layer ids selected by the setup UI")
    args = ap.parse_args()

    if args.cmd == "catalog":
        print(json.dumps({"ok": True, "layers": catalog_public()}, indent=2))
    elif args.cmd == "check-downloads":
        print(json.dumps(check_downloads(), indent=2))
    elif args.cmd == "probe":
        if args.progress:
            # NDJSON stream: start -> one layer event each -> done (or error).
            def emit(evt):
                print(json.dumps(evt), flush=True)
            emit({"event": "start", "total": len(CATALOG)})
            try:
                result = probe(args.aoi, progress=lambda res: emit({"event": "layer", "layer": res}))
            except Exception as err:  # noqa: BLE001
                emit({"event": "error", "error": str(err)[:300]})
                return 1
            emit({"event": "done", **result})
        else:
            print(json.dumps(probe(args.aoi), indent=2))
    elif args.cmd == "fetch":
        ids = [s.strip() for s in args.layers.split(",") if s.strip()]
        print(json.dumps(fetch_selected(args.aoi, os.path.abspath(args.data_dir), ids), indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
