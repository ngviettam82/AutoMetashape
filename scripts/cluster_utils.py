# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Modular Cluster, Network, and IPC Utilities.

Provides production utilities for distributed photogrammetry operations:
- Executable discovery (Metashape Pro, Server, Monitor) across Windows, Linux, and macOS.
- Local Area Network (LAN) primary IP auto-discovery.
- High-speed IPC milestone parsing for real-time progress tracking.
- Pre-flight cluster health diagnostics (TCP socket reachability, UNC storage verification).
- Persistent JSON configuration management for 1-click worker and server launchers.
- Unicode and international path normalization.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
from typing import Any

# Configure Windows console streams for international Unicode paths
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr", "stdin"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Machine-Readable IPC Milestone Protocol Sentinels
# ---------------------------------------------------------------------------
IPC_SENTINEL_PREFIX = "@@AUTOMETASHAPE_STAGE:"
IPC_SENTINEL_SUFFIX = "@@"
IPC_COMPLETE = "@@AUTOMETASHAPE_COMPLETE@@"

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


def parse_ipc_line(line: str, mode: str = "recon") -> tuple[str, str] | None:
    """Parses a subprocess log line into an explicit stage key and description.

    Guarantees:
    - 100% exact match for explicit IPC sentinel tokens (@@AUTOMETASHAPE_STAGE:key:desc@@).
    - Strictly anchored regex for [X/Y] stage tags.
    - Zero loose substring matching: lines containing configuration parameters,
      flags, or C++ internals return None and NEVER trigger fake stage transitions.
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


# ---------------------------------------------------------------------------
# Executable & System Discovery
# ---------------------------------------------------------------------------
def find_metashape_path() -> str | None:
    """Locate Agisoft Metashape Pro executable dynamically across platforms."""
    # 1. Bundled relative to current Python if running inside Metashape's embedded Python
    candidate = os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "metashape.exe"))
    if os.path.isfile(candidate):
        return candidate

    # 2. Check system PATH
    for name in ("metashape", "metashape.exe", "photoscan", "photoscan.exe"):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return p

    # 3. Dynamic Windows environment paths
    if sys.platform == "win32":
        env_dirs = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("ProgramW6432"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        subpaths = [
            os.path.join("Agisoft", "Metashape Pro", "metashape.exe"),
            os.path.join("Agisoft", "PhotoScan Pro", "photoscan.exe"),
            os.path.join("Programs", "Agisoft", "Metashape Pro", "metashape.exe"),
        ]
        for base in env_dirs:
            if base:
                for sub in subpaths:
                    full_p = os.path.join(base, sub)
                    if os.path.isfile(full_p):
                        return full_p

        # Dynamic drive scanning (checks mounted drive letters without assuming C:)
        import string

        for drive in string.ascii_uppercase:
            for sub in (
                rf"{drive}:\Program Files\Agisoft\Metashape Pro\metashape.exe",
                rf"{drive}:\Program Files (x86)\Agisoft\Metashape Pro\metashape.exe",
                rf"{drive}:\Agisoft\Metashape Pro\metashape.exe",
            ):
                if os.path.isfile(sub):
                    return sub

    elif sys.platform == "darwin":
        cand = "/Applications/MetashapePro.app/Contents/MacOS/MetashapePro"
        if os.path.isfile(cand):
            return cand
    else:
        # Linux standard locations
        for cand in ("/opt/metashape-pro/metashape.sh", "/usr/local/bin/metashape", "/usr/bin/metashape"):
            if os.path.isfile(cand):
                return cand

    return None


def find_metashape_server_path(metashape_exe: str | None = None) -> str | None:
    """Locate Metashape Server binary (metashape-server.exe or metashape-server)."""
    if metashape_exe and os.path.isfile(metashape_exe):
        exe_dir = os.path.dirname(metashape_exe)
        for name in ("metashape-server.exe", "metashape-server"):
            cand = os.path.join(exe_dir, name)
            if os.path.isfile(cand):
                return cand

    for name in ("metashape-server.exe", "metashape-server"):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return p

    if sys.platform == "win32":
        for base in [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("ProgramW6432"),
            os.environ.get("PROGRAMFILES(X86)"),
        ]:
            if base:
                cand = os.path.join(base, "Agisoft", "Metashape Pro", "metashape-server.exe")
                if os.path.isfile(cand):
                    return cand

    return None


def find_metashape_monitor_path(metashape_exe: str | None = None) -> str | None:
    """Locate Agisoft Metashape Network Monitor GUI binary (monitor.exe or monitor)."""
    if metashape_exe and os.path.isfile(metashape_exe):
        exe_dir = os.path.dirname(metashape_exe)
        for name in ("monitor.exe", "monitor"):
            cand = os.path.join(exe_dir, name)
            if os.path.isfile(cand):
                return cand

    for name in ("monitor.exe", "monitor"):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return p

    if sys.platform == "win32":
        for base in [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("ProgramW6432"),
            os.environ.get("PROGRAMFILES(X86)"),
        ]:
            if base:
                cand = os.path.join(base, "Agisoft", "Metashape Pro", "monitor.exe")
                if os.path.isfile(cand):
                    return cand

    return None


def get_primary_lan_ip() -> str:
    """Detect the host machine's primary LAN IPv4 address (e.g. 192.168.x.x or 10.x.x.x)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        pass
    finally:
        s.close()

    # Airgapped / offline LAN fallback: inspect hostname IP resolution
    try:
        host_name = socket.gethostname()
        ip = socket.gethostbyname(host_name)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    return "127.0.0.1"


def generate_worker_bat_content(host: str | None = None, port: str = "5840", root_path: str = "") -> str:
    """Generate self-contained Windows batch file content for remote LAN worker nodes."""
    target_host = host.strip() if host and host.strip() else get_primary_lan_ip()
    target_port = port.strip() or "5840"
    target_root = root_path.strip()

    if target_root:
        root_init = f'set "TARGET_ROOT={target_root}"'
    else:
        root_init = r'if "%TARGET_ROOT%"=="" set /p TARGET_ROOT="Enter Shared Network Storage path (e.g. \\HOST_PC\SharedMetashape or Z:\): "'

    return f"""@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
title Metashape Distributed Cluster Worker Node

echo =======================================================================
echo   AutoMetashape Distributed Cluster - Worker Node Launcher
echo   Cluster Target Server : {target_host}:{target_port}
echo =======================================================================
echo.

:: Configure and validate Shared Network Storage root path
{root_init}
if "%TARGET_ROOT%"=="" (
    echo [ERROR] Shared network root path cannot be empty.
    pause
    exit /b 1
)

echo [OK] Shared Network Root   : %TARGET_ROOT%

:: Detect Metashape installation dynamically across environment paths and mounted drives
set "METASHAPE_EXE="
if exist "%ProgramFiles%\\Agisoft\\Metashape Pro\\metashape.exe" (
    set "METASHAPE_EXE=%ProgramFiles%\\Agisoft\\Metashape Pro\\metashape.exe"
) else if exist "%ProgramW6432%\\Agisoft\\Metashape Pro\\metashape.exe" (
    set "METASHAPE_EXE=%ProgramW6432%\\Agisoft\\Metashape Pro\\metashape.exe"
) else if exist "%LOCALAPPDATA%\\Programs\\Agisoft\\Metashape Pro\\metashape.exe" (
    set "METASHAPE_EXE=%LOCALAPPDATA%\\Programs\\Agisoft\\Metashape Pro\\metashape.exe"
)

if "%METASHAPE_EXE%"=="" (
    for %%D in (C D E F G) do (
        if exist "%%D:\\Program Files\\Agisoft\\Metashape Pro\\metashape.exe" (
            set "METASHAPE_EXE=%%D:\\Program Files\\Agisoft\\Metashape Pro\\metashape.exe"
        )
    )
)

if "%METASHAPE_EXE%"=="" (
    where metashape.exe >nul 2>nul
    if !errorlevel! equ 0 (
        set "METASHAPE_EXE=metashape.exe"
    )
)

if "%METASHAPE_EXE%"=="" (
    echo [ERROR] Metashape Pro executable not found in default path or PATH.
    echo Please install Agisoft Metashape Pro or edit this batch file with your path.
    pause
    exit /b 1
)

echo [OK] Using Metashape binary: "%METASHAPE_EXE%"
echo [CONNECTING] Connecting worker node to {target_host}:{target_port}...
echo [READY] Worker is online and waiting for jobs from the server.
echo Press Ctrl+C in this window at any time to disconnect this worker node.
echo.

"%METASHAPE_EXE%" --worker --host {target_host}:{target_port} --root "%TARGET_ROOT%" --cpu-enable 1

if !errorlevel! neq 0 (
    echo.
    echo [DISCONNECTED] Worker node disconnected with status code !errorlevel!.
    pause
)
"""



def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def normalize_unicode_path(path: str) -> str:
    """Normalize and resolve cross-platform paths containing Unicode or diacritics."""
    if not path:
        return ""
    norm = os.path.normpath(path.strip())
    return norm


# ---------------------------------------------------------------------------
# Pre-Flight Diagnostics
# ---------------------------------------------------------------------------
def test_tcp_connection(host: str, port: int, timeout: float = 3.0) -> bool:
    """Test TCP socket reachability to target cluster host and port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def test_unc_path(root_path: str) -> tuple[bool, str]:
    """Verify that the shared network storage root exists and is accessible.

    Returns:
        (is_valid, error_message)
    """
    if not root_path or not root_path.strip():
        return False, "Shared storage path cannot be empty."

    norm_path = os.path.normpath(root_path.strip())
    try:
        if not os.path.exists(norm_path):
            return False, f"Shared storage path does not exist or is unreachable: '{norm_path}'"
        if not os.path.isdir(norm_path):
            return False, f"Shared storage path is not a directory: '{norm_path}'"
        # Test directory listing permissions
        os.listdir(norm_path)
        return True, ""
    except Exception as e:
        return False, f"Access denied to shared storage path '{norm_path}': {e}"


# ---------------------------------------------------------------------------
# Configuration Persistence (1-Click Launchers)
# ---------------------------------------------------------------------------
DEFAULT_WORKER_CONFIG_FILENAME = "worker_config.json"
DEFAULT_SERVER_CONFIG_FILENAME = "server_config.json"


def get_default_config_path(filename: str = DEFAULT_WORKER_CONFIG_FILENAME) -> str:
    """Returns absolute path to configuration file in repository root or user profile."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    local_conf = os.path.join(repo_root, filename)
    if os.path.exists(local_conf) or os.access(repo_root, os.W_OK):
        return local_conf

    user_dir = os.path.join(os.path.expanduser("~"), ".autometashape")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, filename)


def load_cluster_config(config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME) -> dict[str, Any]:
    """Load JSON cluster configuration from disk."""
    path = config_path or get_default_config_path(filename)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cluster_config(config: dict[str, Any], config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME) -> str:
    """Save cluster configuration dictionary to JSON file on disk atomically."""
    path = config_path or get_default_config_path(filename)
    parent_dir = os.path.dirname(os.path.abspath(path))
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
    return path
