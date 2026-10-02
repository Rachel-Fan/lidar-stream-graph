# -*- coding: utf-8 -*-
"""
1.2 Create HUC12 Extent DEM
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Build the hydrologic processing extent and HUC12 DEM for the selected county.

This tool owns the full HUC12-extent workflow:

    County Boundary
        +
    Statewide WBD_HUC12
        ->
    Select complete HUC12 polygons intersecting county
        ->
    Identify all counties touched by those HUC12 polygons
        ->
    Resolve standardized 3-ft DEMs for all touched counties
        ->
    Mosaic DEMs, with target county LAST
        ->
    Create TEMPORARY multi-county source mosaic in scratch
        ->
    Extract final DEM to the HUC12 processing extent
        ->
    Delete temporary source mosaic

ArcGIS Script Tool Parameters
-----------------------------
0  County Processing Folder      Folder          Required Input
1  Overwrite Existing Outputs    Boolean         Optional Input
2  HUC12 Processing Extent       Feature Class   Derived Output
3  HUC12 DEM                     Raster Dataset  Derived Output
4  Processing Status             String          Derived Output

Output location
---------------
Reusable Step 1.2 products are stored under:

    <County Processing Folder>\raster\

Only the HUC12 processing extent and final HUC12 DEM are retained.
The multi-county DEM mosaic is temporary and is deleted after extraction.
Timestamped processing runs begin in Step 2.1.

Workflow-condition UX rule
--------------------------
Expected dependency conditions do NOT raise Python errors:

- Target 3-ft DEM missing/not standardized
- Adjacent county 3-ft DEM missing/not standardized
- Existing output invalid while Overwrite=False

These produce WARN messages + clean return.

True geoprocessing failures still raise normally.
"""

import os
import sys
import glob
import time

import arcpy
from arcpy.sa import SetNull, ExtractByMask


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from setup_common import (
    log,
    warn,
    fail,
    ensure_folder,
    load_json,
    require_geometry,
)


# ---------------------------------------------------------------------
# Algorithm constants
# ---------------------------------------------------------------------

TARGET_CELL_SIZE_FT = 3.0

NODATA_F32_MIN = -3.4028235e+38

EXTREME_ABS_THRESHOLD = 3.0e38

MIN_EXISTING_OUTPUT_SIZE_BYTES = 10_000_000

COUNTY_ABBR_FIELD = "COUNTY_CD"

COUNTY_NAME_FIELD = "COUNTY"


# ---------------------------------------------------------------------
# Parameter helpers
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

        return text in {
            "true",
            "1",
            "yes",
            "y",
        }


def minutes_since(t0):
    return round(
        (time.time() - t0) / 60.0,
        2
    )


def normalized_path(path):
    if not path:
        return ""

    return os.path.normcase(
        os.path.normpath(path)
    )


# ---------------------------------------------------------------------
# Context discovery
# ---------------------------------------------------------------------

def discover_county_config(county_folder):

    config_folder = os.path.join(
        county_folder,
        "config"
    )

    if not os.path.isdir(
        config_folder
    ):
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
                f" - {path}"
                for path in candidates
            )
        )


    return candidates[0]


def discover_latest_validation_report(
    county_folder,
    county_abbr
):

    log_folder = os.path.join(
        county_folder,
        "logs"
    )


    if not os.path.isdir(
        log_folder
    ):

        fail(
            f"County log folder does not exist: "
            f"{log_folder}"
        )


    pattern = os.path.join(
        log_folder,
        f"{county_abbr}_input_validation_*.json"
    )


    candidates = glob.glob(
        pattern
    )


    if not candidates:

        fail(
            f"No county validation report found matching:\n"
            f"{pattern}"
        )


    candidates.sort(
        key=os.path.getmtime,
        reverse=True
    )


    for candidate in candidates:

        try:

            report = load_json(
                candidate
            )

            status = (
                report.get("validation_status")
                or
                report.get("status")
            )

            if status in {
                "PASS",
                "WARN",
            }:

                return candidate, report

        except Exception:

            continue


    fail(
        f"No usable PASS/WARN county validation report "
        f"found in: {log_folder}"
    )


def load_processing_context(
    county_folder
):

    county_config_path = (
        discover_county_config(
            county_folder
        )
    )


    county = load_json(
        county_config_path
    )


    county_info = county.get(
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


    if (
        not county_name
        or
        not county_abbr
    ):

        fail(
            "County Config is missing county name "
            "or abbreviation."
        )


    folder_name = os.path.basename(
        os.path.normpath(
            county_folder
        )
    )


    if (
        folder_name.casefold()
        !=
        county_name.casefold()
    ):

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


    if not os.path.isfile(
        project_config_path
    ):

        fail(
            "Project Config referenced by County Config "
            "does not exist:\n"
            f"{project_config_path}"
        )


    project = load_json(
        project_config_path
    )


    validation_path, validation = (
        discover_latest_validation_report(
            county_folder,
            county_abbr
        )
    )


    report_county = validation.get(
        "county",
        {}
    )


    report_abbr = str(
        report_county.get(
            "abbr",
            ""
        )
    ).strip().upper()


    if (
        report_abbr
        and
        report_abbr != county_abbr
    ):

        fail(
            "County Config / Validation Report mismatch.\n"
            f"County Config: {county_abbr}\n"
            f"Validation Report: {report_abbr}"
        )


    report_project = validation.get(
        "project_config"
    )


    if (
        report_project
        and
        normalized_path(
            report_project
        )
        !=
        normalized_path(
            project_config_path
        )
    ):

        fail(
            "Latest validation report belongs to a "
            "different Project Config."
        )


    report_county_config = validation.get(
        "county_config"
    )


    if (
        report_county_config
        and
        normalized_path(
            report_county_config
        )
        !=
        normalized_path(
            county_config_path
        )
    ):

        fail(
            "Latest validation report belongs to a "
            "different County Config."
        )


    return {
        "project_config_path":
            project_config_path,

        "project":
            project,

        "county_config_path":
            county_config_path,

        "county":
            county,

        "validation_report_path":
            validation_path,

        "validation":
            validation,
    }


# ---------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------

def county_name_for_filename(
    name
):

    return (
        str(name)
        .strip()
        .title()
        .replace(
            " ",
            "_"
        )
        .replace(
            ".",
            ""
        )
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

        result["issues"].append(
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

        data_type = getattr(
            desc,
            "dataType",
            None
        )

        if data_type not in {
            "RasterDataset",
            "RasterLayer",
            "MosaicDataset",
        }:

            result["issues"].append(
                f"Unsupported raster dataType={data_type}"
            )


        sr = desc.spatialReference


        result["wkid"] = getattr(
            sr,
            "factoryCode",
            None
        )


    except Exception as exc:

        result["issues"].append(
            f"Could not describe raster: {exc}"
        )


    try:

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
            f"Could not read cell size: {exc}"
        )


    if (
        expected_wkid is not None
        and
        result["wkid"]
        !=
        int(expected_wkid)
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
            abs(
                result["cell_x"]
                -
                expected_cell_size
            )
            >
            0.05
            or
            abs(
                result["cell_y"]
                -
                expected_cell_size
            )
            >
            0.05
        ):

            result["issues"].append(
                f"Cell size "
                f"({result['cell_x']}, "
                f"{result['cell_y']}) "
                f"!= ~{expected_cell_size}"
            )


    result["valid"] = (
        len(
            result["issues"]
        )
        ==
        0
    )


    return result


# ---------------------------------------------------------------------
# HUC12 processing extent
# ---------------------------------------------------------------------

def build_huc12_processing_extent(
    statewide_huc12,
    county_boundary,
    output_extent,
    overwrite
):
    """
    Select complete statewide HUC12 polygons that INTERSECT the county.

    This is deliberately Select + CopyFeatures, NOT Clip.
    Full watershed polygons are retained outside the county boundary.
    """

    if not arcpy.Exists(
        statewide_huc12
    ):

        fail(
            f"Statewide WBD_HUC12 no longer exists: "
            f"{statewide_huc12}"
        )


    try:

        require_geometry(
            statewide_huc12,
            {"Polygon"}
        )

        require_geometry(
            county_boundary,
            {"Polygon"}
        )

    except Exception as exc:

        fail(
            f"Processing extent source geometry validation failed: "
            f"{exc}"
        )


    # -------------------------------------------------------------
    # Reuse valid existing extent if Overwrite=False
    # -------------------------------------------------------------

    if arcpy.Exists(
        output_extent
    ):

        try:

            existing_count = int(
                arcpy.management.GetCount(
                    output_extent
                )[0]
            )


            existing_shape = getattr(
                arcpy.Describe(
                    output_extent
                ),
                "shapeType",
                None
            )


            existing_valid = (
                existing_count > 0
                and
                existing_shape == "Polygon"
            )


        except Exception:

            existing_valid = False


        if (
            existing_valid
            and
            not overwrite
        ):

            log(
                f"Existing HUC12 Processing Extent is valid "
                f"({existing_count} polygons); reusing it."
            )

            return output_extent


        if (
            not existing_valid
            and
            not overwrite
        ):

            warn(
                "Existing HUC12 Processing Extent does not appear "
                "valid."
            )

            warn(
                "Step 1.2 was not run because "
                "Overwrite Existing Outputs = False. "
                "Enable overwrite to regenerate the processing extent."
            )

            return None


        log(
            f"Overwrite=True. Existing processing extent will be "
            f"regenerated: {output_extent}"
        )


        arcpy.management.Delete(
            output_extent
        )


    layer_name = (
        "step_1_2_wbd_huc12_layer"
    )


    if arcpy.Exists(
        layer_name
    ):

        arcpy.management.Delete(
            layer_name
        )


    try:

        arcpy.management.MakeFeatureLayer(
            statewide_huc12,
            layer_name
        )


        arcpy.management.SelectLayerByLocation(
            in_layer=layer_name,
            overlap_type="INTERSECT",
            select_features=county_boundary,
            selection_type="NEW_SELECTION"
        )


        selected_count = int(
            arcpy.management.GetCount(
                layer_name
            )[0]
        )


        if selected_count <= 0:

            fail(
                "No statewide HUC12 polygons intersect "
                "the selected county boundary."
            )


        log(
            f"Selected {selected_count} complete HUC12 polygons "
            f"intersecting the county."
        )


        ensure_folder(
            os.path.dirname(
                output_extent
            )
        )


        arcpy.management.CopyFeatures(
            layer_name,
            output_extent
        )


    finally:

        try:

            if arcpy.Exists(
                layer_name
            ):

                arcpy.management.Delete(
                    layer_name
                )

        except Exception:

            pass


    if not arcpy.Exists(
        output_extent
    ):

        fail(
            f"HUC12 Processing Extent was not created: "
            f"{output_extent}"
        )


    output_count = int(
        arcpy.management.GetCount(
            output_extent
        )[0]
    )


    if output_count <= 0:

        fail(
            f"HUC12 Processing Extent contains no polygons: "
            f"{output_extent}"
        )


    log(
        f"PASS - HUC12 Processing Extent "
        f"({output_count} complete polygons): "
        f"{output_extent}"
    )


    return output_extent


# ---------------------------------------------------------------------
# Touched counties
# ---------------------------------------------------------------------

def get_touched_counties(
    ohio_counties,
    huc12_extent
):
    """
    Identify all counties intersected by the selected COMPLETE HUC12 polygons.
    """

    fields = {
        field.name
        for field in arcpy.ListFields(
            ohio_counties
        )
    }


    for required in [
        COUNTY_ABBR_FIELD,
        COUNTY_NAME_FIELD,
    ]:

        if required not in fields:

            fail(
                f"Ohio Counties Reference is missing "
                f"required field: {required}"
            )


    layer_name = (
        "step_1_2_ohio_counties_layer"
    )


    if arcpy.Exists(
        layer_name
    ):

        arcpy.management.Delete(
            layer_name
        )


    arcpy.management.MakeFeatureLayer(
        ohio_counties,
        layer_name
    )


    try:

        arcpy.management.SelectLayerByLocation(
            in_layer=layer_name,
            overlap_type="INTERSECT",
            select_features=huc12_extent,
            selection_type="NEW_SELECTION"
        )


        touched = []


        with arcpy.da.SearchCursor(
            layer_name,
            [
                COUNTY_ABBR_FIELD,
                COUNTY_NAME_FIELD,
            ]
        ) as cursor:


            for abbr, name in cursor:


                if (
                    abbr is None
                    or
                    name is None
                ):

                    continue


                touched.append(
                    (
                        str(abbr)
                        .strip()
                        .upper(),

                        str(name)
                        .strip()
                        .title(),
                    )
                )


    finally:

        try:

            arcpy.management.Delete(
                layer_name
            )

        except Exception:

            pass


    return sorted(
        set(
            touched
        ),
        key=lambda row: row[0]
    )


# ---------------------------------------------------------------------
# Resolve standardized DEMs
# ---------------------------------------------------------------------

def resolve_required_dems(
    touched_counties,
    target_name,
    target_abbr,
    dem_3ft_root,
    target_wkid
):

    records = []

    missing = []

    invalid = []


    for abbr, name in touched_counties:


        dem_path = os.path.join(
            dem_3ft_root,
            (
                f"{county_name_for_filename(name)}"
                f"_DEM_3ft.tif"
            )
        )


        check = inspect_raster(
            dem_path,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )


        record = {
            "abbr": abbr,
            "name": name,
            "path": dem_path,
            "check": check,
        }


        records.append(
            record
        )


        if not check[
            "exists"
        ]:

            missing.append(
                record
            )


        elif not check[
            "valid"
        ]:

            invalid.append(
                record
            )


    # Safety: target county must always be present.
    if not any(
        record[
            "abbr"
        ]
        ==
        target_abbr
        for record in records
    ):


        target_path = os.path.join(
            dem_3ft_root,
            (
                f"{county_name_for_filename(target_name)}"
                f"_DEM_3ft.tif"
            )
        )


        target_check = inspect_raster(
            target_path,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )


        target_record = {
            "abbr":
                target_abbr,

            "name":
                target_name,

            "path":
                target_path,

            "check":
                target_check,
        }


        records.append(
            target_record
        )


        if not target_check[
            "exists"
        ]:

            missing.append(
                target_record
            )


        elif not target_check[
            "valid"
        ]:

            invalid.append(
                target_record
            )


    return (
        records,
        missing,
        invalid
    )


# ---------------------------------------------------------------------
# Raster processing helpers
# ---------------------------------------------------------------------

def safe_delete(
    path
):

    try:

        if arcpy.Exists(
            path
        ):

            arcpy.management.Delete(
                path
            )


        elif os.path.exists(
            path
        ):

            os.remove(
                path
            )


    except Exception as exc:

        warn(
            f"Could not delete "
            f"{path}: {exc}"
        )


def set_band_nodata(
    raster_path,
    nodata_value
):

    try:

        arcpy.management.SetRasterProperties(
            in_raster=raster_path,
            nodata=(
                "1 "
                +
                str(
                    nodata_value
                )
            )
        )


    except Exception as exc:

        warn(
            f"Could not set NoData on "
            f"{raster_path}: {exc}"
        )


def log_input_spatial_refs(
    raster_paths
):

    log(
        "Input DEM spatial references:"
    )


    for path in raster_paths:

        try:

            desc = arcpy.Describe(
                path
            )


            sr = desc.spatialReference


            log(
                f"  {os.path.basename(path)} "
                f"-> {sr.name} "
                f"(WKID {getattr(sr, 'factoryCode', None)})"
            )


        except Exception as exc:

            warn(
                f"Could not read spatial reference for "
                f"{path}: {exc}"
            )


def existing_output_is_reusable(
    path,
    target_wkid,
    target_cell_size,
    minimum_size_bytes=None
):

    check = inspect_raster(
        path,
        expected_wkid=target_wkid,
        expected_cell_size=target_cell_size
    )


    if not check[
        "valid"
    ]:

        return False, check


    if (
        minimum_size_bytes is not None
        and
        os.path.isfile(
            path
        )
    ):

        try:

            size = os.path.getsize(
                path
            )


            if size < minimum_size_bytes:

                check[
                    "issues"
                ].append(
                    f"File size {size:,} bytes is below "
                    f"expected minimum "
                    f"{minimum_size_bytes:,}."
                )


                check[
                    "valid"
                ] = False


                return False, check


        except Exception:

            pass


    return True, check


def build_countywide_mosaic(
    mosaic_inputs,
    target_dem,
    output_path
):

    out_folder = os.path.dirname(
        output_path
    )


    ensure_folder(
        out_folder
    )


    target_desc = arcpy.Describe(
        target_dem
    )


    spatial_ref = (
        target_desc.spatialReference
    )


    target_raster = arcpy.Raster(
        target_dem
    )


    cell_size_x = float(
        target_raster.meanCellWidth
    )


    log(
        f"Target DEM spatial reference: "
        f"{spatial_ref.name}"
    )


    log(
        f"Target DEM cell size: "
        f"{cell_size_x}"
    )


    log_input_spatial_refs(
        mosaic_inputs
    )


    old_output_cs = (
        arcpy.env.outputCoordinateSystem
    )

    old_snap = (
        arcpy.env.snapRaster
    )

    old_cell = (
        arcpy.env.cellSize
    )

    old_pyramid = (
        arcpy.env.pyramid
    )

    old_overwrite = (
        arcpy.env.overwriteOutput
    )


    tmp_mosaic = os.path.join(
        out_folder,
        (
            os.path.splitext(
                os.path.basename(
                    output_path
                )
            )[0]
            +
            "_tmp_mosaic.tif"
        )
    )


    tmp_clean = os.path.join(
        out_folder,
        (
            os.path.splitext(
                os.path.basename(
                    output_path
                )
            )[0]
            +
            "_tmp_clean.tif"
        )
    )


    try:

        arcpy.env.outputCoordinateSystem = (
            spatial_ref
        )


        arcpy.env.snapRaster = (
            target_dem
        )


        arcpy.env.cellSize = (
            cell_size_x
        )


        arcpy.env.pyramid = (
            "NONE"
        )


        arcpy.env.overwriteOutput = (
            True
        )


        safe_delete(
            tmp_mosaic
        )

        safe_delete(
            tmp_clean
        )

        safe_delete(
            output_path
        )


        log(
            "Running MosaicToNewRaster "
            "(LAST wins; target county is last)..."
        )


        t0 = time.time()


        arcpy.management.MosaicToNewRaster(
            input_rasters=mosaic_inputs,
            output_location=out_folder,
            raster_dataset_name_with_extension=(
                os.path.basename(
                    tmp_mosaic
                )
            ),
            coordinate_system_for_the_raster=(
                spatial_ref
            ),
            pixel_type="32_BIT_FLOAT",
            cellsize=cell_size_x,
            number_of_bands=1,
            mosaic_method="LAST",
            mosaic_colormap_mode="FIRST"
        )


        log(
            f"Mosaic created in "
            f"{minutes_since(t0)} minutes."
        )


        set_band_nodata(
            tmp_mosaic,
            NODATA_F32_MIN
        )


        log(
            "Cleaning extreme Float32 values "
            "with SetNull..."
        )


        t1 = time.time()


        raster = arcpy.Raster(
            tmp_mosaic
        )


        cleaned = SetNull(
            (
                raster
                >=
                EXTREME_ABS_THRESHOLD
            )
            |
            (
                raster
                <=
                -EXTREME_ABS_THRESHOLD
            ),
            raster
        )


        cleaned.save(
            tmp_clean
        )


        del cleaned

        del raster


        log(
            f"SetNull cleanup completed in "
            f"{minutes_since(t1)} minutes."
        )


        set_band_nodata(
            tmp_clean,
            NODATA_F32_MIN
        )


        arcpy.management.CopyRaster(
            in_raster=tmp_clean,
            out_rasterdataset=output_path,
            pixel_type="32_BIT_FLOAT",
            nodata_value=str(
                NODATA_F32_MIN
            ),
            format="TIFF"
        )


        set_band_nodata(
            output_path,
            NODATA_F32_MIN
        )


    finally:

        safe_delete(
            tmp_mosaic
        )

        safe_delete(
            tmp_clean
        )


        arcpy.env.outputCoordinateSystem = (
            old_output_cs
        )

        arcpy.env.snapRaster = (
            old_snap
        )

        arcpy.env.cellSize = (
            old_cell
        )

        arcpy.env.pyramid = (
            old_pyramid
        )

        arcpy.env.overwriteOutput = (
            old_overwrite
        )


def extract_huc12_dem(
    countywide_dem,
    huc12_extent,
    output_path,
    target_dem
):

    old_snap = (
        arcpy.env.snapRaster
    )

    old_cell = (
        arcpy.env.cellSize
    )

    old_output_cs = (
        arcpy.env.outputCoordinateSystem
    )

    old_overwrite = (
        arcpy.env.overwriteOutput
    )


    try:

        desc = arcpy.Describe(
            target_dem
        )


        raster = arcpy.Raster(
            target_dem
        )


        arcpy.env.snapRaster = (
            target_dem
        )


        arcpy.env.cellSize = (
            raster.meanCellWidth
        )


        arcpy.env.outputCoordinateSystem = (
            desc.spatialReference
        )


        arcpy.env.overwriteOutput = (
            True
        )


        safe_delete(
            output_path
        )


        log(
            "Running ExtractByMask to "
            "HUC12 processing extent..."
        )


        t0 = time.time()


        extracted = ExtractByMask(
            countywide_dem,
            huc12_extent
        )


        extracted.save(
            output_path
        )


        del extracted


        log(
            f"HUC12 extraction completed in "
            f"{minutes_since(t0)} minutes."
        )


    finally:

        arcpy.env.snapRaster = (
            old_snap
        )

        arcpy.env.cellSize = (
            old_cell
        )

        arcpy.env.outputCoordinateSystem = (
            old_output_cs
        )

        arcpy.env.overwriteOutput = (
            old_overwrite
        )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    start = time.time()


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


    project = context[
        "project"
    ]


    county = context[
        "county"
    ]


    validation = context[
        "validation"
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


    log("=" * 72)

    log(
        "1.2 Create HUC12 Extent DEM"
    )

    log(
        f"County: "
        f"{county_name} ({county_abbr})"
    )

    log(
        f"County Config: "
        f"{context['county_config_path']}"
    )

    log(
        f"Project Config: "
        f"{context['project_config_path']}"
    )

    log(
        f"Validation Report: "
        f"{context['validation_report_path']}"
    )

    log("=" * 72)


    # -------------------------------------------------------------
    # County-level raster output folder
    #
    # Step 1.2 remains reusable county-level preparation.
    # Timestamped processing runs do not begin until Step 2.1.
    # -------------------------------------------------------------

    raster_folder = os.path.join(
        county_folder,
        "raster"
    )

    ensure_folder(
        raster_folder
    )

    log(
        f"Raster Output Folder: "
        f"{raster_folder}"
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

    countywide_dem = None


    try:

        # ---------------------------------------------------------
        # Resolve source datasets
        # ---------------------------------------------------------

        try:

            ohio_counties = (
                project["sources"]
                ["ohio_counties"]
            )


            statewide_huc12 = (
                project["sources"]
                ["statewide_huc12"]
            )


            dem_3ft_root = (
                project["sources"]
                ["dem_3ft_root"]
            )


        except Exception:

            fail(
                "Project Config is missing one or more required sources: "
                "ohio_counties, statewide_huc12, dem_3ft_root."
            )


        resolved = (
            validation.get(
                "resolved_inputs"
            )
            or
            {}
        )


        county_boundary = (
            resolved.get(
                "county_boundary"
            )
        )


        if (
            not county_boundary
            or
            not arcpy.Exists(
                county_boundary
            )
        ):

            fail(
                "Latest 0.3 Validation Report does not contain "
                "a usable county boundary."
            )


        if not arcpy.Exists(
            statewide_huc12
        ):

            fail(
                f"Statewide WBD_HUC12 no longer exists: "
                f"{statewide_huc12}"
            )


        if not arcpy.Exists(
            ohio_counties
        ):

            fail(
                f"Ohio Counties Reference no longer exists: "
                f"{ohio_counties}"
            )


        if not os.path.isdir(
            dem_3ft_root
        ):

            fail(
                f"DEM 3-ft Root no longer exists: "
                f"{dem_3ft_root}"
            )


        # ---------------------------------------------------------
        # Target Step 1.1 DEM
        # ---------------------------------------------------------

        target_dem = os.path.join(
            dem_3ft_root,
            (
                f"{county_name_for_filename(county_name)}"
                f"_DEM_3ft.tif"
            )
        )


        target_check = inspect_raster(
            target_dem,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )


        if not target_check[
            "exists"
        ]:

            warn(
                "Step 1.2 was not run because the target county "
                "3-ft DEM does not exist. Run Step 1.1 first."
            )


            warn(
                f"Expected target DEM: "
                f"{target_dem}"
            )


            arcpy.SetParameterAsText(
                4,
                "SKIPPED_TARGET_DEM_NOT_READY"
            )


            return


        if not target_check[
            "valid"
        ]:

            warn(
                "Step 1.2 was not run because the target county "
                "3-ft DEM does not meet current production requirements."
            )


            warn(
                "Target DEM issue(s): "
                +
                "; ".join(
                    target_check[
                        "issues"
                    ]
                )
            )


            arcpy.SetParameterAsText(
                4,
                "SKIPPED_TARGET_DEM_INVALID"
            )


            return


        log(
            f"PASS - Target 3-ft DEM: "
            f"{target_dem}"
        )


        # ---------------------------------------------------------
        # 1.2A Build HUC12 processing extent
        # ---------------------------------------------------------

        huc12_extent = os.path.join(
            raster_folder,
            f"{county_abbr}_HUC12.shp"
        )


        huc12_extent = (
            build_huc12_processing_extent(
                statewide_huc12=statewide_huc12,
                county_boundary=county_boundary,
                output_extent=huc12_extent,
                overwrite=overwrite
            )
        )


        if not huc12_extent:

            arcpy.SetParameterAsText(
                4,
                "SKIPPED_INVALID_EXISTING_EXTENT"
            )

            return


        # ---------------------------------------------------------
        # 1.2B Identify all counties touched by HUC12 extent
        # ---------------------------------------------------------

        touched_counties = (
            get_touched_counties(
                ohio_counties,
                huc12_extent
            )
        )


        if not touched_counties:

            fail(
                "No counties intersect the generated "
                "HUC12 processing extent."
            )


        log(
            f"Counties touched by HUC12 extent: "
            f"{len(touched_counties)}"
        )


        for abbr, name in touched_counties:

            log(
                f"  {abbr} - {name}"
            )


        # ---------------------------------------------------------
        # 1.2C Resolve touched-county DEMs
        # ---------------------------------------------------------

        dem_records, missing, invalid = (
            resolve_required_dems(
                touched_counties=touched_counties,
                target_name=county_name,
                target_abbr=county_abbr,
                dem_3ft_root=dem_3ft_root,
                target_wkid=target_wkid
            )
        )


        if missing:

            warn(
                "Step 1.2 was not run because one or more "
                "required touched-county 3-ft DEMs are missing."
            )


            for record in missing:

                warn(
                    f"Missing DEM: "
                    f"{record['name']} "
                    f"({record['abbr']}) -> "
                    f"{record['path']}"
                )


            arcpy.SetParameterAsText(
                2,
                huc12_extent
            )


            arcpy.SetParameterAsText(
                4,
                "SKIPPED_MISSING_ADJACENT_DEM"
            )


            return


        if invalid:

            warn(
                "Step 1.2 was not run because one or more "
                "required touched-county DEMs are not standardized."
            )


            for record in invalid:

                warn(
                    f"Invalid DEM: "
                    f"{record['name']} "
                    f"({record['abbr']}) -> "
                    f"{record['path']} | "
                    +
                    "; ".join(
                        record[
                            "check"
                        ][
                            "issues"
                        ]
                    )
                )


            arcpy.SetParameterAsText(
                2,
                huc12_extent
            )


            arcpy.SetParameterAsText(
                4,
                "SKIPPED_INVALID_ADJACENT_DEM"
            )


            return


        # ---------------------------------------------------------
        # Target must be LAST because mosaic_method="LAST"
        # ---------------------------------------------------------

        other_dems = [
            record[
                "path"
            ]
            for record in dem_records
            if record[
                "abbr"
            ]
            !=
            county_abbr
        ]


        mosaic_inputs = (
            other_dems
            +
            [
                target_dem
            ]
        )


        log(
            "Mosaic input order "
            "(LAST wins; target county last):"
        )


        for index, raster_path in enumerate(
            mosaic_inputs,
            1
        ):

            log(
                f"  {index}. "
                f"{raster_path}"
            )


        # ---------------------------------------------------------
        # Output names
        # ---------------------------------------------------------

        # ---------------------------------------------------------
        # Temporary multi-county source mosaic
        #
        # This is an INTERNAL intermediate only. It is needed to
        # build the final HUC12 DEM, but it is not a retained
        # county-level product.
        # ---------------------------------------------------------

        try:
            scratch_root = (
                county["paths"]
                ["scratch_root"]
            )
        except Exception:
            fail(
                "County Config does not contain "
                "paths > scratch_root."
            )

        ensure_folder(
            scratch_root
        )

        countywide_dem = os.path.join(
            scratch_root,
            f"{county_abbr}_HUC12_Source_Mosaic_tmp.tif"
        )


        huc12_dem = os.path.join(
            raster_folder,
            f"{county_abbr}_DEM_HUC12.tif"
        )


        # ---------------------------------------------------------
        # 1.2D Temporary multi-county source mosaic
        #
        # Always rebuild this scratch intermediate when HUC12 DEM
        # needs to be generated. It is deleted after extraction.
        # ---------------------------------------------------------

        if arcpy.Exists(
            countywide_dem
        ):
            safe_delete(
                countywide_dem
            )

        log(
            "Building temporary multi-county source mosaic..."
        )

        build_countywide_mosaic(
            mosaic_inputs=mosaic_inputs,
            target_dem=target_dem,
            output_path=countywide_dem
        )

        tmp_check = inspect_raster(
            countywide_dem,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )

        if not tmp_check[
            "valid"
        ]:
            fail(
                "Temporary multi-county source mosaic failed QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in tmp_check[
                        "issues"
                    ]
                )
            )

        log(
            f"PASS - Temporary source mosaic: "
            f"{countywide_dem}"
        )


        # ---------------------------------------------------------
        # 1.2E HUC12 DEM
        # ---------------------------------------------------------

        if arcpy.Exists(
            huc12_dem
        ):

            reusable, check = (
                existing_output_is_reusable(
                    huc12_dem,
                    target_wkid,
                    TARGET_CELL_SIZE_FT
                )
            )


            if (
                not overwrite
                and
                reusable
            ):

                log(
                    "Existing HUC12 DEM meets current "
                    "requirements; reusing it."
                )


                arcpy.SetParameterAsText(
                    2,
                    huc12_extent
                )


                arcpy.SetParameterAsText(
                    3,
                    huc12_dem
                )


                arcpy.SetParameterAsText(
                    4,
                    "SKIPPED_EXISTING_VALID"
                )


                return


            if (
                not overwrite
                and
                not reusable
            ):

                warn(
                    "Existing HUC12 DEM does not meet current "
                    "production requirements."
                )


                warn(
                    "HUC12 DEM was not regenerated because "
                    "Overwrite Existing Outputs = False."
                )


                arcpy.SetParameterAsText(
                    2,
                    huc12_extent
                )


                arcpy.SetParameterAsText(
                    3,
                    huc12_dem
                )


                arcpy.SetParameterAsText(
                    4,
                    "SKIPPED_INVALID_EXISTING_HUC12"
                )


                return


        extract_huc12_dem(
            countywide_dem=countywide_dem,
            huc12_extent=huc12_extent,
            output_path=huc12_dem,
            target_dem=target_dem
        )


        huc_check = inspect_raster(
            huc12_dem,
            expected_wkid=target_wkid,
            expected_cell_size=TARGET_CELL_SIZE_FT
        )


        if not huc_check[
            "valid"
        ]:

            fail(
                "New HUC12 DEM failed output QC:\n"
                +
                "\n".join(
                    f" - {issue}"
                    for issue in huc_check[
                        "issues"
                    ]
                )
            )


        log(
            f"PASS - HUC12 DEM: "
            f"{huc12_dem}"
        )


        # ---------------------------------------------------------
        # Remove internal source mosaic after successful extraction
        # ---------------------------------------------------------

        safe_delete(
            countywide_dem
        )

        log(
            "Temporary multi-county source mosaic deleted."
        )


        log(
            f"Step 1.2 completed in "
            f"{minutes_since(start)} minutes."
        )


        arcpy.SetParameterAsText(
            2,
            huc12_extent
        )


        arcpy.SetParameterAsText(
            3,
            huc12_dem
        )


        arcpy.SetParameterAsText(
            4,
            "PASS"
        )


    finally:

        # Best-effort cleanup of the internal multi-county mosaic,
        # including runs that fail after the temporary raster is created.
        try:
            if countywide_dem:
                safe_delete(
                    countywide_dem
                )
        except Exception:
            pass

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


    except Exception:

        try:

            arcpy.SetParameterAsText(
                4,
                "FAIL"
            )

        except Exception:

            pass


        raise