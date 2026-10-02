# -*- coding: utf-8 -*-
"""
2.3 Fill, Flow Direction, Flow Accumulation
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Continue an EXPLICIT Processing Run created by Step 2.1.

The user selects the Processing Run Folder directly. The tool does not search
for the latest run and does not create a new run.

Expected input inside the SAME run
----------------------------------
<ABBR>_DEM_HUC12_AGREE.tif

Outputs inside the SAME run
---------------------------
<ABBR>_DEM_HUC12_AGREE_Fill.tif
<ABBR>_DEM_HUC12_AGREE_FD.tif
<ABBR>_DEM_HUC12_AGREE_FA.tif

Processing sequence
-------------------
Fill
    ->
Flow Direction (D8, NORMAL)
    ->
Flow Accumulation (FLOAT)

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder         Folder          Required Input
1  Overwrite Existing Outputs    Boolean         Optional Input
2  Filled DEM                    Raster Dataset  Derived Output
3  Flow Direction Raster         Raster Dataset  Derived Output
4  Flow Accumulation Raster      Raster Dataset  Derived Output
5  Processing Status             String          Derived Output

UX rule
-------
Expected workflow conditions use WARN + clean return:
- Step 2.2 AGREE DEM not ready / invalid
- Existing Fill/FD/FA outputs partial or invalid while Overwrite=False

True Fill / FD / FA processing failures or new-output QC failures raise.
"""

import os
import sys
import time
import datetime

import arcpy
from arcpy.sa import (
    Fill,
    FlowDirection,
    FlowAccumulation,
    Raster,
)


SCRIPT_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

if SCRIPT_DIR not in sys.path:
    sys.path.insert(
        0,
        SCRIPT_DIR
    )


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
# Parameter helpers
# ---------------------------------------------------------------------

def p(index):

    value = arcpy.GetParameterAsText(
        index
    )

    return (
        value.strip()
        if value
        else ""
    )


def get_bool(
    index,
    default=False
):

    try:

        value = arcpy.GetParameter(
            index
        )

        if value is None:
            return default

        return bool(
            value
        )

    except Exception:

        text = p(
            index
        ).lower()

        if not text:
            return default

        return text in {
            "true",
            "1",
            "yes",
            "y",
        }


# ---------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------

def load_run_context(
    run_folder
):

    if not os.path.isdir(
        run_folder
    ):

        fail(
            f"Processing Run Folder does not exist: "
            f"{run_folder}"
        )


    run_config_path = os.path.join(
        run_folder,
        "run_config.json"
    )


    if not os.path.isfile(
        run_config_path
    ):

        fail(
            "Selected folder is not a valid processing run. "
            f"Missing run_config.json: {run_folder}"
        )


    run_config = load_json(
        run_config_path
    )


    run_id = str(
        run_config.get(
            "run_id",
            ""
        )
    ).strip()


    county_info = run_config.get(
        "county",
        {}
    )


    county_name = str(
        county_info.get(
            "name",
            ""
        )
    ).strip().title()


    county_abbr = str(
        county_info.get(
            "abbr",
            ""
        )
    ).strip().upper()


    county_config_path = run_config.get(
        "county_config",
        ""
    )


    project_config_path = run_config.get(
        "project_config",
        ""
    )


    if not run_id:

        fail(
            "run_config.json does not contain run_id."
        )


    if not county_name or not county_abbr:

        fail(
            "run_config.json does not contain valid county identity."
        )


    if not os.path.isfile(
        county_config_path
    ):

        fail(
            f"County Config referenced by run does not exist: "
            f"{county_config_path}"
        )


    if not os.path.isfile(
        project_config_path
    ):

        fail(
            f"Project Config referenced by run does not exist: "
            f"{project_config_path}"
        )


    county = load_json(
        county_config_path
    )


    project = load_json(
        project_config_path
    )


    config_abbr = str(
        county.get(
            "county",
            {}
        ).get(
            "abbr",
            ""
        )
    ).strip().upper()


    if config_abbr != county_abbr:

        fail(
            "Processing Run / County Config mismatch.\n"
            f"Run: {county_abbr}\n"
            f"County Config: {config_abbr}"
        )


    folder_name = os.path.basename(
        os.path.normpath(
            run_folder
        )
    )


    if folder_name != run_id:

        warn(
            "Processing Run Folder name does not exactly match "
            f"run_config run_id. Folder='{folder_name}', "
            f"run_id='{run_id}'. Continuing because run_config is valid."
        )


    return {
        "run_folder":
            run_folder,

        "run_config_path":
            run_config_path,

        "run_config":
            run_config,

        "run_id":
            run_id,

        "county_name":
            county_name,

        "county_abbr":
            county_abbr,

        "county_config_path":
            county_config_path,

        "county":
            county,

        "project_config_path":
            project_config_path,

        "project":
            project,
    }


def update_run_config(
    context,
    tool_status,
    fill_path=None,
    fd_path=None,
    fa_path=None
):

    run_config = context[
        "run_config"
    ]


    run_config[
        "last_updated"
    ] = (
        datetime.datetime.now()
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )


    run_config[
        "status"
    ] = tool_status


    processing = run_config.setdefault(
        "processing",
        {}
    )


    processing[
        "2.3"
    ] = {
        "status":
            tool_status,

        "fill":
            fill_path,

        "flow_direction":
            fd_path,

        "flow_accumulation":
            fa_path,
    }


    outputs = run_config.setdefault(
        "outputs",
        {}
    )


    if fill_path:

        outputs[
            "filled_dem"
        ] = fill_path


    if fd_path:

        outputs[
            "flow_direction"
        ] = fd_path


    if fa_path:

        outputs[
            "flow_accumulation"
        ] = fa_path


    save_json(
        run_config,
        context[
            "run_config_path"
        ]
    )


# ---------------------------------------------------------------------
# Raster inspection
# ---------------------------------------------------------------------

def inspect_raster(
    raster_path,
    expected_wkid=None,
    expected_cell_size=None
):

    result = {
        "exists":
            False,

        "valid":
            False,

        "wkid":
            None,

        "cell_x":
            None,

        "cell_y":
            None,

        "issues":
            [],
    }


    if not arcpy.Exists(
        raster_path
    ):

        result[
            "issues"
        ].append(
            "Raster does not exist."
        )

        return result


    result[
        "exists"
    ] = True


    try:

        desc = arcpy.Describe(
            raster_path
        )


        result[
            "wkid"
        ] = getattr(
            desc.spatialReference,
            "factoryCode",
            None
        )


        raster = Raster(
            raster_path
        )


        result[
            "cell_x"
        ] = float(
            raster.meanCellWidth
        )


        result[
            "cell_y"
        ] = float(
            raster.meanCellHeight
        )


    except Exception as exc:

        result[
            "issues"
        ].append(
            f"Could not inspect raster: "
            f"{exc}"
        )


    if (
        expected_wkid is not None
        and
        result[
            "wkid"
        ]
        !=
        int(
            expected_wkid
        )
    ):

        result[
            "issues"
        ].append(
            f"WKID "
            f"{result['wkid']} "
            f"!= required "
            f"{expected_wkid}"
        )


    if (
        expected_cell_size is not None
        and
        result[
            "cell_x"
        ]
        is not None
        and
        result[
            "cell_y"
        ]
        is not None
    ):

        if (
            abs(
                result[
                    "cell_x"
                ]
                -
                expected_cell_size
            )
            >
            0.05
            or
            abs(
                result[
                    "cell_y"
                ]
                -
                expected_cell_size
            )
            >
            0.05
        ):

            result[
                "issues"
            ].append(
                f"Cell size "
                f"({result['cell_x']}, "
                f"{result['cell_y']}) "
                f"!= ~{expected_cell_size}"
            )


    result[
        "valid"
    ] = (
        len(
            result[
                "issues"
            ]
        )
        ==
        0
    )


    return result


def inspect_output_set(
    fill_path,
    fd_path,
    fa_path,
    expected_wkid,
    expected_cell_size
):

    checks = {
        "Fill":
            inspect_raster(
                fill_path,
                expected_wkid,
                expected_cell_size
            ),

        "Flow Direction":
            inspect_raster(
                fd_path,
                expected_wkid,
                expected_cell_size
            ),

        "Flow Accumulation":
            inspect_raster(
                fa_path,
                expected_wkid,
                expected_cell_size
            ),
    }


    all_exist = all(
        check[
            "exists"
        ]
        for check in checks.values()
    )


    all_valid = all(
        check[
            "valid"
        ]
        for check in checks.values()
    )


    return (
        checks,
        all_exist,
        all_valid
    )


# ---------------------------------------------------------------------
# Scratch
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


def safe_delete(
    path
):

    if not path:
        return


    try:

        if arcpy.Exists(
            path
        ):

            arcpy.management.Delete(
                path
            )


    except Exception as exc:

        warn(
            f"Cleanup warning for "
            f"{path}: {exc}"
        )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    run_folder = p(
        0
    )


    overwrite = get_bool(
        1,
        False
    )


    if not run_folder:

        fail(
            "Processing Run Folder is required."
        )


    context = load_run_context(
        run_folder
    )


    county = context[
        "county"
    ]


    county_info = county[
        "county"
    ]


    county_name = context[
        "county_name"
    ]


    county_abbr = context[
        "county_abbr"
    ]


    target_wkid = int(
        county_info[
            "wkid"
        ]
    )


    init_tool_log(
        county_folder=run_folder,
        tool_id="2.3",
        tool_name="Fill_FD_FA",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder":
                run_folder,

            "Run ID":
                context[
                    "run_id"
                ],
        }
    )


    log(
        "=" * 72
    )


    log(
        "2.3 Fill, Flow Direction, Flow Accumulation"
    )


    log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )


    log(
        f"Processing Run: "
        f"{context['run_id']}"
    )


    log(
        "=" * 72
    )


    # -------------------------------------------------------------
    # Step 2.2 input inside SAME run
    # -------------------------------------------------------------

    agree_dem = os.path.join(
        run_folder,
        f"{county_abbr}_DEM_HUC12_AGREE.tif"
    )


    agree_check = inspect_raster(
        agree_dem,
        expected_wkid=target_wkid,
        expected_cell_size=None
    )


    if not agree_check[
        "exists"
    ]:

        warn(
            "Step 2.3 was not run because the Step 2.2 "
            "AGREE DEM does not exist in this run."
        )


        warn(
            f"Expected input: "
            f"{agree_dem}"
        )


        update_run_config(
            context,
            "SKIPPED_AGREE_DEM_NOT_READY"
        )


        finish_tool_log(
            "SKIPPED_AGREE_DEM_NOT_READY"
        )


        arcpy.SetParameterAsText(
            5,
            "SKIPPED_AGREE_DEM_NOT_READY"
        )


        return


    if not agree_check[
        "valid"
    ]:

        warn(
            "Step 2.3 was not run because the Step 2.2 "
            "AGREE DEM does not meet current production requirements."
        )


        warn(
            "Input issue(s): "
            +
            "; ".join(
                agree_check[
                    "issues"
                ]
            )
        )


        update_run_config(
            context,
            "SKIPPED_AGREE_DEM_INVALID"
        )


        finish_tool_log(
            "SKIPPED_AGREE_DEM_INVALID"
        )


        arcpy.SetParameterAsText(
            5,
            "SKIPPED_AGREE_DEM_INVALID"
        )


        return


    input_cell_size = float(
        agree_check[
            "cell_x"
        ]
    )


    log(
        f"PASS - AGREE DEM: "
        f"{agree_dem}"
    )


    log(
        f"Hydrologic processing cell size: "
        f"{input_cell_size}"
    )


    # -------------------------------------------------------------
    # Outputs inside SAME run
    # -------------------------------------------------------------

    base_name = os.path.splitext(
        os.path.basename(
            agree_dem
        )
    )[0]


    out_fill_path = os.path.join(
        run_folder,
        f"{base_name}_Fill.tif"
    )


    out_fd_path = os.path.join(
        run_folder,
        f"{base_name}_FD.tif"
    )


    out_fa_path = os.path.join(
        run_folder,
        f"{base_name}_FA.tif"
    )


    log(
        "Output paths:"
    )


    log(
        f"  Fill: "
        f"{out_fill_path}"
    )


    log(
        f"  FD  : "
        f"{out_fd_path}"
    )


    log(
        f"  FA  : "
        f"{out_fa_path}"
    )


    # -------------------------------------------------------------
    # Existing-output behavior
    # -------------------------------------------------------------

    checks, all_exist, all_valid = (
        inspect_output_set(
            out_fill_path,
            out_fd_path,
            out_fa_path,
            target_wkid,
            input_cell_size
        )
    )


    if (
        all_exist
        and
        all_valid
        and
        not overwrite
    ):

        log(
            "Existing Fill / FD / FA outputs in this run "
            "all meet current requirements; reusing them."
        )


        update_run_config(
            context,
            "SKIPPED_EXISTING_VALID",
            out_fill_path,
            out_fd_path,
            out_fa_path
        )


        finish_tool_log(
            "SKIPPED_EXISTING_VALID"
        )


        arcpy.SetParameterAsText(
            2,
            out_fill_path
        )


        arcpy.SetParameterAsText(
            3,
            out_fd_path
        )


        arcpy.SetParameterAsText(
            4,
            out_fa_path
        )


        arcpy.SetParameterAsText(
            5,
            "SKIPPED_EXISTING_VALID"
        )


        return


    any_exist = any(
        check[
            "exists"
        ]
        for check in checks.values()
    )


    if (
        any_exist
        and
        not overwrite
    ):

        warn(
            "Existing Fill / Flow Direction / "
            "Flow Accumulation outputs in this run are "
            "incomplete or invalid."
        )


        issue_parts = []


        for label, check in checks.items():

            if not check[
                "exists"
            ]:

                issue_parts.append(
                    f"{label}: missing"
                )


            elif not check[
                "valid"
            ]:

                issue_parts.append(
                    f"{label}: "
                    +
                    "; ".join(
                        check[
                            "issues"
                        ]
                    )
                )


        warn(
            "Step 2.3 was not run because "
            "Overwrite Existing Outputs = False. "
            +
            " | ".join(
                issue_parts
            )
        )


        update_run_config(
            context,
            "SKIPPED_INVALID_OR_PARTIAL_EXISTING",
            (
                out_fill_path
                if arcpy.Exists(
                    out_fill_path
                )
                else None
            ),
            (
                out_fd_path
                if arcpy.Exists(
                    out_fd_path
                )
                else None
            ),
            (
                out_fa_path
                if arcpy.Exists(
                    out_fa_path
                )
                else None
            )
        )


        finish_tool_log(
            "SKIPPED_INVALID_OR_PARTIAL_EXISTING"
        )


        arcpy.SetParameterAsText(
            2,
            (
                out_fill_path
                if arcpy.Exists(
                    out_fill_path
                )
                else ""
            )
        )


        arcpy.SetParameterAsText(
            3,
            (
                out_fd_path
                if arcpy.Exists(
                    out_fd_path
                )
                else ""
            )
        )


        arcpy.SetParameterAsText(
            4,
            (
                out_fa_path
                if arcpy.Exists(
                    out_fa_path
                )
                else ""
            )
        )


        arcpy.SetParameterAsText(
            5,
            "SKIPPED_INVALID_OR_PARTIAL_EXISTING"
        )


        return


    if overwrite:

        for output_path in [
            out_fill_path,
            out_fd_path,
            out_fa_path,
        ]:

            safe_delete(
                output_path
            )


    # -------------------------------------------------------------
    # Spatial Analyst
    # -------------------------------------------------------------

    spatial_status = (
        arcpy.CheckExtension(
            "Spatial"
        )
    )


    log(
        f"Spatial Analyst status: "
        f"{spatial_status}"
    )


    if spatial_status != "Available":

        fail(
            f"Spatial Analyst extension is not available "
            f"(status={spatial_status})."
        )


    arcpy.CheckOutExtension(
        "Spatial"
    )


    old_overwrite = (
        arcpy.env.overwriteOutput
    )


    old_scratch = (
        arcpy.env.scratchWorkspace
    )


    old_extent = (
        arcpy.env.extent
    )


    old_cell = (
        arcpy.env.cellSize
    )


    old_snap = (
        arcpy.env.snapRaster
    )


    old_output_cs = (
        arcpy.env.outputCoordinateSystem
    )


    try:

        scratch_gdb = ensure_run_scratch_gdb(
            run_folder
        )


        arcpy.env.scratchWorkspace = (
            scratch_gdb
        )


        arcpy.env.overwriteOutput = (
            True
        )


        try:

            arcpy.env.parallelProcessingFactor = (
                "0"
            )


            arcpy.env.processorType = (
                "CPU"
            )


            log(
                "Using CPU processing with "
                "parallelProcessingFactor=0."
            )


        except Exception as exc:

            warn(
                f"Could not set CPU/parallel processing "
                f"environment: {exc}"
            )


        dem = Raster(
            agree_dem
        )


        dem_desc = arcpy.Describe(
            agree_dem
        )


        arcpy.env.extent = (
            dem.extent
        )


        arcpy.env.cellSize = (
            dem.meanCellWidth
        )


        arcpy.env.snapRaster = (
            agree_dem
        )


        arcpy.env.outputCoordinateSystem = (
            dem_desc.spatialReference
        )


        log(
            f"DEM loaded: "
            f"{dem.width} x {dem.height} cells, "
            f"cell size={dem.meanCellWidth}"
        )


        # =========================================================
        # FILL
        # =========================================================

        log_step_start(
            "Fill"
        )


        filled = Fill(
            dem
        )


        filled.save(
            out_fill_path
        )


        log_step_end(
            "Fill"
        )


        fill_check = inspect_raster(
            out_fill_path,
            target_wkid,
            input_cell_size
        )


        if not fill_check[
            "valid"
        ]:

            fail(
                "New Filled DEM failed output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in fill_check[
                        "issues"
                    ]
                )
            )


        # =========================================================
        # FLOW DIRECTION
        # =========================================================

        log_step_start(
            "Flow Direction D8"
        )


        fd = FlowDirection(
            filled,
            force_flow="NORMAL",
            flow_direction_type="D8"
        )


        fd.save(
            out_fd_path
        )


        log_step_end(
            "Flow Direction D8"
        )


        fd_check = inspect_raster(
            out_fd_path,
            target_wkid,
            input_cell_size
        )


        if not fd_check[
            "valid"
        ]:

            fail(
                "New Flow Direction raster failed "
                "output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in fd_check[
                        "issues"
                    ]
                )
            )


        # =========================================================
        # FLOW ACCUMULATION
        # =========================================================

        log_step_start(
            "Flow Accumulation FLOAT"
        )


        fa = FlowAccumulation(
            fd,
            in_weight_raster=None,
            data_type="FLOAT"
        )


        fa.save(
            out_fa_path
        )


        log_step_end(
            "Flow Accumulation FLOAT"
        )


        fa_check = inspect_raster(
            out_fa_path,
            target_wkid,
            input_cell_size
        )


        if not fa_check[
            "valid"
        ]:

            fail(
                "New Flow Accumulation raster failed "
                "output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in fa_check[
                        "issues"
                    ]
                )
            )


        # ---------------------------------------------------------
        # Update run record
        # ---------------------------------------------------------

        update_run_config(
            context,
            "PASS",
            out_fill_path,
            out_fd_path,
            out_fa_path
        )


        log(
            "PASS - Fill, Flow Direction, and "
            "Flow Accumulation outputs verified."
        )


        finish_tool_log(
            "PASS"
        )


        arcpy.SetParameterAsText(
            2,
            out_fill_path
        )


        arcpy.SetParameterAsText(
            3,
            out_fd_path
        )


        arcpy.SetParameterAsText(
            4,
            out_fa_path
        )


        arcpy.SetParameterAsText(
            5,
            "PASS"
        )


    finally:

        arcpy.env.overwriteOutput = (
            old_overwrite
        )


        arcpy.env.scratchWorkspace = (
            old_scratch
        )


        arcpy.env.extent = (
            old_extent
        )


        arcpy.env.cellSize = (
            old_cell
        )


        arcpy.env.snapRaster = (
            old_snap
        )


        arcpy.env.outputCoordinateSystem = (
            old_output_cs
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
                5,
                "FAIL"
            )

        except Exception:

            pass


        raise