# ArcGIS Pro Toolbox Configuration — Production Baseline

This folder documents the ArcGIS Pro script toolbox configuration for the **pre-Test-Mode production baseline**. The original internal `.atbx` and internal DOCX guidance are intentionally not stored in this public repository. Executable Python sources are versioned under `../scripts/`.

> Parameter order is part of the script interface because the Python tools use `arcpy.GetParameterAsText(index)` / `arcpy.GetParameter(index)`. Keep the indices exactly as documented.

## Workflow

```text
0.1 Configure Project Sources
  → 0.2 Initialize County
  → 0.3 Validate County Inputs
  → 1.1 Mosaic Tiles, Resample, Reproject
  → 1.2 Create HUC12 Extent DEM
  → 2.1 Apply Transportation Infrastructure Conditioning
  → 2.2 DEM Hydrologic Reconditioning
  → 2.3 Fill, Flow Direction, Flow Accumulation
  → 3.1 Generate Streams & Catchments
  → 3.2 Generate Stream Characteristics
  → 3.3 Calculate Stream JD Confidence
  → 3.4 Package Final Deliverables
```

## 0.1 Configure Project Sources

**Script:** `scripts/step_0_1_configure_project_sources.py`

Creates a timestamped project configuration JSON and validates shared production data sources. `WBD_HUC12` is resolved automatically from the Tools Source GDB and is not a separate UI parameter.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Project Config Folder | Folder | Required | Input |
| 1 | Ohio Counties Reference | Feature Layer | Required | Input |
| 2 | DEM by County Root | Folder | Required | Input |
| 3 | DEM 3-ft Root | Folder | Required | Input |
| 4 | Streams by County Root | Folder | Required | Input |
| 5 | Tools Source GDB | Workspace | Required | Input |
| 6 | NHD Flowline Raster Folder | Folder | Required | Input |
| 7 | Final Deliverable Root | Folder | Required | Input |
| 8 | Scratch Root | Folder | Optional | Input |
| 9 | Project Config File | File | Derived | Output |
| 10 | Validation Status | String | Derived | Output |

Baseline validation expects county fields `COUNTY`, `COUNTY_CD`, `STATE_PLAN`, and `geomorphic`, the shared Tools GDB datasets referenced by source, and all four North/South NHD raster variants.

## 0.2 Initialize County

**Script:** `scripts/step_0_2_initialize_county.py`

Creates the county-specific production configuration. County lookup is case-insensitive.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Project Config File | File | Required | Input |
| 1 | County Name | String | Required | Input |
| 2 | County Config File | File | Derived | Output |
| 3 | County Abbreviation | String | Derived | Output |
| 4 | Validation Status | String | Derived | Output |

The county configuration records county abbreviation, State Plane zone, geomorphic class, target WKID, and selected NHD raster.

## 0.3 Validate County Inputs

**Script:** `scripts/step_0_3_validate_county_inputs.py`

Validates pre-processing inputs. HUC12 processing is intentionally deferred to Step 1.2.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Project Config File | File | Required | Input |
| 1 | County Config File | File | Required | Input |
| 2 | Raw DEM Tile Folder | Folder | Optional | Input |
| 3 | Validation Report | File | Derived | Output |
| 4 | Validation Status | String | Derived | Output |

## 1.1 Mosaic Tiles, Resample, Reproject

**Script:** `scripts/step_1_1_mosaic_resample_reproject.py`

Discovers county configuration and validation context, mosaics source DEM tiles with GDAL, and produces the standardized 3-ft county DEM.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | County Processing Folder | Folder | Required | Input |
| 1 | Overwrite Existing Output | Boolean | Optional | Input |
| 2 | Output 3-ft DEM | Raster Dataset | Derived | Output |
| 3 | Processing Status | String | Derived | Output |

**Processing setting:** target cell size = **3 ft**; reprojection/resampling uses **BILINEAR**.

## 1.2 Create HUC12 Extent DEM

**Script:** `scripts/step_1_2_create_huc12_extent_dem.py`

Selects complete HUC12 polygons intersecting the target county, resolves standardized DEM coverage from counties touched by those HUC12 polygons, builds the temporary multi-county mosaic, and extracts the retained HUC12 DEM.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | County Processing Folder | Folder | Required | Input |
| 1 | Overwrite Existing Outputs | Boolean | Optional | Input |
| 2 | HUC12 Processing Extent | Feature Class | Derived | Output |
| 3 | HUC12 DEM | Raster Dataset | Derived | Output |
| 4 | Processing Status | String | Derived | Output |

Reusable outputs are stored under `<County Processing Folder>\raster\`. The multi-county DEM mosaic is temporary.

## 2.1 Apply Transportation Infrastructure Conditioning

**Script:** `scripts/step_2_1_infrastructure_conditioning.py`

Starts the timestamped Processing Run and applies transportation infrastructure conditioning to the HUC12 DEM.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | County Processing Folder | Folder | Required | Input |
| 1 | Overwrite Existing Output | Boolean | Optional | Input |
| 2 | Processing Run Folder | Folder | Derived | Output |
| 3 | Conditioned DEM | Raster Dataset | Derived | Output |
| 4 | Processing Status | String | Derived | Output |

Baseline algorithm settings: source `TIMS_road_rail_conduit_bridge_buffer`, zone field `SFN`, Zonal Statistics `MINIMUM`, 3-ft target cell size.

## 2.2 DEM Hydrologic Reconditioning

**Script:** `scripts/step_2_2_dem_reconditioning.py`

Runs the AGREE-style hydrologic reconditioning stage using the county-selected NHD raster.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Coarse DEM Factor | Long | Optional | Input |
| 2 | Buffer Cells | Long | Optional | Input |
| 3 | Smooth Drop | Long | Optional | Input |
| 4 | Sharp Drop | Long | Optional | Input |
| 5 | Overwrite Existing Output | Boolean | Optional | Input |
| 6 | Reconditioned DEM | Raster Dataset | Derived | Output |
| 7 | Processing Status | String | Derived | Output |

**Baseline defaults:** Coarse DEM Factor = **2**; Buffer Cells = **5**; Smooth Drop = **10**; Sharp Drop = **100**.

## 2.3 Fill, Flow Direction, Flow Accumulation

**Script:** `scripts/step_2_3_fill_fd_fa.py`

Creates the filled DEM, flow-direction raster, and flow-accumulation raster.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Overwrite Existing Outputs | Boolean | Optional | Input |
| 2 | Filled DEM | Raster Dataset | Derived | Output |
| 3 | Flow Direction Raster | Raster Dataset | Derived | Output |
| 4 | Flow Accumulation Raster | Raster Dataset | Derived | Output |
| 5 | Processing Status | String | Derived | Output |

## 3.1 Generate Streams & Catchments

**Script:** `scripts/step_3_1_stream_catchment.py`

Generates stream and catchment families for one or more drainage thresholds.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Flow Direction Raster | Raster Dataset | Required | Input |
| 2 | Flow Accumulation Raster | Raster Dataset | Required | Input |
| 3 | Drainage Thresholds | Long | Required, MultiValue | Input |
| 4 | Output Streams Working GDB | Workspace | Required | Output |
| 5 | Overwrite Existing Families | Boolean | Optional | Input |
| 6 | Streams Working GDB | Workspace | Derived | Output |
| 7 | Processing Status | String | Derived | Output |

**Recommended production thresholds:** `5000;10000;20000`.

Expected family names include `<County>_Stream_5k`, `<County>_Catchment_5k`, and equivalent 10k/20k families.

## 3.2 Generate Stream Characteristics

**Script:** `scripts/step_3_2_generate_characteristics.py`

Enriches stream segments with drainage-area, land-cover, and terrain characteristics required by confidence calculation.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Streams Working GDB | Workspace | Required | Input |
| 2 | Original HUC12 DEM | Raster Dataset | Required | Input |
| 3 | NLCD Raster | Raster Dataset | Required | Input |
| 4 | County Boundary | Feature Layer | Required | Input |
| 5 | Thresholds to Process | String | Required, MultiValue | Input |
| 6 | Overwrite Existing Characteristics | Boolean | Optional | Input |
| 7 | Characteristics Output GDB | Workspace | Derived | Output |
| 8 | Processing Status | String | Derived | Output |

Baseline processing calculates local drainage area, cumulative drainage area, NLCD majority/type/importance, and DEM slope using the **original unprocessed HUC12 DEM**. Outputs use `_w_characteristics`.

## 3.3 Calculate Stream JD Confidence

**Script:** `scripts/step_3_3_calculate_jd_confidence.py`

Calculates rule-based confidence attributes for stream features already enriched by Step 3.2.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Streams Working GDB | Workspace | Required | Input |
| 2 | Thresholds to Process | String | Required, MultiValue | Input |
| 3 | Major River Buffer | Feature Layer | Required | Input |
| 4 | Lake / Wetland Buffer | Feature Layer | Required | Input |
| 5 | Apply Downstream Propagation | Boolean | Optional | Input |
| 6 | Erase Streams Within Water Buffers | Boolean | Optional | Input |
| 7 | Overwrite Existing JD | Boolean | Optional | Input |
| 8 | JD Output GDB | Workspace | Derived | Output |
| 9 | Processing Status | String | Derived | Output |

The tool adds/updates `CDA_Prob`, `Land_Use_Prob`, `DEM_Prob`, and `JD_Confidence`; applies the fixed methodology lookup matrix; optionally propagates confidence downstream and erases stream portions within configured water buffers; and writes `_w_characteristics_JD` outputs.

## 3.4 Package Final Deliverables

**Script:** `scripts/step_3_4_package_final_deliverables.py`

Packages one explicit Processing Run into standardized DEM and stream deliverable geodatabases.

| # | Parameter | Data type | Requirement | Direction |
|---|---|---|---|---|
| 0 | Processing Run Folder | Folder | Required | Input |
| 1 | Original HUC12 DEM | Raster Dataset | Required | Input |
| 2 | Processed / Reconditioned DEM | Raster Dataset | Required | Input |
| 3 | Streams Working GDB | Workspace | Required | Input |
| 4 | Final Deliverable Root | Folder | Required | Input |
| 5 | Thresholds to Package | String | Required, MultiValue | Input |
| 6 | Include Base Streams/Catchments | Boolean | Optional | Input |
| 7 | Include Characteristics | Boolean | Optional | Input |
| 8 | Include JD Confidence | Boolean | Optional | Input |
| 9 | Overwrite Existing DEMs | Boolean | Optional | Input |
| 10 | Overwrite Existing Families | Boolean | Optional | Input |
| 11 | Preview Only | Boolean | Optional | Input |
| 12 | Final County Folder | Folder | Derived | Output |
| 13 | Final DEM GDB | Workspace | Derived | Output |
| 14 | Final Streams GDB | Workspace | Derived | Output |
| 15 | Packaging Status | String | Derived | Output |

Baseline code defaults the three include switches to **True** when unset, overwrite switches to **False**, and Preview Only to **True**.

```text
<Final Deliverable Root>/<County>/
    <County>_DEM.gdb
        <County>_DEM_HUC12
        <County>_DEM_HUC12_Processed

    <County>_Streams.gdb
        <County>_Stream_<threshold>
        <County>_Catchment_<threshold>
        <County>_Stream_<threshold>_w_characteristics
        <County>_Stream_<threshold>_w_characteristics_JD
```

## Recreating the ArcGIS Pro toolbox

1. Create a new ArcGIS Pro toolbox.
2. Add one **Script** tool for each processing step above.
3. Point each tool to its corresponding Python file under `scripts/`.
4. Add parameters in the exact numeric order shown here.
5. Match the documented ArcGIS data type, Required/Optional/Derived setting, and Input/Output direction.
6. Enable **MultiValue** for threshold parameters where documented.
7. Use the documented production defaults for Step 2.2.
8. Use `5000;10000;20000` as the normal Step 3.1 production threshold set.
9. Preserve the script directory relationship so every processing script can import `setup_common.py`.

## Baseline note

This document describes the initial production source snapshot only. It intentionally does **not** describe the later single-HUC12 Test Mode or generalized Production/Test processing context. Those changes belong in a later Git revision so GitHub can show the evolution of both toolbox configuration and source code.