# -*- coding: utf-8 -*-
import json
import os
import tempfile
import time
import datetime
import traceback
import arcpy

_CURRENT_LOG_FILE = None
_CURRENT_TOOL_ID = None
_CURRENT_TOOL_NAME = None
_RUN_START_TIME = None
_CURRENT_STEP_START = None
_CURRENT_STEP_NAME = None


def _now_display():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_file():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _elapsed_since(start_time):
    if start_time is None:
        return "00:00:00"
    seconds = max(0, int(time.time() - start_time))
    return f"{seconds//3600:02d}:{(seconds%3600)//60:02d}:{seconds%60:02d}"


def _safe_name(text):
    text = str(text).strip()
    for char in [" ", ".", "/", "\\", ":", ";", "-"]:
        text = text.replace(char, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def exists(path):
    return bool(path) and (arcpy.Exists(path) or os.path.exists(path))


def ensure_folder(path):
    if not path:
        fail("Folder path is blank.")
    os.makedirs(path, exist_ok=True)


def save_json(obj, path):
    ensure_folder(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2)
    os.replace(tmp, path)


def load_json(path):
    if not os.path.isfile(path):
        fail(f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def require_fields(dataset, fields):
    existing = {field.name for field in arcpy.ListFields(dataset)}
    missing = [field_name for field_name in fields if field_name not in existing]
    if missing:
        fail(f"{dataset} missing required fields: {missing}")


def require_geometry(dataset, allowed):
    shape_type = getattr(arcpy.Describe(dataset), "shapeType", None)
    if shape_type not in allowed:
        fail(f"{dataset} geometry={shape_type}; expected {sorted(allowed)}")


def default_scratch():
    return os.path.join(tempfile.gettempdir(), "ODOT_Stream_Scratch")


def _append_to_run_log(level, message):
    global _CURRENT_LOG_FILE
    if not _CURRENT_LOG_FILE:
        return
    try:
        os.makedirs(os.path.dirname(_CURRENT_LOG_FILE), exist_ok=True)
        with open(_CURRENT_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{_now_display()}] [{level}] {message}\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except Exception:
                pass
    except Exception:
        pass


def _discover_county_abbr_for_log(county_folder):
    try:
        config_folder = os.path.join(county_folder, "config")
        if not os.path.isdir(config_folder):
            return None
        candidates = [
            os.path.join(config_folder, name)
            for name in os.listdir(config_folder)
            if name.lower().endswith("_county_config.json")
        ]
        if len(candidates) != 1:
            return None
        with open(candidates[0], "r", encoding="utf-8") as handle:
            config = json.load(handle)
        return config.get("county", {}).get("abbr")
    except Exception:
        return None


def init_tool_log(county_folder, tool_id, tool_name, county_abbr=None, extra_context=None):
    global _CURRENT_LOG_FILE, _CURRENT_TOOL_ID, _CURRENT_TOOL_NAME
    global _RUN_START_TIME, _CURRENT_STEP_START, _CURRENT_STEP_NAME

    _CURRENT_TOOL_ID = str(tool_id)
    _CURRENT_TOOL_NAME = str(tool_name)
    _RUN_START_TIME = time.time()
    _CURRENT_STEP_START = None
    _CURRENT_STEP_NAME = None

    if not county_abbr:
        county_abbr = _discover_county_abbr_for_log(county_folder)
    if not county_abbr:
        county_abbr = os.path.basename(os.path.normpath(county_folder))

    log_folder = os.path.join(county_folder, "logs")
    os.makedirs(log_folder, exist_ok=True)

    file_name = (
        f"{_safe_name(county_abbr).upper()}_"
        f"{_safe_name(tool_id)}_"
        f"{_safe_name(tool_name)}_"
        f"{_now_file()}.log"
    )
    _CURRENT_LOG_FILE = os.path.join(log_folder, file_name)

    with open(_CURRENT_LOG_FILE, "w", encoding="utf-8") as handle:
        handle.write("ODOT Ohio Stream Delineation Toolbox Run Log\n")
        handle.write("=" * 78 + "\n")
        handle.write(f"Tool ID      : {_CURRENT_TOOL_ID}\n")
        handle.write(f"Tool Name    : {_CURRENT_TOOL_NAME}\n")
        handle.write(f"County Folder: {county_folder}\n")
        handle.write(f"Started      : {_now_display()}\n")
        if extra_context:
            for key, value in extra_context.items():
                handle.write(f"{key}: {value}\n")
        handle.write("=" * 78 + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except Exception:
            pass

    log(f"Persistent run log: {_CURRENT_LOG_FILE}")
    log(f"TOOL START - {_CURRENT_TOOL_ID} {_CURRENT_TOOL_NAME}")
    return _CURRENT_LOG_FILE


def get_current_log_path():
    return _CURRENT_LOG_FILE


def log_step_start(step_name):
    global _CURRENT_STEP_START, _CURRENT_STEP_NAME
    _CURRENT_STEP_NAME = str(step_name)
    _CURRENT_STEP_START = time.time()
    log(f"STEP START - {_CURRENT_STEP_NAME}")


def log_step_end(step_name=None, status="PASS"):
    global _CURRENT_STEP_START, _CURRENT_STEP_NAME
    name = step_name or _CURRENT_STEP_NAME or "Unnamed Step"
    elapsed = _elapsed_since(_CURRENT_STEP_START)
    log(f"STEP END - {name} | Status={status} | Elapsed={elapsed}")
    _CURRENT_STEP_START = None
    _CURRENT_STEP_NAME = None


def finish_tool_log(status, message=None):
    elapsed = _elapsed_since(_RUN_START_TIME)
    if message:
        log(message)
    log(
        f"TOOL END - {_CURRENT_TOOL_ID} {_CURRENT_TOOL_NAME} | "
        f"Status={status} | Elapsed={elapsed}"
    )


def log_exception(exc=None, include_traceback=True):
    if exc is not None:
        _append_to_run_log("ERROR", f"Exception: {exc}")
    if include_traceback:
        try:
            tb = traceback.format_exc()
            if tb and tb.strip() != "NoneType: None":
                for line in tb.rstrip().splitlines():
                    _append_to_run_log("TRACEBACK", line)
        except Exception:
            pass


def log(message):
    text = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(text)
    try:
        arcpy.AddMessage(text)
    except Exception:
        pass
    _append_to_run_log("INFO", message)


def warn(message):
    text = f"[{time.strftime('%H:%M:%S')}] [WARN] {message}"
    print(text)
    try:
        arcpy.AddWarning(text)
    except Exception:
        pass
    _append_to_run_log("WARNING", message)


def fail(message):
    try:
        arcpy.AddError(message)
    except Exception:
        pass
    _append_to_run_log("ERROR", message)
    raise RuntimeError(message)
