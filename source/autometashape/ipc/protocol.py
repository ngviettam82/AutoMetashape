# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Machine-readable IPC milestone protocol and atomic sidecar telemetry."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time

# ---------------------------------------------------------------------------
# Machine-Readable IPC Milestone Protocol Sentinels
# ---------------------------------------------------------------------------
IPC_SENTINEL_PREFIX: str = "@@AUTOMETASHAPE_STAGE:"
IPC_SENTINEL_SUFFIX: str = "@@"
IPC_PAUSE_CROP: str = "@@AUTOMETASHAPE_PAUSE_CROP@@"
IPC_COMPLETE: str = "@@AUTOMETASHAPE_COMPLETE@@"

RECON_TAG_REGEX = re.compile(r"^\s*\[([1-7][a-z0-9]*)/[6-7]\]\s*(.*)", re.IGNORECASE)
EXPORT_TAG_REGEX = re.compile(
    r"^\s*\[(import|model info|mesh|color calibration|texture|delighting|1/2|2/2|point cloud|dem|orthomosaic|fbx|ply|dxf|tie points|tiled model|contours|report)\]\s*(.*)",
    re.IGNORECASE,
)

RECON_TAG_MAP: dict[str, tuple[str, str]] = {
    "1": ("ingest", "Ingesting photos & blur filter"),
    "2": ("match", "GPU Feature Matching"),
    "3": ("align", "Camera Alignment & Tie Points"),
    "3b": ("align", "Alignment & Bounding Box Crop"),
    "4": ("depth", "MVS Dense Stereopsis"),
    "4b": ("point_cloud", "Generating Dense Point Cloud"),
    "5": ("mesh", "Fusing Surface Mesh Geometry"),
    "6": ("texture", "Baking 8K Texture Atlas"),
    "6b": ("tiled_model", "Building Hierarchical 3D Tiled Model"),
    "7b": ("dem", "Building Digital Elevation Model"),
    "7b2": ("dem", "Generating Topographic Contours"),
    "7c": ("ortho", "Generating True Orthomosaic"),
    "7d": ("point_cloud", "Exporting Dense Point Cloud Deliverable"),
    "7e": ("export", "Exporting Official Quality PDF Report"),
    "7": ("export", "Exporting GLB, OBJ & Production Assets"),
}

EXPORT_TAG_MAP: dict[str, tuple[str, str]] = {
    "import": ("project", "Loading Metashape Project"),
    "model info": ("mesh", "Validating 3D Mesh Geometry"),
    "mesh": ("mesh", "Validating 3D Mesh Geometry"),
    "color calibration": ("texture", "Radiometric Color Calibration"),
    "texture": ("texture", "Re-baking Texture Atlas"),
    "delighting": ("texture", "Delighting & Shadow Removal"),
    "1/2": ("models", "Exporting Binary GLB"),
    "2/2": ("models", "Exporting Wavefront OBJ & Textures"),
    "fbx": ("models", "Exporting Autodesk FBX"),
    "ply": ("models", "Exporting Stanford PLY"),
    "dxf": ("models", "Exporting AutoCAD DXF"),
    "tie points": ("models", "Exporting Sparse Tie Points"),
    "point cloud": ("deliverables", "Exporting Dense Point Cloud"),
    "dem": ("deliverables", "Exporting GeoTIFF Elevation Model"),
    "orthomosaic": ("deliverables", "Exporting GeoTIFF Orthomosaic"),
    "tiled model": ("deliverables", "Building & Exporting 3D Tiles"),
    "contours": ("deliverables", "Exporting Topographic Contours"),
    "report": ("deliverables", "Exporting Official PDF Report"),
}

_ACTIVE_STATUS_FILE: str | None = None


def set_active_status_file(path: str | None) -> None:
    """Set the active status sidecar JSON file path for worker IPC telemetry.

    Args:
        path: Absolute or relative file path to the status sidecar JSON, or None.
    """
    global _ACTIVE_STATUS_FILE
    _ACTIVE_STATUS_FILE = path


def emit_ipc_stage(stage_key: str, description: str, progress: float = 0.0) -> None:
    """Emits an explicit machine-readable IPC milestone sentinel to stdout and atomic status sidecar.

    Args:
        stage_key: Canonical short key representing the processing stage.
        description: Human-readable description of current task activity.
        progress: Stage progress percentage between 0.0 and 100.0.
    """
    print(f"{IPC_SENTINEL_PREFIX}{stage_key}:{description}{IPC_SENTINEL_SUFFIX}", flush=True)
    global _ACTIVE_STATUS_FILE
    if _ACTIVE_STATUS_FILE:
        try:
            parent = os.path.dirname(os.path.abspath(_ACTIVE_STATUS_FILE))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            data = {
                "stage_key": stage_key,
                "stage_desc": description,
                "progress": float(progress),
                "worker_node": socket.gethostname(),
                "status": "running",
                "timestamp": time.time(),
            }
            tmp_path = f"{_ACTIVE_STATUS_FILE}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as sf:
                json.dump(data, sf, indent=2)
            os.replace(tmp_path, _ACTIVE_STATUS_FILE)
        except Exception:
            pass


def emit_ipc_event(sentinel: str) -> None:
    """Emits an explicit machine-readable IPC control event to stdout and updates status sidecar.

    Args:
        sentinel: Complete sentinel token string (e.g. IPC_COMPLETE or IPC_PAUSE_CROP).
    """
    print(sentinel, flush=True)
    global _ACTIVE_STATUS_FILE
    if _ACTIVE_STATUS_FILE and "COMPLETE" in sentinel:
        try:
            parent = os.path.dirname(os.path.abspath(_ACTIVE_STATUS_FILE))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            data = {
                "stage_key": "complete",
                "stage_desc": "Pipeline Complete",
                "progress": 100.0,
                "worker_node": socket.gethostname(),
                "status": "completed",
                "timestamp": time.time(),
            }
            tmp_path = f"{_ACTIVE_STATUS_FILE}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as sf:
                json.dump(data, sf, indent=2)
            os.replace(tmp_path, _ACTIVE_STATUS_FILE)
        except Exception:
            pass


def emit_ipc_error(stage_key: str, error_message: str) -> None:
    """Emits an explicit error status to stdout and the status sidecar JSON.

    Args:
        stage_key: Active stage during which the error occurred.
        error_message: Formatted error explanation string.
    """
    err_line = f"[ERROR] Pipeline error in stage '{stage_key}': {error_message}"
    print(err_line, file=sys.stderr, flush=True)
    global _ACTIVE_STATUS_FILE
    if _ACTIVE_STATUS_FILE:
        try:
            parent = os.path.dirname(os.path.abspath(_ACTIVE_STATUS_FILE))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            data = {
                "stage_key": stage_key,
                "stage_desc": f"Error: {error_message}",
                "progress": 0.0,
                "worker_node": socket.gethostname(),
                "status": "error",
                "error": error_message,
                "timestamp": time.time(),
            }
            tmp_path = f"{_ACTIVE_STATUS_FILE}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as sf:
                json.dump(data, sf, indent=2)
            os.replace(tmp_path, _ACTIVE_STATUS_FILE)
        except Exception:
            pass


def parse_ipc_line(line: str, mode: str = "recon") -> tuple[str, str] | None:
    """Parses a subprocess log line into an explicit stage key and description.

    Guarantees:
    - 100% exact match for explicit IPC sentinel tokens (@@AUTOMETASHAPE_STAGE:key:desc@@).
    - Strictly anchored regex for [X/Y] stage tags.
    - Zero loose substring matching: lines containing configuration parameters,
      flags, or C++ internals return None and NEVER trigger fake stage transitions.

    Args:
        line: Raw stdout/stderr line from subprocess.
        mode: Subprocess pipeline mode ('recon' or 'export').

    Returns:
        Tuple of (stage_key, stage_description) if matched, or None.
    """
    stripped = line.strip()
    if not stripped:
        return None

    # 1. Primary: Explicit Machine-Readable IPC Sentinel Token
    if stripped.startswith(IPC_SENTINEL_PREFIX) and stripped.endswith(IPC_SENTINEL_SUFFIX):
        payload = stripped[len(IPC_SENTINEL_PREFIX) : -len(IPC_SENTINEL_SUFFIX)].strip()
        parts = payload.split(":", 1)
        key = parts[0].strip().lower()
        desc = parts[1].strip() if len(parts) > 1 else ""
        return (key, desc)

    # 2. Secondary: Strictly Anchored [X/Y] Tags
    if mode == "recon":
        if stripped.startswith("[Auto-Crop"):
            return ("align", "Alignment & Bounding Box Crop")

        m = RECON_TAG_REGEX.match(stripped)
        if m:
            tag = m.group(1).lower()
            if tag in RECON_TAG_MAP:
                key, default_desc = RECON_TAG_MAP[tag]
                desc = m.group(2).strip() or default_desc
                return (key, desc)

    elif mode == "export":
        m = EXPORT_TAG_REGEX.match(stripped)
        if m:
            tag = m.group(1).lower()
            if tag in EXPORT_TAG_MAP:
                key, default_desc = EXPORT_TAG_MAP[tag]
                desc = m.group(2).strip() or default_desc
                return (key, desc)

    return None
