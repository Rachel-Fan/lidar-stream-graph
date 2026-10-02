# -*- coding: utf-8 -*-
"""
3.3 Calculate JD Confidence
ODOT Ohio Stream Delineation - ArcGIS Pro Script Tool

Refactored from:
    Calculate-JD-Confidence-input-gdb-folder-1209.py

IMPORTANT DEPENDENCY
--------------------
This tool operates on stream feature classes that ALREADY contain the
characteristic fields produced by Generate Characteristics:

    *_w_characteristics

Therefore, with the current numbering:
    3.3 Generate Characteristics
must be completed before:
    3.3 Calculate JD Confidence

If characteristic feature classes are not present, this tool issues WARN
messages and exits cleanly. It does not attempt to infer or recreate missing
characteristic fields.

ArcGIS Script Tool Parameters
-----------------------------
0  Processing Run Folder                 Folder          Required Input
1  Streams Working GDB                   Workspace       Required Input
2  Thresholds to Process                 String          Required Input, MultiValue
3  Major River Buffer                    Feature Layer   Required Input
4  Lake / Wetland Buffer                 Feature Layer   Required Input
5  Apply Downstream Propagation          Boolean         Optional Input
6  Erase Streams Within Water Buffers    Boolean         Optional Input
7  Overwrite Existing JD                 Boolean         Optional Input
8  JD Output GDB                         Workspace       Derived Output
9  Processing Status                     String          Derived Output

Methodology note
----------------
The JD lookup matrix itself remains fixed in the script because it represents
the approved methodology rather than a routine operator choice.


Processing
----------
For each base feature class ending in:
    _w_characteristics

and not already ending in:
    _w_characteristics_JD

the tool:
1. Adds/updates:
       CDA_Prob
       Land_Use_Prob
       DEM_Prob
       JD_Confidence
2. Classifies cumulative drainage area.
3. Classifies NLCD land use.
4. Classifies DEM slope.
5. Applies the rule-based JD lookup matrix.
6. Propagates confidence downstream so downstream reaches cannot have
   a lower confidence rank than contributing upstream reaches.
7. Erases stream portions inside major-river and lake buffers.
8. Saves:
       *_w_characteristics_JD

All outputs stay inside the SAME Processing Run / Streams Working GDB.
"""

import os
import sys
import time
import datetime
from collections import defaultdict, deque

import arcpy


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
    save_json,
    load_json,
    init_tool_log,
    finish_tool_log,
    log_step_start,
    log_step_end,
    log_exception,
)


IN_MEMORY = "in_memory"

RIVER_BUFFER_NAME = (
    "OH_NHD_5m_River_buffer_500ft_w_Ohio_Maumee"
)

LAKE_BUFFER_NAME = (
    "OH_Wetlands_Lakes_Buffer_200ft"
)

JD_FIELDS = [
    ("CDA_Prob", "TEXT", 50),
    ("Land_Use_Prob", "TEXT", 50),
    ("DEM_Prob", "TEXT", 50),
    ("JD_Confidence", "TEXT", 50),
]

CONFIDENCE_RANK = {
    "1-10%": 0,
    "10-25%": 1,
    "25-50%": 2,
    "50-75%": 3,
    "75-95%": 4,
    "96-100%": 5,
}

RANK_TO_CONFIDENCE = {
    value: key
    for key, value in CONFIDENCE_RANK.items()
}


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


def parse_threshold_tokens(text):

    allowed = {
        "5k",
        "10k",
        "20k",
    }

    if not text:
        return [
            "5k",
            "10k",
            "20k",
        ]

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

        if token not in allowed:
            fail(
                f"Unsupported threshold '{token}'. "
                "Allowed values: 5k, 10k, 20k."
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

    return {
        "run_folder": run_folder,
        "run_config_path": run_config_path,
        "run_config": run_config,
        "run_id": run_config.get("run_id"),
        "county_name": county_name,
        "county_abbr": county_abbr,
        "county": county,
        "project": project,
    }


def update_run_config(
    context,
    status,
    results,
    working_gdb,
    thresholds=None,
    apply_downstream_propagation=True,
    erase_water_buffers=True
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
        "3.3"
    ] = {
        "status": status,
        "working_gdb": working_gdb,
        "thresholds": thresholds or [],
        "apply_downstream_propagation": apply_downstream_propagation,
        "erase_streams_within_water_buffers": erase_water_buffers,
        "results": results,
    }

    save_json(
        run_config,
        context[
            "run_config_path"
        ]
    )


# ---------------------------------------------------------------------
# Classification helpers - preserved from source
# ---------------------------------------------------------------------

def cda_range(val):
    try:
        val = float(val)
    except (ValueError, TypeError):
        return "Unknown"

    if val < 0.1:
        return "0-0.1"
    elif val < 0.25:
        return "0.1-0.25"
    elif val < 0.5:
        return "0.25-0.5"
    elif val < 0.75:
        return "0.5-0.75"
    elif val < 1:
        return "0.75-1"
    elif val < 20:
        return "1-20"
    else:
        return "20+"


def classify_land_use(code):
    try:
        code = int(code)
    except (ValueError, TypeError):
        return "Unknown"

    if code in [21, 22, 71, 81]:
        return "Open Spaces"
    elif code in [23, 24, 31]:
        return "Modified Land"
    elif code in [41, 42, 43, 52, 90]:
        return "Forested"
    elif code in [11, 12, 95]:
        return "Saturated Surface"
    elif code == 82:
        return "Agricultural"

    return "Unknown"


def classify_slope_value(val):
    if val is None:
        return "N/A"

    try:
        value = float(val)
    except (ValueError, TypeError):
        return "N/A"

    if value < 1:
        return "0-1"
    elif value < 2:
        return "1-2"
    elif value < 5:
        return "2-5"
    elif value < 10:
        return "5-10"
    elif value < 20:
        return "10-20"
    else:
        return "20+"


# ---------------------------------------------------------------------
# JD lookup matrix - preserved from source 1209
# ---------------------------------------------------------------------

JD_LOOKUP = {
    "Saturated Surface": {
        "0-0.1": {k: "10-25%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "0.1-0.25": {k: "25-50%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "0.25-0.5": {k: "50-75%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "0.5-0.75": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "0.75-1": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "1-20": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "20+": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
    },

    "Forested": {
        "0-0.1": {"0-1":"1-10%","1-2":"1-10%","2-5":"1-10%","5-10":"1-10%","10-20":"1-10%","20+":"1-10%"},
        "0.1-0.25": {"0-1":"10-25%","1-2":"10-25%","2-5":"10-25%","5-10":"10-25%","10-20":"10-25%","20+":"1-10%"},
        "0.25-0.5": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"50-75%","20+":"25-50%"},
        "0.5-0.75": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"50-75%","20+":"25-50%"},
        "0.75-1": {"0-1":"75-95%","1-2":"75-95%","2-5":"75-95%","5-10":"75-95%","10-20":"75-95%","20+":"75-95%"},
        "1-20": {k: "96-100%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "20+": {k: "96-100%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
    },

    "Open Spaces": {
        "0-0.1": {"0-1":"1-10%","1-2":"1-10%","2-5":"1-10%","5-10":"1-10%","10-20":"1-10%","20+":"1-10%"},
        "0.1-0.25": {"0-1":"10-25%","1-2":"10-25%","2-5":"10-25%","5-10":"10-25%","10-20":"10-25%","20+":"1-10%"},
        "0.25-0.5": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"50-75%","20+":"25-50%"},
        "0.5-0.75": {"0-1":"75-95%","1-2":"75-95%","2-5":"75-95%","5-10":"75-95%","10-20":"50-75%","20+":"25-50%"},
        "0.75-1": {"0-1":"75-95%","1-2":"75-95%","2-5":"75-95%","5-10":"75-95%","10-20":"50-75%","20+":"25-50%"},
        "1-20": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "20+": {k: "96-100%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
    },

    "Agricultural": {
        "0-0.1": {"0-1":"1-10%","1-2":"1-10%","2-5":"1-10%","5-10":"1-10%","10-20":"1-10%","20+":"1-10%"},
        "0.1-0.25": {"0-1":"10-25%","1-2":"10-25%","2-5":"10-25%","5-10":"10-25%","10-20":"10-25%","20+":"1-10%"},
        "0.25-0.5": {"0-1":"25-50%","1-2":"25-50%","2-5":"50-75%","5-10":"50-75%","10-20":"25-50%","20+":"10-25%"},
        "0.5-0.75": {"0-1":"25-50%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"25-50%","20+":"10-25%"},
        "0.75-1": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"25-50%","20+":"25-50%"},
        "1-20": {"0-1":"75-95%","1-2":"75-95%","2-5":"75-95%","5-10":"75-95%","10-20":"50-75%","20+":"50-75%"},
        "20+": {k: "96-100%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
    },

    "Modified Land": {
        "0-0.1": {"0-1":"1-10%","1-2":"1-10%","2-5":"1-10%","5-10":"1-10%","10-20":"1-10%","20+":"1-10%"},
        "0.1-0.25": {"0-1":"10-25%","1-2":"10-25%","2-5":"10-25%","5-10":"10-25%","10-20":"1-10%","20+":"1-10%"},
        "0.25-0.5": {"0-1":"25-50%","1-2":"25-50%","2-5":"25-50%","5-10":"25-50%","10-20":"1-10%","20+":"1-10%"},
        "0.5-0.75": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"1-10%","20+":"1-10%"},
        "0.75-1": {"0-1":"50-75%","1-2":"50-75%","2-5":"50-75%","5-10":"50-75%","10-20":"1-10%","20+":"1-10%"},
        "1-20": {k: "75-95%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
        "20+": {k: "96-100%" for k in ["0-1","1-2","2-5","5-10","10-20","20+"]},
    },
}


def get_jd_confidence(
    land,
    cda,
    slope
):
    return (
        JD_LOOKUP
        .get(land, {})
        .get(cda_range(cda), {})
        .get(slope, "Unknown")
    )


# ---------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------

def get_field_name(
    fc,
    desired,
    alternatives=None,
    required=True
):

    if alternatives is None:
        alternatives = []

    fields = arcpy.ListFields(
        fc
    )

    name_map = {
        field.name.lower():
            field.name
        for field in fields
    }

    if desired.lower() in name_map:
        return name_map[
            desired.lower()
        ]

    for alternative in alternatives:
        if alternative.lower() in name_map:
            return name_map[
                alternative.lower()
            ]

    truncated = desired[:10].lower()

    for field in fields:
        if field.name.lower().startswith(
            truncated
        ):
            return field.name

    if required:
        fail(
            f"Could not find field '{desired}' on {fc}. "
            f"Tried alternatives: {alternatives}."
        )

    return None


def add_jd_fields(
    fc
):

    existing = {
        field.name
        for field in arcpy.ListFields(
            fc
        )
    }

    for name, field_type, length in JD_FIELDS:
        if name not in existing:
            arcpy.management.AddField(
                fc,
                name,
                field_type,
                field_length=length
            )


# ---------------------------------------------------------------------
# Buffer erase
# ---------------------------------------------------------------------

def erase_by_lakes_and_river(
    stream_fc,
    river_buffer,
    lake_buffer,
    apply_erase=True
):

    if not apply_erase:

        log(
            "Water-buffer erase disabled; retaining all stream features."
        )

        return stream_fc


    timestamp = datetime.datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    combined_buffer = os.path.join(
        IN_MEMORY,
        f"combined_buffer_{timestamp}"
    )

    erased_fc = os.path.join(
        IN_MEMORY,
        f"stream_erased_{timestamp}"
    )

    original_count = int(
        arcpy.management.GetCount(
            stream_fc
        )[0]
    )

    log_step_start(
        "Union River and Lake Buffers"
    )

    arcpy.analysis.Union(
        [
            river_buffer,
            lake_buffer,
        ],
        combined_buffer
    )

    log_step_end(
        "Union River and Lake Buffers"
    )

    log_step_start(
        "Erase Streams by Water Buffers"
    )

    arcpy.analysis.Erase(
        stream_fc,
        combined_buffer,
        erased_fc
    )

    log_step_end(
        "Erase Streams by Water Buffers"
    )

    erased_count = int(
        arcpy.management.GetCount(
            erased_fc
        )[0]
    )

    log(
        f"Features before erase: {original_count}; "
        f"after erase: {erased_count}; "
        f"removed: {original_count - erased_count}"
    )

    return erased_fc


# ---------------------------------------------------------------------
# One feature class
# ---------------------------------------------------------------------

def process_one_fc(
    input_fc,
    output_fc,
    river_buffer,
    lake_buffer,
    apply_downstream_propagation=True,
    erase_water_buffers=True
):

    try:
        arcpy.management.Delete(
            IN_MEMORY
        )
    except Exception:
        pass

    memory_fc = os.path.join(
        IN_MEMORY,
        "stream_with_jd_confidence"
    )

    log_step_start(
        f"Copy {os.path.basename(input_fc)} to memory"
    )

    arcpy.management.CopyFeatures(
        input_fc,
        memory_fc
    )

    log_step_end(
        f"Copy {os.path.basename(input_fc)} to memory"
    )

    add_jd_fields(
        memory_fc
    )

    from_node_field = get_field_name(
        memory_fc,
        "from_node",
        alternatives=["from_node"]
    )

    to_node_field = get_field_name(
        memory_fc,
        "to_node",
        alternatives=["to_node"]
    )

    nlcd_field = get_field_name(
        memory_fc,
        "NLCD_Majority",
        alternatives=["NLCD_Major"]
    )

    dem_field = get_field_name(
        memory_fc,
        "DEM_Slope",
        alternatives=["DEM_Slope"]
    )

    cda_field = get_field_name(
        memory_fc,
        "CDA_SqMi",
        alternatives=["CDA_SqMi"]
    )

    log(
        f"Fields: from_node={from_node_field}; "
        f"to_node={to_node_field}; "
        f"NLCD={nlcd_field}; DEM={dem_field}; CDA={cda_field}"
    )

    jd_dict = {}
    original_jd_text = {}
    downstream = defaultdict(list)
    in_degree = defaultdict(int)
    distribution = defaultdict(int)

    log_step_start(
        "Initial JD Classification"
    )

    with arcpy.da.UpdateCursor(
        memory_fc,
        [
            from_node_field,
            to_node_field,
            nlcd_field,
            dem_field,
            cda_field,
            "CDA_Prob",
            "Land_Use_Prob",
            "DEM_Prob",
            "JD_Confidence",
        ]
    ) as cursor:

        for row in cursor:

            land_use = classify_land_use(
                row[2]
            )

            slope_class = classify_slope_value(
                row[3]
            )

            cda_class = cda_range(
                row[4]
            )

            confidence = get_jd_confidence(
                land_use,
                row[4],
                slope_class
            )

            row[5] = cda_class
            row[6] = land_use
            row[7] = slope_class
            row[8] = confidence

            from_node = row[0]
            to_node = row[1]

            # Preserve unclassified / missing-input reaches as None.
            # This is important now that Step 3.2 leaves missing DEM_Slope
            # as NULL instead of converting it to a real 0-1% slope.
            jd_dict[
                from_node
            ] = CONFIDENCE_RANK.get(
                confidence
            )

            original_jd_text[
                from_node
            ] = confidence

            downstream[
                from_node
            ].append(
                to_node
            )

            in_degree[
                to_node
            ] += 1

            cursor.updateRow(
                row
            )

            if confidence not in (
                "Unknown",
                None,
                "",
            ):
                distribution[
                    confidence
                ] += 1

    log_step_end(
        "Initial JD Classification"
    )

    if apply_downstream_propagation:

        log_step_start(
            "Hierarchical JD Propagation"
        )

        queue = deque(
            node
            for node in jd_dict
            if in_degree[
                node
            ] == 0
        )

        while queue:

            node = queue.popleft()

            for downstream_node in downstream[
                node
            ]:

                upstream_rank = jd_dict.get(
                    node
                )

                downstream_rank = jd_dict.get(
                    downstream_node
                )

                # Propagate only a known upstream confidence.
                # If the downstream reach is currently Unknown/None,
                # a known upstream rank may populate it. If both are
                # unknown, it remains Unknown.
                if (
                    upstream_rank is not None
                    and
                    (
                        downstream_rank is None
                        or
                        downstream_rank
                        <
                        upstream_rank
                    )
                ):

                    jd_dict[
                        downstream_node
                    ] = upstream_rank

                in_degree[
                    downstream_node
                ] -= 1

                if in_degree[
                    downstream_node
                ] == 0:
                    queue.append(
                        downstream_node
                    )

        log_step_end(
            "Hierarchical JD Propagation"
        )

    else:

        log(
            "Downstream JD propagation disabled; "
            "retaining initial reach-level confidence."
        )

    log_step_start(
        "Write Propagated JD Confidence"
    )

    with arcpy.da.UpdateCursor(
        memory_fc,
        [
            from_node_field,
            "JD_Confidence",
        ]
    ) as cursor:

        for row in cursor:

            if apply_downstream_propagation:

                propagated_rank = jd_dict.get(
                    row[
                        0
                    ]
                )

                if propagated_rank is None:

                    row[
                        1
                    ] = original_jd_text.get(
                        row[
                            0
                        ],
                        "Unknown"
                    )

                else:

                    row[
                        1
                    ] = RANK_TO_CONFIDENCE.get(
                        propagated_rank,
                        original_jd_text.get(
                            row[
                                0
                            ],
                            "Unknown"
                        )
                    )

            else:

                row[
                    1
                ] = original_jd_text.get(
                    row[
                        0
                    ],
                    "Unknown"
                )

            cursor.updateRow(
                row
            )

    log_step_end(
        "Write Propagated JD Confidence"
    )

    erased_fc = erase_by_lakes_and_river(
        memory_fc,
        river_buffer,
        lake_buffer,
        apply_erase=erase_water_buffers
    )

    if arcpy.Exists(
        output_fc
    ):
        arcpy.management.Delete(
            output_fc
        )

    log_step_start(
        f"Save JD Output {os.path.basename(output_fc)}"
    )

    arcpy.management.CopyFeatures(
        erased_fc,
        output_fc
    )

    log_step_end(
        f"Save JD Output {os.path.basename(output_fc)}"
    )

    final_count = int(
        arcpy.management.GetCount(
            output_fc
        )[0]
    )

    if final_count <= 0:
        fail(
            f"JD output contains no features: "
            f"{output_fc}"
        )

    return {
        "output": output_fc,
        "feature_count": final_count,
        "distribution": dict(
            distribution
        ),
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

    threshold_text = p(
        2
    )

    river_buffer = p(
        3
    )

    lake_buffer = p(
        4
    )

    apply_downstream_propagation = get_bool(
        5,
        True
    )

    erase_water_buffers = get_bool(
        6,
        True
    )

    overwrite = get_bool(
        7,
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

    if not river_buffer:
        fail(
            "Major River Buffer is required."
        )

    if not lake_buffer:
        fail(
            "Lake / Wetland Buffer is required."
        )

    thresholds_to_process = parse_threshold_tokens(
        threshold_text
    )

    context = load_run_context(
        run_folder
    )

    county_abbr = context[
        "county_abbr"
    ]

    county_name = context[
        "county_name"
    ]

    init_tool_log(
        county_folder=run_folder,
        tool_id="3.3",
        tool_name="Calculate_JD_Confidence",
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

            "Thresholds":
                thresholds_to_process,

            "Major River Buffer":
                river_buffer,

            "Lake / Wetland Buffer":
                lake_buffer,

            "Apply Downstream Propagation":
                apply_downstream_propagation,

            "Erase Streams Within Water Buffers":
                erase_water_buffers,
        }
    )

    log("=" * 72)
    log("3.3 Calculate JD Confidence")
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
            "does not exist in this processing run."
        )

        update_run_config(
            context,
            "SKIPPED_STREAMS_GDB_NOT_READY",
            {},
            working_gdb,
            thresholds=thresholds_to_process,
            apply_downstream_propagation=apply_downstream_propagation,
            erase_water_buffers=erase_water_buffers
        )

        finish_tool_log(
            "SKIPPED_STREAMS_GDB_NOT_READY"
        )

        arcpy.SetParameterAsText(
            9,
            "SKIPPED_STREAMS_GDB_NOT_READY"
        )

        return

    tools_source = context[
        "project"
    ].get(
        "tools_source",
        {}
    )

    expected_river_buffer = tools_source.get(
        RIVER_BUFFER_NAME
    )

    expected_lake_buffer = tools_source.get(
        LAKE_BUFFER_NAME
    )

    for label, selected_path, expected_path in [
        (
            "Major River Buffer",
            river_buffer,
            expected_river_buffer,
        ),
        (
            "Lake / Wetland Buffer",
            lake_buffer,
            expected_lake_buffer,
        ),
    ]:

        if not arcpy.Exists(
            selected_path
        ):
            fail(
                f"{label} does not exist: "
                f"{selected_path}"
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
                "resolved from Project Config."
            )

        log(
            f"PASS - {label}: "
            f"{selected_path}"
        )

    old_workspace = arcpy.env.workspace
    old_overwrite = arcpy.env.overwriteOutput

    try:

        arcpy.env.workspace = (
            working_gdb
        )

        arcpy.env.overwriteOutput = (
            True
        )

        all_fcs = (
            arcpy.ListFeatureClasses(
                "*_w_characteristics*"
            )
            or
            []
        )

        candidate_fcs = []

        for fc in all_fcs:

            base = arcpy.Describe(
                fc
            ).baseName

            if base.endswith(
                "_w_characteristics_JD"
            ):
                continue

            if base.endswith(
                "_w_characteristics"
            ):

                matched_threshold = None

                for threshold in thresholds_to_process:

                    if (
                        f"_Stream_{threshold}_"
                        in base
                    ):

                        matched_threshold = threshold

                        break

                if matched_threshold:

                    candidate_fcs.append(
                        fc
                    )

        if not candidate_fcs:

            warn(
                "No '*_w_characteristics' feature classes were found "
                "in the Streams Working GDB."
            )

            warn(
                "JD Confidence depends on characteristic fields "
                "(NLCD_Majority, DEM_Slope, CDA_SqMi, from_node, to_node). "
                "Run 3.2 Generate Stream Characteristics before this tool."
            )

            update_run_config(
                context,
                "SKIPPED_CHARACTERISTICS_NOT_READY",
                {},
                working_gdb,
                thresholds=thresholds_to_process,
                apply_downstream_propagation=apply_downstream_propagation,
                erase_water_buffers=erase_water_buffers
            )

            finish_tool_log(
                "SKIPPED_CHARACTERISTICS_NOT_READY"
            )

            arcpy.SetParameterAsText(
                8,
                working_gdb
            )

            arcpy.SetParameterAsText(
                9,
                "SKIPPED_CHARACTERISTICS_NOT_READY"
            )

            return

        log(
            f"Characteristic stream datasets found: "
            f"{len(candidate_fcs)}"
        )

        results = {}

        for fc in sorted(
            candidate_fcs
        ):

            input_fc = os.path.join(
                working_gdb,
                fc
            )

            base = arcpy.Describe(
                input_fc
            ).baseName

            output_fc = os.path.join(
                working_gdb,
                f"{base}_JD"
            )

            if arcpy.Exists(
                output_fc
            ) and not overwrite:

                log(
                    f"Existing JD output found; reusing: "
                    f"{output_fc}"
                )

                results[
                    base
                ] = {
                    "status":
                        "SKIPPED_EXISTING_VALID",

                    "output":
                        output_fc,
                }

                continue

            log_step_start(
                f"JD Dataset - {base}"
            )

            result = process_one_fc(
                input_fc=input_fc,
                output_fc=output_fc,
                river_buffer=river_buffer,
                lake_buffer=lake_buffer,
                apply_downstream_propagation=apply_downstream_propagation,
                erase_water_buffers=erase_water_buffers
            )

            log_step_end(
                f"JD Dataset - {base}"
            )

            result[
                "status"
            ] = "PASS"

            results[
                base
            ] = result

        update_run_config(
            context,
            "PASS",
            results,
            working_gdb,
            thresholds=thresholds_to_process,
            apply_downstream_propagation=apply_downstream_propagation,
            erase_water_buffers=erase_water_buffers
        )

        finish_tool_log(
            "PASS"
        )

        arcpy.SetParameterAsText(
            8,
            working_gdb
        )

        arcpy.SetParameterAsText(
            9,
            "PASS"
        )

    finally:

        arcpy.env.workspace = (
            old_workspace
        )

        arcpy.env.overwriteOutput = (
            old_overwrite
        )


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
                9,
                "FAIL"
            )
        except Exception:
            pass

        raise
