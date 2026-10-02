# -*- coding: utf-8 -*-
"""
0.3 Validate County Inputs
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Validate PRE-PROCESSING county inputs only.

This tool does NOT create, accept, or validate any HUC12 / watershed
processing extent. That logic belongs entirely to Step 1.2.

Validated here
--------------
- Project Config / County Config consistency
- Raw DEM tile folder used by Step 1.1
- Selected county boundary feature class
- County-selected NHD raster used later by DEM reconditioning

ArcGIS Script Tool Parameters
-----------------------------
0  Project Config File      File     Required Input
1  County Config File       File     Required Input
2  Raw DEM Tile Folder      Folder   Optional Input
3  Validation Report        File     Derived Output
4  Validation Status        String   Derived Output
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
    save_json,
    load_json,
    require_geometry,
)


def p(index):
    value = arcpy.GetParameterAsText(index)
    return value.strip() if value else ""


def normalized_path(path):
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path))


def validate_config_chain(project_config_path, project, county):
    linked_project = county.get("project_config", "")

    if not linked_project:
        fail("County Config does not contain a project_config reference.")

    if normalized_path(linked_project) != normalized_path(project_config_path):
        fail(
            "Project Config / County Config mismatch.\n"
            f"Selected Project Config: {project_config_path}\n"
            f"County Config points to: {linked_project}"
        )

    if "sources" not in project:
        fail("Project Config does not contain a sources section.")

    if "county" not in county:
        fail("County Config does not contain a county section.")


def validate_raw_dem_folder(folder_path, validated, failures):
    log("Checking raw DEM tile folder...")

    if not os.path.isdir(folder_path):
        failures.append(f"Raw DEM tile folder missing: {folder_path}")
        return

    raster_tiles = [
        filename
        for filename in os.listdir(folder_path)
        if filename.lower().endswith((".tif", ".tiff", ".img"))
    ]

    if not raster_tiles:
        failures.append(
            f"Raw DEM folder contains no .tif/.tiff/.img raster tiles: "
            f"{folder_path}"
        )
        return

    validated.append(
        {
            "item": "Raw DEM Tile Folder",
            "path": folder_path,
            "tile_count": len(raster_tiles),
        }
    )

    log(
        f"PASS - Raw DEM Tile Folder "
        f"({len(raster_tiles)} candidate tiles): {folder_path}"
    )


def resolve_and_validate_county_boundary(
    project,
    county_abbr,
    validated,
    failures
):
    log("Checking county boundary...")

    try:
        county_boundary_dataset = project["tools_source"]["OH_County_Boundary"]
    except Exception:
        failures.append(
            "Project Config does not contain "
            "tools_source > OH_County_Boundary."
        )
        return None

    if not arcpy.Exists(county_boundary_dataset):
        failures.append(
            f"OH_County_Boundary feature dataset missing: "
            f"{county_boundary_dataset}"
        )
        return None

    county_boundary = os.path.join(county_boundary_dataset, county_abbr)

    if not arcpy.Exists(county_boundary):
        failures.append(
            f"County boundary feature class missing for "
            f"{county_abbr}: {county_boundary}"
        )
        return county_boundary

    try:
        require_geometry(county_boundary, {"Polygon"})
    except Exception as exc:
        failures.append(
            f"County boundary exists but failed validation: "
            f"{county_boundary}. Reason: {exc}"
        )
        return county_boundary

    count = int(arcpy.management.GetCount(county_boundary)[0])

    if count <= 0:
        failures.append(
            f"County boundary contains no features: {county_boundary}"
        )
        return county_boundary

    validated.append(
        {
            "item": "County Boundary",
            "path": county_boundary,
            "feature_count": count,
        }
    )

    log(f"PASS - County Boundary: {county_boundary}")

    return county_boundary


def validate_nhd_raster(county, validated, failures):
    log("Checking selected NHD raster...")

    try:
        nhd_raster = county["selected_sources"]["nhd_stream_raster"]
    except Exception:
        failures.append(
            "County Config does not contain "
            "selected_sources > nhd_stream_raster."
        )
        return None

    if not arcpy.Exists(nhd_raster):
        failures.append(f"Selected NHD raster missing: {nhd_raster}")
        return nhd_raster

    try:
        desc = arcpy.Describe(nhd_raster)
        data_type = getattr(desc, "dataType", None)

        if data_type not in {
            "RasterDataset",
            "RasterLayer",
            "MosaicDataset",
        }:
            failures.append(
                f"Selected NHD source is not a supported raster. "
                f"dataType={data_type}: {nhd_raster}"
            )
            return nhd_raster

    except Exception as exc:
        failures.append(
            f"Could not describe selected NHD raster: "
            f"{nhd_raster}. Reason: {exc}"
        )
        return nhd_raster

    validated.append(
        {
            "item": "Selected NHD Raster",
            "path": nhd_raster,
        }
    )

    log(f"PASS - Selected NHD Raster: {nhd_raster}")

    return nhd_raster


def main():
    project_config_path = p(0)
    county_config_path = p(1)
    raw_dem_override = p(2)

    if not project_config_path:
        fail("Project Config File is required.")

    if not county_config_path:
        fail("County Config File is required.")

    project = load_json(project_config_path)
    county = load_json(county_config_path)

    validate_config_chain(
        project_config_path,
        project,
        county
    )

    county_info = county["county"]

    county_name = str(county_info["name"]).strip().title()
    county_abbr = str(county_info["abbr"]).strip().upper()
    state_plane = str(county_info["state_plane"]).strip().upper()
    target_wkid = int(county_info["wkid"])

    log("=" * 72)
    log("0.3 Validate County Inputs")
    log(f"County: {county_name} ({county_abbr})")
    log(f"State Plane: {state_plane}")
    log(f"Production WKID: {target_wkid}")
    log("=" * 72)

    sources = project["sources"]

    if raw_dem_override:
        raw_dem_folder = raw_dem_override
    else:
        raw_dem_folder = os.path.join(
            sources["dem_by_county_root"],
            county_abbr,
            "img"
        )

    validated = []
    warnings = []
    failures = []

    validate_raw_dem_folder(
        raw_dem_folder,
        validated,
        failures
    )

    county_boundary = resolve_and_validate_county_boundary(
        project,
        county_abbr,
        validated,
        failures
    )

    nhd_raster = validate_nhd_raster(
        county,
        validated,
        failures
    )

    if failures:
        status = "FAIL"
    elif warnings:
        status = "WARN"
    else:
        status = "PASS"

    report_folder = county["paths"]["log_folder"]

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    validation_report = os.path.join(
        report_folder,
        f"{county_abbr}_input_validation_{timestamp}.json"
    )

    report = {
        "project_config": project_config_path,
        "county_config": county_config_path,
        "county": county_info,
        "validation_status": status,

        "resolved_inputs": {
            "raw_dem_tile_folder": raw_dem_folder,
            "county_boundary": county_boundary,
            "nhd_stream_raster": nhd_raster,
        },

        "validated": validated,
        "warnings": warnings,
        "failures": failures,
    }

    save_json(report, validation_report)

    arcpy.SetParameterAsText(3, validation_report)
    arcpy.SetParameterAsText(4, status)

    if failures:
        failure_text = "\n".join(
            f" - {message}"
            for message in failures
        )

        fail(
            "County input validation failed:\n"
            + failure_text
        )

    log(f"0.3 validation complete: {status}")
    log(f"Validation report: {validation_report}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            arcpy.SetParameterAsText(4, "FAIL")
        except Exception:
            pass
        raise