# -*- coding: utf-8 -*-
"""
3.4 Package Final Deliverables
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Refactored from:
    Package-Final-Deliverables-toolbox.py

Purpose
-------
Package one EXPLICIT Processing Run into the standardized county deliverable
structure.

The user selects the Processing Run Folder. The tool then resolves:

Original DEM
    <County Folder>\raster\<ABBR>_DEM_HUC12.tif

Processed DEM
    <Processing Run Folder>\<ABBR>_DEM_HUC12_AGREE.tif

Streams Working GDB
    <Processing Run Folder>\<County>_Streams_Working.gdb

Final Deliverable Root
    project_config.json > sources > final_deliverable_root

Final structure
---------------
<Final Deliverable Root>\<County>\
    <County>_DEM.gdb
        <County>_DEM_HUC12
        <County>_DEM_HUC12_Processed

    <County>_Streams.gdb
        <County>_Stream_5k
        <County>_Catchment_5k
        <County>_Stream_5k_w_characteristics
        <County>_Stream_5k_w_characteristics_JD

        <County>_Stream_10k
        <County>_Catchment_10k
        <County>_Stream_10k_w_characteristics
        <County>_Stream_10k_w_characteristics_JD

        <County>_Stream_20k
        <County>_Catchment_20k
        <County>_Stream_20k_w_characteristics
        <County>_Stream_20k_w_characteristics_JD

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder              Folder          Required Input
1  Original HUC12 DEM                 Raster Dataset  Required Input
2  Processed / Reconditioned DEM      Raster Dataset  Required Input
3  Streams Working GDB                Workspace       Required Input
4  Final Deliverable Root             Folder          Required Input
5  Thresholds to Package              String          Required Input, MultiValue
6  Include Base Streams/Catchments    Boolean         Optional Input
7  Include Characteristics            Boolean         Optional Input
8  Include JD Confidence              Boolean         Optional Input
9  Overwrite Existing DEMs            Boolean         Optional Input
10 Overwrite Existing Families        Boolean         Optional Input
11 Preview Only                       Boolean         Optional Input
12 Final County Folder                Folder          Derived Output
13 Final DEM GDB                      Workspace       Derived Output
14 Final Streams GDB                  Workspace       Derived Output
15 Packaging Status                   String          Derived Output



Notes
-----
- No machine path is hard-coded.
- County and projection come from run/county config.
- Final root comes from Step 0 project config.
- Logs and packaging action CSV remain in the selected Processing Run Folder.
"""

import os
import re
import sys
import csv
import datetime
from collections import defaultdict

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


EXPECTED_WKID = {
    "N": 6549,
    "S": 6551,
}

THRESHOLD_PAT = re.compile(
    r"(\d+k)(?:_|$)",
    re.IGNORECASE
)

EXPECTED_TOKENS = [
    "5k",
    "10k",
    "20k",
]


# ---------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------

def p(index):

    value = arcpy.GetParameterAsText(
        index
    )

    return value.strip() if value else ""


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


def normalize_threshold_token(
    token
):

    token = (
        str(
            token
        )
        .strip()
        .strip("'")
        .strip('"')
        .lower()
    )

    if not token:
        return None

    # Allow either 5000 or 5k style input.
    if token.endswith(
        "k"
    ):

        numeric = token[
            :-1
        ]

        try:

            value = float(
                numeric
            )

        except Exception:

            fail(
                f"Invalid threshold token: {token}"
            )

        if value <= 0:

            fail(
                f"Threshold must be positive: {token}"
            )

        if value.is_integer():

            return f"{int(value)}k"

        return f"{value:g}k"


    try:

        numeric_value = float(
            token
        )

    except Exception:

        fail(
            f"Invalid threshold token: {token}"
        )

    if numeric_value <= 0:

        fail(
            f"Threshold must be positive: {token}"
        )

    if numeric_value >= 1000:

        k_value = (
            numeric_value
            /
            1000.0
        )

        if k_value.is_integer():

            return f"{int(k_value)}k"

        return f"{k_value:g}k"


    # If user enters a small bare number such as "5", interpret as 5k
    # because stream product naming uses k-units.
    if numeric_value.is_integer():

        return f"{int(numeric_value)}k"

    return f"{numeric_value:g}k"


def parse_threshold_tokens(
    text
):

    if not text:

        fail(
            "Thresholds to Package is required."
        )

    values = []

    for token in text.split(
        ";"
    ):

        normalized = normalize_threshold_token(
            token
        )

        if normalized:

            values.append(
                normalized
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


    county_folder = run_config.get(
        "county_folder",
        ""
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


    if not county_folder:

        county_folder = os.path.dirname(
            os.path.dirname(
                run_folder
            )
        )


    return {
        "run_folder":
            run_folder,

        "run_config_path":
            run_config_path,

        "run_config":
            run_config,

        "run_id":
            run_config.get(
                "run_id"
            ),

        "county_name":
            county_name,

        "county_abbr":
            county_abbr,

        "county_folder":
            county_folder,

        "county":
            county,

        "project":
            project,
    }


def update_run_config(
    context,
    status,
    final_county_folder,
    dem_gdb,
    streams_gdb,
    summary,
    preview
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


    processing = run_config.setdefault(
        "processing",
        {}
    )


    processing[
        "3.4"
    ] = {
        "status":
            status,

        "preview_only":
            preview,

        "final_county_folder":
            final_county_folder,

        "final_dem_gdb":
            dem_gdb,

        "final_streams_gdb":
            streams_gdb,

        "summary":
            summary,
    }


    if not preview:

        run_config[
            "status"
        ] = status


        outputs = run_config.setdefault(
            "final_deliverables",
            {}
        )


        outputs[
            "county_folder"
        ] = final_county_folder


        outputs[
            "dem_gdb"
        ] = dem_gdb


        outputs[
            "streams_gdb"
        ] = streams_gdb


    save_json(
        run_config,
        context[
            "run_config_path"
        ]
    )


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------

def ensure_gdb(
    gdb_path,
    preview=False
):

    if arcpy.Exists(
        gdb_path
    ):
        return


    if preview:

        log(
            f"[Preview] Would create GDB: "
            f"{gdb_path}"
        )

        return


    parent = os.path.dirname(
        gdb_path
    )


    ensure_folder(
        parent
    )


    arcpy.management.CreateFileGDB(
        parent,
        os.path.basename(
            gdb_path
        )
    )


def feature_count_safe(
    fc
):

    try:

        return int(
            arcpy.management.GetCount(
                fc
            )[0]
        )

    except Exception:

        return None


def get_sr_info(
    dataset
):

    try:

        sr = arcpy.Describe(
            dataset
        ).spatialReference


        return (
            getattr(
                sr,
                "factoryCode",
                None
            ),
            getattr(
                sr,
                "name",
                None
            ),
        )


    except Exception:

        return (
            None,
            None
        )


def raster_extent_tuple(
    raster_path
):

    extent = arcpy.Describe(
        raster_path
    ).extent


    return (
        round(
            extent.XMin,
            6
        ),
        round(
            extent.YMin,
            6
        ),
        round(
            extent.XMax,
            6
        ),
        round(
            extent.YMax,
            6
        ),
    )


def raster_resolution_tuple(
    raster_path
):

    raster = arcpy.Raster(
        raster_path
    )


    return (
        round(
            float(
                raster.meanCellWidth
            ),
            6
        ),
        round(
            float(
                raster.meanCellHeight
            ),
            6
        ),
    )


# ---------------------------------------------------------------------
# Action CSV
# ---------------------------------------------------------------------

def write_action_csv(
    run_folder,
    county_name,
    action_rows
):

    if not action_rows:

        return None


    logs_folder = os.path.join(
        run_folder,
        "logs"
    )


    ensure_folder(
        logs_folder
    )


    timestamp = (
        datetime.datetime.now()
        .strftime(
            "%Y%m%d_%H%M%S"
        )
    )


    csv_path = os.path.join(
        logs_folder,
        (
            f"{county_name}_"
            f"packaging_actions_"
            f"{timestamp}.csv"
        )
    )


    fieldnames = [
        "timestamp",
        "section",
        "token",
        "family_type",
        "source",
        "target",
        "action",
    ]


    with open(
        csv_path,
        "w",
        newline="",
        encoding="utf-8"
    ) as handle:

        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames
        )


        writer.writeheader()


        writer.writerows(
            action_rows
        )


    return csv_path


def record_action(
    rows,
    section,
    source,
    target,
    action,
    token="",
    family_type=""
):

    rows.append(
        {
            "timestamp":
                datetime.datetime.now()
                .strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

            "section":
                section,

            "token":
                token,

            "family_type":
                family_type,

            "source":
                source,

            "target":
                target,

            "action":
                action,
        }
    )


# ---------------------------------------------------------------------
# DEM packaging
# ---------------------------------------------------------------------

def package_dem(
    src_raster,
    out_raster,
    expected_wkid,
    overwrite,
    preview,
    label,
    action_rows
):

    if not arcpy.Exists(
        src_raster
    ):

        fail(
            f"{label} source does not exist: "
            f"{src_raster}"
        )


    source_wkid, source_sr_name = (
        get_sr_info(
            src_raster
        )
    )


    action = (
        "copy"
        if source_wkid == expected_wkid
        else
        "project"
    )


    target_exists = arcpy.Exists(
        out_raster
    )


    log(
        f"{label} SOURCE: "
        f"{src_raster}"
    )


    log(
        f"{label} TARGET: "
        f"{out_raster}"
    )


    if target_exists and not overwrite:

        log(
            f"{label}: SKIP - target already exists "
            f"and overwrite=False."
        )


        if not preview:

            record_action(
                action_rows,
                "DEM",
                src_raster,
                out_raster,
                "skip",
                family_type=label
            )


        return "SKIPPED_EXISTING"


    if preview:

        log(
            f"[Preview] {label}: would "
            f"{action.upper()}."
        )


        return (
            f"PREVIEW_{action.upper()}"
        )


    if target_exists:

        arcpy.management.Delete(
            out_raster
        )


    log_step_start(
        f"Package {label}"
    )


    if action == "copy":

        arcpy.management.CopyRaster(
            src_raster,
            out_raster
        )


    else:

        warn(
            f"{label} WKID={source_wkid} "
            f"({source_sr_name}); expected "
            f"{expected_wkid}. Projecting."
        )


        arcpy.management.ProjectRaster(
            src_raster,
            out_raster,
            arcpy.SpatialReference(
                expected_wkid
            )
        )


    log_step_end(
        f"Package {label}"
    )


    record_action(
        action_rows,
        "DEM",
        src_raster,
        out_raster,
        action,
        family_type=label
    )


    return action.upper()


# ---------------------------------------------------------------------
# Stream family discovery
# ---------------------------------------------------------------------

def get_threshold_token(
    name
):

    match = THRESHOLD_PAT.search(
        name
    )


    return (
        match.group(
            1
        ).lower()
        if match
        else None
    )


def classify_stream_family(
    base_name
):

    value = base_name.lower()


    if (
        "w_characteristics_jd"
        in value
    ):

        return "stream_jd"


    if (
        "stream"
        in value
        and
        value.endswith(
            "w_characteristics"
        )
    ):

        return "stream_char"


    if "catchment" in value:

        return "catchment"


    if "stream" in value:

        return "stream"


    return None


def scan_stream_families(
    source_gdb
):

    old_workspace = (
        arcpy.env.workspace
    )


    families = defaultdict(
        dict
    )


    try:

        arcpy.env.workspace = (
            source_gdb
        )


        for fc in (
            arcpy.ListFeatureClasses()
            or
            []
        ):

            full_path = os.path.join(
                source_gdb,
                fc
            )


            base = arcpy.Describe(
                full_path
            ).baseName


            token = get_threshold_token(
                base
            )


            family_type = (
                classify_stream_family(
                    base
                )
            )


            if (
                not token
                or
                not family_type
            ):

                continue


            # Base stream could accidentally classify a characteristic
            # dataset only if ordering changes. Keep strict precedence.
            if (
                family_type
                in
                families[
                    token
                ]
            ):

                warn(
                    f"Duplicate {token} "
                    f"{family_type} detected; keeping first. "
                    f"Ignored: {full_path}"
                )


                continue


            families[
                token
            ][
                family_type
            ] = full_path


    finally:

        arcpy.env.workspace = (
            old_workspace
        )


    return families


def expected_family_paths(
    streams_gdb,
    county_name,
    token
):

    return {
        "catchment":
            os.path.join(
                streams_gdb,
                f"{county_name}_Catchment_{token}"
            ),

        "stream":
            os.path.join(
                streams_gdb,
                f"{county_name}_Stream_{token}"
            ),

        "stream_char":
            os.path.join(
                streams_gdb,
                (
                    f"{county_name}_Stream_{token}"
                    f"_w_characteristics"
                )
            ),

        "stream_jd":
            os.path.join(
                streams_gdb,
                (
                    f"{county_name}_Stream_{token}"
                    f"_w_characteristics_JD"
                )
            ),
    }


def package_stream_family(
    token,
    family,
    streams_gdb,
    county_name,
    expected_wkid,
    overwrite,
    preview,
    action_rows,
    include_base=True,
    include_characteristics=True,
    include_jd=True
):

    expected_types = set()

    if include_base:
        expected_types.update(
            {
                "catchment",
                "stream",
            }
        )

    if include_characteristics:
        expected_types.add(
            "stream_char"
        )

    if include_jd:
        expected_types.add(
            "stream_jd"
        )


    found_types = set(
        family.keys()
    )


    missing_types = (
        expected_types
        -
        found_types
    )


    if missing_types:

        warn(
            f"{token} source family is incomplete. "
            f"Missing: {sorted(missing_types)}"
        )


    targets = expected_family_paths(
        streams_gdb,
        county_name,
        token
    )


    existing_targets = [
        path
        for path in targets.values()
        if arcpy.Exists(
            path
        )
    ]


    if (
        existing_targets
        and
        overwrite
        and
        not preview
    ):

        log_step_start(
            f"Delete Existing {token} Deliverable Family"
        )


        for path in targets.values():

            if arcpy.Exists(
                path
            ):

                arcpy.management.Delete(
                    path
                )


        log_step_end(
            f"Delete Existing {token} Deliverable Family"
        )


    result = {}


    requested_types = []

    if include_base:
        requested_types.extend(
            [
                "catchment",
                "stream",
            ]
        )

    if include_characteristics:
        requested_types.append(
            "stream_char"
        )

    if include_jd:
        requested_types.append(
            "stream_jd"
        )

    for family_type in requested_types:

        if family_type not in family:

            result[
                family_type
            ] = "MISSING_SOURCE"

            continue


        source_fc = family[
            family_type
        ]


        target_fc = targets[
            family_type
        ]


        target_exists = (
            arcpy.Exists(
                target_fc
            )
        )


        if (
            target_exists
            and
            not overwrite
        ):

            log(
                f"{token} {family_type}: "
                f"SKIP existing target."
            )


            if not preview:

                record_action(
                    action_rows,
                    "Streams",
                    source_fc,
                    target_fc,
                    "skip",
                    token,
                    family_type
                )


            result[
                family_type
            ] = "SKIPPED_EXISTING"

            continue


        source_wkid, source_sr_name = (
            get_sr_info(
                source_fc
            )
        )


        if source_wkid != expected_wkid:

            warn(
                f"{token} {family_type}: "
                f"WKID={source_wkid} "
                f"({source_sr_name}); expected "
                f"{expected_wkid}. "
                f"Source is copied without reprojection, "
                f"matching original packaging behavior."
            )


        if preview:

            log(
                f"[Preview] Would copy "
                f"{source_fc} -> {target_fc}"
            )


            result[
                family_type
            ] = "PREVIEW_COPY"

            continue


        log_step_start(
            f"Copy {token} {family_type}"
        )


        arcpy.management.CopyFeatures(
            source_fc,
            target_fc
        )


        log_step_end(
            f"Copy {token} {family_type}"
        )


        record_action(
            action_rows,
            "Streams",
            source_fc,
            target_fc,
            (
                "replace"
                if target_exists
                else
                "copy"
            ),
            token,
            family_type
        )


        result[
            family_type
        ] = (
            "REPLACED"
            if target_exists
            else
            "COPIED"
        )


    return result


# ---------------------------------------------------------------------
# Final QC
# ---------------------------------------------------------------------

def qc_dem_gdb(
    dem_gdb,
    county_name
):

    expected_names = {
        f"{county_name}_DEM_HUC12",
        f"{county_name}_DEM_HUC12_Processed",
    }


    if not arcpy.Exists(
        dem_gdb
    ):

        return {
            "status":
                "MISSING_GDB",
        }


    old_workspace = arcpy.env.workspace


    try:

        arcpy.env.workspace = (
            dem_gdb
        )


        found = {
            arcpy.Describe(
                raster
            ).baseName
            for raster in (
                arcpy.ListRasters()
                or
                []
            )
        }


    finally:

        arcpy.env.workspace = (
            old_workspace
        )


    missing = sorted(
        expected_names
        -
        found
    )


    unexpected = sorted(
        found
        -
        expected_names
    )


    result = {
        "missing":
            missing,

        "unexpected":
            unexpected,
    }


    raw_path = os.path.join(
        dem_gdb,
        f"{county_name}_DEM_HUC12"
    )


    processed_path = os.path.join(
        dem_gdb,
        f"{county_name}_DEM_HUC12_Processed"
    )


    if (
        arcpy.Exists(
            raw_path
        )
        and
        arcpy.Exists(
            processed_path
        )
    ):

        raw_extent = raster_extent_tuple(
            raw_path
        )


        processed_extent = (
            raster_extent_tuple(
                processed_path
            )
        )


        extent_diffs = [
            abs(
                a
                -
                b
            )
            for a, b
            in zip(
                raw_extent,
                processed_extent
            )
        ]


        result[
            "extent_match_within_5_units"
        ] = all(
            diff <= 5.0
            for diff in extent_diffs
        )


        result[
            "raw_resolution"
        ] = raster_resolution_tuple(
            raw_path
        )


        result[
            "processed_resolution"
        ] = (
            raster_resolution_tuple(
                processed_path
            )
        )


        raw_wkid, _ = get_sr_info(
            raw_path
        )


        proc_wkid, _ = get_sr_info(
            processed_path
        )


        result[
            "projection_match"
        ] = (
            raw_wkid
            ==
            proc_wkid
        )


    result[
        "status"
    ] = (
        "PASS"
        if (
            not missing
            and
            not unexpected
        )
        else
        "WARN"
    )


    return result


def qc_streams_gdb(
    streams_gdb,
    county_name,
    selected_tokens,
    include_base=True,
    include_characteristics=True,
    include_jd=True
):

    result = {}

    if not arcpy.Exists(
        streams_gdb
    ):

        return {
            "status":
                "MISSING_GDB"
        }

    old_workspace = (
        arcpy.env.workspace
    )

    try:

        arcpy.env.workspace = (
            streams_gdb
        )

        found = {
            arcpy.Describe(
                fc
            ).baseName
            for fc in (
                arcpy.ListFeatureClasses()
                or
                []
            )
        }

        rasters = (
            arcpy.ListRasters()
            or
            []
        )

    finally:

        arcpy.env.workspace = (
            old_workspace
        )

    overall_pass = True

    for token in selected_tokens:

        expected = set()

        if include_base:
            expected.update(
                {
                    f"{county_name}_Catchment_{token}",
                    f"{county_name}_Stream_{token}",
                }
            )

        if include_characteristics:
            expected.add(
                (
                    f"{county_name}_Stream_{token}"
                    f"_w_characteristics"
                )
            )

        if include_jd:
            expected.add(
                (
                    f"{county_name}_Stream_{token}"
                    f"_w_characteristics_JD"
                )
            )

        family_found = {
            name
            for name in found
            if get_threshold_token(
                name
            ) == token
        }

        missing = sorted(
            expected
            -
            family_found
        )

        unexpected = sorted(
            family_found
            -
            expected
        )

        result[
            token
        ] = {
            "missing":
                missing,

            "unexpected":
                unexpected,

            "status":
                (
                    "PASS"
                    if (
                        not missing
                        and
                        not unexpected
                    )
                    else
                    "WARN"
                ),
        }

        if missing or unexpected:
            overall_pass = False

    result[
        "raster_count"
    ] = len(
        rasters
    )

    if rasters:
        overall_pass = False

    result[
        "status"
    ] = (
        "PASS"
        if overall_pass
        else
        "WARN"
    )

    return result

def cleanup_stream_rasters(
    streams_gdb,
    preview,
    action_rows
):

    if not arcpy.Exists(
        streams_gdb
    ):

        return 0


    old_workspace = (
        arcpy.env.workspace
    )


    deleted = 0


    try:

        arcpy.env.workspace = (
            streams_gdb
        )


        rasters = (
            arcpy.ListRasters()
            or
            []
        )


    finally:

        arcpy.env.workspace = (
            old_workspace
        )


    for raster in rasters:

        path = os.path.join(
            streams_gdb,
            raster
        )


        if preview:

            log(
                f"[Preview] Would delete unexpected "
                f"Streams GDB raster: {path}"
            )


            continue


        arcpy.management.Delete(
            path
        )


        deleted += 1


        record_action(
            action_rows,
            "Streams",
            path,
            path,
            "delete_raster",
            family_type="raster_cleanup"
        )


    return deleted


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    run_folder = p(
        0
    )


    raw_dem = p(
        1
    )


    processed_dem = p(
        2
    )


    source_streams_gdb = p(
        3
    )


    final_root = p(
        4
    )


    threshold_text = p(
        5
    )


    include_base = get_bool(
        6,
        True
    )


    include_characteristics = get_bool(
        7,
        True
    )


    include_jd = get_bool(
        8,
        True
    )


    overwrite_dems = get_bool(
        9,
        False
    )


    overwrite_families = get_bool(
        10,
        False
    )


    preview_only = get_bool(
        11,
        True
    )


    if not run_folder:

        fail(
            "Processing Run Folder is required."
        )


    if not raw_dem:

        fail(
            "Original HUC12 DEM is required."
        )


    if not processed_dem:

        fail(
            "Processed / Reconditioned DEM is required."
        )


    if not source_streams_gdb:

        fail(
            "Streams Working GDB is required."
        )


    if not final_root:

        fail(
            "Final Deliverable Root is required."
        )


    selected_tokens = parse_threshold_tokens(
        threshold_text
    )


    if not (
        include_base
        or
        include_characteristics
        or
        include_jd
    ):

        fail(
            "Select at least one stream product type to package."
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


    county = context[
        "county"
    ]


    project = context[
        "project"
    ]


    init_tool_log(
        county_folder=run_folder,
        tool_id="3.4",
        tool_name="Package_Final_Deliverables",
        county_abbr=county_abbr,
        extra_context={
            "Processing Run Folder":
                run_folder,

            "Run ID":
                context[
                    "run_id"
                ],

            "Original HUC12 DEM":
                raw_dem,

            "Processed / Reconditioned DEM":
                processed_dem,

            "Streams Working GDB":
                source_streams_gdb,

            "Final Deliverable Root":
                final_root,

            "Selected Thresholds":
                selected_tokens,

            "Include Base Streams/Catchments":
                include_base,

            "Include Characteristics":
                include_characteristics,

            "Include JD Confidence":
                include_jd,

            "Preview Only":
                preview_only,
        }
    )


    log("=" * 72)

    log(
        "3.4 Package Final Deliverables"
    )

    log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )

    log(
        f"Processing Run: "
        f"{context['run_id']}"
    )

    log("=" * 72)


    # -------------------------------------------------------------
    # Visible packaging sources and destination
    # -------------------------------------------------------------

    expected_final_root = (
        project.get(
            "sources",
            {}
        ).get(
            "final_deliverable_root"
        )
    )

    expected_raw_dem = os.path.join(
        context[
            "county_folder"
        ],
        "raster",
        f"{county_abbr}_DEM_HUC12.tif"
    )

    expected_processed_dem = os.path.join(
        run_folder,
        f"{county_abbr}_DEM_HUC12_AGREE.tif"
    )

    expected_streams_gdb = os.path.join(
        run_folder,
        f"{county_name}_Streams_Working.gdb"
    )

    visible_paths = [
        (
            "Original HUC12 DEM",
            raw_dem,
            expected_raw_dem,
            True,
        ),
        (
            "Processed / Reconditioned DEM",
            processed_dem,
            expected_processed_dem,
            True,
        ),
        (
            "Streams Working GDB",
            source_streams_gdb,
            expected_streams_gdb,
            True,
        ),
        (
            "Final Deliverable Root",
            final_root,
            expected_final_root,
            False,
        ),
    ]

    for label, selected_path, expected_path, use_arcpy_exists in visible_paths:

        exists_now = (
            arcpy.Exists(
                selected_path
            )
            if use_arcpy_exists
            else
            os.path.isdir(
                selected_path
            )
        )

        if not exists_now:

            if (
                label == "Final Deliverable Root"
                and
                not preview_only
            ):

                ensure_folder(
                    selected_path
                )

            elif label == "Final Deliverable Root":

                warn(
                    f"Final Deliverable Root does not currently exist; "
                    f"Preview will continue: {selected_path}"
                )

            else:

                fail(
                    f"Required packaging source is missing "
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
                f"{label} overrides the default path resolved "
                "from the Processing Run / Project Config."
            )

        log(
            f"PASS - {label}: "
            f"{selected_path}"
        )

    # -------------------------------------------------------------
    # Projection
    # -------------------------------------------------------------

    state_plan = str(
        county[
            "county"
        ][
            "state_plane"
        ]
    ).strip().upper()


    expected_wkid = int(
        county[
            "county"
        ].get(
            "wkid",
            EXPECTED_WKID.get(
                state_plan
            )
        )
    )


    if not expected_wkid:

        fail(
            f"Could not resolve expected WKID for "
            f"State Plane '{state_plan}'."
        )


    # -------------------------------------------------------------
    # Final output structure
    # -------------------------------------------------------------

    final_county_folder = os.path.join(
        final_root,
        county_name
    )


    dem_gdb = os.path.join(
        final_county_folder,
        f"{county_name}_DEM.gdb"
    )


    streams_gdb = os.path.join(
        final_county_folder,
        f"{county_name}_Streams.gdb"
    )


    if not preview_only:

        ensure_folder(
            final_county_folder
        )


    ensure_gdb(
        dem_gdb,
        preview_only
    )


    ensure_gdb(
        streams_gdb,
        preview_only
    )


    action_rows = []


    summary = {
        "selected_thresholds": selected_tokens,
        "include_base_streams_catchments": include_base,
        "include_characteristics": include_characteristics,
        "include_jd_confidence": include_jd,
        "dem": {},
        "streams": {},
        "deleted_stream_rasters": 0,
        "dem_qc": {},
        "streams_qc": {},
        "action_csv": None,
    }


    # -------------------------------------------------------------
    # Package DEMs
    # -------------------------------------------------------------

    raw_target = os.path.join(
        dem_gdb,
        f"{county_name}_DEM_HUC12"
    )


    processed_target = os.path.join(
        dem_gdb,
        f"{county_name}_DEM_HUC12_Processed"
    )


    summary[
        "dem"
    ][
        "original"
    ] = package_dem(
        raw_dem,
        raw_target,
        expected_wkid,
        overwrite_dems,
        preview_only,
        "Original DEM",
        action_rows
    )


    summary[
        "dem"
    ][
        "processed"
    ] = package_dem(
        processed_dem,
        processed_target,
        expected_wkid,
        overwrite_dems,
        preview_only,
        "Processed DEM",
        action_rows
    )


    # -------------------------------------------------------------
    # Scan / package stream families
    # -------------------------------------------------------------

    families = scan_stream_families(
        source_streams_gdb
    )


    if not families:

        fail(
            "No stream/catchment families were found in "
            f"{source_streams_gdb}"
        )


    for token in selected_tokens:

        if token not in families:

            warn(
                f"No source family found for expected "
                f"threshold {token}."
            )


            summary[
                "streams"
            ][
                token
            ] = {
                "status":
                    "MISSING_SOURCE_FAMILY"
            }


            continue


        summary[
            "streams"
        ][
            token
        ] = package_stream_family(
            token=token,
            family=families[
                token
            ],
            streams_gdb=streams_gdb,
            county_name=county_name,
            expected_wkid=expected_wkid,
            overwrite=overwrite_families,
            preview=preview_only,
            action_rows=action_rows,
            include_base=include_base,
            include_characteristics=include_characteristics,
            include_jd=include_jd
        )


    # -------------------------------------------------------------
    # Remove raster intermediates if final Streams GDB has any
    # -------------------------------------------------------------

    summary[
        "deleted_stream_rasters"
    ] = cleanup_stream_rasters(
        streams_gdb,
        preview_only,
        action_rows
    )


    # -------------------------------------------------------------
    # Final QC
    # -------------------------------------------------------------

    if preview_only:

        summary[
            "dem_qc"
        ] = {
            "status":
                "PREVIEW_NOT_EXECUTED"
        }


        summary[
            "streams_qc"
        ] = {
            "status":
                "PREVIEW_NOT_EXECUTED"
        }


        status = "PREVIEW"


    else:

        log_step_start(
            "Final DEM Deliverable QC"
        )


        dem_qc = qc_dem_gdb(
            dem_gdb,
            county_name
        )


        log_step_end(
            "Final DEM Deliverable QC"
        )


        log_step_start(
            "Final Streams Deliverable QC"
        )


        streams_qc = qc_streams_gdb(
            streams_gdb,
            county_name,
            selected_tokens,
            include_base=include_base,
            include_characteristics=include_characteristics,
            include_jd=include_jd
        )


        log_step_end(
            "Final Streams Deliverable QC"
        )


        summary[
            "dem_qc"
        ] = dem_qc


        summary[
            "streams_qc"
        ] = streams_qc


        if (
            dem_qc.get(
                "status"
            )
            ==
            "PASS"
            and
            streams_qc.get(
                "status"
            )
            ==
            "PASS"
        ):

            status = "PASS"


        else:

            status = "WARN"


        action_csv = write_action_csv(
            run_folder,
            county_name,
            action_rows
        )


        summary[
            "action_csv"
        ] = action_csv


    # -------------------------------------------------------------
    # Log summary
    # -------------------------------------------------------------

    log(
        f"Final County Folder: "
        f"{final_county_folder}"
    )


    log(
        f"Final DEM GDB: "
        f"{dem_gdb}"
    )


    log(
        f"Final Streams GDB: "
        f"{streams_gdb}"
    )


    log(
        f"DEM QC Status: "
        f"{summary['dem_qc'].get('status')}"
    )


    log(
        f"Streams QC Status: "
        f"{summary['streams_qc'].get('status')}"
    )


    if summary[
        "action_csv"
    ]:

        log(
            f"Packaging Action CSV: "
            f"{summary['action_csv']}"
        )


    # -------------------------------------------------------------
    # Update run record
    # -------------------------------------------------------------

    update_run_config(
        context=context,
        status=status,
        final_county_folder=final_county_folder,
        dem_gdb=dem_gdb,
        streams_gdb=streams_gdb,
        summary=summary,
        preview=preview_only
    )


    finish_tool_log(
        status
    )


    arcpy.SetParameterAsText(
        12,
        final_county_folder
    )


    arcpy.SetParameterAsText(
        13,
        dem_gdb
    )


    arcpy.SetParameterAsText(
        14,
        streams_gdb
    )


    arcpy.SetParameterAsText(
        15,
        status
    )


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
                15,
                "FAIL"
            )

        except Exception:

            pass


        raise