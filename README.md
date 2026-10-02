# LiDAR Stream Graph

A production geospatial processing workflow for deriving hydrologically conditioned terrain, delineating streams and catchments from LiDAR-derived elevation data, and generating enriched stream segments for hydrologic network and graph analysis.

## Overview

LiDAR Stream Graph organizes a repeatable ArcGIS Pro workflow around the following processing sequence:

```text
LiDAR-derived DEM
        ↓
DEM preparation and reprojection
        ↓
Transportation infrastructure conditioning
        ↓
Hydrologic reconditioning
        ↓
Fill → Flow Direction → Flow Accumulation
        ↓
Stream delineation
        ↓
Catchment delineation
        ↓
Stream characteristics and enrichment
        ↓
Confidence / decision-support attributes
        ↓
Enriched stream network
```

The repository preserves the processing workflow as versioned source code so changes to hydrologic processing, validation, automation, and network enrichment can be reviewed through Git history.

## Baseline release

The initial repository baseline represents the production county-processing workflow before introduction of the single-HUC12 Test Mode. It is retained as the comparison point for subsequent workflow generalization and QC updates.

## Repository structure

- `scripts/` — ArcGIS Pro / ArcPy processing scripts and shared utilities.
- `toolbox/` — ArcGIS Pro toolbox.
- `docs/` — technical guidance and workflow documentation.

## Processing stages

- **0 — Configuration and validation:** configure shared project sources, initialize a processing area, and validate required inputs.
- **1 — DEM preparation:** mosaic, resample, reproject, and prepare the DEM for the hydrologic processing extent.
- **2 — Hydrologic conditioning:** apply transportation-infrastructure conditioning, DEM reconditioning, fill, flow direction, and flow accumulation.
- **3 — Stream network generation and enrichment:** generate streams and catchments, calculate stream characteristics and confidence attributes, and package deliverables.

## Environment

The workflow is designed for ArcGIS Pro and uses Python/ArcPy. Some DEM preparation operations may also rely on GDAL available in the processing environment.

## Versioning approach

Major workflow updates are committed against the same source paths rather than stored as duplicate old/new script folders. This allows GitHub to show line-level changes between production baselines, Test Mode development, QC fixes, and future enhancements.

## Data

Source elevation, transportation, land-cover, hydrography, and other project datasets are not stored in this repository. Paths to external datasets are supplied through workflow configuration.

## Status

**Production baseline:** pre-Test-Mode county workflow.

A later revision will add the single-HUC12 Test Mode and shared production/test processing context after QC is complete.
