# -*- coding: utf-8 -*-
"""
2.2 DEM Hydrologic Reconditioning
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Continue an EXPLICIT timestamped Processing Run created by Step 2.1.

The user selects the Processing Run Folder directly. The tool does not search
for the latest run and does not create a new run.

Expected run structure
----------------------
<County Folder>\working\<County>_<timestamp>\
    run_config.json
    logs\
    scratch\
    <ABBR>_DEM_HUC12_with_Zonal.tif

Step 2.2 creates:
    <ABBR>_DEM_HUC12_AGREE.tif

and updates run_config.json in the SAME run folder.

Production defaults
-------------------
Coarse DEM Factor = 2
Buffer Cells      = 5
Smooth Drop       = 10
Sharp Drop        = 100

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder         Folder          Required Input
1  Coarse DEM Factor             Long            Optional Input
2  Buffer Cells                  Long            Optional Input
3  Smooth Drop                   Long            Optional Input
4  Sharp Drop                    Long            Optional Input
5  Overwrite Existing Output     Boolean         Optional Input
6  Reconditioned DEM             Raster Dataset  Derived Output
7  Processing Status             String          Derived Output
"""

import os
import sys
import time
import datetime

import arcpy
import arcpy.sa as sa


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
# Defaults
# ---------------------------------------------------------------------

DEFAULT_COARSE_FACTOR = 2
DEFAULT_BUFFER_CELLS = 5
DEFAULT_SMOOTH_DROP = 10
DEFAULT_SHARP_DROP = 100


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


def get_long(
    index,
    default
):

    value = p(
        index
    )

    if not value:
        return int(
            default
        )

    return int(
        value
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


    config_county = county.get(
        "county",
        {}
    )


    config_abbr = str(
        config_county.get(
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
    output_dem=None,
    parameters=None
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
        "2.2"
    ] = {
        "status":
            tool_status,

        "parameters":
            parameters
            or {},

        "output":
            output_dem,
    }


    outputs = run_config.setdefault(
        "outputs",
        {}
    )


    if output_dem:

        outputs[
            "agree_dem"
        ] = output_dem


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
        "exists": False,
        "valid": False,
        "wkid": None,
        "cell_x": None,
        "cell_y": None,
        "issues": [],
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
            f"Could not inspect raster: {exc}"
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
            f"WKID {result['wkid']} "
            f"!= required {expected_wkid}"
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


        elif os.path.isfile(
            path
        ):

            os.remove(
                path
            )


    except Exception as exc:

        warn(
            f"Cleanup warning for "
            f"{path}: {exc}"
        )


# ---------------------------------------------------------------------
# Coarse DEM
# ---------------------------------------------------------------------

def build_coarse_dem(
    in_dem,
    factor,
    run_folder,
    overwrite
):

    if factor <= 1:

        log(
            "Coarse DEM Factor <= 1; "
            "using Step 2.1 DEM directly."
        )

        return in_dem


    cache_folder = os.path.join(
        run_folder,
        "cache"
    )


    ensure_folder(
        cache_folder
    )


    desc = arcpy.Describe(
        in_dem
    )


    target_cell = (
        float(
            arcpy.Raster(
                in_dem
            ).meanCellWidth
        )
        *
        float(
            factor
        )
    )


    coarse_dem = os.path.join(
        cache_folder,
        f"{desc.baseName}_coarse_x{factor}.tif"
    )


    if arcpy.Exists(
        coarse_dem
    ):

        check = inspect_raster(
            coarse_dem,
            expected_wkid=getattr(
                desc.spatialReference,
                "factoryCode",
                None
            ),
            expected_cell_size=target_cell
        )


        if (
            check[
                "valid"
            ]
            and
            not overwrite
        ):

            log(
                f"Existing coarse DEM is valid; "
                f"reusing: {coarse_dem}"
            )

            return coarse_dem


        safe_delete(
            coarse_dem
        )


    log_step_start(
        "Resample Coarse DEM"
    )


    arcpy.management.Resample(
        in_dem,
        coarse_dem,
        target_cell,
        "BILINEAR"
    )


    log_step_end(
        "Resample Coarse DEM"
    )


    return coarse_dem


# ---------------------------------------------------------------------
# NHD prep
# ---------------------------------------------------------------------

def safe_allnodata(
    raster_path
):

    old_extent = arcpy.env.extent

    old_mask = arcpy.env.mask


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

        except Exception as exc:

            warn(
                f"CalculateStatistics skipped/failed for "
                f"prepared NHD raster: {exc}"
            )


        try:

            value = (
                arcpy.management.GetRasterProperties(
                    raster_path,
                    "ALLNODATA"
                )
                .getOutput(
                    0
                )
            )


            return str(
                value
            )


        except Exception:

            return "unknown"


    finally:

        arcpy.env.extent = (
            old_extent
        )


        arcpy.env.mask = (
            old_mask
        )


def prepare_stream_raster_for_dem(
    stream_raster_src,
    dem_raster,
    scratch_gdb,
    temp_paths,
    nodata_value=0
):

    dem = arcpy.Raster(
        dem_raster
    )


    dem_desc = arcpy.Describe(
        dem_raster
    )


    dem_sr = (
        dem_desc.spatialReference
    )


    dem_cell = float(
        dem.meanCellWidth
    )


    dem_extent = (
        dem.extent
    )


    src_desc = arcpy.Describe(
        stream_raster_src
    )


    src_sr = (
        src_desc.spatialReference
    )


    work_raster = (
        stream_raster_src
    )


    src_wkid = getattr(
        src_sr,
        "factoryCode",
        None
    )


    dem_wkid = getattr(
        dem_sr,
        "factoryCode",
        None
    )


    if src_wkid != dem_wkid:

        projected = arcpy.CreateUniqueName(
            "nhd_proj",
            scratch_gdb
        )


        temp_paths.append(
            projected
        )


        log_step_start(
            "Project NHD Raster"
        )


        arcpy.management.ProjectRaster(
            in_raster=work_raster,
            out_raster=projected,
            out_coor_system=dem_sr,
            resampling_type="NEAREST",
            cell_size=dem_cell
        )


        log_step_end(
            "Project NHD Raster"
        )


        work_raster = (
            projected
        )


    src_cell = float(
        arcpy.Raster(
            work_raster
        ).meanCellWidth
    )


    if abs(
        src_cell
        -
        dem_cell
    ) > 1e-6:

        resampled = arcpy.CreateUniqueName(
            "nhd_res",
            scratch_gdb
        )


        temp_paths.append(
            resampled
        )


        log_step_start(
            "Resample NHD Raster"
        )


        arcpy.management.Resample(
            work_raster,
            resampled,
            dem_cell,
            "NEAREST"
        )


        log_step_end(
            "Resample NHD Raster"
        )


        work_raster = (
            resampled
        )


    clipped = arcpy.CreateUniqueName(
        "nhd_clip",
        scratch_gdb
    )


    temp_paths.append(
        clipped
    )


    extent_text = (
        f"{dem_extent.XMin} "
        f"{dem_extent.YMin} "
        f"{dem_extent.XMax} "
        f"{dem_extent.YMax}"
    )


    log_step_start(
        "Clip NHD Raster to AGREE DEM"
    )


    arcpy.management.Clip(
        in_raster=work_raster,
        rectangle=extent_text,
        out_raster=clipped,
        nodata_value=str(
            nodata_value
        ),
        clipping_geometry="NONE",
        maintain_clipping_extent="MAINTAIN_EXTENT"
    )


    log_step_end(
        "Clip NHD Raster to AGREE DEM"
    )


    allnodata = safe_allnodata(
        clipped
    )


    log(
        f"Prepared NHD raster ALLNODATA="
        f"{allnodata}"
    )


    if allnodata == "1":

        fail(
            "Prepared NHD raster is entirely NoData within "
            "the AGREE DEM extent."
        )


    return clipped


# ---------------------------------------------------------------------
# Euclidean AGREE
# ---------------------------------------------------------------------

def run_euclidean_agree(
    dem_raster_path,
    streams_raster_path,
    output_dem,
    buffer_cells,
    smooth_drop,
    sharp_drop,
    scratch_gdb
):

    temp_paths = []


    dem_raster = sa.Raster(
        dem_raster_path
    )


    dem_extent = (
        dem_raster.extent
    )


    dem_cellsize = float(
        dem_raster.meanCellWidth
    )


    buffer_distance = (
        float(
            buffer_cells
        )
        -
        0.5
    ) * dem_cellsize


    try:

        if dem_raster.minimum is None:

            arcpy.management.CalculateStatistics(
                dem_raster_path
            )


            dem_raster = sa.Raster(
                dem_raster_path
            )


    except Exception:

        arcpy.management.CalculateStatistics(
            dem_raster_path
        )


        dem_raster = sa.Raster(
            dem_raster_path
        )


    shifted = False

    shift_value = 0


    if (
        dem_raster.minimum is not None
        and
        dem_raster.minimum <= 0
    ):

        shifted = True


        minimum_offset = (
            abs(
                float(
                    dem_raster.minimum
                )
            )
            +
            1
        )


        shift_value = (
            float(
                smooth_drop
            )
            +
            float(
                sharp_drop
            )
            +
            minimum_offset
        )


        log(
            f"DEM minimum <= 0; temporary upward shift="
            f"{shift_value}"
        )


        dem_raster = (
            dem_raster
            +
            shift_value
        )


    streams_raster = (
        prepare_stream_raster_for_dem(
            stream_raster_src=streams_raster_path,
            dem_raster=dem_raster_path,
            scratch_gdb=scratch_gdb,
            temp_paths=temp_paths,
            nodata_value=0
        )
    )


    try:

        log_step_start(
            "Assign DEM Values to Stream Cells"
        )


        stream_bool = (
            sa.Raster(
                streams_raster
            )
            >
            0
        )


        dem_agree = sa.Con(
            stream_bool,
            dem_raster
        )


        log_step_end(
            "Assign DEM Values to Stream Cells"
        )


        log_step_start(
            "Build Smooth Stream Surface"
        )


        smooth_geo = (
            dem_agree
            -
            smooth_drop
        )


        if not dem_raster.isInteger:

            smooth_geo = sa.Int(
                smooth_geo
            )


        smooth_geo = sa.Con(
            stream_bool,
            smooth_geo
        )


        log_step_end(
            "Build Smooth Stream Surface"
        )


        log_step_start(
            "EucAllocation Inside Buffer"
        )


        vec_eucdist_path = arcpy.CreateUniqueName(
            "veceucdist",
            scratch_gdb
        )


        temp_paths.append(
            vec_eucdist_path
        )


        vec_eucallo_geo = sa.EucAllocation(
            smooth_geo,
            buffer_distance,
            out_distance_raster=vec_eucdist_path
        )


        vec_eucdist_raster = sa.Raster(
            vec_eucdist_path
        )


        log_step_end(
            "EucAllocation Inside Buffer"
        )


        log_step_start(
            "Build Buffer Elevation Donut"
        )


        buffer_elev = sa.Con(
            sa.IsNull(
                vec_eucdist_raster
            ),
            dem_raster
        )


        buffer_elev_path = arcpy.CreateUniqueName(
            "bufferlev",
            scratch_gdb
        )


        temp_paths.append(
            buffer_elev_path
        )


        arcpy.management.CopyRaster(
            buffer_elev,
            buffer_elev_path
        )


        log_step_end(
            "Build Buffer Elevation Donut"
        )


        log_step_start(
            "EucAllocation Outside Buffer"
        )


        old_extent = arcpy.env.extent

        old_mask = arcpy.env.mask


        try:

            inside_zone = sa.IsNull(
                buffer_elev
            )


            inside_zone_path = arcpy.CreateUniqueName(
                "inside_zone",
                scratch_gdb
            )


            temp_paths.append(
                inside_zone_path
            )


            inside_zone.save(
                inside_zone_path
            )


            if not dem_raster.isInteger:

                buffer_elev_integer = sa.Int(
                    buffer_elev
                )


            else:

                buffer_elev_integer = (
                    buffer_elev
                )


            buffer_elev_integer_path = (
                arcpy.CreateUniqueName(
                    "buffer_elevi",
                    scratch_gdb
                )
            )


            temp_paths.append(
                buffer_elev_integer_path
            )


            buffer_elev_integer.save(
                buffer_elev_integer_path
            )


            arcpy.env.extent = (
                dem_extent
            )


            arcpy.env.mask = (
                inside_zone_path
            )


            buf_eucdist_path = arcpy.CreateUniqueName(
                "bufeucdist",
                scratch_gdb
            )


            temp_paths.append(
                buf_eucdist_path
            )


            buf_eucallo_geo = sa.EucAllocation(
                buffer_elev_integer_path,
                out_distance_raster=buf_eucdist_path
            )


            buf_eucdist_raster = sa.Raster(
                buf_eucdist_path
            )


        finally:

            arcpy.env.extent = (
                old_extent
            )


            arcpy.env.mask = (
                old_mask
            )


        log_step_end(
            "EucAllocation Outside Buffer"
        )


        log_step_start(
            "Blend Smooth Elevation Surface"
        )


        smooth_elev = (
            vec_eucallo_geo
            +
            (
                (
                    buf_eucallo_geo
                    -
                    vec_eucallo_geo
                )
                /
                (
                    buf_eucdist_raster
                    +
                    vec_eucdist_raster
                )
                *
                vec_eucdist_raster
            )
        )


        smooth_elev_path = arcpy.CreateUniqueName(
            "smoothelev",
            scratch_gdb
        )


        temp_paths.append(
            smooth_elev_path
        )


        smooth_elev.save(
            smooth_elev_path
        )


        log_step_end(
            "Blend Smooth Elevation Surface"
        )


        sharp_geo = (
            smooth_geo
            -
            sharp_drop
        )


        log_step_start(
            "Mosaic Smooth and Sharp Surfaces"
        )


        arcpy.env.extent = (
            "DEFAULT"
        )


        mosaic_result = (
            arcpy.management.Mosaic(
                [
                    smooth_elev,
                    sharp_geo,
                ],
                buffer_elev_path,
                "LAST"
            )
        )


        agreed_dem = sa.Raster(
            mosaic_result[
                0
            ]
        )


        log_step_end(
            "Mosaic Smooth and Sharp Surfaces"
        )


        if shifted:

            log(
                f"Reversing temporary DEM shift by "
                f"{shift_value}"
            )


            agreed_dem = (
                agreed_dem
                -
                shift_value
            )


        arcpy.env.extent = (
            dem_extent
        )


        arcpy.env.cellSize = (
            dem_cellsize
        )


        arcpy.env.mask = (
            None
        )


        safe_delete(
            output_dem
        )


        log_step_start(
            "Save AGREE DEM"
        )


        agreed_dem.save(
            output_dem
        )


        log_step_end(
            "Save AGREE DEM"
        )


    finally:

        for path in temp_paths:

            safe_delete(
                path
            )


    return output_dem


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    run_folder = p(
        0
    )


    factor = get_long(
        1,
        DEFAULT_COARSE_FACTOR
    )


    buffer_cells = get_long(
        2,
        DEFAULT_BUFFER_CELLS
    )


    smooth_drop = get_long(
        3,
        DEFAULT_SMOOTH_DROP
    )


    sharp_drop = get_long(
        4,
        DEFAULT_SHARP_DROP
    )


    overwrite = get_bool(
        5,
        False
    )


    if not run_folder:

        fail(
            "Processing Run Folder is required."
        )


    if factor < 1:

        fail(
            "Coarse DEM Factor must be >= 1."
        )


    if buffer_cells < 1:

        fail(
            "Buffer Cells must be >= 1."
        )


    if smooth_drop < 0:

        fail(
            "Smooth Drop must be >= 0."
        )


    if sharp_drop < 0:

        fail(
            "Sharp Drop must be >= 0."
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


    county_abbr = context[
        "county_abbr"
    ]


    county_name = context[
        "county_name"
    ]


    target_wkid = int(
        county_info[
            "wkid"
        ]
    )


    init_tool_log(
        county_folder=run_folder,
        tool_id="2.2",
        tool_name="DEM_Reconditioning",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder":
                run_folder,

            "Run ID":
                context[
                    "run_id"
                ],

            "Coarse DEM Factor":
                factor,

            "Buffer Cells":
                buffer_cells,

            "Smooth Drop":
                smooth_drop,

            "Sharp Drop":
                sharp_drop,
        }
    )


    log(
        "=" * 72
    )


    log(
        "2.2 DEM Hydrologic Reconditioning"
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
    # Step 2.1 input inside SAME run
    # -------------------------------------------------------------

    conditioned_dem = os.path.join(
        run_folder,
        f"{county_abbr}_DEM_HUC12_with_Zonal.tif"
    )


    input_check = inspect_raster(
        conditioned_dem,
        expected_wkid=target_wkid,
        expected_cell_size=3.0
    )


    if not input_check[
        "exists"
    ]:

        warn(
            "Step 2.2 was not run because the Step 2.1 "
            "conditioned DEM does not exist in this run."
        )


        warn(
            f"Expected input: "
            f"{conditioned_dem}"
        )


        update_run_config(
            context,
            "SKIPPED_CONDITIONED_DEM_NOT_READY",
            parameters={
                "coarse_factor":
                    factor,

                "buffer_cells":
                    buffer_cells,

                "smooth_drop":
                    smooth_drop,

                "sharp_drop":
                    sharp_drop,
            }
        )


        finish_tool_log(
            "SKIPPED_CONDITIONED_DEM_NOT_READY"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_CONDITIONED_DEM_NOT_READY"
        )


        return


    if not input_check[
        "valid"
    ]:

        warn(
            "Step 2.2 was not run because the Step 2.1 "
            "conditioned DEM is invalid."
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


        update_run_config(
            context,
            "SKIPPED_CONDITIONED_DEM_INVALID"
        )


        finish_tool_log(
            "SKIPPED_CONDITIONED_DEM_INVALID"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_CONDITIONED_DEM_INVALID"
        )


        return


    # -------------------------------------------------------------
    # NHD from county config
    # -------------------------------------------------------------

    try:

        nhd_raster = (
            county[
                "selected_sources"
            ][
                "nhd_stream_raster"
            ]
        )


    except Exception:

        fail(
            "County Config does not contain "
            "selected_sources > nhd_stream_raster."
        )


    if not arcpy.Exists(
        nhd_raster
    ):

        warn(
            "Step 2.2 was not run because the selected NHD "
            "raster is unavailable."
        )


        update_run_config(
            context,
            "SKIPPED_NHD_NOT_READY"
        )


        finish_tool_log(
            "SKIPPED_NHD_NOT_READY"
        )


        arcpy.SetParameterAsText(
            7,
            "SKIPPED_NHD_NOT_READY"
        )


        return


    expected_output_cell = (
        float(
            input_check[
                "cell_x"
            ]
        )
        *
        factor
    )


    output_dem = os.path.join(
        run_folder,
        f"{county_abbr}_DEM_HUC12_AGREE.tif"
    )


    parameters = {
        "coarse_factor":
            factor,

        "buffer_cells":
            buffer_cells,

        "smooth_drop":
            smooth_drop,

        "sharp_drop":
            sharp_drop,

        "nhd_raster":
            nhd_raster,
    }


    # -------------------------------------------------------------
    # Existing output behavior
    # -------------------------------------------------------------

    if arcpy.Exists(
        output_dem
    ):

        existing_check = inspect_raster(
            output_dem,
            expected_wkid=target_wkid,
            expected_cell_size=expected_output_cell
        )


        if (
            not overwrite
            and
            existing_check[
                "valid"
            ]
        ):

            log(
                "Existing AGREE DEM in this run is valid; "
                "reusing it."
            )


            update_run_config(
                context,
                "SKIPPED_EXISTING_VALID",
                output_dem=output_dem,
                parameters=parameters
            )


            finish_tool_log(
                "SKIPPED_EXISTING_VALID"
            )


            arcpy.SetParameterAsText(
                6,
                output_dem
            )


            arcpy.SetParameterAsText(
                7,
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
                "Existing AGREE DEM does not meet current "
                "requirements."
            )


            warn(
                "Step 2.2 was not run because "
                "Overwrite Existing Output = False."
            )


            update_run_config(
                context,
                "SKIPPED_INVALID_EXISTING",
                output_dem=output_dem,
                parameters=parameters
            )


            finish_tool_log(
                "SKIPPED_INVALID_EXISTING"
            )


            arcpy.SetParameterAsText(
                6,
                output_dem
            )


            arcpy.SetParameterAsText(
                7,
                "SKIPPED_INVALID_EXISTING"
            )


            return


    # -------------------------------------------------------------
    # Spatial Analyst / environment
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


    old_snap = (
        arcpy.env.snapRaster
    )


    old_extent = (
        arcpy.env.extent
    )


    old_cell = (
        arcpy.env.cellSize
    )


    old_output_cs = (
        arcpy.env.outputCoordinateSystem
    )


    old_mask = (
        arcpy.env.mask
    )


    old_scratch = (
        arcpy.env.scratchWorkspace
    )


    old_overwrite = (
        arcpy.env.overwriteOutput
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


        except Exception as exc:

            warn(
                f"Could not set CPU/parallel processing "
                f"environment: {exc}"
            )


        coarse_dem = build_coarse_dem(
            in_dem=conditioned_dem,
            factor=factor,
            run_folder=run_folder,
            overwrite=overwrite
        )


        coarse_desc = arcpy.Describe(
            coarse_dem
        )


        coarse_raster = arcpy.Raster(
            coarse_dem
        )


        arcpy.env.snapRaster = (
            coarse_dem
        )


        arcpy.env.extent = (
            coarse_desc.extent
        )


        arcpy.env.cellSize = (
            coarse_raster.meanCellWidth
        )


        arcpy.env.outputCoordinateSystem = (
            coarse_desc.spatialReference
        )


        arcpy.env.mask = (
            None
        )


        run_euclidean_agree(
            dem_raster_path=coarse_dem,
            streams_raster_path=nhd_raster,
            output_dem=output_dem,
            buffer_cells=buffer_cells,
            smooth_drop=smooth_drop,
            sharp_drop=sharp_drop,
            scratch_gdb=scratch_gdb
        )


        output_check = inspect_raster(
            output_dem,
            expected_wkid=target_wkid,
            expected_cell_size=expected_output_cell
        )


        if not output_check[
            "valid"
        ]:

            fail(
                "New AGREE DEM failed output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in output_check[
                        "issues"
                    ]
                )
            )


        update_run_config(
            context,
            "PASS",
            output_dem=output_dem,
            parameters=parameters
        )


        log(
            f"PASS - Reconditioned DEM: "
            f"{output_dem}"
        )


        finish_tool_log(
            "PASS"
        )


        arcpy.SetParameterAsText(
            6,
            output_dem
        )


        arcpy.SetParameterAsText(
            7,
            "PASS"
        )


    finally:

        arcpy.env.snapRaster = (
            old_snap
        )


        arcpy.env.extent = (
            old_extent
        )


        arcpy.env.cellSize = (
            old_cell
        )


        arcpy.env.outputCoordinateSystem = (
            old_output_cs
        )


        arcpy.env.mask = (
            old_mask
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
                7,
                "FAIL"
            )

        except Exception:

            pass


        raise