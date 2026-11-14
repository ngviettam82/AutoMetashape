# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware, network IP, and executable discovery across workstation environments."""

from __future__ import annotations

import os
import shutil
import socket
import string
import subprocess
import sys
from typing import Any


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


def is_integrated_gpu(name: str) -> bool:
    """Return True if GPU device name matches known integrated graphics (Intel iGPU, AMD APU)."""
    lower = name.lower()
    return any(
        ig in lower
        for ig in (
            "intel(r) graphics",
            "intel(r) hd",
            "intel(r) uhd",
            "intel(r) iris",
            "radeon(tm) graphics",
            "radeon graphics",
            "vega",
        )
    )


def detect_gpu_devices() -> list[dict[str, Any]]:
    """Detect available discrete high-performance GPU devices, filtering out integrated graphics.

    Returns:
        List of dicts: [{"index": int, "name": str, "mem_mb": int}]
    """
    # 1. Metashape Python API (when running inside embedded Metashape runtime)
    try:
        import Metashape

        if hasattr(Metashape, "app") and hasattr(Metashape.app, "enumGPUDevices"):
            gpus = Metashape.app.enumGPUDevices()
            if gpus:
                devices = []
                for i, g in enumerate(gpus):
                    name = g.get("name", f"GPU {i}")
                    if is_integrated_gpu(name):
                        continue
                    devices.append(
                        {
                            "index": i,
                            "name": name,
                            "mem_mb": int(g.get("mem_size", 0) // (1024 * 1024)),
                        }
                    )
                if devices:
                    return devices
    except Exception:
        pass

    # 2. nvidia-smi CLI (standard NVIDIA driver tool on Windows & Linux)
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi and sys.platform == "win32":
        for cand in (
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"),
            os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "NVIDIA Corporation",
                "NVSMI",
                "nvidia-smi.exe",
            ),
        ):
            if os.path.isfile(cand):
                nvidia_smi = cand
                break

    if nvidia_smi:
        try:
            res = subprocess.run(
                [nvidia_smi, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0 and res.stdout.strip():
                devices = []
                for line in res.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        idx = int(parts[0]) if parts[0].isdigit() else len(devices)
                        name = parts[1]
                        mem_mb = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
                        devices.append({"index": idx, "name": name, "mem_mb": mem_mb})
                if devices:
                    return devices
        except Exception:
            pass

    # 3. Windows WMI CIM query (supports AMD Radeon, Intel Arc, and NVIDIA)
    if sys.platform == "win32":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                devices = []
                for name in res.stdout.strip().splitlines():
                    clean_name = name.strip()
                    if not clean_name or is_integrated_gpu(clean_name):
                        continue
                    lower = clean_name.lower()
                    if any(k in lower for k in ("nvidia", "geforce", "rtx", "quadro", "tesla", "radeon", "intel arc")):
                        devices.append({"index": len(devices), "name": clean_name, "mem_mb": 0})
                if devices:
                    return devices
        except Exception:
            pass

    return []


def generate_worker_bat_content(
    host: str | None = None,
    port: str = "5840",
    root_path: str = "",
    cpu_enable: int = 1,
    gpu_mask: str | int | None = None,
) -> str:
    """Generate self-contained Windows batch file content for remote LAN worker nodes."""
    target_host = host.strip() if host and host.strip() else get_primary_lan_ip()
    target_port = port.strip() or "5840"
    target_root = root_path.strip()

    if target_root:
        root_init = f'set "TARGET_ROOT={target_root}"'
    else:
        root_init = r'if "%TARGET_ROOT%"=="" set /p TARGET_ROOT="Enter Shared Network Storage path (e.g. \\HOST_PC\SharedMetashape or Z:\): "'

    gpu_flag = ""
    if gpu_mask is not None:
        mask_str = str(gpu_mask).strip()
        if mask_str and mask_str.lower() not in ("auto", "none", "default"):
            gpu_flag = f" --gpu-mask {mask_str}"

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

"%METASHAPE_EXE%" --worker --host {target_host}:{target_port} --root "%TARGET_ROOT%" --cpu-enable {cpu_enable}{gpu_flag}

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


def extract_unc_root(path: str) -> str:
    """Extracts UNC share root (\\\\server\\share) if path is a UNC path.

    Args:
        path: Path string to inspect.

    Returns:
        UNC root string with backslashes (e.g. '\\\\server\\share') or empty string if not UNC.
    """
    if not path or not isinstance(path, str):
        return ""
    norm = path.strip().replace("/", "\\")
    if norm.startswith(r"\\"):
        parts = [p for p in norm[2:].split("\\") if p]
        if len(parts) >= 2:
            return f"\\\\{parts[0]}\\{parts[1]}"
    return ""
