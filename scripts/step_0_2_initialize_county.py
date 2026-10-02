# -*- coding: utf-8 -*-
"""
0.2 Initialize County
Case-insensitive county lookup fix.

Parameters
----------
0 Project Config File        File     Required Input
1 County Name                String   Required Input
2 County Config File         File     Derived Output
3 County Abbreviation        String   Derived Output
4 Validation Status          String   Derived Output
"""

import os
import sys
import arcpy

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from setup_common import (
    log,
    fail,
    ensure_folder,
    save_json,
    load_json,
    require_fields,
)


def p(index):
    value = arcpy.GetParameterAsText(index)
    return value.strip() if value else ""


def main():
    project_path = p(0)
    county_name_input = p(1)

    if not project_path:
        fail("Project Config File is required.")

    if not county_name_input:
        fail("County Name is required.")

    project = load_json(project_path)

    ohio = project["sources"]["ohio_counties"]

    if not arcpy.Exists(ohio):
        fail(f"Ohio Counties dataset no longer exists: {ohio}")

    require_fields(
        ohio,
        ["COUNTY", "COUNTY_CD", "STATE_PLAN", "geomorphic"]
    )

    # ---------------------------------------------------------
    # IMPORTANT:
    # Do NOT use an exact SQL WHERE clause here.
    #
    # The source COUNTY field may contain values such as "WOOD"
    # while the ToolValidator displays "Wood".
    # With shapefiles, an exact text comparison can therefore
    # return zero rows depending on stored capitalization.
    #
    # There are only 88 Ohio counties, so reading all rows and
    # matching normalized text is simple and robust.
    # ---------------------------------------------------------
    target = county_name_input.strip().casefold()

    matches = []

    with arcpy.da.SearchCursor(
        ohio,
        ["COUNTY", "COUNTY_CD", "STATE_PLAN", "geomorphic"]
    ) as cursor:
        for row in cursor:
            raw_name = row[0]

            if raw_name is None:
                continue

            if str(raw_name).strip().casefold() == target:
                matches.append(row)

    if len(matches) != 1:
        fail(
            f"Expected exactly one county record matching "
            f"'{county_name_input}'. Found {len(matches)}."
        )

    name, abbr, sp, gm = matches[0]

    county_name = str(name).strip().title()
    county_abbr = str(abbr).strip().upper()

    sp = str(sp).strip().upper()
    gm = str(gm).strip().upper()

    if sp.startswith("N"):
        state_plan = "N"
        wkid = 6549
    elif sp.startswith("S"):
        state_plan = "S"
        wkid = 6551
    else:
        fail(f"Unsupported STATE_PLAN: {sp}")

    if gm in {"L", "LO", "LOW"}:
        geomorphic = "LOW"
    elif gm in {"H", "HI", "HIGH"}:
        geomorphic = "HIGH"
    else:
        fail(f"Unsupported geomorphic value: {gm}")

    nhd_map = {
        ("N", "LOW"): "OH_NHD_w_name_North.tif",
        ("S", "LOW"): "OH_NHD_w_name_South.tif",
        ("N", "HIGH"): "OH_NHD_2m_w_name_North.tif",
        ("S", "HIGH"): "OH_NHD_2m_w_name_South.tif",
    }

    nhd_name = nhd_map[(state_plan, geomorphic)]

    if nhd_name not in project["nhd_rasters"]:
        fail(
            f"Required NHD raster is not recorded in project config: "
            f"{nhd_name}"
        )

    nhd = project["nhd_rasters"][nhd_name]

    if not arcpy.Exists(nhd):
        fail(f"Selected NHD raster no longer exists: {nhd}")

    county_folder = os.path.join(
        project["sources"]["streams_by_county_root"],
        county_name
    )

    config_folder = os.path.join(
        county_folder,
        "config"
    )

    log_folder = os.path.join(
        county_folder,
        "logs"
    )

    scratch = os.path.join(
        project["sources"]["scratch_root"],
        county_abbr
    )

    for folder in [
        county_folder,
        config_folder,
        log_folder,
        scratch,
    ]:
        ensure_folder(folder)

    county_config_path = os.path.join(
        config_folder,
        f"{county_abbr}_county_config.json"
    )

    cfg = {
        "project_config": project_path,

        "county": {
            "name": county_name,
            "abbr": county_abbr,
            "state_plane": state_plan,
            "geomorphic": geomorphic,
            "wkid": wkid,
        },

        "paths": {
            "county_folder": county_folder,
            "config_folder": config_folder,
            "log_folder": log_folder,
            "scratch_root": scratch,
        },

        "selected_sources": {
            "ohio_counties": ohio,
            "nhd_stream_raster": nhd,
        }
    }

    save_json(
        cfg,
        county_config_path
    )

    log(f"Initialized county: {county_name} ({county_abbr})")
    log(f"STATE_PLAN: {state_plan}")
    log(f"Geomorphic: {geomorphic}")
    log(f"WKID: {wkid}")
    log(f"NHD raster: {nhd}")
    log(f"County config: {county_config_path}")

    arcpy.SetParameterAsText(
        2,
        county_config_path
    )

    arcpy.SetParameterAsText(
        3,
        county_abbr
    )

    arcpy.SetParameterAsText(
        4,
        "PASS"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception:
        try:
            arcpy.SetParameterAsText(
                4,
                "FAIL"
            )
        except Exception:
            pass

        raise