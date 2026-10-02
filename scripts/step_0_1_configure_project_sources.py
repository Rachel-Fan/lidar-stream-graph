# -*- coding: utf-8 -*-
"""
0.1 Configure Project Sources
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

No new UI parameter is added for WBD_HUC12.
The tool automatically resolves:
    <Tools Source GDB>\WBD_HUC12

ArcGIS Script Tool Parameters
-----------------------------
0  Project Config Folder             Folder           Required Input
1  Ohio Counties Reference           Feature Layer    Required Input
2  DEM by County Root                Folder           Required Input
3  DEM 3-ft Root                     Folder           Required Input
4  Streams by County Root            Folder           Required Input
5  Tools Source GDB                  Workspace        Required Input
6  NHD Flowline Raster Folder        Folder           Required Input
7  Final Deliverable Root            Folder           Required Input
8  Scratch Root                      Folder           Optional Input
9  Project Config File               File             Derived Output
10 Validation Status                 String           Derived Output
"""

import os
import sys
import datetime
import arcpy

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from setup_common import (
    log,
    fail,
    exists,
    ensure_folder,
    save_json,
    require_fields,
    require_geometry,
    default_scratch,
)

COUNTY_FIELDS = ["COUNTY", "COUNTY_CD", "STATE_PLAN", "geomorphic"]

TOOLS_GDB_REQUIRED = [
    "TIMS_road_rail_conduit_bridge_buffer",
    "OH_NHD_5m_River_buffer_500ft_w_Ohio_Maumee",
    "OH_Wetlands_Lakes_Buffer_200ft",
    "OH_NLCD_2021",
    "NLCD_type",
    "MapUnitPolygonsCONUS_OH",
    "CONUS_OH_with_muname",
    "OH_County_Boundary",
    "WBD_HUC12",
]

NHD_REQUIRED = [
    "OH_NHD_w_name_North.tif",
    "OH_NHD_w_name_South.tif",
    "OH_NHD_2m_w_name_North.tif",
    "OH_NHD_2m_w_name_South.tif",
]

HUC12_FIELD_CANDIDATES = ["HUC12", "huc12", "HUC_12"]


def p(index, default=""):
    value = arcpy.GetParameterAsText(index)
    if value and value.strip():
        return value.strip()
    return default


def validate_wbd_huc12(tools_gdb):
    wbd_huc12 = os.path.join(tools_gdb, "WBD_HUC12")

    if not arcpy.Exists(wbd_huc12):
        fail(
            f"Required WBD_HUC12 feature class is missing "
            f"from Tools Source GDB: {wbd_huc12}"
        )

    require_geometry(wbd_huc12, {"Polygon"})

    count = int(arcpy.management.GetCount(wbd_huc12)[0])
    if count <= 0:
        fail(f"WBD_HUC12 contains no features: {wbd_huc12}")

    field_names = [f.name for f in arcpy.ListFields(wbd_huc12)]
    upper_map = {name.upper(): name for name in field_names}

    huc12_field = None
    for candidate in HUC12_FIELD_CANDIDATES:
        if candidate.upper() in upper_map:
            huc12_field = upper_map[candidate.upper()]
            break

    if huc12_field is None:
        fail(
            "WBD_HUC12 is missing a recognizable HUC12 identifier field. "
            f"Expected one of: {HUC12_FIELD_CANDIDATES}"
        )

    sampled = 0
    invalid_samples = []

    with arcpy.da.SearchCursor(wbd_huc12, [huc12_field]) as cursor:
        for row in cursor:
            value = row[0]
            if value is None:
                continue

            text = str(value).strip()
            if not text:
                continue

            sampled += 1

            if len(text) != 12 or not text.isdigit():
                invalid_samples.append(text)

            if sampled >= 100:
                break

    if sampled == 0:
        fail(
            f"WBD_HUC12 field '{huc12_field}' contains no usable values "
            f"in the sampled records."
        )

    if invalid_samples:
        fail(
            f"WBD_HUC12 field '{huc12_field}' failed HUC12 format validation. "
            f"Expected 12-digit HUC codes. "
            f"Example invalid value(s): {invalid_samples[:5]}"
        )

    log(
        f"PASS - WBD_HUC12: {count} polygons, "
        f"HUC12 field='{huc12_field}'"
    )

    return {
        "path": wbd_huc12,
        "huc12_field": huc12_field,
        "feature_count": count,
    }


def main():
    config_folder = p(0)
    ohio_counties = p(1)
    dem_by_county_root = p(2)
    dem_3ft_root = p(3)
    streams_root = p(4)
    tools_gdb = p(5)
    nhd_folder = p(6)
    final_root = p(7)
    scratch_root = p(8, default_scratch())

    log("0.1 Configure Project Sources")

    for label, path in [
        ("Project Config Folder", config_folder),
        ("Ohio Counties Reference", ohio_counties),
        ("DEM by County Root", dem_by_county_root),
        ("DEM 3-ft Root", dem_3ft_root),
        ("Streams by County Root", streams_root),
        ("Tools Source GDB", tools_gdb),
        ("NHD Flowline Raster Folder", nhd_folder),
        ("Final Deliverable Root", final_root),
    ]:
        if not exists(path):
            fail(f"{label} does not exist: {path}")
        log(f"PASS - {label}: {path}")

    require_geometry(ohio_counties, {"Polygon"})
    require_fields(ohio_counties, COUNTY_FIELDS)

    county_count = int(arcpy.management.GetCount(ohio_counties)[0])
    if county_count <= 0:
        fail(f"Ohio Counties Reference contains no features: {ohio_counties}")

    log(f"PASS - Ohio Counties schema ({county_count} features)")

    tool_paths = {}
    for name in TOOLS_GDB_REQUIRED:
        path = os.path.join(tools_gdb, name)
        if not arcpy.Exists(path):
            fail(f"Required Tools Source GDB item missing: {path}")

        tool_paths[name] = path
        log(f"PASS - Tools source exists: {name}")

    wbd_info = validate_wbd_huc12(tools_gdb)

    nhd_paths = {}
    for name in NHD_REQUIRED:
        path = os.path.join(nhd_folder, name)
        if not arcpy.Exists(path):
            fail(f"Required NHD raster missing: {path}")

        nhd_paths[name] = path
        log(f"PASS - NHD raster: {name}")

    ensure_folder(config_folder)
    ensure_folder(scratch_root)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    config_path = os.path.join(
        config_folder,
        f"project_config_{timestamp}.json"
    )

    cfg = {
        "project_name": "ODOT Ohio Stream Delineation",
        "config_version": "1.1",

        "sources": {
            "ohio_counties": ohio_counties,
            "dem_by_county_root": dem_by_county_root,
            "dem_3ft_root": dem_3ft_root,
            "streams_by_county_root": streams_root,
            "tools_source_gdb": tools_gdb,
            "nhd_flowline_raster_folder": nhd_folder,
            "final_deliverable_root": final_root,
            "scratch_root": scratch_root,
            "statewide_huc12": wbd_info["path"],
        },

        "tools_source": tool_paths,

        "nhd_rasters": nhd_paths,

        "county_fields": {
            "name": "COUNTY",
            "abbr": "COUNTY_CD",
            "state_plane": "STATE_PLAN",
            "geomorphic": "geomorphic",
        },

        "wbd_huc12": {
            "path": wbd_info["path"],
            "huc12_field": wbd_info["huc12_field"],
            "feature_count": wbd_info["feature_count"],
        },
    }

    save_json(cfg, config_path)

    log(f"Project configuration created: {config_path}")

    arcpy.SetParameterAsText(9, config_path)
    arcpy.SetParameterAsText(10, "PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            arcpy.SetParameterAsText(10, "FAIL")
        except Exception:
            pass
        raise