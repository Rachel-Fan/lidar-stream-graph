# -*- coding: utf-8 -*-
"""
3.2 Generate Stream Characteristics
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Refactored from:
    Generate-Characteristics-Fields-1208.py

IMPORTANT WORKFLOW ORDER
------------------------
This tool generates the characteristic fields required by JD Confidence.
Therefore the actual execution order is:

    3.1 Generate Streams & Catchments
    3.2 Generate Characteristics
    3.2 Calculate JD Confidence

The numbering can be swapped later if desired.

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder               Folder          Required Input
1  Streams Working GDB                 Workspace       Required Input
2  Original HUC12 DEM                  Raster Dataset  Required Input
3  NLCD Raster                         Raster Dataset  Required Input
4  County Boundary                     Feature Layer   Required Input
5  Thresholds to Process               String          Required Input, MultiValue
6  Overwrite Existing Characteristics  Boolean         Optional Input
7  Characteristics Output GDB          Workspace       Derived Output
8  Processing Status                   String          Derived Output

Internally resolved reference datasets
--------------------------------------
The following sources are still used by the characteristics workflow but are
not exposed as user-facing parameters. They are resolved from Project Config:

- NLCD_type
- OH_NHD_5m_River_buffer_500ft_w_Ohio_Maumee
- OH_Wetlands_Lakes_Buffer_200ft


Input feature families in SAME run
----------------------------------
<County>_Stream_5k / <County>_Catchment_5k
<County>_Stream_10k / <County>_Catchment_10k
<County>_Stream_20k / <County>_Catchment_20k

Output families in SAME GDB
---------------------------
<County>_Stream_5k_w_characteristics
<County>_Stream_10k_w_characteristics
<County>_Stream_20k_w_characteristics

Source behavior preserved from the executed 1208 main pipeline
---------------------------------------------------------------
1. Local drainage area from catchment polygon.
2. Cumulative drainage area from stream topology.
3. NLCD_Majority / NLCD_Type / Landuse_Imp.
4. DEM_Slope using the ORIGINAL, unprocessed HUC12 DEM.
5. Save *_w_characteristics.

NOTE:
The source file also defines process_soil_data(), but its main() does not call
that function. This refactor follows the actual executed source pipeline and
does not silently add the soil step.
"""

import os
import sys
import time
import re
import datetime
from collections import defaultdict, deque

import arcpy


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


IN_MEMORY = "in_memory"

SCALES = [
    "5k",
    "10k",
    "20k",
]

RIVER_BUFFER_NAME = (
    "OH_NHD_5m_River_buffer_500ft_w_Ohio_Maumee"
)

LAKE_BUFFER_NAME = (
    "OH_Wetlands_Lakes_Buffer_200ft"
)

NLCD_TYPE_NAME = "NLCD_type"
NLCD_RASTER_NAME = "OH_NLCD_2021"


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


def parse_threshold_tokens(text):

    if not text:
        return list(
            SCALES
        )

    values = []

    for token in text.split(
        ";"
    ):
        token = (
            token.strip()
            .strip("'")
            .strip('"')
            .lower()
        )

        if not token:
            continue

        if token.endswith(
            "000"
        ) and token[:-3].isdigit():
            token = f"{int(token[:-3])}k"

        if token not in SCALES:
            fail(
                f"Unsupported threshold '{token}'. "
                f"Allowed values: {', '.join(SCALES)}"
            )

        values.append(
            token
        )

    values = list(
        dict.fromkeys(
            values
        )
    )

    if not values:
        fail(
            "At least one threshold must be selected."
        )

    return values


# ---------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------

def load_run_context(run_folder):

    if not os.path.isdir(run_folder):
        fail(
            f"Processing Run Folder does not exist: "
            f"{run_folder}"
        )

    run_config_path = os.path.join(
        run_folder,
        "run_config.json"
    )

    if not os.path.isfile(run_config_path):
        fail(
            "Selected folder is not a valid processing run. "
            f"Missing run_config.json: {run_folder}"
        )

    run_config = load_json(run_config_path)

    county_info = run_config.get(
        "county",
        {}
    )

    county_name = str(
        county_info.get("name", "")
    ).strip().title()

    county_abbr = str(
        county_info.get("abbr", "")
    ).strip().upper()

    county_config_path = run_config.get(
        "county_config",
        ""
    )

    project_config_path = run_config.get(
        "project_config",
        ""
    )

    county_folder = run_config.get(
        "county_folder",
        ""
    )

    if not county_name or not county_abbr:
        fail(
            "run_config.json does not contain valid county identity."
        )

    if not os.path.isfile(county_config_path):
        fail(
            f"County Config referenced by run does not exist: "
            f"{county_config_path}"
        )

    if not os.path.isfile(project_config_path):
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

    if not county_folder:
        county_folder = os.path.dirname(
            os.path.dirname(
                run_folder
            )
        )

    return {
        "run_folder": run_folder,
        "run_config_path": run_config_path,
        "run_config": run_config,
        "run_id": run_config.get("run_id"),
        "county_name": county_name,
        "county_abbr": county_abbr,
        "county_folder": county_folder,
        "county": county,
        "project": project,
    }


def update_run_config(
    context,
    status,
    results,
    working_gdb
):

    run_config = context[
        "run_config"
    ]

    run_config[
        "last_updated"
    ] = (
        datetime.datetime.now()
        .strftime("%Y-%m-%d %H:%M:%S")
    )

    run_config[
        "status"
    ] = status

    processing = run_config.setdefault(
        "processing",
        {}
    )

    processing[
        "3.2"
    ] = {
        "status": status,
        "working_gdb": working_gdb,
        "results": results,
    }

    save_json(
        run_config,
        context[
            "run_config_path"
        ]
    )


# ---------------------------------------------------------------------
# Dataset / schema helpers
# ---------------------------------------------------------------------

def find_id_field(
    fc,
    candidates=(
        "grid_code",
        "gridcode",
        "gridid",
        "grid_id",
        "GridID",
    )
):
    field_map = {
        field.name.lower():
            field.name
        for field in arcpy.ListFields(
            fc
        )
    }

    for candidate in candidates:
        if candidate.lower() in field_map:
            return field_map[
                candidate.lower()
            ]

    fail(
        f"No stream/catchment identifier field found on {fc}. "
        f"Expected one of {candidates}."
    )


def ensure_grid_code_field(
    feature_class
):

    fields = arcpy.ListFields(
        feature_class
    )

    names = {
        field.name:
            field
        for field in fields
    }

    if "grid_code" in names:
        return

    source_field = None

    for field in fields:
        if field.name.lower() in {
            "gridcode",
            "gridid",
            "grid_id",
        }:
            source_field = field
            break

    if source_field is None:
        fail(
            f"No identifier field found on {feature_class}; "
            "expected grid_code, gridcode, or GridID."
        )

    try:
        arcpy.management.AlterField(
            feature_class,
            source_field.name,
            "grid_code",
            "grid_code"
        )

    except Exception:

        type_map = {
            "SmallInteger": "SHORT",
            "Integer": "LONG",
            "Double": "DOUBLE",
            "Single": "FLOAT",
            "String": "TEXT",
        }

        new_type = type_map.get(
            source_field.type,
            "LONG"
        )

        kwargs = {}

        if new_type == "TEXT":
            kwargs[
                "field_length"
            ] = 255

        arcpy.management.AddField(
            feature_class,
            "grid_code",
            new_type,
            **kwargs
        )

        arcpy.management.CalculateField(
            feature_class,
            "grid_code",
            f"!{source_field.name}!",
            "PYTHON3"
        )


def get_oid_field_name(fc):
    for field in arcpy.ListFields(
        fc
    ):
        if field.type == "OID":
            return field.name

    fail(
        f"No OID field found on {fc}"
    )


def find_fc_by_basename(
    workspace,
    target_basename
):

    old_workspace = arcpy.env.workspace

    try:
        arcpy.env.workspace = workspace

        for fc in (
            arcpy.ListFeatureClasses(
                f"{target_basename}*"
            )
            or
            []
        ):
            if (
                arcpy.Describe(
                    fc
                ).baseName.lower()
                ==
                target_basename.lower()
            ):
                return os.path.join(
                    workspace,
                    fc
                )

    finally:
        arcpy.env.workspace = (
            old_workspace
        )

    return None


# ---------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------

def configure_spatial_env(
    stream_fc,
    dem_raster
):

    stream_desc = arcpy.Describe(
        stream_fc
    )

    dem_desc = arcpy.Describe(
        dem_raster
    )

    stream_sr = stream_desc.spatialReference
    dem_sr = dem_desc.spatialReference

    arcpy.env.outputCoordinateSystem = (
        stream_sr
    )

    stream_code = getattr(
        stream_sr,
        "factoryCode",
        None
    )

    dem_code = getattr(
        dem_sr,
        "factoryCode",
        None
    )

    stream_name = getattr(
        stream_sr,
        "name",
        None
    )

    dem_name = getattr(
        dem_sr,
        "name",
        None
    )

    # Primary CRS check: factory code.
    # ArcGIS may return None for linearUnitMeters even when both datasets
    # are in the same projected CRS, so unit metadata is not used to
    # generate a mismatch warning.
    if (
        stream_code
        and
        dem_code
    ):

        if stream_code == dem_code:

            log(
                f"PASS - Stream and Original DEM CRS aligned: "
                f"WKID {stream_code}."
            )

        else:

            warn(
                f"Stream CRS and Original DEM CRS differ. "
                f"Stream WKID={stream_code}; DEM WKID={dem_code}."
            )

    else:

        # Fallback if a factory code is unavailable.
        if (
            stream_name
            and
            dem_name
            and
            stream_name == dem_name
        ):

            log(
                f"PASS - Stream and Original DEM CRS aligned: "
                f"{stream_name}."
            )

        else:

            warn(
                f"Could not confirm Stream / Original DEM CRS alignment. "
                f"Stream SR={stream_name}; DEM SR={dem_name}."
            )


# ---------------------------------------------------------------------
# 1 Drainage Area
# ---------------------------------------------------------------------

def calculate_drainage_area(
    stream_fc,
    catchment_fc
):

    temp_drainage = os.path.join(
        IN_MEMORY,
        "temp_drainage_area"
    )

    if arcpy.Exists(
        temp_drainage
    ):
        arcpy.management.Delete(
            temp_drainage
        )

    arcpy.management.CopyFeatures(
        stream_fc,
        temp_drainage
    )

    ensure_grid_code_field(
        temp_drainage
    )

    catchment_id = find_id_field(
        catchment_fc
    )

    existing = {
        field.name
        for field in arcpy.ListFields(
            temp_drainage
        )
    }

    for name in [
        "Drainage_Area",
        "Drainage_Ar_Acres",
    ]:
        if name not in existing:
            arcpy.management.AddField(
                temp_drainage,
                name,
                "DOUBLE"
            )

    sr = arcpy.Describe(
        catchment_fc
    ).spatialReference

    meters_per_unit = getattr(
        sr,
        "linearUnitMeters",
        None
    )

    def to_acres(area):
        if meters_per_unit:
            return (
                area
                *
                meters_per_unit
                *
                meters_per_unit
                /
                4046.8564224
            )

        return area / 43560.0

    area_lookup = {}

    with arcpy.da.SearchCursor(
        catchment_fc,
        [
            catchment_id,
            "SHAPE@AREA",
        ]
    ) as cursor:

        for gid, area in cursor:
            area_lookup[
                gid
            ] = (
                area,
                to_acres(
                    area
                ),
            )

    updated = 0
    missing = 0

    with arcpy.da.UpdateCursor(
        temp_drainage,
        [
            "grid_code",
            "Drainage_Area",
            "Drainage_Ar_Acres",
        ]
    ) as cursor:

        for row in cursor:

            gid = row[0]

            if gid in area_lookup:
                row[1] = area_lookup[
                    gid
                ][0]

                row[2] = area_lookup[
                    gid
                ][1]

                updated += 1

            else:
                row[1] = None
                row[2] = None
                missing += 1

            cursor.updateRow(
                row
            )

    log(
        f"Drainage area populated: "
        f"updated={updated:,}; missing={missing:,}."
    )

    return temp_drainage


# ---------------------------------------------------------------------
# 2 Cumulative Drainage Area
# ---------------------------------------------------------------------

def calculate_cda(
    stream_input
):

    temp_cda = os.path.join(
        IN_MEMORY,
        "temp_cda"
    )

    if arcpy.Exists(
        temp_cda
    ):
        arcpy.management.Delete(
            temp_cda
        )

    arcpy.management.CopyFeatures(
        stream_input,
        temp_cda
    )

    existing = {
        field.name
        for field in arcpy.ListFields(
            temp_cda
        )
    }

    for name in [
        "CDA_Acre",
        "CDA_SqMi",
    ]:
        if name not in existing:
            arcpy.management.AddField(
                temp_cda,
                name,
                "DOUBLE"
            )

    required = {
        field.name
        for field in arcpy.ListFields(
            temp_cda
        )
    }

    for field in [
        "from_node",
        "to_node",
        "Drainage_Ar_Acres",
    ]:
        if field not in required:
            fail(
                f"Required field '{field}' missing from "
                f"{stream_input}."
            )

    drainage_area = defaultdict(
        float
    )

    downstream = defaultdict(
        list
    )

    in_degree = defaultdict(
        int
    )

    with arcpy.da.SearchCursor(
        temp_cda,
        [
            "from_node",
            "to_node",
            "Drainage_Ar_Acres",
        ]
    ) as cursor:

        for from_node, to_node, acres in cursor:

            drainage_area[
                from_node
            ] += (
                float(acres)
                if acres is not None
                else 0.0
            )

            downstream[
                from_node
            ].append(
                to_node
            )

            in_degree[
                to_node
            ] += 1

            if to_node not in drainage_area:
                drainage_area[
                    to_node
                ] = 0.0

    cda_values = dict(
        drainage_area
    )

    queue = deque(
        node
        for node in drainage_area
        if in_degree[
            node
        ] == 0
    )

    while queue:

        node = queue.popleft()

        for downstream_node in downstream[
            node
        ]:

            cda_values[
                downstream_node
            ] = (
                cda_values.get(
                    downstream_node,
                    0.0
                )
                +
                cda_values.get(
                    node,
                    0.0
                )
            )

            in_degree[
                downstream_node
            ] -= 1

            if in_degree[
                downstream_node
            ] == 0:
                queue.append(
                    downstream_node
                )

    with arcpy.da.UpdateCursor(
        temp_cda,
        [
            "from_node",
            "CDA_Acre",
            "CDA_SqMi",
        ]
    ) as cursor:

        for row in cursor:

            acres = cda_values.get(
                row[0],
                0.0
            )

            row[1] = acres
            row[2] = (
                acres
                *
                0.0015625
            )

            cursor.updateRow(
                row
            )

    return temp_cda


# ---------------------------------------------------------------------
# 3 NLCD / Landuse
# ---------------------------------------------------------------------

def calculate_nlcd_fields(
    original_streams,
    county_boundary,
    river_buffer,
    lake_buffer,
    nlcd_raster,
    nlcd_type_table,
    scratch_gdb
):

    old_snap = arcpy.env.snapRaster
    old_cell = arcpy.env.cellSize
    old_parallel = None

    try:
        old_parallel = arcpy.env.parallelProcessingFactor
    except Exception:
        pass

    try:

        arcpy.env.snapRaster = (
            nlcd_raster
        )

        arcpy.env.cellSize = (
            nlcd_raster
        )

        try:
            arcpy.env.parallelProcessingFactor = (
                "75%"
            )
        except Exception:
            pass

        def make(name):
            return os.path.join(
                scratch_gdb,
                name
            )

        merged_buffer = make(
            "char_water_buffers_merged"
        )

        combined_buffer = make(
            "char_combined_buffer"
        )

        temp_clipped = make(
            "char_streams_clip"
        )

        overlapped = make(
            "char_streams_overlap_water"
        )

        stream_remaining = make(
            "char_streams_remaining"
        )

        remain_diss = make(
            "char_streams_remaining_diss"
        )

        zone_raster = make(
            "char_nlcd_zone"
        )

        zonal_table = make(
            "char_nlcd_zonal_stats"
        )

        for path in [
            merged_buffer,
            combined_buffer,
            temp_clipped,
            overlapped,
            stream_remaining,
            remain_diss,
            zone_raster,
            zonal_table,
        ]:
            if arcpy.Exists(
                path
            ):
                arcpy.management.Delete(
                    path
                )

        arcpy.management.Merge(
            [
                river_buffer,
                lake_buffer,
            ],
            merged_buffer
        )

        try:
            arcpy.analysis.PairwiseDissolve(
                merged_buffer,
                combined_buffer
            )
        except Exception:
            arcpy.management.Dissolve(
                merged_buffer,
                combined_buffer
            )

        arcpy.analysis.Clip(
            original_streams,
            county_boundary,
            temp_clipped
        )

        if int(
            arcpy.management.GetCount(
                temp_clipped
            )[0]
        ) == 0:
            fail(
                "NLCD county clip produced 0 stream features."
            )

        ensure_grid_code_field(
            temp_clipped
        )

        if "OrigLength" not in {
            f.name
            for f in arcpy.ListFields(
                temp_clipped
            )
        }:
            arcpy.management.AddField(
                temp_clipped,
                "OrigLength",
                "DOUBLE"
            )

        with arcpy.da.UpdateCursor(
            temp_clipped,
            [
                "OrigLength",
                "SHAPE@",
            ]
        ) as cursor:

            for row in cursor:
                row[0] = (
                    row[1].length
                    if row[1]
                    else 0.0
                )
                cursor.updateRow(
                    row
                )

        arcpy.analysis.Intersect(
            [
                temp_clipped,
                combined_buffer,
            ],
            overlapped,
            output_type="LINE"
        )

        overlap_lengths = defaultdict(
            float
        )

        if int(
            arcpy.management.GetCount(
                overlapped
            )[0]
        ) > 0:

            overlap_grid = find_id_field(
                overlapped
            )

            with arcpy.da.SearchCursor(
                overlapped,
                [
                    overlap_grid,
                    "SHAPE@LENGTH",
                ]
            ) as cursor:

                for gid, length in cursor:
                    overlap_lengths[
                        gid
                    ] += (
                        length
                        or
                        0.0
                    )

        original_lengths = {
            gid: (
                length
                or
                0.0
            )
            for gid, length
            in arcpy.da.SearchCursor(
                temp_clipped,
                [
                    "grid_code",
                    "OrigLength",
                ]
            )
        }

        existing = {
            f.name
            for f in arcpy.ListFields(
                temp_clipped
            )
        }

        if "Landuse_Imp" not in existing:
            arcpy.management.AddField(
                temp_clipped,
                "Landuse_Imp",
                "TEXT",
                field_length=100
            )

        with arcpy.da.UpdateCursor(
            temp_clipped,
            [
                "grid_code",
                "Landuse_Imp",
            ]
        ) as cursor:

            for row in cursor:

                gid = row[0]

                total = original_lengths.get(
                    gid,
                    0.0
                )

                overlap = overlap_lengths.get(
                    gid,
                    0.0
                )

                if total <= 0:
                    label = (
                        "Outside the Lakes/Ohio River"
                    )

                else:
                    ratio = (
                        overlap
                        /
                        total
                    )

                    if ratio >= 0.999:
                        label = (
                            "100% inside Lakes/Ohio River"
                        )

                    elif ratio >= 0.75:
                        label = (
                            "Over 75% inside Lakes/Ohio River"
                        )

                    elif ratio > 0:
                        label = (
                            "Less than 75% inside Lakes/Ohio River"
                        )

                    else:
                        label = (
                            "Outside the Lakes/Ohio River"
                        )

                row[1] = label

                cursor.updateRow(
                    row
                )

        arcpy.analysis.Erase(
            temp_clipped,
            combined_buffer,
            stream_remaining
        )

        arcpy.management.Dissolve(
            stream_remaining,
            remain_diss,
            [
                "grid_code"
            ]
        )

        nlcd_lookup = {}

        if int(
            arcpy.management.GetCount(
                remain_diss
            )[0]
        ) > 0:

            cell = arcpy.Describe(
                nlcd_raster
            ).meanCellWidth

            arcpy.conversion.PolylineToRaster(
                in_features=remain_diss,
                value_field="grid_code",
                out_rasterdataset=zone_raster,
                cell_assignment="MAXIMUM_LENGTH",
                priority_field="",
                cellsize=cell
            )

            arcpy.sa.ZonalStatisticsAsTable(
                in_zone_data=zone_raster,
                zone_field="Value",
                in_value_raster=nlcd_raster,
                out_table=zonal_table,
                ignore_nodata="DATA",
                statistics_type="MAJORITY"
            )

            with arcpy.da.SearchCursor(
                zonal_table,
                [
                    "VALUE",
                    "MAJORITY",
                ]
            ) as cursor:

                for gid, majority in cursor:
                    nlcd_lookup[
                        gid
                    ] = (
                        str(
                            majority
                        )
                        if majority is not None
                        else "11"
                    )

        existing = {
            f.name
            for f in arcpy.ListFields(
                temp_clipped
            )
        }

        if "NLCD_Majority" not in existing:
            arcpy.management.AddField(
                temp_clipped,
                "NLCD_Majority",
                "TEXT",
                field_length=100
            )

        if "NLCD_Type" not in existing:
            arcpy.management.AddField(
                temp_clipped,
                "NLCD_Type",
                "TEXT",
                field_length=100
            )

        with arcpy.da.UpdateCursor(
            temp_clipped,
            [
                "grid_code",
                "NLCD_Majority",
            ]
        ) as cursor:

            for row in cursor:
                row[1] = nlcd_lookup.get(
                    row[0],
                    "11"
                )
                cursor.updateRow(
                    row
                )

        nlcd_type_dict = {
            str(code):
                land_type
            for code, land_type
            in arcpy.da.SearchCursor(
                nlcd_type_table,
                [
                    "Code",
                    "Land_Use_Type",
                ]
            )
        }

        with arcpy.da.UpdateCursor(
            temp_clipped,
            [
                "NLCD_Majority",
                "NLCD_Type",
            ]
        ) as cursor:

            for row in cursor:
                row[1] = nlcd_type_dict.get(
                    str(
                        row[0]
                    ),
                    "Unknown"
                )
                cursor.updateRow(
                    row
                )

        return temp_clipped

    finally:

        arcpy.env.snapRaster = (
            old_snap
        )

        arcpy.env.cellSize = (
            old_cell
        )

        if old_parallel is not None:
            try:
                arcpy.env.parallelProcessingFactor = (
                    old_parallel
                )
            except Exception:
                pass


# ---------------------------------------------------------------------
# 4 DEM Slope
# ---------------------------------------------------------------------

def calculate_dem_slope(
    input_fc,
    original_dem,
    river_buffer,
    lake_buffer,
    run_logs_folder,
    scale
):

    timestamp = datetime.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    oid_field = get_oid_field_name(
        input_fc
    )

    existing = {
        f.name
        for f in arcpy.ListFields(
            input_fc
        )
    }

    if "SourceOID" not in existing:
        arcpy.management.AddField(
            input_fc,
            "SourceOID",
            "LONG"
        )

        with arcpy.da.UpdateCursor(
            input_fc,
            [
                "SourceOID",
                oid_field,
            ]
        ) as cursor:

            for row in cursor:
                row[0] = row[1]
                cursor.updateRow(
                    row
                )

    combined_buffer = os.path.join(
        IN_MEMORY,
        f"char_dem_buffer_{timestamp}"
    )

    stream_trimmed = os.path.join(
        IN_MEMORY,
        f"char_dem_trimmed_{timestamp}"
    )

    output_fc = os.path.join(
        IN_MEMORY,
        f"char_slope_output_{timestamp}"
    )

    start_pts = os.path.join(
        IN_MEMORY,
        f"char_start_pts_{timestamp}"
    )

    end_pts = os.path.join(
        IN_MEMORY,
        f"char_end_pts_{timestamp}"
    )

    arcpy.analysis.Union(
        [
            river_buffer,
            lake_buffer,
        ],
        combined_buffer
    )

    arcpy.analysis.Erase(
        input_fc,
        combined_buffer,
        stream_trimmed
    )

    arcpy.management.CopyFeatures(
        input_fc,
        output_fc
    )

    if "DEM_Slope" not in {
        f.name
        for f in arcpy.ListFields(
            output_fc
        )
    }:
        arcpy.management.AddField(
            output_fc,
            "DEM_Slope",
            "DOUBLE"
        )

    arcpy.management.FeatureVerticesToPoints(
        stream_trimmed,
        start_pts,
        "START"
    )

    arcpy.management.FeatureVerticesToPoints(
        stream_trimmed,
        end_pts,
        "END"
    )

    arcpy.ddd.AddSurfaceInformation(
        start_pts,
        original_dem,
        "Z",
        "BILINEAR"
    )

    arcpy.ddd.AddSurfaceInformation(
        end_pts,
        original_dem,
        "Z",
        "BILINEAR"
    )

    start_dict = {
        source_oid:
            float(z)
        for source_oid, z
        in arcpy.da.SearchCursor(
            start_pts,
            [
                "SourceOID",
                "Z",
            ]
        )
        if z is not None
    }

    end_dict = {
        source_oid:
            float(z)
        for source_oid, z
        in arcpy.da.SearchCursor(
            end_pts,
            [
                "SourceOID",
                "Z",
            ]
        )
        if z is not None
    }

    length_dict = {
        source_oid:
            float(length)
        for source_oid, length
        in arcpy.da.SearchCursor(
            stream_trimmed,
            [
                "SourceOID",
                "SHAPE@LENGTH",
            ]
        )
        if length is not None
        and
        length > 0
    }

    ensure_folder(
        run_logs_folder
    )

    all_log = os.path.join(
        run_logs_folder,
        f"slope_log_{scale}_{timestamp}.csv"
    )

    missing_log = os.path.join(
        run_logs_folder,
        f"slope_missing_{scale}_{timestamp}.csv"
    )

    missing_rows = []

    with open(
        all_log,
        "w",
        encoding="utf-8"
    ) as handle:

        handle.write(
            "SourceOID,grid_code,from_node,to_node,"
            "Start_Elev,End_Elev,DEM_Slope\n"
        )

        with arcpy.da.UpdateCursor(
            output_fc,
            [
                "SourceOID",
                "grid_code",
                "from_node",
                "to_node",
                "DEM_Slope",
            ]
        ) as cursor:

            for row in cursor:

                source_oid = row[0]

                z1 = start_dict.get(
                    source_oid
                )

                z2 = end_dict.get(
                    source_oid
                )

                length = length_dict.get(
                    source_oid
                )

                if (
                    z1 is not None
                    and
                    z2 is not None
                    and
                    length is not None
                    and
                    length > 0
                ):
                    slope = abs(
                        (
                            z1
                            -
                            z2
                        )
                        /
                        length
                        *
                        100.0
                    )

                    row[4] = slope

                    handle.write(
                        f"{row[0]},{row[1]},{row[2]},{row[3]},"
                        f"{z1:.2f},{z2:.2f},{slope:.2f}\n"
                    )

                else:
                    # Preserve missing DEM slope as NULL.
                    # Do not convert missing elevation sampling to 0.0,
                    # because downstream JD classification would treat
                    # 0.0 as a real 0-1% slope.
                    row[4] = None

                    missing_rows.append(
                        f"{row[0]},{row[1]},{row[2]},{row[3]},,,\n"
                    )

                cursor.updateRow(
                    row
                )

    if missing_rows:

        with open(
            missing_log,
            "w",
            encoding="utf-8"
        ) as handle:

            handle.write(
                "SourceOID,grid_code,from_node,to_node,"
                "Start_Elev,End_Elev\n"
            )

            handle.writelines(
                missing_rows
            )

        warn(
            f"{scale}: DEM slope missing for "
            f"{len(missing_rows):,} features. "
            f"DEM_Slope remains NULL for these features. "
            f"See {missing_log}"
        )

    return output_fc


# ---------------------------------------------------------------------
# One threshold
# ---------------------------------------------------------------------

def process_pair(
    scale,
    stream_fc,
    catchment_fc,
    output_fc,
    context,
    source_data,
    scratch_gdb
):

    try:
        arcpy.management.Delete(
            IN_MEMORY
        )
    except Exception:
        pass

    configure_spatial_env(
        stream_fc,
        source_data[
            "original_dem"
        ]
    )

    initial_count = int(
        arcpy.management.GetCount(
            stream_fc
        )[0]
    )

    log_step_start(
        f"{scale} - Drainage Area"
    )

    temp_drainage = calculate_drainage_area(
        stream_fc,
        catchment_fc
    )

    log_step_end(
        f"{scale} - Drainage Area"
    )


    log_step_start(
        f"{scale} - Cumulative Drainage Area"
    )

    temp_cda = calculate_cda(
        temp_drainage
    )

    log_step_end(
        f"{scale} - Cumulative Drainage Area"
    )


    log_step_start(
        f"{scale} - NLCD Characteristics"
    )

    temp_nlcd = calculate_nlcd_fields(
        original_streams=temp_cda,
        county_boundary=source_data[
            "county_boundary"
        ],
        river_buffer=source_data[
            "river_buffer"
        ],
        lake_buffer=source_data[
            "lake_buffer"
        ],
        nlcd_raster=source_data[
            "nlcd_raster"
        ],
        nlcd_type_table=source_data[
            "nlcd_type"
        ],
        scratch_gdb=scratch_gdb
    )

    log_step_end(
        f"{scale} - NLCD Characteristics"
    )


    log_step_start(
        f"{scale} - DEM Slope"
    )

    temp_slope = calculate_dem_slope(
        input_fc=temp_nlcd,
        original_dem=source_data[
            "original_dem"
        ],
        river_buffer=source_data[
            "river_buffer"
        ],
        lake_buffer=source_data[
            "lake_buffer"
        ],
        run_logs_folder=os.path.join(
            context[
                "run_folder"
            ],
            "logs"
        ),
        scale=scale
    )

    log_step_end(
        f"{scale} - DEM Slope"
    )


    if arcpy.Exists(
        output_fc
    ):
        arcpy.management.Delete(
            output_fc
        )

    log_step_start(
        f"{scale} - Save Characteristics"
    )

    arcpy.management.CopyFeatures(
        temp_slope,
        output_fc
    )

    log_step_end(
        f"{scale} - Save Characteristics"
    )

    final_count = int(
        arcpy.management.GetCount(
            output_fc
        )[0]
    )

    if final_count <= 0:
        fail(
            f"{scale}: characteristics output "
            f"contains no features."
        )

    return {
        "status": "PASS",
        "stream_input": stream_fc,
        "catchment_input": catchment_fc,
        "output": output_fc,
        "initial_count": initial_count,
        "final_count": final_count,
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    run_folder = p(
        0
    )

    working_gdb = p(
        1
    )

    original_dem = p(
        2
    )

    nlcd_raster = p(
        3
    )

    county_boundary = p(
        4
    )

    threshold_text = p(
        5
    )

    overwrite = get_bool(
        6,
        False
    )

    if not run_folder:
        fail(
            "Processing Run Folder is required."
        )

    if not working_gdb:
        fail(
            "Streams Working GDB is required."
        )

    if not original_dem:
        fail(
            "Original HUC12 DEM is required."
        )

    if not nlcd_raster:
        fail(
            "NLCD Raster is required."
        )

    if not county_boundary:
        fail(
            "County Boundary is required."
        )

    thresholds_to_process = parse_threshold_tokens(
        threshold_text
    )

    context = load_run_context(
        run_folder
    )

    county_name = context[
        "county_name"
    ]

    county_abbr = context[
        "county_abbr"
    ]

    init_tool_log(
        county_folder=run_folder,
        tool_id="3.2",
        tool_name="Generate_Stream_Characteristics",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder":
                run_folder,

            "Run ID":
                context[
                    "run_id"
                ],

            "Streams Working GDB":
                working_gdb,

            "Original HUC12 DEM":
                original_dem,

            "NLCD Raster":
                nlcd_raster,

            "County Boundary":
                county_boundary,

            "Thresholds":
                thresholds_to_process,
        }
    )

    log("=" * 72)
    log("3.2 Generate Stream Characteristics")
    log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )
    log("=" * 72)

    expected_working_gdb = os.path.join(
        run_folder,
        f"{county_name}_Streams_Working.gdb"
    )

    if os.path.normcase(
        os.path.normpath(
            working_gdb
        )
    ) != os.path.normcase(
        os.path.normpath(
            expected_working_gdb
        )
    ):
        warn(
            "Streams Working GDB overrides the default GDB "
            "resolved from this Processing Run."
        )

    if not arcpy.Exists(
        working_gdb
    ):

        warn(
            "Step 3.3 was not run because the Streams Working GDB "
            "does not exist in this run."
        )

        update_run_config(
            context,
            "SKIPPED_STREAMS_GDB_NOT_READY",
            {},
            working_gdb
        )

        finish_tool_log(
            "SKIPPED_STREAMS_GDB_NOT_READY"
        )

        arcpy.SetParameterAsText(
            8,
            "SKIPPED_STREAMS_GDB_NOT_READY"
        )

        return

    # -------------------------------------------------------------
    # Visible operational source inputs + internally resolved references
    #
    # User-visible operational inputs:
    #   - Original HUC12 DEM
    #   - NLCD Raster
    #   - County Boundary
    #
    # Internal shared references remain config-driven:
    #   - NLCD_type
    #   - Major River Buffer
    #   - Lake / Wetland Buffer
    # -------------------------------------------------------------

    project = context[
        "project"
    ]

    tools_source = project.get(
        "tools_source",
        {}
    )

    nlcd_type = tools_source.get(
        NLCD_TYPE_NAME
    )

    river_buffer = tools_source.get(
        RIVER_BUFFER_NAME
    )

    lake_buffer = tools_source.get(
        LAKE_BUFFER_NAME
    )

    internal_sources = {
        "NLCD Lookup Table": nlcd_type,
        "Major River Buffer": river_buffer,
        "Lake / Wetland Buffer": lake_buffer,
    }

    for label, path_value in internal_sources.items():

        if not path_value:
            fail(
                f"Project Config does not define required internal "
                f"characteristics source: {label}."
            )

        if not arcpy.Exists(
            path_value
        ):
            fail(
                f"Required internal characteristics source is missing "
                f"({label}): {path_value}"
            )

        log(
            f"PASS - Internal {label}: "
            f"{path_value}"
        )

    source_data = {
        "original_dem": original_dem,
        "county_boundary": county_boundary,
        "river_buffer": river_buffer,
        "lake_buffer": lake_buffer,
        "nlcd_raster": nlcd_raster,
        "nlcd_type": nlcd_type,
    }

    # Expected visible defaults for transparency / override warnings.
    expected_original_dem = (
        context[
            "run_config"
        ]
        .get(
            "source_inputs",
            {}
        )
        .get(
            "huc12_dem"
        )
        or os.path.join(
            context[
                "county_folder"
            ],
            "raster",
            f"{county_abbr}_DEM_HUC12.tif"
        )
    )

    expected_nlcd = tools_source.get(
        NLCD_RASTER_NAME
    )

    county_boundary_root = tools_source.get(
        "OH_County_Boundary"
    )

    expected_county_boundary = (
        os.path.join(
            county_boundary_root,
            county_abbr
        )
        if county_boundary_root
        else None
    )

    visible_sources = {
        "Original HUC12 DEM": (
            original_dem,
            expected_original_dem
        ),

        "NLCD Raster": (
            nlcd_raster,
            expected_nlcd
        ),

        "County Boundary": (
            county_boundary,
            expected_county_boundary
        ),
    }

    for label, pair in visible_sources.items():

        selected_path, expected_path = pair

        if not arcpy.Exists(
            selected_path
        ):
            fail(
                f"Required characteristics source is missing "
                f"({label}): {selected_path}"
            )

        if (
            expected_path
            and
            os.path.normcase(
                os.path.normpath(
                    selected_path
                )
            )
            !=
            os.path.normcase(
                os.path.normpath(
                    expected_path
                )
            )
        ):
            warn(
                f"{label} overrides the default source "
                "resolved from the Processing Run / Project Config."
            )

        log(
            f"PASS - {label}: "
            f"{selected_path}"
        )

    # -------------------------------------------------------------
    # Scratch
    # -------------------------------------------------------------

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

    # -------------------------------------------------------------
    # License / environment
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

    old_workspace = arcpy.env.workspace
    old_scratch = arcpy.env.scratchWorkspace
    old_overwrite = arcpy.env.overwriteOutput

    try:

        arcpy.env.workspace = (
            working_gdb
        )

        arcpy.env.scratchWorkspace = (
            scratch_gdb
        )

        arcpy.env.overwriteOutput = (
            True
        )

        # ---------------------------------------------------------
        # Discover 3.1 stream/catchment pairs
        # ---------------------------------------------------------

        pairs = []

        for scale in thresholds_to_process:

            stream_base = (
                f"{county_name}_Stream_{scale}"
            )

            catchment_base = (
                f"{county_name}_Catchment_{scale}"
            )

            stream_fc = find_fc_by_basename(
                working_gdb,
                stream_base
            )

            catchment_fc = find_fc_by_basename(
                working_gdb,
                catchment_base
            )

            if (
                stream_fc
                and
                catchment_fc
            ):
                pairs.append(
                    (
                        scale,
                        stream_fc,
                        catchment_fc,
                    )
                )

            else:
                if not stream_fc:
                    warn(
                        f"{scale}: missing stream input "
                        f"{stream_base}."
                    )

                if not catchment_fc:
                    warn(
                        f"{scale}: missing catchment input "
                        f"{catchment_base}."
                    )

        if not pairs:

            warn(
                "No complete Stream/Catchment threshold families "
                "were found. Run Step 3.1 first."
            )

            update_run_config(
                context,
                "SKIPPED_STREAM_CATCHMENT_NOT_READY",
                {},
                working_gdb
            )

            finish_tool_log(
                "SKIPPED_STREAM_CATCHMENT_NOT_READY"
            )

            arcpy.SetParameterAsText(
                7,
                working_gdb
            )

            arcpy.SetParameterAsText(
                8,
                "SKIPPED_STREAM_CATCHMENT_NOT_READY"
            )

            return

        # ---------------------------------------------------------
        # Process each available threshold independently
        # ---------------------------------------------------------

        results = {}

        any_pass = False
        any_skip = False

        for (
            scale,
            stream_fc,
            catchment_fc
        ) in pairs:

            output_fc = os.path.join(
                working_gdb,
                (
                    f"{county_name}_Stream_"
                    f"{scale}_w_characteristics"
                )
            )

            if (
                arcpy.Exists(
                    output_fc
                )
                and
                not overwrite
            ):

                count = int(
                    arcpy.management.GetCount(
                        output_fc
                    )[0]
                )

                if count > 0:

                    log(
                        f"{scale}: existing characteristics output "
                        f"is valid; reusing."
                    )

                    results[
                        scale
                    ] = {
                        "status":
                            "SKIPPED_EXISTING_VALID",

                        "output":
                            output_fc,

                        "feature_count":
                            count,
                    }

                    any_pass = True

                    continue

                warn(
                    f"{scale}: existing characteristics output "
                    "contains no features."
                )

                results[
                    scale
                ] = {
                    "status":
                        "SKIPPED_INVALID_EXISTING",

                    "output":
                        output_fc,
                }

                any_skip = True

                continue

            try:

                log(
                    f"{scale}: starting characteristics processing."
                )

                result = process_pair(
                    scale=scale,
                    stream_fc=stream_fc,
                    catchment_fc=catchment_fc,
                    output_fc=output_fc,
                    context=context,
                    source_data=source_data,
                    scratch_gdb=scratch_gdb
                )

                log(
                    f"{scale}: characteristics processing completed."
                )

                results[
                    scale
                ] = result

                any_pass = True

            except Exception as exc:

                warn(
                    f"{scale}: characteristics processing failed: "
                    f"{exc}"
                )

                results[
                    scale
                ] = {
                    "status":
                        "FAILED",

                    "error":
                        str(
                            exc
                        ),
                }

                any_skip = True

                # Preserve source batch behavior:
                # one scale failure does not prevent another scale.
                continue

        # ---------------------------------------------------------
        # Status
        # ---------------------------------------------------------

        if any_pass and any_skip:
            status = "PARTIAL"

        elif any_pass:
            status = "PASS"

        else:
            status = "FAILED"

        update_run_config(
            context,
            status,
            results,
            working_gdb
        )

        finish_tool_log(
            status
        )

        arcpy.SetParameterAsText(
            7,
            working_gdb
        )

        arcpy.SetParameterAsText(
            8,
            status
        )

    finally:

        arcpy.env.workspace = (
            old_workspace
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
                8,
                "FAIL"
            )
        except Exception:
            pass

        raise