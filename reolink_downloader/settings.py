#!/usr/bin/env python3
"""
Form-editable settings for the timelapse pipeline.

The web control panel (see server.py) lets the client change the daily
start/end time, the speed-up factor, crop, output quality and Drive folder
WITHOUT touching any code. Those values live in settings.json (next to this
file) and are layered on top of config.py.

How it works:
  - defaults()  -> the baseline values, read from config.py.
  - load()      -> defaults merged with whatever is saved in settings.json.
  - save(dict)  -> validate + persist to settings.json AND apply to the live
                   `config` module in memory, so the very next recording job
                   uses the new values (no service restart needed).
  - apply_to_config() is called once on startup so a saved settings.json is
                   active immediately.

Only the fields below are editable. Anything not listed (camera credentials,
disk guards, etc.) stays in config.py and is not exposed to the form.
"""
import json
import os
import threading
from datetime import datetime

import config

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
_lock = threading.Lock()

# Fields that map 1:1 onto a config.py attribute (form key == config attr).
# value = (python type, config attribute name).
_CONFIG_FIELDS = {
    "SPEED_FACTOR": int,
    "SPEED_WIDTH": int,
    "SPEED_CRF": int,
    "INTRO_SECONDS": float,
    "CROP_LEFT": float,
    "CROP_RIGHT": float,
    "CROP_TOP": float,
    "CROP_BOTTOM": float,
    "GDRIVE_FOLDER_ID": str,
    "AUDIO_ENABLED": bool,
    "GDRIVE_AUDIO_FOLDER_ID": str,
}

# Scheduler-only fields (not config attributes). key -> (type, config default attr)
_SCHED_FIELDS = {
    "enabled": (bool, "SCHEDULE_ENABLED"),
    "start_time": (str, "SCHEDULE_START"),
    "end_time": (str, "SCHEDULE_END"),
    "timezone": (str, "SCHEDULE_TIMEZONE"),
}


def defaults():
    d = {}
    for key in _CONFIG_FIELDS:
        d[key] = getattr(config, key, None)
    for key, (_typ, cfg_attr) in _SCHED_FIELDS.items():
        d[key] = getattr(config, cfg_attr, None)
    return d


def load():
    """defaults() overlaid with saved settings.json (if present)."""
    d = defaults()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                d.update({k: v for k, v in saved.items() if k in d})
        except (OSError, ValueError):
            pass  # corrupt/missing -> fall back to defaults
    return d


def _coerce(key, value):
    """Best-effort type coercion; returns (ok, value)."""
    if key in _CONFIG_FIELDS:
        typ = _CONFIG_FIELDS[key]
    elif key in _SCHED_FIELDS:
        typ = _SCHED_FIELDS[key][0]
    else:
        return False, value
    try:
        if typ is bool:
            if isinstance(value, str):
                return True, value.strip().lower() in ("1", "true", "on", "yes")
            return True, bool(value)
        if value is None or value == "":
            # only strings may legitimately be empty (e.g. blank folder id)
            return (typ is str), (value if typ is str else None)
        return True, typ(value)
    except (ValueError, TypeError):
        return False, value


def _valid_hhmm(s):
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except (ValueError, TypeError):
        return False


def validate(merged):
    """Return a list of human-readable problems (empty = ok)."""
    errs = []
    for t in ("start_time", "end_time"):
        if not _valid_hhmm(merged.get(t, "")):
            errs.append(f"{t} must be HH:MM (24h), e.g. 06:00")
    if _coerce("SPEED_FACTOR", merged.get("SPEED_FACTOR"))[1] and int(merged["SPEED_FACTOR"]) < 1:
        errs.append("Speed factor must be at least 1.")
    for c in ("CROP_LEFT", "CROP_RIGHT", "CROP_TOP", "CROP_BOTTOM"):
        try:
            v = float(merged.get(c, 0) or 0)
            if not (0 <= v < 1):
                errs.append(f"{c} must be between 0 and 1.")
        except (ValueError, TypeError):
            errs.append(f"{c} must be a number between 0 and 1.")
    if (float(merged.get("CROP_LEFT", 0) or 0) + float(merged.get("CROP_RIGHT", 0) or 0)) >= 1:
        errs.append("CROP_LEFT + CROP_RIGHT must be < 1.")
    if (float(merged.get("CROP_TOP", 0) or 0) + float(merged.get("CROP_BOTTOM", 0) or 0)) >= 1:
        errs.append("CROP_TOP + CROP_BOTTOM must be < 1.")
    return errs


def apply_to_config(settings=None):
    """Push the config-backed fields onto the live `config` module in memory."""
    s = settings if settings is not None else load()
    for key in _CONFIG_FIELDS:
        if key in s and s[key] is not None:
            ok, val = _coerce(key, s[key])
            if ok:
                setattr(config, key, val)


def save(new_values):
    """Validate, persist to settings.json, and apply to live config.

    Returns (settings_dict, errors_list). On errors nothing is written.
    """
    with _lock:
        merged = load()
        for k, v in new_values.items():
            if k in _CONFIG_FIELDS or k in _SCHED_FIELDS:
                ok, val = _coerce(k, v)
                if ok:
                    merged[k] = val

        errs = validate(merged)
        if errs:
            return merged, errs

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        apply_to_config(merged)
        return merged, []


def duration_hours(settings=None):
    """Hours between start_time and end_time (wraps past midnight)."""
    s = settings if settings is not None else load()
    t0 = datetime.strptime(s["start_time"], "%H:%M")
    t1 = datetime.strptime(s["end_time"], "%H:%M")
    mins = (t1.hour * 60 + t1.minute) - (t0.hour * 60 + t0.minute)
    if mins <= 0:
        mins += 24 * 60
    return round(mins / 60.0, 4)
