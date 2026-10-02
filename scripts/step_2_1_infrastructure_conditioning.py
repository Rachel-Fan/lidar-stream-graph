# -*- coding: utf-8 -*-
"""
2.1 Apply Transportation Infrastructure Conditioning
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Start a NEW timestamped processing run for the selected county, then apply
transportation-aware elevation conditioning to the reusable county-level
HUC12 DEM created by Step 1.2.

Architecture
------------
Step 1.2 reusable input:
    <County Folder>\raster\<ABBR>_DEM_HUC12.tif

Step 2.1 creates:
    <County Folder>\working\<County>_<YYYYMMDD_HHMMSS>\

Inside that run folder:
    run_config.json
    logs\
    <ABBR>_DEM_HUC12_with_Zonal.tif

All downstream tools (2.2 onward) should use the Processing Run Folder as
their explicit input.

Original processing method preserved
------------------------------------
1. Select TIMS infrastructure polygons intersecting the DEM extent.
2. ZonalStatistics using MINIMUM elevation by SFN.
3. Overlay zonal-minimum values on the HUC12 DEM.
4. Where no zonal value exists, retain the original DEM.
5. If no infrastructure polygons intersect, copy the HUC12 DEM directly.

ArcGIS Script Tool Parameters
-----------------------------
0  County Processing Folder      Folder          Required Input
1  Overwrite Existing Output     Boolean         Optional Input
2  Processing Run Folder         Folder          Derived Output
3  Conditioned DEM               Raster Dataset  Derived Output
4  Processing Status             String          Derived Output
"""

import os
import sys
import glob
import time
import datetime

import arcpy
from arcpy.sa import ZonalStatistics, Con, IsNull


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


from setup_common import (
    log,
    warn,
    fail,
    ensure_folder,
    save_json,
    load_json,
    init_tool_log,
    finish_tool_log,
    log_step_start,
    log_step_end,
    log_exception,
)


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

ZONE_SOURCE_NAME = "TIMS_road_rail_conduit_bridge_buffer"
ZONE_FIELD = "SFN"
STATS_TYPE = "MINIMUM"
TARGET_CELL_SIZE_FT = 3.0


# ---------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------

def p(index):
    value = arcpy.GetParameterAsText(index)
    return value.strip() if value else ""


def get_bool(index, default=False):
    try:
        value = arcpy.GetParameter(index)
        if value is None:
            return default
        return bool(value)
    except Exception:
        text = p(index).lower()
        if not text:
            return default
        return text in {"true", "1", "yes", "y"}


# ---------------------------------------------------------------------
# Context discovery
# ---------------------------------------------------------------------

def discover_county_config(county_folder):

    config_folder = os.path.join(
        county_folder,
        "config"
    )

    if not os.path.isdir(config_folder):
        fail(
            f"County config folder does not exist: "
            f"{config_folder}"
        )

    candidates = sorted(
        glob.glob(
            os.path.join(
                config_folder,
                "*_county_config.json"
            )
        )
    )

    if len(candidates) == 0:
        fail(
            f"No county config JSON found in: "
            f"{config_folder}"
        )

    if len(candidates) > 1:
        fail(
            f"Expected exactly one county config JSON in "
            f"{config_folder}, but found {len(candidates)}:\n"
            +
            "\n".join(
                f" - {candidate}"
                for candidate in candidates
            )
        )

    return candidates[0]


def load_processing_context(county_folder):

    county_config_path = discover_county_config(
        county_folder
    )

    county = load_json(
        county_config_path
    )

    county_info = county.get(
        "county",
        {}
    )

    county_name = str(
        county_info.get("name", "")
    ).strip().title()

    county_abbr = str(
        county_info.get("abbr", "")
    ).strip().upper()

    if not county_name or not county_abbr:
        fail(
            "County Config is missing county name "
            "or abbreviation."
        )

    folder_name = os.path.basename(
        os.path.normpath(county_folder)
    )

    if folder_name.casefold() != county_name.casefold():
        fail(
            "County Processing Folder / County Config mismatch.\n"
            f"Folder: {county_folder}\n"
            f"County Config says: "
            f"{county_name} ({county_abbr})"
        )

    project_config_path = county.get(
        "project_config",
        ""
    )

    if not project_config_path:
        fail(
            "County Config does not contain project_config."
        )

    if not os.path.isfile(project_config_path):
        fail(
            "Project Config referenced by County Config "
            "does not exist:\n"
            f"{project_config_path}"
        )

    project = load_json(
        project_config_path
    )

    return {
        "county_config_path": county_config_path,
        "county": county,
        "project_config_path": project_config_path,
        "project": project,
    }


# ---------------------------------------------------------------------
# Raster / feature validation
# ---------------------------------------------------------------------

def inspect_raster(
    raster_path,
    expected_wkid=None,
    expected_cell_size=None
):

    result = {
        "exists": False,
        "valid": False,
        "wkid": None,
        "cell_x": None,
        "cell_y": None,
        "issues": [],
    }

    if not arcpy.Exists(raster_path):
        result["issues"].append(
            "Raster does not exist."
        )
        return result

    result["exists"] = True

    try:
        desc = arcpy.Describe(
            raster_path
        )

        result["wkid"] = getattr(
            desc.spatialReference,
            "factoryCode",
            None
        )

        raster = arcpy.Raster(
            raster_path
        )

        result["cell_x"] = float(
            raster.meanCellWidth
        )

        result["cell_y"] = float(
            raster.meanCellHeight
        )

    except Exception as exc:
        result["issues"].append(
            f"Could not inspect raster: {exc}"
        )

    if (
        expected_wkid is not None
        and
        result["wkid"] != int(expected_wkid)
    ):
        result["issues"].append(
            f"WKID {result['wkid']} "
            f"!= required {expected_wkid}"
        )

    if (
        expected_cell_size is not None
        and
        result["cell_x"] is not None
        and
        result["cell_y"] is not None
    ):
        if (
            abs(result["cell_x"] - expected_cell_size) > 0.05
            or
            abs(result["cell_y"] - expected_cell_size) > 0.05
        ):
            result["issues"].append(
                f"Cell size "
                f"({result['cell_x']}, {result['cell_y']}) "
                f"!= ~{expected_cell_size}"
            )

    result["valid"] = (
        len(result["issues"]) == 0
    )

    return result


def validate_zone_source(zone_source):

    if not arcpy.Exists(zone_source):
        fail(
            f"Infrastructure zone source does not exist: "
            f"{zone_source}"
        )

    desc = arcpy.Describe(
        zone_source
    )

    shape_type = getattr(
        desc,
        "shapeType",
        None
    )

    if shape_type != "Polygon":
        fail(
            f"Infrastructure zone source must be Polygon. "
            f"Found geometry={shape_type}: {zone_source}"
        )

    field_names = {
        field.name
        for field in arcpy.ListFields(
            zone_source
        )
    }

    if ZONE_FIELD not in field_names:
        fail(
            f"Infrastructure zone source is missing required "
            f"zone field '{ZONE_FIELD}': {zone_source}"
        )

    feature_count = int(
        arcpy.management.GetCount(
            zone_source
        )[0]
    )

    if feature_count <= 0:
        fail(
            f"Infrastructure zone source contains no features: "
            f"{zone_source}"
        )

    log(
        f"PASS - Infrastructure Source: "
        f"{feature_count} polygons; "
        f"Zone Field={ZONE_FIELD}"
    )


# ---------------------------------------------------------------------
# Run folder creation
# ---------------------------------------------------------------------

def create_processing_run(
    county_folder,
    county_name,
    county_abbr,
    county_config_path,
    project_config_path,
    source_huc12_dem
):
    """
    Step 2.1 always starts a NEW processing run.
    """

    working_root = os.path.join(
        county_folder,
        "working"
    )

    ensure_folder(
        working_root
    )

    timestamp = datetime.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    run_id = (
        f"{county_name}_{timestamp}"
    )

    run_folder = os.path.join(
        working_root,
        run_id
    )

    ensure_folder(
        run_folder
    )

    logs_folder = os.path.join(
        run_folder,
        "logs"
    )

    ensure_folder(
        logs_folder
    )

    run_config_path = os.path.join(
        run_folder,
        "run_config.json"
    )

    run_config = {
        "run_id": run_id,
        "created": datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "county": {
            "name": county_name,
            "abbr": county_abbr,
        },
        "county_folder": county_folder,
        "county_config": county_config_path,
        "project_config": project_config_path,
        "source_inputs": {
            "huc12_dem": source_huc12_dem,
        },
        "run_folder": run_folder,
        "logs_folder": logs_folder,
        "status": "INITIALIZED",
    }

    save_json(
        run_config,
        run_config_path
    )

    return (
        run_folder,
        run_config_path,
        run_config
    )


def update_run_config(
    run_config_path,
    run_config,
    status,
    outputs=None
):

    run_config["status"] = status

    run_config["last_updated"] = (
        datetime.datetime.now()
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    if outputs is not None:
        run_config["outputs"] = outputs

    save_json(
        run_config,
        run_config_path
    )


# ---------------------------------------------------------------------
# Scratch helpers
# ---------------------------------------------------------------------

def ensure_run_scratch_gdb(
    run_folder
):

    scratch_folder = os.path.join(
        run_folder,
        "scratch"
    )

    ensure_folder(
        scratch_folder
    )

    scratch_gdb = os.path.join(
        scratch_folder,
        "scratch.gdb"
    )

    if not arcpy.Exists(
        scratch_gdb
    ):
        arcpy.management.CreateFileGDB(
            scratch_folder,
            "scratch.gdb"
        )

    return scratch_gdb


def safe_delete(path):

    if not path:
        return

    try:
        if arcpy.Exists(path):
            arcpy.management.Delete(
                path
            )
    except Exception as exc:
        warn(
            f"Cleanup warning for "
            f"{path}: {exc}"
        )


# ---------------------------------------------------------------------
# Spatial subset
# ---------------------------------------------------------------------

def make_extent_polygon_fc(
    raster_path,
    out_fc
):

    desc = arcpy.Describe(
        raster_path
    )

    extent = desc.extent
    sr = desc.spatialReference

    array = arcpy.Array(
        [
            arcpy.Point(extent.XMin, extent.YMin),
            arcpy.Point(extent.XMin, extent.YMax),
            arcpy.Point(extent.XMax, extent.YMax),
            arcpy.Point(extent.XMax, extent.YMin),
            arcpy.Point(extent.XMin, extent.YMin),
        ]
    )

    polygon = arcpy.Polygon(
        array,
        sr
    )

    out_folder = os.path.dirname(
        out_fc
    )

    out_name = os.path.basename(
        out_fc
    )

    safe_delete(
        out_fc
    )

    arcpy.management.CreateFeatureclass(
        out_path=out_folder,
        out_name=out_name,
        geometry_type="POLYGON",
        spatial_reference=sr
    )

    with arcpy.da.InsertCursor(
        out_fc,
        ["SHAPE@"]
    ) as cursor:
        cursor.insertRow(
            [polygon]
        )

    return out_fc


def select_zone_features_by_value_raster(
    zone_source,
    value_raster,
    out_fc,
    scratch_gdb
):

    extent_fc = os.path.join(
        scratch_gdb,
        "tmp_dem_extent"
    )

    make_extent_polygon_fc(
        value_raster,
        extent_fc
    )

    zone_layer = (
        "step_2_1_zone_layer"
    )

    if arcpy.Exists(zone_layer):
        arcpy.management.Delete(
            zone_layer
        )

    safe_delete(
        out_fc
    )

    try:
        arcpy.management.MakeFeatureLayer(
            zone_source,
            zone_layer
        )

        arcpy.management.SelectLayerByLocation(
            in_layer=zone_layer,
            overlap_type="INTERSECT",
            select_features=extent_fc,
            selection_type="NEW_SELECTION"
        )

        selected_count = int(
            arcpy.management.GetCount(
                zone_layer
            )[0]
        )

        log(
            f"Selected {selected_count} infrastructure "
            f"features intersecting DEM extent."
        )

        if selected_count == 0:
            return None

        arcpy.management.CopyFeatures(
            zone_layer,
            out_fc
        )

        return out_fc

    finally:
        safe_delete(
            zone_layer
        )

        safe_delete(
            extent_fc
        )


# ---------------------------------------------------------------------
# Zonal conditioning
# ---------------------------------------------------------------------

def apply_zonal_conditioning(
    value_raster,
    zone_source,
    scratch_gdb,
    county_abbr,
    final_out
):

    zone_subset = os.path.join(
        scratch_gdb,
        f"{county_abbr}_zone_subset"
    )

    zonal_out = os.path.join(
        scratch_gdb,
        f"{county_abbr}_ALL_Zonal_Statistics"
    )

    safe_delete(
        zone_subset
    )

    safe_delete(
        zonal_out
    )

    log_step_start(
        "Select Intersecting Infrastructure"
    )

    selected_zone_subset = (
        select_zone_features_by_value_raster(
            zone_source=zone_source,
            value_raster=value_raster,
            out_fc=zone_subset,
            scratch_gdb=scratch_gdb
        )
    )

    log_step_end(
        "Select Intersecting Infrastructure"
    )

    if (
        not selected_zone_subset
        or
        not arcpy.Exists(
            selected_zone_subset
        )
    ):
        log(
            "No intersecting infrastructure features found. "
            "Copying HUC12 DEM directly to conditioned output."
        )

        log_step_start(
            "Copy HUC12 DEM"
        )

        arcpy.management.CopyRaster(
            value_raster,
            final_out
        )

        log_step_end(
            "Copy HUC12 DEM"
        )

        return (
            "PASS_NO_INTERSECTING_INFRASTRUCTURE",
            0
        )

    selected_count = int(
        arcpy.management.GetCount(
            selected_zone_subset
        )[0]
    )

    log_step_start(
        "Zonal Statistics Minimum"
    )

    zonal_stats = ZonalStatistics(
        in_zone_data=selected_zone_subset,
        zone_field=ZONE_FIELD,
        in_value_raster=value_raster,
        statistics_type=STATS_TYPE,
        ignore_nodata="DATA"
    )

    zonal_stats.save(
        zonal_out
    )

    del zonal_stats

    log_step_end(
        "Zonal Statistics Minimum"
    )

    if not arcpy.Exists(
        zonal_out
    ):
        fail(
            f"ZonalStatistics output was not created: "
            f"{zonal_out}"
        )

    log_step_start(
        "Overlay Zonal Minimum"
    )

    zonal_raster = arcpy.Raster(
        zonal_out
    )

    dem_raster = arcpy.Raster(
        value_raster
    )

    final_raster = Con(
        IsNull(
            zonal_raster
        ),
        dem_raster,
        zonal_raster
    )

    final_raster.save(
        final_out
    )

    del final_raster
    del zonal_raster
    del dem_raster

    log_step_end(
        "Overlay Zonal Minimum"
    )

    safe_delete(
        zonal_out
    )

    safe_delete(
        zone_subset
    )

    return (
        "PASS",
        selected_count
    )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    county_folder = p(0)

    overwrite = get_bool(
        1,
        False
    )

    if not county_folder:
        fail(
            "County Processing Folder is required."
        )

    if not os.path.isdir(
        county_folder
    ):
        fail(
            f"County Processing Folder does not exist: "
            f"{county_folder}"
        )

    context = load_processing_context(
        county_folder
    )

    county = context[
        "county"
    ]

    project = context[
        "project"
    ]

    county_info = county[
        "county"
    ]

    county_name = str(
        county_info[
            "name"
        ]
    ).strip().title()

    county_abbr = str(
        county_info[
            "abbr"
        ]
    ).strip().upper()

    target_wkid = int(
        county_info[
            "wkid"
        ]
    )

    # -------------------------------------------------------------
    # Step 1.2 reusable source DEM
    # -------------------------------------------------------------

    source_huc12_dem = os.path.join(
        county_folder,
        "raster",
        f"{county_abbr}_DEM_HUC12.tif"
    )

    input_check = inspect_raster(
        source_huc12_dem,
        expected_wkid=target_wkid,
        expected_cell_size=TARGET_CELL_SIZE_FT
    )

    if not input_check[
        "exists"
    ]:
        warn(
            "Step 2.1 was not run because the Step 1.2 "
            "HUC12 DEM does not exist."
        )

        warn(
            f"Expected input: "
            f"{source_huc12_dem}"
        )

        arcpy.SetParameterAsText(
            4,
            "SKIPPED_HUC12_DEM_NOT_READY"
        )

        return

    if not input_check[
        "valid"
    ]:
        warn(
            "Step 2.1 was not run because the Step 1.2 "
            "HUC12 DEM does not meet current production requirements."
        )

        warn(
            "Input issue(s): "
            +
            "; ".join(
                input_check[
                    "issues"
                ]
            )
        )

        arcpy.SetParameterAsText(
            4,
            "SKIPPED_HUC12_DEM_INVALID"
        )

        return

    # -------------------------------------------------------------
    # Create NEW processing run
    # -------------------------------------------------------------

    (
        run_folder,
        run_config_path,
        run_config
    ) = create_processing_run(
        county_folder=county_folder,
        county_name=county_name,
        county_abbr=county_abbr,
        county_config_path=context[
            "county_config_path"
        ],
        project_config_path=context[
            "project_config_path"
        ],
        source_huc12_dem=source_huc12_dem
    )

    # Start persistent log INSIDE this run.
    init_tool_log(
        county_folder=run_folder,
        tool_id="2.1",
        tool_name="Infrastructure_Conditioning",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder": run_folder,
            "Source HUC12 DEM": source_huc12_dem,
        }
    )

    log("=" * 72)
    log(
        "2.1 Apply Transportation Infrastructure Conditioning"
    )
    log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )
    log(
        f"Processing Run Folder: "
        f"{run_folder}"
    )
    log("=" * 72)

    # -------------------------------------------------------------
    # Infrastructure source
    # -------------------------------------------------------------

    try:
        zone_source = (
            project["tools_source"]
            [ZONE_SOURCE_NAME]
        )
    except Exception:
        fail(
            f"Project Config does not contain "
            f"tools_source > {ZONE_SOURCE_NAME}."
        )

    validate_zone_source(
        zone_source
    )

    # -------------------------------------------------------------
    # Output
    # -------------------------------------------------------------

    final_out = os.path.join(
        run_folder,
        f"{county_abbr}_DEM_HUC12_with_Zonal.tif"
    )

    run_config[
        "source_inputs"
    ][
        "infrastructure_source"
    ] = zone_source

    run_config[
        "outputs"
    ] = {
        "conditioned_dem": final_out,
    }

    update_run_config(
        run_config_path,
        run_config,
        "READY_TO_PROCESS",
        run_config["outputs"]
    )

    # Fresh run normally means output does not exist, but preserve
    # overwrite behavior in case the folder was manually reused.
    if arcpy.Exists(
        final_out
    ):

        existing_check = inspect_raster(
            final_out,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )

        if (
            not overwrite
            and
            existing_check[
                "valid"
            ]
        ):
            log(
                "Existing conditioned DEM meets current "
                "production requirements; reusing it."
            )

            update_run_config(
                run_config_path,
                run_config,
                "SKIPPED_EXISTING_VALID",
                run_config["outputs"]
            )

            finish_tool_log(
                "SKIPPED_EXISTING_VALID"
            )

            arcpy.SetParameterAsText(
                2,
                run_folder
            )

            arcpy.SetParameterAsText(
                3,
                final_out
            )

            arcpy.SetParameterAsText(
                4,
                "SKIPPED_EXISTING_VALID"
            )

            return

        if (
            not overwrite
            and
            not existing_check[
                "valid"
            ]
        ):
            warn(
                "Existing conditioned DEM does not meet "
                "current production requirements."
            )

            warn(
                "Step 2.1 was not run because "
                "Overwrite Existing Output = False."
            )

            update_run_config(
                run_config_path,
                run_config,
                "SKIPPED_INVALID_EXISTING",
                run_config["outputs"]
            )

            finish_tool_log(
                "SKIPPED_INVALID_EXISTING"
            )

            arcpy.SetParameterAsText(
                2,
                run_folder
            )

            arcpy.SetParameterAsText(
                3,
                final_out
            )

            arcpy.SetParameterAsText(
                4,
                "SKIPPED_INVALID_EXISTING"
            )

            return

        arcpy.management.Delete(
            final_out
        )

    # -------------------------------------------------------------
    # Spatial Analyst
    # -------------------------------------------------------------

    if (
        arcpy.CheckExtension(
            "Spatial"
        )
        !=
        "Available"
    ):
        fail(
            "Spatial Analyst extension is not available."
        )

    arcpy.CheckOutExtension(
        "Spatial"
    )

    old_snap = arcpy.env.snapRaster
    old_cell = arcpy.env.cellSize
    old_extent = arcpy.env.extent
    old_output_cs = arcpy.env.outputCoordinateSystem
    old_scratch = arcpy.env.scratchWorkspace
    old_overwrite = arcpy.env.overwriteOutput

    try:

        scratch_gdb = ensure_run_scratch_gdb(
            run_folder
        )

        desc = arcpy.Describe(
            source_huc12_dem
        )

        arcpy.env.snapRaster = (
            source_huc12_dem
        )

        arcpy.env.cellSize = (
            source_huc12_dem
        )

        arcpy.env.extent = (
            source_huc12_dem
        )

        arcpy.env.outputCoordinateSystem = (
            desc.spatialReference
        )

        arcpy.env.scratchWorkspace = (
            scratch_gdb
        )

        arcpy.env.overwriteOutput = (
            True
        )

        status, selected_count = (
            apply_zonal_conditioning(
                value_raster=source_huc12_dem,
                zone_source=zone_source,
                scratch_gdb=scratch_gdb,
                county_abbr=county_abbr,
                final_out=final_out
            )
        )

        output_check = inspect_raster(
            final_out,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )

        if not output_check[
            "valid"
        ]:
            fail(
                "New transportation-conditioned DEM failed "
                "output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in output_check[
                        "issues"
                    ]
                )
            )

        run_config[
            "processing"
        ] = {
            "tool": "2.1",
            "zone_field": ZONE_FIELD,
            "statistics_type": STATS_TYPE,
            "selected_infrastructure_feature_count": selected_count,
        }

        update_run_config(
            run_config_path,
            run_config,
            status,
            run_config[
                "outputs"
            ]
        )

        log(
            f"PASS - Conditioned DEM: "
            f"{final_out}"
        )

        log(
            f"Processing Run Folder: "
            f"{run_folder}"
        )

        finish_tool_log(
            status
        )

        arcpy.SetParameterAsText(
            2,
            run_folder
        )

        arcpy.SetParameterAsText(
            3,
            final_out
        )

        arcpy.SetParameterAsText(
            4,
            status
        )

    finally:

        arcpy.env.snapRaster = (
            old_snap
        )

        arcpy.env.cellSize = (
            old_cell
        )

        arcpy.env.extent = (
            old_extent
        )

        arcpy.env.outputCoordinateSystem = (
            old_output_cs
        )

        arcpy.env.scratchWorkspace = (
            old_scratch
        )

        arcpy.env.overwriteOutput = (
            old_overwrite
        )

        try:
            arcpy.CheckInExtension(
                "Spatial"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------

if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        try:
            log_exception(
                exc
            )
        except Exception:
            pass

        try:
            finish_tool_log(
                "FAIL"
            )
        except Exception:
            pass

        try:
            arcpy.SetParameterAsText(
                4,
                "FAIL"
            )
        except Exception:
            pass

        raise