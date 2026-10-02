# -*- coding: utf-8 -*-
"""
1.1 Mosaic Tiles, Resample, Reproject
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Purpose
-------
Use a County Processing Folder as the only required context input.

The tool automatically discovers:
    - County Config JSON from <County Folder>/config
    - Project Config JSON from county_config["project_config"]
    - Latest County Validation Report from <County Folder>/logs

It then:
    1) Reads the validated raw DEM tile folder
    2) Validates GDAL-readable raster tiles
    3) Mosaics valid tiles using GDAL BuildVRT + Translate
    4) Reprojects to county production CRS if needed
    5) Resamples to 3-ft using BILINEAR
    6) Validates newly generated final output

ArcGIS Script Tool Parameters
-----------------------------
0  County Processing Folder      Folder          Required Input
1  Overwrite Existing Output     Boolean         Optional Input
2  Output 3-ft DEM               Raster Dataset  Derived Output
3  Processing Status             String          Derived Output
"""

import os
import sys
import glob
import time

import arcpy
from osgeo import gdal

gdal.UseExceptions()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from setup_common import (
    log,
    warn,
    fail,
    ensure_folder,
    load_json,
)

# ---------------------------------------------------------------------
# Algorithm constants
# ---------------------------------------------------------------------

TARGET_CELL_SIZE_FT = 3.0
EXTENSIONS = ("*.tif", "*.tiff", "*.img")
ABNORMAL_ELEVATION_THRESHOLD = 10000
MAX_INVALID_TILE_PRINT = 20


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
        return text in {"true", "1", "yes", "y"}


def minutes_since(t0):
    return round((time.time() - t0) / 60.0, 2)


# ---------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------

def discover_county_config(county_folder):
    config_folder = os.path.join(county_folder, "config")

    if not os.path.isdir(config_folder):
        fail(
            f"County config folder does not exist: {config_folder}"
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
            f"No county config JSON found in: {config_folder}"
        )

    if len(candidates) > 1:
        fail(
            f"Expected exactly one county config JSON in "
            f"{config_folder}, but found {len(candidates)}:\n"
            + "\n".join(f" - {x}" for x in candidates)
        )

    return candidates[0]


def discover_latest_validation_report(county_folder, county_abbr):
    log_folder = os.path.join(county_folder, "logs")

    if not os.path.isdir(log_folder):
        fail(
            f"County log folder does not exist: {log_folder}"
        )

    pattern = os.path.join(
        log_folder,
        f"{county_abbr}_input_validation_*.json"
    )

    candidates = glob.glob(pattern)

    if not candidates:
        fail(
            f"No county validation report found matching:\n{pattern}"
        )

    # Newest modification time first.
    candidates.sort(
        key=os.path.getmtime,
        reverse=True
    )

    # Return first report that is structurally readable.
    for candidate in candidates:
        try:
            report = load_json(candidate)

            status = (
                report.get("validation_status")
                or
                report.get("status")
            )

            if status in {"PASS", "WARN"}:
                return candidate, report

        except Exception:
            continue

    fail(
        f"No usable PASS/WARN county validation report found in: "
        f"{log_folder}"
    )


def load_processing_context(county_folder):
    """
    Discover and validate:
      county config
      project config
      latest validation report
    """

    county_config_path = discover_county_config(
        county_folder
    )

    county = load_json(
        county_config_path
    )

    if "county" not in county:
        fail(
            f"County config missing 'county' section: "
            f"{county_config_path}"
        )

    county_info = county["county"]

    county_name = str(
        county_info.get("name", "")
    ).strip().title()

    county_abbr = str(
        county_info.get("abbr", "")
    ).strip().upper()

    if not county_name or not county_abbr:
        fail(
            f"County config is missing county name/abbr: "
            f"{county_config_path}"
        )

    # Ensure selected folder matches county config context.
    folder_name = os.path.basename(
        os.path.normpath(county_folder)
    )

    if (
        folder_name.casefold()
        !=
        county_name.casefold()
    ):
        fail(
            "County Processing Folder / County Config mismatch.\n"
            f"Folder: {county_folder}\n"
            f"County Config says: {county_name} ({county_abbr})"
        )

    project_config_path = county.get(
        "project_config",
        ""
    )

    if not project_config_path:
        fail(
            f"County config does not contain project_config path: "
            f"{county_config_path}"
        )

    if not os.path.isfile(project_config_path):
        fail(
            f"Project Config referenced by County Config does not exist:\n"
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
        report_county.get("abbr", "")
    ).strip().upper()

    if report_abbr and report_abbr != county_abbr:
        fail(
            "County Config / Validation Report mismatch.\n"
            f"County Config: {county_abbr}\n"
            f"Validation Report: {report_abbr}"
        )

    # If report records which config it came from, verify it.
    report_project = validation.get(
        "project_config"
    )

    if report_project:
        if (
            os.path.normcase(
                os.path.normpath(report_project)
            )
            !=
            os.path.normcase(
                os.path.normpath(project_config_path)
            )
        ):
            fail(
                "Latest validation report belongs to a different "
                "Project Config."
            )

    report_county_config = validation.get(
        "county_config"
    )

    if report_county_config:
        if (
            os.path.normcase(
                os.path.normpath(report_county_config)
            )
            !=
            os.path.normcase(
                os.path.normpath(county_config_path)
            )
        ):
            fail(
                "Latest validation report belongs to a different "
                "County Config."
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
# Tile handling
# ---------------------------------------------------------------------

def collect_tiles(img_folder):
    rasters = []

    for ext in EXTENSIONS:
        rasters.extend(
            glob.glob(
                os.path.join(
                    img_folder,
                    ext
                )
            )
        )

    return sorted(
        set(rasters)
    )


def validate_tiles(raster_files):
    valid = []
    invalid = []

    for path in raster_files:
        try:
            ds = gdal.Open(
                path,
                gdal.GA_ReadOnly
            )

            if ds is None:
                invalid.append(
                    (
                        path,
                        "GDAL could not open"
                    )
                )
                continue

            if ds.RasterCount < 1:
                invalid.append(
                    (
                        path,
                        "No raster bands"
                    )
                )
                ds = None
                continue

            band = ds.GetRasterBand(1)

            if band is None:
                invalid.append(
                    (
                        path,
                        "Band 1 missing"
                    )
                )
                ds = None
                continue

            ds = None
            valid.append(path)

        except Exception as exc:
            invalid.append(
                (
                    path,
                    str(exc)
                )
            )

    return valid, invalid


def print_invalid_tiles(invalid_tiles):
    if not invalid_tiles:
        return

    warn(
        f"Invalid / unsupported tiles skipped: "
        f"{len(invalid_tiles)}"
    )

    for path, reason in invalid_tiles[
        :MAX_INVALID_TILE_PRINT
    ]:
        warn(
            f"  {os.path.basename(path)} | "
            f"{reason}"
        )

    if (
        len(invalid_tiles)
        >
        MAX_INVALID_TILE_PRINT
    ):
        warn(
            f"  ... and "
            f"{len(invalid_tiles) - MAX_INVALID_TILE_PRINT} more"
        )


# ---------------------------------------------------------------------
# Raster QA
# ---------------------------------------------------------------------

def get_raster_min_max(raster_path):
    ds = gdal.Open(
        raster_path,
        gdal.GA_ReadOnly
    )

    if ds is None:
        fail(
            f"Cannot open raster: "
            f"{raster_path}"
        )

    band = ds.GetRasterBand(1)

    if band is None:
        ds = None

        fail(
            f"Raster has no band 1: "
            f"{raster_path}"
        )

    stats = band.GetStatistics(
        False,
        True
    )

    if stats is None:
        ds = None

        fail(
            f"Failed to compute raster statistics: "
            f"{raster_path}"
        )

    minimum = float(
        stats[0]
    )

    maximum = float(
        stats[1]
    )

    ds = None

    return minimum, maximum


def warn_if_abnormal(
    minimum,
    maximum,
    stage_label
):
    if (
        abs(minimum)
        >
        ABNORMAL_ELEVATION_THRESHOLD
        or
        abs(maximum)
        >
        ABNORMAL_ELEVATION_THRESHOLD
    ):
        warn(
            f"{stage_label} elevation range looks abnormal: "
            f"min={minimum:.3f}, "
            f"max={maximum:.3f}"
        )


# ---------------------------------------------------------------------
# GDAL mosaic
# ---------------------------------------------------------------------

def gdal_mosaic_to_single_tif(
    raster_files,
    out_tif,
    work_dir
):
    ensure_folder(
        work_dir
    )

    vrt_path = os.path.join(
        work_dir,
        "step_1_1_temp_mosaic.vrt"
    )

    if os.path.exists(
        vrt_path
    ):
        try:
            os.remove(
                vrt_path
            )
        except Exception:
            pass

    if os.path.exists(
        out_tif
    ):
        try:
            os.remove(
                out_tif
            )
        except Exception:
            pass

    log(
        "GDAL BuildVRT..."
    )

    vrt = gdal.BuildVRT(
        vrt_path,
        raster_files
    )

    if vrt is None:
        fail(
            "gdal.BuildVRT failed."
        )

    vrt = None

    log(
        "GDAL Translate VRT -> "
        "temporary Float32 GeoTIFF..."
    )

    out_ds = gdal.Translate(
        out_tif,
        vrt_path,
        options=gdal.TranslateOptions(
            format="GTiff",
            creationOptions=[
                "COMPRESS=LZW",
                "TILED=YES",
                "BIGTIFF=YES",
            ],
            outputType=gdal.GDT_Float32,
        )
    )

    if out_ds is None:
        fail(
            "gdal.Translate failed."
        )

    out_ds = None

    try:
        if os.path.exists(
            vrt_path
        ):
            os.remove(
                vrt_path
            )

    except Exception:
        pass


# ---------------------------------------------------------------------
# Reproject / resample
# ---------------------------------------------------------------------

def get_factory_code(
    raster_path
):
    try:
        sr = (
            arcpy.Describe(
                raster_path
            )
            .spatialReference
        )

        code = getattr(
            sr,
            "factoryCode",
            None
        )

        if code in (
            None,
            0,
        ):
            return None

        return int(code)

    except Exception:
        return None


def project_or_resample(
    in_raster,
    out_raster,
    target_wkid
):
    arcpy.env.overwriteOutput = True

    source_wkid = (
        get_factory_code(
            in_raster
        )
    )

    log(
        f"Source WKID: "
        f"{source_wkid if source_wkid else 'Unknown'}"
    )

    log(
        f"Target WKID: "
        f"{target_wkid}"
    )

    if arcpy.Exists(
        out_raster
    ):
        arcpy.management.Delete(
            out_raster
        )

    if (
        source_wkid
        !=
        int(target_wkid)
    ):

        log(
            "ProjectRaster: reproject + "
            "resample to 3 ft..."
        )

        arcpy.management.ProjectRaster(
            in_raster=in_raster,
            out_raster=out_raster,
            out_coor_system=arcpy.SpatialReference(
                int(target_wkid)
            ),
            resampling_type="BILINEAR",
            cell_size=TARGET_CELL_SIZE_FT,
            geographic_transform=""
        )

    else:

        log(
            "CRS already matches target. "
            "Resampling to 3 ft..."
        )

        arcpy.management.Resample(
            in_raster=in_raster,
            out_raster=out_raster,
            cell_size=TARGET_CELL_SIZE_FT,
            resampling_type="BILINEAR"
        )



def inspect_existing_output(
    out_raster,
    target_wkid
):
    """
    Inspect an existing output WITHOUT raising an exception.

    Returns:
        {
            "valid": bool,
            "wkid": int|None,
            "cell_x": float|None,
            "cell_y": float|None,
            "issues": [str, ...]
        }

    This is used only for pre-existing outputs when Overwrite=False.
    A non-compliant existing output is a workflow condition, not a
    processing failure, so the caller can WARN and return cleanly.
    """

    result = {
        "valid": False,
        "wkid": None,
        "cell_x": None,
        "cell_y": None,
        "issues": [],
    }

    if not (
        arcpy.Exists(out_raster)
        or
        os.path.exists(out_raster)
    ):
        result["issues"].append(
            "Existing output does not exist."
        )
        return result

    try:
        desc = arcpy.Describe(
            out_raster
        )

        sr = desc.spatialReference

        wkid = getattr(
            sr,
            "factoryCode",
            None
        )

        result["wkid"] = wkid

    except Exception as exc:
        result["issues"].append(
            f"Could not read spatial reference: {exc}"
        )

    try:
        raster = arcpy.Raster(
            out_raster
        )

        cell_x = float(
            raster.meanCellWidth
        )

        cell_y = float(
            raster.meanCellHeight
        )

        result["cell_x"] = cell_x
        result["cell_y"] = cell_y

    except Exception as exc:
        result["issues"].append(
            f"Could not read raster cell size: {exc}"
        )

    if (
        result["wkid"]
        !=
        int(target_wkid)
    ):
        result["issues"].append(
            f"WKID {result['wkid']} != required {target_wkid}"
        )

    if (
        result["cell_x"] is None
        or
        result["cell_y"] is None
    ):
        result["issues"].append(
            "Raster cell size could not be validated."
        )

    else:
        if (
            abs(
                result["cell_x"]
                -
                TARGET_CELL_SIZE_FT
            )
            >
            0.05
            or
            abs(
                result["cell_y"]
                -
                TARGET_CELL_SIZE_FT
            )
            >
            0.05
        ):
            result["issues"].append(
                f"Cell size "
                f"({result['cell_x']}, {result['cell_y']}) "
                f"!= ~{TARGET_CELL_SIZE_FT} ft"
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
# Final output QC
# ---------------------------------------------------------------------

def validate_final_output(
    out_raster,
    target_wkid
):
    if not arcpy.Exists(
        out_raster
    ):
        fail(
            f"Final 3-ft DEM was not created: "
            f"{out_raster}"
        )

    desc = arcpy.Describe(
        out_raster
    )

    sr = desc.spatialReference

    wkid = getattr(
        sr,
        "factoryCode",
        None
    )

    raster = arcpy.Raster(
        out_raster
    )

    cell_x = float(
        raster.meanCellWidth
    )

    cell_y = float(
        raster.meanCellHeight
    )

    if (
        wkid
        !=
        int(target_wkid)
    ):
        fail(
            f"Final DEM WKID={wkid}, "
            f"expected {target_wkid}: "
            f"{out_raster}"
        )

    if (
        abs(
            cell_x
            -
            TARGET_CELL_SIZE_FT
        )
        >
        0.05
        or
        abs(
            cell_y
            -
            TARGET_CELL_SIZE_FT
        )
        >
        0.05
    ):
        fail(
            f"Final DEM cell size "
            f"X={cell_x}, Y={cell_y}; "
            f"expected ~3 ft."
        )

    log(
        f"PASS - Final DEM "
        f"WKID={wkid}, "
        f"cell size=({cell_x}, {cell_y})"
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

    # -------------------------------------------------------------
    # Discover processing context automatically
    # -------------------------------------------------------------

    context = (
        load_processing_context(
            county_folder
        )
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

    county_name = county_info[
        "name"
    ]

    county_abbr = county_info[
        "abbr"
    ]

    target_wkid = int(
        county_info[
            "wkid"
        ]
    )

    log("=" * 72)

    log(
        "1.1 Mosaic Tiles, "
        "Resample, Reproject"
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
    # Read resolved input from latest 0.3 report
    # -------------------------------------------------------------

    resolved = (
        validation.get(
            "resolved_inputs"
        )
        or
        validation.get(
            "resolved_candidates"
        )
        or
        {}
    )

    raw_tile_folder = (
        resolved.get(
            "raw_dem_tile_folder"
        )
    )

    if not raw_tile_folder:

        fail(
            "Latest County Validation Report does not "
            "contain resolved raw_dem_tile_folder."
        )

    if not os.path.isdir(
        raw_tile_folder
    ):
        fail(
            f"Validated raw DEM tile folder is "
            f"no longer available: "
            f"{raw_tile_folder}"
        )


    # -------------------------------------------------------------
    # Output root from project config
    # -------------------------------------------------------------

    try:
        dem_3ft_root = (
            project["sources"]
            ["dem_3ft_root"]
        )

    except Exception:

        fail(
            "Project Config does not contain "
            "sources > dem_3ft_root."
        )

    if not os.path.isdir(
        dem_3ft_root
    ):
        fail(
            f"DEM 3-ft Root no longer exists: "
            f"{dem_3ft_root}"
        )


    clean_county_name = (
        county_name
        .replace(
            " ",
            "_"
        )
        .replace(
            ".",
            ""
        )
    )


    final_tif = os.path.join(
        dem_3ft_root,
        f"{clean_county_name}_DEM_3ft.tif"
    )


    # -------------------------------------------------------------
    # Existing output behavior
    # -------------------------------------------------------------

    if (
        arcpy.Exists(
            final_tif
        )
        or
        os.path.exists(
            final_tif
        )
    ):

        if not overwrite:

            existing_check = (
                inspect_existing_output(
                    final_tif,
                    target_wkid
                )
            )


            if existing_check[
                "valid"
            ]:

                log(
                    "Existing 3-ft DEM meets current "
                    "production requirements."
                )

                log(
                    f"Reusing existing output: "
                    f"{final_tif}"
                )

                arcpy.SetParameterAsText(
                    2,
                    final_tif
                )

                arcpy.SetParameterAsText(
                    3,
                    "SKIPPED_EXISTING_VALID"
                )

                return


            # -----------------------------------------------------
            # IMPORTANT UX RULE:
            #
            # Existing non-compliant output + Overwrite=False
            # is NOT a processing failure.
            #
            # Warn clearly, set status, and return normally.
            # Do not raise, do not create traceback, and do not
            # show "Failed script".
            # -----------------------------------------------------

            warn(
                "Existing 3-ft DEM does not meet current "
                "production requirements. "
                f"Existing WKID: "
                f"{existing_check['wkid']}; "
                f"Existing cell size: "
                f"({existing_check['cell_x']}, "
                f"{existing_check['cell_y']}); "
                f"Required WKID: {target_wkid}; "
                f"Required cell size: "
                f"~{TARGET_CELL_SIZE_FT} ft."
            )

            warn(
                "Step 1.1 was not run because "
                "Overwrite Existing Output = False. "
                "Enable Overwrite Existing Output "
                "to regenerate the DEM."
            )

            arcpy.SetParameterAsText(
                2,
                final_tif
            )

            arcpy.SetParameterAsText(
                3,
                "SKIPPED_INVALID_EXISTING"
            )

            return


        log(
            "Overwrite=True. "
            f"Existing output will be replaced: "
            f"{final_tif}"
        )

    # -------------------------------------------------------------
    # County-specific scratch
    # -------------------------------------------------------------

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


    temporary_mosaic = os.path.join(
        scratch_root,
        (
            f"{county_abbr}_"
            f"DEM_countywide_mosaic_tmp.tif"
        )
    )


    # -------------------------------------------------------------
    # Discover / validate tiles
    # -------------------------------------------------------------

    raster_files = collect_tiles(
        raw_tile_folder
    )


    if not raster_files:

        fail(
            f"No raster tiles found in: "
            f"{raw_tile_folder}"
        )


    log(
        f"Found {len(raster_files)} "
        f"candidate raster tiles."
    )


    valid_tiles, invalid_tiles = (
        validate_tiles(
            raster_files
        )
    )


    print_invalid_tiles(
        invalid_tiles
    )


    if not valid_tiles:

        fail(
            "No valid DEM tiles remained "
            "after validation."
        )


    log(
        f"Valid DEM tiles: "
        f"{len(valid_tiles)}"
    )


    # -------------------------------------------------------------
    # Tile elevation QA
    # -------------------------------------------------------------

    tile_min = float(
        "inf"
    )

    tile_max = float(
        "-inf"
    )


    log(
        "Checking elevation range "
        "across valid tiles..."
    )


    for raster_path in valid_tiles:

        minimum, maximum = (
            get_raster_min_max(
                raster_path
            )
        )

        tile_min = min(
            tile_min,
            minimum
        )

        tile_max = max(
            tile_max,
            maximum
        )


    log(
        f"Tile elevation range: "
        f"min={tile_min:.3f}, "
        f"max={tile_max:.3f}"
    )


    warn_if_abnormal(
        tile_min,
        tile_max,
        "Tile"
    )


    # -------------------------------------------------------------
    # Mosaic
    # -------------------------------------------------------------

    log(
        "Creating county-wide "
        "temporary mosaic..."
    )


    gdal_mosaic_to_single_tif(
        raster_files=valid_tiles,
        out_tif=temporary_mosaic,
        work_dir=scratch_root
    )


    mosaic_min, mosaic_max = (
        get_raster_min_max(
            temporary_mosaic
        )
    )


    log(
        f"Mosaic elevation range: "
        f"min={mosaic_min:.3f}, "
        f"max={mosaic_max:.3f}"
    )


    warn_if_abnormal(
        mosaic_min,
        mosaic_max,
        "Mosaic"
    )


    # -------------------------------------------------------------
    # Reproject / resample
    # -------------------------------------------------------------

    project_or_resample(
        in_raster=temporary_mosaic,
        out_raster=final_tif,
        target_wkid=target_wkid
    )


    # -------------------------------------------------------------
    # Final QA
    # -------------------------------------------------------------

    validate_final_output(
        final_tif,
        target_wkid
    )


    final_min, final_max = (
        get_raster_min_max(
            final_tif
        )
    )


    log(
        f"Final elevation range: "
        f"min={final_min:.3f}, "
        f"max={final_max:.3f}"
    )


    warn_if_abnormal(
        final_min,
        final_max,
        "Final"
    )


    # -------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------

    try:

        if os.path.exists(
            temporary_mosaic
        ):
            os.remove(
                temporary_mosaic
            )

    except Exception as exc:

        warn(
            f"Could not delete "
            f"temporary mosaic: {exc}"
        )


    log(
        f"Step 1.1 completed in "
        f"{minutes_since(start)} minutes."
    )


    log(
        f"Output 3-ft DEM: "
        f"{final_tif}"
    )


    arcpy.SetParameterAsText(
        2,
        final_tif
    )


    arcpy.SetParameterAsText(
        3,
        "PASS"
    )


# ---------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------

if __name__ == "__main__":

    try:

        main()

    except Exception:

        try:

            arcpy.SetParameterAsText(
                3,
                "FAIL"
            )

        except Exception:

            pass

        raise