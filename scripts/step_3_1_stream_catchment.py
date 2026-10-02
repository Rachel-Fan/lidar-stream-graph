# -*- coding: utf-8 -*-
"""
3.1 Generate Streams and Catchments
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Refactored from:
    stream_catchment_workflow_toolbox.py

Purpose
-------
Generate threshold-specific stream networks and catchments from the
Step 2.3 Flow Direction and Flow Accumulation rasters.

The original ArcHydro-style five-part workflow is preserved:
    1. Stream Definition
    2. Stream Segmentation
    3. Catchment Grid Delineation
    4. Catchment Polygon Processing + HydroID
    5. Drainage Line Processing

This refactored tool:
- Uses Processing Run Folder as the explicit run context.
- Exposes Flow Direction, Flow Accumulation, thresholds, and output GDB
  as visible operational parameters.
- A ToolValidator can auto-populate those operational parameters from
  the selected Processing Run Folder while still allowing user override.
- Never searches for the latest run.
- Supports multiple drainage thresholds in ONE execution.
- Uses county identity from run_config.json / County Config.

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder         Folder          Required Input
1  Flow Direction Raster         Raster Dataset  Required Input
2  Flow Accumulation Raster      Raster Dataset  Required Input
3  Drainage Thresholds           Long            Required Input, MultiValue
4  Output Streams Working GDB    Workspace       Required Output
5  Overwrite Existing Families   Boolean         Optional Input
6  Streams Working GDB           Workspace       Derived Output
7  Processing Status             String          Derived Output

Recommended Threshold values
----------------------------
5000;10000;20000

These are the three primary thresholds documented for statewide production.

Expected output family names
----------------------------
{County}_Stream_5k
{County}_Catchment_5k
{County}_Stream_10k
{County}_Catchment_10k
{County}_Stream_20k
{County}_Catchment_20k
"""

import os
import sys
import glob
import time
import gc
import datetime

import arcpy
from arcpy.sa import (
    Con,
    StreamLink,
    Watershed,
    StreamToFeature,
    IsNull,
)

# ArcHydro helper modules expected in the ArcGIS Pro environment.
import apwrutils
import hydroconfig
import assignhydroid


SCRIPT_DIR = os.path.dirname(
    os.path.abspath(
        __file__
    )
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
# Constants
# ---------------------------------------------------------------------

DEFAULT_THRESHOLDS = [
    5000,
    10000,
    20000,
]



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


def parse_thresholds(
    text
):

    if not text:

        return list(
            DEFAULT_THRESHOLDS
        )


    values = []


    for token in text.split(
        ";"
    ):

        token = (
            token.strip()
            .strip("'")
            .strip('"')
        )


        if not token:
            continue


        try:

            value = int(
                token
            )

        except Exception:

            fail(
                f"Drainage Threshold must be an integer. "
                f"Invalid value: {token}"
            )


        if value <= 0:

            fail(
                f"Drainage Threshold must be > 0. "
                f"Invalid value: {value}"
            )


        values.append(
            value
        )


    values = sorted(
        set(
            values
        )
    )


    if not values:

        fail(
            "At least one drainage threshold is required."
        )


    return values


def threshold_token(
    threshold
):

    threshold = int(
        threshold
    )


    if (
        threshold % 1000
        ==
        0
    ):

        return (
            f"{threshold // 1000}k"
        )


    return str(
        threshold
    )


def format_elapsed(
    start
):

    elapsed = (
        time.time()
        -
        start
    )


    hrs = int(
        elapsed // 3600
    )


    mins = int(
        (
            elapsed % 3600
        )
        //
        60
    )


    secs = int(
        elapsed % 60
    )


    return (
        f"{hrs:02d}:"
        f"{mins:02d}:"
        f"{secs:02d}"
    )


# ---------------------------------------------------------------------
# Processing Run context
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
    threshold_results,
    working_gdb
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
        "3.1"
    ] = {
        "status":
            tool_status,

        "working_gdb":
            working_gdb,

        "thresholds":
            threshold_results,
    }


    outputs = run_config.setdefault(
        "outputs",
        {}
    )


    outputs[
        "streams_working_gdb"
    ] = working_gdb


    save_json(
        run_config,
        context[
            "run_config_path"
        ]
    )


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

def run_log(
    message
):

    log(
        message
    )


# ---------------------------------------------------------------------
# Raster inspection
# ---------------------------------------------------------------------

def inspect_raster(
    raster_path,
    expected_wkid=None
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


        raster = arcpy.Raster(
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


def safe_allnodata(
    raster_path
):

    old_extent = (
        arcpy.env.extent
    )


    old_mask = (
        arcpy.env.mask
    )


    try:

        arcpy.env.extent = (
            "DEFAULT"
        )


        arcpy.env.mask = (
            None
        )


        try:

            arcpy.management.CalculateStatistics(
                raster_path
            )

        except Exception:

            pass


        try:

            return (
                arcpy.management.GetRasterProperties(
                    raster_path,
                    "ALLNODATA"
                )
                .getOutput(
                    0
                )
            )


        except Exception:

            return (
                "unknown"
            )


    finally:

        arcpy.env.extent = (
            old_extent
        )


        arcpy.env.mask = (
            old_mask
        )


# ---------------------------------------------------------------------
# ArcHydro environment
# ---------------------------------------------------------------------

def ensure_spatial():

    if (
        arcpy.CheckExtension(
            "Spatial"
        )
        !=
        "Available"
    ):

        raise RuntimeError(
            "Spatial Analyst extension is not available."
        )


    arcpy.CheckOutExtension(
        "Spatial"
    )


def set_hydro_env(
    reference_raster
):

    raster = arcpy.Raster(
        reference_raster
    )


    arcpy.env.overwriteOutput = (
        True
    )


    arcpy.env.snapRaster = (
        raster
    )


    arcpy.env.cellSize = (
        raster.meanCellWidth
    )


    arcpy.env.extent = (
        raster.extent
    )


    arcpy.env.outputCoordinateSystem = (
        arcpy.Describe(
            raster
        ).spatialReference
    )


    arcpy.env.mask = (
        raster
    )


def ensure_scratch_gdb(
    scratch_root
):

    ensure_folder(
        scratch_root
    )


    scratch_gdb = os.path.join(
        scratch_root,
        "scratch.gdb"
    )


    if not arcpy.Exists(
        scratch_gdb
    ):

        arcpy.management.CreateFileGDB(
            scratch_root,
            "scratch.gdb"
        )


    return scratch_gdb


# ---------------------------------------------------------------------
# 1) Stream Definition
# ---------------------------------------------------------------------

def run_stream_definition(
    flow_acc_raster,
    threshold,
    out_stream_raster
):

    set_hydro_env(
        flow_acc_raster
    )


    fac = arcpy.Raster(
        flow_acc_raster
    )


    if (
        fac.minimum is None
        or
        fac.maximum is None
    ):

        arcpy.management.CalculateStatistics(
            flow_acc_raster
        )


        fac = arcpy.Raster(
            flow_acc_raster
        )


    run_log(
        f"FAC range: "
        f"min={fac.minimum}, "
        f"max={fac.maximum}"
    )


    if (
        fac.maximum not in (
            None,
            0,
        )
        and
        threshold
        >=
        fac.maximum
    ):

        return (
            False,
            (
                f"Threshold {threshold} >= "
                f"maximum FAC {fac.maximum}."
            )
        )


    run_log(
        f"Using threshold = "
        f"{threshold} cells."
    )


    if arcpy.Exists(
        out_stream_raster
    ):

        arcpy.management.Delete(
            out_stream_raster
        )


    stream_raster = Con(
        fac
        >
        int(
            threshold
        ),
        1
    )


    stream_raster.save(
        out_stream_raster
    )


    if safe_allnodata(
        out_stream_raster
    ) == "1":

        return (
            False,
            "Stream Definition produced an all-NoData raster."
        )


    return (
        True,
        out_stream_raster
    )


# ---------------------------------------------------------------------
# 2) Stream Segmentation
# ---------------------------------------------------------------------

def run_stream_segmentation(
    stream_raster,
    flow_dir_raster,
    out_stream_link
):

    set_hydro_env(
        flow_dir_raster
    )


    if arcpy.Exists(
        out_stream_link
    ):

        arcpy.management.Delete(
            out_stream_link
        )


    stream_link = StreamLink(
        stream_raster,
        flow_dir_raster
    )


    stream_link.save(
        out_stream_link
    )


    if safe_allnodata(
        out_stream_link
    ) == "1":

        fail(
            "Stream Segmentation produced "
            "an all-NoData raster."
        )


    return out_stream_link


# ---------------------------------------------------------------------
# 3) Catchment Grid
# ---------------------------------------------------------------------

def run_catchment_grid(
    flow_dir_raster,
    stream_link,
    out_catchment_raster
):

    set_hydro_env(
        flow_dir_raster
    )


    if arcpy.Exists(
        out_catchment_raster
    ):

        arcpy.management.Delete(
            out_catchment_raster
        )


    catchments = Watershed(
        flow_dir_raster,
        stream_link
    )


    catchments.save(
        out_catchment_raster
    )


    arcpy.management.BuildRasterAttributeTable(
        out_catchment_raster
    )


    if safe_allnodata(
        out_catchment_raster
    ) == "1":

        fail(
            "Catchment Grid Delineation produced "
            "an all-NoData raster."
        )


    return out_catchment_raster


# ---------------------------------------------------------------------
# 4) Catchment Polygon Processing
# ---------------------------------------------------------------------

def run_catchment_polygon_processing(
    catchment_raster,
    out_catchments,
    flow_dir_raster
):

    set_hydro_env(
        flow_dir_raster
    )


    if arcpy.Exists(
        out_catchments
    ):

        arcpy.management.Delete(
            out_catchments
        )


    try:

        arcpy.RasterToPolygon_conversion(
            in_raster=arcpy.Raster(
                catchment_raster
            ),
            out_polygon_features=out_catchments,
            simplify="NO_SIMPLIFY",
            raster_field="VALUE",
            create_multipart_features="MULTIPLE_OUTER_PART",
            max_vertices_per_feature=""
        )


    except Exception:

        run_log(
            "Primary RasterToPolygon method failed; "
            "using legacy RasterToPolygon + Dissolve fallback."
        )


        temp_polys = arcpy.CreateUniqueName(
            "catchment_polys",
            arcpy.env.scratchGDB
        )


        arcpy.conversion.RasterToPolygon(
            arcpy.Raster(
                catchment_raster
            ),
            temp_polys,
            "NO_SIMPLIFY"
        )


        arcpy.management.Dissolve(
            temp_polys,
            out_catchments,
            "gridcode"
        )


        try:

            if arcpy.Exists(
                temp_polys
            ):

                arcpy.management.Delete(
                    temp_polys
                )


        except Exception:

            pass


    # -------------------------------------------------------------
    # Standardize GridID field
    # -------------------------------------------------------------

    field_names = {
        field.name
        for field in arcpy.ListFields(
            out_catchments
        )
    }


    if "Id" in field_names:

        arcpy.management.DeleteField(
            out_catchments,
            "Id"
        )


    field_names = {
        field.name
        for field in arcpy.ListFields(
            out_catchments
        )
    }


    grid_source = None


    for candidate in [
        "gridcode",
        "GRIDCODE",
        "GridCode",
        "VALUE",
        "Value",
    ]:

        if candidate in field_names:

            grid_source = (
                candidate
            )

            break


    if (
        grid_source
        and
        grid_source
        !=
        "GridID"
    ):

        arcpy.management.AlterField(
            out_catchments,
            grid_source,
            "GridID",
            "GridID"
        )


    # -------------------------------------------------------------
    # Assign HydroID using original ArcHydro tool.
    # -------------------------------------------------------------

    run_log(
        "Assigning catchment HydroIDs..."
    )


    tool = (
        assignhydroid.AssignHydroID()
    )


    tool.DebugLevel = (
        1
    )


    parameter_fc = arcpy.Parameter()

    parameter_fc.value = (
        out_catchments
    )


    parameter_overwrite = arcpy.Parameter()

    parameter_overwrite.value = (
        "OVERWRITE_NO"
    )


    tool.execute(
        [
            parameter_fc,
            parameter_overwrite,
        ],
        None
    )


    # -------------------------------------------------------------
    # Indices
    # -------------------------------------------------------------

    fields_after = {
        field.name
        for field in arcpy.ListFields(
            out_catchments
        )
    }


    if "GridID" in fields_after:

        try:

            arcpy.management.AddIndex(
                out_catchments,
                "GridID",
                "GridID_Index",
                "NON_UNIQUE",
                "ASCENDING"
            )

        except Exception:

            pass


    if "HydroID" in fields_after:

        try:

            arcpy.management.AddIndex(
                out_catchments,
                "HydroID",
                "HydroID_Index",
                "NON_UNIQUE",
                "ASCENDING"
            )

        except Exception:

            pass


    count = int(
        arcpy.management.GetCount(
            out_catchments
        )[0]
    )


    if count <= 0:

        fail(
            "Catchment polygon output contains "
            "no features."
        )


    return out_catchments


# ---------------------------------------------------------------------
# 5) Drainage Line Processing
# ---------------------------------------------------------------------

def detect_gridcode_field(
    fc_path
):

    existing = {
        field.name
        for field in arcpy.ListFields(
            fc_path
        )
    }


    candidates = [
        "Grid_Code",
        "GRID_CODE",
        "grid_code",
        "GridCode",
        "gridcode",
        "gridcode_1",
        "VALUE",
        "Value",
    ]


    return next(
        (
            candidate
            for candidate in candidates
            if candidate in existing
        ),
        None
    )


def cleanup_duplicate_stream_features(
    stream_fc,
    gridcode_field
):
    """
    Preserve the original cleanup rule:
    for duplicate grid codes, keep the longest feature.
    """

    length_field = (
        apwrutils.FN_ShapeAtLength
    )


    if not arcpy.ListFields(
        stream_fc,
        gridcode_field
    ):

        warn(
            f"{gridcode_field} not found. "
            "Skipping duplicate cleanup."
        )

        return


    if not arcpy.ListFields(
        stream_fc,
        length_field
    ):

        warn(
            f"{length_field} not found. "
            "Skipping duplicate cleanup."
        )

        return


    oid_field = arcpy.Describe(
        stream_fc
    ).OIDFieldName


    fields = [
        oid_field,
        gridcode_field,
        length_field,
    ]


    # Build grid -> [(oid, length), ...]
    groups = {}


    with arcpy.da.SearchCursor(
        stream_fc,
        fields
    ) as cursor:

        for oid, gridcode, shape_length in cursor:

            groups.setdefault(
                gridcode,
                []
            ).append(
                (
                    oid,
                    shape_length,
                )
            )


    delete_oids = []


    for gridcode, rows in groups.items():

        if len(
            rows
        ) <= 1:

            continue


        longest_oid = max(
            rows,
            key=lambda row: (
                row[
                    1
                ]
                if row[
                    1
                ]
                is not None
                else
                -1
            )
        )[
            0
        ]


        delete_oids.extend(
            oid
            for oid, _ in rows
            if oid
            !=
            longest_oid
        )


    if not delete_oids:

        return


    delete_set = set(
        delete_oids
    )


    with arcpy.da.UpdateCursor(
        stream_fc,
        [
            oid_field
        ]
    ) as cursor:

        for row in cursor:

            if row[
                0
            ] in delete_set:

                cursor.deleteRow()


    run_log(
        f"Duplicate cleanup deleted "
        f"{len(delete_oids)} line(s)."
    )


def run_drainage_line_processing(
    stream_link,
    flow_dir_raster,
    out_stream
):

    set_hydro_env(
        flow_dir_raster
    )


    temp_stream = arcpy.CreateUniqueName(
        "tmp_drainage_line",
        arcpy.env.scratchGDB
    )


    try:

        run_log(
            "Running StreamToFeature..."
        )


        StreamToFeature(
            stream_link,
            flow_dir_raster,
            temp_stream,
            "NO_SIMPLIFY"
        )


        if arcpy.Exists(
            out_stream
        ):

            arcpy.management.Delete(
                out_stream
            )


        arcpy.management.CopyFeatures(
            temp_stream,
            out_stream
        )


        grid_field = detect_gridcode_field(
            out_stream
        )


        if grid_field:

            cleanup_duplicate_stream_features(
                out_stream,
                grid_field
            )


        else:

            warn(
                "No GridCode-like field found on stream output; "
                "duplicate cleanup skipped."
            )


    finally:

        try:

            if arcpy.Exists(
                temp_stream
            ):

                arcpy.management.Delete(
                    temp_stream
                )


        except Exception:

            pass


    count = int(
        arcpy.management.GetCount(
            out_stream
        )[0]
    )


    if count <= 0:

        fail(
            "Drainage Line output contains "
            "no features."
        )


    return out_stream


# ---------------------------------------------------------------------
# Output family helpers
# ---------------------------------------------------------------------

def inspect_feature_class(
    path,
    expected_geometry,
    expected_wkid
):

    result = {
        "exists":
            False,

        "valid":
            False,

        "issues":
            [],
    }


    if not arcpy.Exists(
        path
    ):

        return result


    result[
        "exists"
    ] = True


    try:

        desc = arcpy.Describe(
            path
        )


        geometry = getattr(
            desc,
            "shapeType",
            None
        )


        wkid = getattr(
            desc.spatialReference,
            "factoryCode",
            None
        )


        if geometry != expected_geometry:

            result[
                "issues"
            ].append(
                f"Geometry {geometry} "
                f"!= {expected_geometry}"
            )


        if wkid != int(
            expected_wkid
        ):

            result[
                "issues"
            ].append(
                f"WKID {wkid} "
                f"!= {expected_wkid}"
            )


        count = int(
            arcpy.management.GetCount(
                path
            )[0]
        )


        if count <= 0:

            result[
                "issues"
            ].append(
                "Feature class contains no features."
            )


    except Exception as exc:

        result[
            "issues"
        ].append(
            f"Could not inspect feature class: "
            f"{exc}"
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


def delete_threshold_family(
    stream_fc,
    catchment_fc
):

    for path in [
        stream_fc,
        catchment_fc,
    ]:

        if arcpy.Exists(
            path
        ):

            arcpy.management.Delete(
                path
            )


# ---------------------------------------------------------------------
# Per-threshold workflow
# ---------------------------------------------------------------------

def process_threshold(
    threshold,
    county_name,
    county_abbr,
    target_wkid,
    flow_acc_raster,
    flow_dir_raster,
    output_gdb,
    overwrite
):

    token = threshold_token(
        threshold
    )


    out_stream = os.path.join(
        output_gdb,
        f"{county_name}_Stream_{token}"
    )


    out_catchment = os.path.join(
        output_gdb,
        f"{county_name}_Catchment_{token}"
    )


    stream_check = inspect_feature_class(
        out_stream,
        "Polyline",
        target_wkid
    )


    catchment_check = inspect_feature_class(
        out_catchment,
        "Polygon",
        target_wkid
    )


    # -------------------------------------------------------------
    # Existing family behavior
    # -------------------------------------------------------------

    if (
        stream_check[
            "valid"
        ]
        and
        catchment_check[
            "valid"
        ]
        and
        not overwrite
    ):

        run_log(
            f"{token}: existing Stream + Catchment "
            f"family is valid; reusing."
        )


        return (
            "SKIPPED_EXISTING_VALID",
            out_stream,
            out_catchment
        )


    any_exists = (
        stream_check[
            "exists"
        ]
        or
        catchment_check[
            "exists"
        ]
    )


    if (
        any_exists
        and
        not overwrite
    ):

        warn(
            f"{token}: existing Stream/Catchment family is "
            f"incomplete or invalid."
        )


        warn(
            f"{token}: threshold was skipped because "
            f"Overwrite Existing Families = False."
        )


        return (
            "SKIPPED_INVALID_OR_PARTIAL_EXISTING",
            out_stream,
            out_catchment
        )


    if overwrite:

        delete_threshold_family(
            out_stream,
            out_catchment
        )


    # -------------------------------------------------------------
    # Per-threshold intermediate rasters
    # -------------------------------------------------------------

    stream_raster = os.path.join(
        output_gdb,
        f"Str_{token}"
    )


    stream_link = os.path.join(
        output_gdb,
        f"StrLnk_{token}"
    )


    catchment_raster = os.path.join(
        output_gdb,
        f"Cat_{token}"
    )


    intermediates = [
        stream_raster,
        stream_link,
        catchment_raster,
    ]


    for path in intermediates:

        if arcpy.Exists(
            path
        ):

            arcpy.management.Delete(
                path
            )


    try:

        run_log(
            f"----- Processing threshold "
            f"{threshold} ({token}) -----"
        )


        # 1 Stream Definition
        t1 = time.time()

        log_step_start(
            f"{token} - Stream Definition"
        )


        valid_stream, stream_result = (
            run_stream_definition(
                flow_acc_raster,
                threshold,
                stream_raster
            )
        )


        if not valid_stream:

            warn(
                f"{token}: no valid stream raster generated. "
                f"{stream_result}"
            )


            return (
                "SKIPPED_NO_STREAMS",
                None,
                None
            )


        log_step_end(
            f"{token} - Stream Definition"
        )


        run_log(
            f"{token}: Stream Definition finished "
            f"in {format_elapsed(t1)}"
        )


        # 2 Stream Segmentation
        t2 = time.time()

        log_step_start(
            f"{token} - Stream Segmentation"
        )


        run_stream_segmentation(
            stream_raster,
            flow_dir_raster,
            stream_link
        )


        log_step_end(
            f"{token} - Stream Segmentation"
        )


        run_log(
            f"{token}: Stream Segmentation finished "
            f"in {format_elapsed(t2)}"
        )


        # 3 Catchment Grid
        t3 = time.time()

        log_step_start(
            f"{token} - Catchment Grid"
        )


        run_catchment_grid(
            flow_dir_raster,
            stream_link,
            catchment_raster
        )


        log_step_end(
            f"{token} - Catchment Grid"
        )


        run_log(
            f"{token}: Catchment Grid finished "
            f"in {format_elapsed(t3)}"
        )


        # 4 Catchment Polygon
        t4 = time.time()

        log_step_start(
            f"{token} - Catchment Polygon"
        )


        run_catchment_polygon_processing(
            catchment_raster,
            out_catchment,
            flow_dir_raster
        )


        log_step_end(
            f"{token} - Catchment Polygon"
        )


        run_log(
            f"{token}: Catchment Polygon finished "
            f"in {format_elapsed(t4)}"
        )


        # 5 Drainage Line
        t5 = time.time()

        log_step_start(
            f"{token} - Drainage Line"
        )


        run_drainage_line_processing(
            stream_link,
            flow_dir_raster,
            out_stream
        )


        log_step_end(
            f"{token} - Drainage Line"
        )


        run_log(
            f"{token}: Drainage Line finished "
            f"in {format_elapsed(t5)}"
        )


        # ---------------------------------------------------------
        # Final family QC
        # ---------------------------------------------------------

        stream_final = inspect_feature_class(
            out_stream,
            "Polyline",
            target_wkid
        )


        catchment_final = inspect_feature_class(
            out_catchment,
            "Polygon",
            target_wkid
        )


        if not stream_final[
            "valid"
        ]:

            fail(
                f"{token}: new Stream output failed QC: "
                +
                "; ".join(
                    stream_final[
                        "issues"
                    ]
                )
            )


        if not catchment_final[
            "valid"
        ]:

            fail(
                f"{token}: new Catchment output failed QC: "
                +
                "; ".join(
                    catchment_final[
                        "issues"
                    ]
                )
            )


        run_log(
            f"{token}: PASS - "
            f"Stream={out_stream}"
        )


        run_log(
            f"{token}: PASS - "
            f"Catchment={out_catchment}"
        )


        return (
            "PASS",
            out_stream,
            out_catchment
        )


    finally:

        # Remove intermediate rasters for this threshold.
        for path in intermediates:

            try:

                if arcpy.Exists(
                    path
                ):

                    arcpy.management.Delete(
                        path
                    )


            except Exception as exc:

                warn(
                    f"{token}: could not delete intermediate "
                    f"{path}: {exc}"
                )


        # ArcHydro may create this table.
        for name in [
            "APUNIQUEID",
            "DrainageLine_FS",
        ]:

            path = os.path.join(
                output_gdb,
                name
            )


            try:

                if arcpy.Exists(
                    path
                ):

                    arcpy.management.Delete(
                        path
                    )


            except Exception:

                pass


        gc.collect()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    t_all = time.time()


    run_folder = p(
        0
    )


    flow_dir_raster = p(
        1
    )


    flow_acc_raster = p(
        2
    )


    threshold_text = p(
        3
    )


    output_gdb = p(
        4
    )


    overwrite = get_bool(
        5,
        False
    )


    if not run_folder:

        fail(
            "Processing Run Folder is required."
        )


    if not flow_dir_raster:

        fail(
            "Flow Direction Raster is required."
        )


    if not flow_acc_raster:

        fail(
            "Flow Accumulation Raster is required."
        )


    if not output_gdb:

        fail(
            "Output Streams Working GDB is required."
        )


    if not output_gdb.lower().endswith(
        ".gdb"
    ):

        fail(
            "Output Streams Working GDB must be a File Geodatabase "
            "path ending in '.gdb'."
        )


    thresholds = parse_thresholds(
        threshold_text
    )


    # -------------------------------------------------------------
    # Context
    # -------------------------------------------------------------

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
        tool_id="3.1",
        tool_name="Generate_Streams_Catchments",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder":
                run_folder,

            "Run ID":
                context[
                    "run_id"
                ],

            "Flow Direction Raster":
                flow_dir_raster,

            "Flow Accumulation Raster":
                flow_acc_raster,

            "Thresholds":
                thresholds,

            "Output Streams Working GDB":
                output_gdb,
        }
    )


    # -------------------------------------------------------------
    # Visible operational FD / FA inputs
    #
    # ToolValidator normally populates these from the selected
    # Processing Run Folder. Users can explicitly override them.
    # -------------------------------------------------------------

    expected_base_name = (
        f"{county_abbr}_"
        f"DEM_HUC12_AGREE"
    )


    expected_fd = os.path.join(
        run_folder,
        f"{expected_base_name}_FD.tif"
    )


    expected_fa = os.path.join(
        run_folder,
        f"{expected_base_name}_FA.tif"
    )


    if os.path.normcase(
        os.path.normpath(
            flow_dir_raster
        )
    ) != os.path.normcase(
        os.path.normpath(
            expected_fd
        )
    ):

        warn(
            "Flow Direction Raster overrides the default raster "
            "for this Processing Run."
        )


    if os.path.normcase(
        os.path.normpath(
            flow_acc_raster
        )
    ) != os.path.normcase(
        os.path.normpath(
            expected_fa
        )
    ):

        warn(
            "Flow Accumulation Raster overrides the default raster "
            "for this Processing Run."
        )


    fd_check = inspect_raster(
        flow_dir_raster,
        target_wkid
    )


    fa_check = inspect_raster(
        flow_acc_raster,
        target_wkid
    )


    if not fd_check[
        "exists"
    ]:

        warn(
            "Step 3.1 was not run because the Step 2.3 "
            "Flow Direction raster does not exist."
        )


        warn(
            f"Selected FD: "
            f"{flow_dir_raster}"
        )


        update_run_config(
            context,
            "SKIPPED_FD_NOT_READY",
            {},
            ""
        )


        finish_tool_log(
            "SKIPPED_FD_NOT_READY"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_FD_NOT_READY"
        )


        return


    if not fa_check[
        "exists"
    ]:

        warn(
            "Step 3.1 was not run because the Step 2.3 "
            "Flow Accumulation raster does not exist."
        )


        warn(
            f"Selected FA: "
            f"{flow_acc_raster}"
        )


        update_run_config(
            context,
            "SKIPPED_FA_NOT_READY",
            {},
            ""
        )


        finish_tool_log(
            "SKIPPED_FA_NOT_READY"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_FA_NOT_READY"
        )


        return


    if (
        not fd_check[
            "valid"
        ]
        or
        not fa_check[
            "valid"
        ]
    ):

        warn(
            "Step 3.1 was not run because the Step 2.3 "
            "FD/FA rasters do not match the county production CRS."
        )


        update_run_config(
            context,
            "SKIPPED_FD_FA_INVALID",
            {},
            ""
        )


        finish_tool_log(
            "SKIPPED_FD_FA_INVALID"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_FD_FA_INVALID"
        )


        return


    # FD and FA must align.
    if (
        abs(
            fd_check[
                "cell_x"
            ]
            -
            fa_check[
                "cell_x"
            ]
        )
        >
        1e-6
        or
        abs(
            fd_check[
                "cell_y"
            ]
            -
            fa_check[
                "cell_y"
            ]
        )
        >
        1e-6
    ):

        fail(
            "Flow Direction and Flow Accumulation "
            "cell sizes do not match."
        )


    fd_extent = arcpy.Describe(
        flow_dir_raster
    ).extent


    fa_extent = arcpy.Describe(
        flow_acc_raster
    ).extent


    extent_values_fd = (
        fd_extent.XMin,
        fd_extent.YMin,
        fd_extent.XMax,
        fd_extent.YMax,
    )


    extent_values_fa = (
        fa_extent.XMin,
        fa_extent.YMin,
        fa_extent.XMax,
        fa_extent.YMax,
    )


    if any(
        abs(
            a
            -
            b
        )
        >
        1e-6
        for a, b in zip(
            extent_values_fd,
            extent_values_fa
        )
    ):

        fail(
            "Flow Direction and Flow Accumulation "
            "extents do not match."
        )


    # -------------------------------------------------------------
    # Visible operational output GDB
    #
    # ToolValidator normally proposes:
    #   <Run Folder>\<County>_Streams_Working.gdb
    # but the user may explicitly choose another File GDB.
    # -------------------------------------------------------------

    expected_output_gdb = os.path.join(
        run_folder,
        f"{county_name}_Streams_Working.gdb"
    )


    if os.path.normcase(
        os.path.normpath(
            output_gdb
        )
    ) != os.path.normcase(
        os.path.normpath(
            expected_output_gdb
        )
    ):

        warn(
            "Output Streams Working GDB overrides the default "
            "GDB location for this Processing Run."
        )


    if not arcpy.Exists(
        output_gdb
    ):

        output_parent = os.path.dirname(
            output_gdb
        )


        if not os.path.isdir(
            output_parent
        ):

            os.makedirs(
                output_parent,
                exist_ok=True
            )


        arcpy.management.CreateFileGDB(
            output_parent,
            os.path.basename(
                output_gdb
            )
        )


    # -------------------------------------------------------------
    # Scratch
    # -------------------------------------------------------------

    scratch_root = os.path.join(
        run_folder,
        "scratch"
    )


    scratch_gdb = ensure_scratch_gdb(
        scratch_root
    )


    # -------------------------------------------------------------
    # Log
    # -------------------------------------------------------------

    run_log(
        "=" * 72
    )


    run_log(
        "3.1 Generate Streams and Catchments"
    )


    run_log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )


    run_log(
        f"Thresholds: "
        f"{thresholds}"
    )


    run_log(
        f"Flow Direction: "
        f"{flow_dir_raster}"
    )


    run_log(
        f"Flow Accumulation: "
        f"{flow_acc_raster}"
    )


    run_log(
        f"Working GDB: "
        f"{output_gdb}"
    )


    run_log(
        f"Processing Run Folder: "
        f"{run_folder}"
    )


    run_log(
        "=" * 72
    )


    # -------------------------------------------------------------
    # Spatial Analyst + environment
    # -------------------------------------------------------------

    ensure_spatial()


    old_scratch = (
        arcpy.env.scratchWorkspace
    )


    old_overwrite = (
        arcpy.env.overwriteOutput
    )


    try:

        arcpy.env.scratchWorkspace = (
            scratch_gdb
        )


        arcpy.env.overwriteOutput = (
            True
        )


        results = {}


        for threshold in thresholds:

            token = threshold_token(
                threshold
            )


            status, stream_fc, catchment_fc = (
                process_threshold(
                    threshold=threshold,
                    county_name=county_name,
                    county_abbr=county_abbr,
                    target_wkid=target_wkid,
                    flow_acc_raster=flow_acc_raster,
                    flow_dir_raster=flow_dir_raster,
                    output_gdb=output_gdb,
                    overwrite=overwrite
                )
            )


            results[
                token
            ] = {
                "status":
                    status,

                "stream":
                    stream_fc,

                "catchment":
                    catchment_fc,
            }


        # ---------------------------------------------------------
        # Final summary
        # ---------------------------------------------------------

        run_log(
            "----- Threshold Summary -----"
        )


        any_pass = False

        any_skip = False


        for token in sorted(
            results.keys(),
            key=lambda value: (
                int(
                    value[
                        :-1
                    ]
                )
                if value.endswith(
                    "k"
                )
                else
                int(
                    value
                )
            )
        ):

            result = results[
                token
            ]


            run_log(
                f"{token}: "
                f"{result['status']}"
            )


            if result[
                "status"
            ] in {
                "PASS",
                "SKIPPED_EXISTING_VALID",
            }:

                any_pass = True


            else:

                any_skip = True


        if (
            any_pass
            and
            any_skip
        ):

            overall_status = (
                "PARTIAL"
            )


        elif any_pass:

            overall_status = (
                "PASS"
            )


        else:

            overall_status = (
                "SKIPPED"
            )


        run_log(
            f"Overall Status: "
            f"{overall_status}"
        )


        run_log(
            f"Total runtime: "
            f"{format_elapsed(t_all)}"
        )


        update_run_config(
            context,
            overall_status,
            results,
            output_gdb
        )


        finish_tool_log(
            overall_status
        )


        arcpy.SetParameterAsText(
            6,
            output_gdb
        )


        arcpy.SetParameterAsText(
            7,
            overall_status
        )


    finally:

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
                7,
                "FAIL"
            )

        except Exception:

            pass


        raise