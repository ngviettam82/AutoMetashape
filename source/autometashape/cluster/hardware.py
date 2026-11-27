# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware, network IP, and executable discovery across workstation environments."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
from typing import Any


def _is_fixed_local_drive_fast(drive_letter: str) -> bool:
    """Return True if ``drive_letter`` is a fixed local Windows drive without touching SMB/network I/O."""
    if sys.platform != "win32":
        return True
    try:
        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None:
            return True
        # DRIVE_FIXED = 3; rejects DRIVE_NO_ROOT_DIR (1), DRIVE_REMOVABLE (2), DRIVE_REMOTE (4), DRIVE_CDROM (5)
        return int(kernel32.GetDriveTypeW(f"{drive_letter.upper()}:\\")) == 3
    except Exception:
        return True


def get_local_hostname() -> str:
    """Return the local host machine name cross-platform (Windows, Linux, macOS)."""
    name = (os.environ.get("COMPUTERNAME") or "").strip()
    if name:
        return name
    try:
        host = socket.gethostname().strip()
        if host:
            return host
    except Exception:
        pass
    return "unknown"


def is_healthy_online_worker(worker: Any) -> bool:
    """Return True if a Metashape Server worker record is online and not marked aborted."""
    if not isinstance(worker, dict):
        return False
    state = str(worker.get("state", "")).strip().lower()
    return state not in ("offline", "aborted", "disconnected") and not bool(worker.get("aborted"))


def find_metashape_path() -> str | None:
    """Locate Agisoft Metashape Pro executable dynamically across platforms."""
    # 1. Bundled relative to current Python if running inside Metashape's embedded Python
    py_dir = os.path.dirname(sys.executable)
    for rel_cand in (
        os.path.join(py_dir, "..", "metashape.exe"),
        os.path.join(py_dir, "..", "metashape"),
        os.path.join(py_dir, "..", "..", "metashape"),
        os.path.join(py_dir, "..", "..", "metashape.sh"),
    ):
        candidate = os.path.normpath(rel_cand)
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

        # Dynamic drive scanning (check GetDriveTypeW first to avoid 15-30s SMB hangs on offline shares)
        for drive in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(drive):
                continue
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
        for cand in (
            "/opt/metashape-pro/metashape.sh",
            "/opt/metashape-pro/metashape",
            "/usr/local/bin/metashape",
            "/usr/bin/metashape",
        ):
            if os.path.isfile(cand):
                return cand

    return None


def find_metashape_python_path(metashape_exe: str | None = None) -> str | None:
    """Locate Metashape Pro bundled Python executable (python/python.exe).

    Prioritizes the self-contained Python bundled inside Metashape Pro installations,
    enabling full cluster worker operation on machines without system Python.
    """
    if not metashape_exe:
        metashape_exe = find_metashape_path()

    if metashape_exe and os.path.isfile(metashape_exe):
        exe_dir = os.path.dirname(metashape_exe)
        for sub in ("python/python.exe", "python/bin/python3", "python/bin/python", "python.exe"):
            cand = os.path.normpath(os.path.join(exe_dir, sub))
            if os.path.isfile(cand):
                return cand

    # Check standard install locations
    if sys.platform == "win32":
        for base in [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("ProgramW6432"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]:
            if base:
                for sub in (
                    os.path.join("Agisoft", "Metashape Pro", "python", "python.exe"),
                    os.path.join("Programs", "Agisoft", "Metashape Pro", "python", "python.exe"),
                ):
                    cand = os.path.join(base, sub)
                    if os.path.isfile(cand):
                        return cand

        for drive in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(drive):
                continue
            for sub in (
                rf"{drive}:\Program Files\Agisoft\Metashape Pro\python\python.exe",
                rf"{drive}:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe",
                rf"{drive}:\Agisoft\Metashape Pro\python\python.exe",
            ):
                if os.path.isfile(sub):
                    return sub

    # Fallback to sys.executable if it is located inside a Metashape folder
    if "metashape" in sys.executable.lower():
        return sys.executable

    return sys.executable


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
            os.environ.get("LOCALAPPDATA"),
        ]:
            if base:
                for sub in (
                    os.path.join("Agisoft", "Metashape Pro", "metashape-server.exe"),
                    os.path.join("Programs", "Agisoft", "Metashape Pro", "metashape-server.exe"),
                ):
                    cand = os.path.join(base, sub)
                    if os.path.isfile(cand):
                        return cand
        for drive in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(drive):
                continue
            for sub in (
                rf"{drive}:\Program Files\Agisoft\Metashape Pro\metashape-server.exe",
                rf"{drive}:\Agisoft\Metashape Pro\metashape-server.exe",
            ):
                if os.path.isfile(sub):
                    return sub
    elif sys.platform == "darwin":
        cand = "/Applications/MetashapePro.app/Contents/MacOS/metashape-server"
        if os.path.isfile(cand):
            return cand
    else:
        for cand in (
            "/opt/metashape-pro/metashape-server",
            "/usr/local/bin/metashape-server",
            "/usr/bin/metashape-server",
        ):
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
            os.environ.get("LOCALAPPDATA"),
        ]:
            if base:
                for sub in (
                    os.path.join("Agisoft", "Metashape Pro", "monitor.exe"),
                    os.path.join("Programs", "Agisoft", "Metashape Pro", "monitor.exe"),
                ):
                    cand = os.path.join(base, sub)
                    if os.path.isfile(cand):
                        return cand
        for drive in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(drive):
                continue
            for sub in (
                rf"{drive}:\Program Files\Agisoft\Metashape Pro\monitor.exe",
                rf"{drive}:\Agisoft\Metashape Pro\monitor.exe",
            ):
                if os.path.isfile(sub):
                    return sub
    elif sys.platform == "darwin":
        cand = "/Applications/MetashapePro.app/Contents/MacOS/monitor"
        if os.path.isfile(cand):
            return cand
    else:
        for cand in ("/opt/metashape-pro/monitor", "/usr/local/bin/monitor"):
            if os.path.isfile(cand):
                return cand

    return None


def get_primary_lan_ip() -> str:
    """Detect the host machine's primary LAN IPv4 address (e.g. 192.168.x.x or 10.x.x.x)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        s.close()

    # Airgapped / offline LAN fallback: inspect hostname IP resolution across all IPv4 addresses
    try:
        host_name = socket.gethostname()
        ip = socket.gethostbyname(host_name)
        if ip and not ip.startswith("127."):
            return ip
        for res in socket.getaddrinfo(host_name, None, socket.AF_INET):
            cand = res[4][0]
            if cand and not cand.startswith("127."):
                return cand
    except Exception:
        pass

    return "127.0.0.1"


_TRADEMARK_RE = re.compile(r"(?:\(r\)|\(tm\)|[®™])", re.IGNORECASE)
_AMD_IGPU_RE = re.compile(r"\b(?:610m|660m|680m|740m|760m|780m|820m|840m|860m|880m|890m)\b")
_INTEL_DISCRETE_ARC_RE = re.compile(r"\barc\s+(?:[ab]\d{3}m?|pro)\b")
_INTEL_IGPU_RE = re.compile(r"\bintel\s+(?:uhd|hd|iris)\b")


def _normalize_gpu_name(name: str) -> str:
    """Lowercase GPU name and strip ASCII/Unicode trademark markers ((R), (TM), ®, ™)."""
    cleaned = _TRADEMARK_RE.sub("", str(name or ""))
    return " ".join(cleaned.lower().split())


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def is_integrated_gpu(name: str) -> bool:
    """Return True if GPU device name matches integrated graphics or software renderers."""
    lower = _normalize_gpu_name(name)
    if not lower:
        return False
    # Software / virtual renderers
    if any(sw in lower for sw in ("microsoft basic render", "llvmpipe", "swiftshader", "softpipe")):
        return True
    # Discrete Intel Arc A-series / B-series / mobile M / Pro GPUs are NOT integrated
    if _INTEL_DISCRETE_ARC_RE.search(lower):
        return False
    # Integrated Intel Arc Graphics (Core Ultra laptop/desktop iGPU without A###/B###/Pro model)
    if "intel" in lower and "arc" in lower:
        return True
    if _INTEL_IGPU_RE.search(lower):
        return True
    # Discrete AMD RX Vega / Radeon Pro Vega / Vega Frontier are NOT integrated
    if any(dv in lower for dv in ("rx vega", "pro vega", "vega frontier")):
        return False
    # Integrated AMD Ryzen mobile/desktop APUs (610M-890M, Vega iGPU, generic Radeon Graphics)
    if "radeon" in lower and _AMD_IGPU_RE.search(lower):
        return True
    return any(
        ig in lower
        for ig in (
            "intel graphics",
            "radeon graphics",
            "vega",
        )
    )


def compute_discrete_gpu_mask(gpus: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """Compute Metashape's exact discrete GPU bitmask from an ordered ``enumGPUDevices()`` list.

    Excludes integrated graphics (Intel UHD/HD/Iris/Core Ultra Arc iGPU, AMD Ryzen APUs) and
    software renderers while selecting all discrete NVIDIA, AMD Radeon RX/Pro, and Intel Arc A/B
    GPUs using their exact Metashape device index (``1 << i``). Falls back to Apple Silicon or
    non-integrated compute devices when no PCIe discrete GPU is present.

    Returns:
        Tuple of ``(bitmask: int, selected_devices: list[dict])``.
    """
    if not gpus:
        return 0, []

    discrete_mask = 0
    selected: list[dict[str, Any]] = []
    non_igpu_fallback: list[tuple[int, dict[str, Any]]] = []

    for i, g in enumerate(gpus):
        if not isinstance(g, dict):
            continue
        raw_name = str(g.get("name", f"GPU {i}")).strip()
        name_low = _normalize_gpu_name(raw_name)
        vendor_low = _normalize_gpu_name(str(g.get("vendor", "")))
        mem_bytes = _safe_int(g.get("mem_size", 0), 0)
        mem_mb = mem_bytes // (1024 * 1024) if mem_bytes > 0 else _safe_int(g.get("mem_mb", 0), 0)

        if is_integrated_gpu(raw_name):
            continue

        entry = {"index": i, "name": raw_name, "mem_mb": mem_mb}
        non_igpu_fallback.append((i, entry))

        is_discrete = bool(_INTEL_DISCRETE_ARC_RE.search(name_low)) or any(
            k in vendor_low or k in name_low
            for k in (
                "nvidia",
                "geforce",
                "rtx",
                "gtx",
                "quadro",
                "tesla",
                "radeon",
                "firepro",
                "instinct",
                "intel arc",
                "apple",
            )
        )
        if is_discrete:
            discrete_mask |= 1 << i
            selected.append(entry)

    if discrete_mask > 0:
        return discrete_mask, selected

    # Fallback: if there are non-integrated GPUs whose vendor string is unrecognized, enable them
    if non_igpu_fallback:
        fb_mask = 0
        fb_devs: list[dict[str, Any]] = []
        for idx, entry in non_igpu_fallback:
            fb_mask |= 1 << idx
            fb_devs.append(entry)
        return fb_mask, fb_devs

    return 0, []


def probe_metashape_gpu_mask(
    metashape_exe: str | None = None,
    timeout: float = 30.0,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Query Metashape Pro's native ``enumGPUDevices()`` to compute the exact discrete ``--gpu-mask``.

    CRITICAL: Never derive Metashape's ``--gpu-mask`` bit indices from ``nvidia-smi`` or WMI because
    Metashape enumerates OpenCL/Vulkan iGPUs (e.g. Intel Graphics at index 0) alongside CUDA GPUs
    (e.g. RTX 5080 at index 1 -> mask 2). Probing ``Metashape.app.enumGPUDevices()`` guarantees
    100% index parity with ``metashape --worker --gpu-mask``.

    Returns:
        Tuple of ``(bitmask_or_None, selected_devices)``. Returns ``(None, [])`` if Metashape cannot
        be probed (allowing safe fallback without guessing wrong indices).
    """
    # 1. Fast path: already running inside Metashape embedded Python
    try:
        import Metashape

        if hasattr(Metashape, "app") and hasattr(Metashape.app, "enumGPUDevices"):
            raw_gpus = Metashape.app.enumGPUDevices() or []
            mask, devs = compute_discrete_gpu_mask(list(raw_gpus))
            return mask, devs
    except Exception:
        pass

    # 2. Subprocess probe via metashape -r (used by launch_worker.py under standalone python.exe)
    exe = metashape_exe or find_metashape_path()
    if not exe or not os.path.isfile(exe):
        return None, []

    probe_code = (
        "import json, Metashape\n"
        "gpus = Metashape.app.enumGPUDevices() if hasattr(Metashape, 'app') and hasattr(Metashape.app, 'enumGPUDevices') else []\n"
        "print('===AM_GPUS_START===')\n"
        "print(json.dumps(list(gpus), default=str))\n"
        "print('===AM_GPUS_END===')\n"
    )
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="_am_gpu_probe_", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(probe_code)
        probe_cmd = [exe]
        probe_env = os.environ.copy()
        if sys.platform.startswith("linux") and not probe_env.get("DISPLAY"):
            probe_cmd.extend(["-platform", "offscreen"])
            probe_env.setdefault("QT_QPA_PLATFORM", "offscreen")
        probe_cmd.extend(["-r", tmp_path])
        res = subprocess.run(
            probe_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_env,
            timeout=timeout,
        )
        stdout = res.stdout or ""
        s_tok = "===AM_GPUS_START==="
        e_tok = "===AM_GPUS_END==="
        if s_tok in stdout and e_tok in stdout:
            payload = stdout.split(s_tok, 1)[1].split(e_tok, 1)[0].strip()
            raw_gpus = json.loads(payload)
            if isinstance(raw_gpus, list):
                mask, devs = compute_discrete_gpu_mask(raw_gpus)
                return mask, devs
    except Exception:
        pass
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return None, []


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
                    if not isinstance(g, dict):
                        continue
                    name = str(g.get("name", f"GPU {i}")).strip()
                    if is_integrated_gpu(name):
                        continue
                    devices.append(
                        {
                            "index": i,
                            "name": name,
                            "mem_mb": _safe_int(g.get("mem_size", 0), 0) // (1024 * 1024),
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
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip():
                devices = []
                for name in res.stdout.strip().splitlines():
                    clean_name = name.strip()
                    if not clean_name or is_integrated_gpu(clean_name):
                        continue
                    lower = _normalize_gpu_name(clean_name)
                    if _INTEL_DISCRETE_ARC_RE.search(lower) or any(
                        k in lower for k in ("nvidia", "geforce", "rtx", "quadro", "tesla", "radeon", "intel arc")
                    ):
                        devices.append({"index": len(devices), "name": clean_name, "mem_mb": 0})
                if devices:
                    return devices
        except Exception:
            pass

    return []


_BATCH_UNSAFE_CHARS_RE = re.compile(r'[\r\n&|<>^"%`]')


def generate_worker_bat_content(
    host: str | None = None,
    port: str | int = "5840",
    root_path: str = "",
    cpu_enable: int = 1,
    gpu_mask: str | int | None = None,
) -> str:
    """Generate self-contained Windows batch file content for remote LAN worker nodes."""
    target_host = host.strip() if host and host.strip() else get_primary_lan_ip()
    if _BATCH_UNSAFE_CHARS_RE.search(target_host):
        raise ValueError(f"Unsafe characters in worker host: {target_host!r}")
    target_port = str(port).strip() if port is not None else "5840"
    target_port = target_port or "5840"
    if not target_port.isdigit() or not (1 <= int(target_port) <= 65535):
        raise ValueError(f"Invalid worker port: {target_port!r}")
    cpu_flag_val = 1 if int(cpu_enable) else 0
    target_root = root_path.strip()
    if _BATCH_UNSAFE_CHARS_RE.search(target_root):
        raise ValueError(f"Unsafe characters in worker root_path: {target_root!r}")

    if target_root:
        root_init = f'set "TARGET_ROOT={target_root}"'
    else:
        root_init = r'if "%TARGET_ROOT%"=="" set /p TARGET_ROOT="Enter Shared Network Storage path (e.g. \\HOST_PC\SharedMetashape or Z:\): "'

    gpu_flag = ""
    if gpu_mask is not None:
        mask_str = str(gpu_mask).strip()
        if mask_str and mask_str.lower() not in ("auto", "none", "default"):
            if not re.fullmatch(r"(?:0x[0-9a-fA-F]+|\d+)", mask_str):
                raise ValueError(f"Invalid gpu_mask value: {mask_str!r}")
            gpu_flag = f" --gpu-mask {mask_str}"

    return f"""@echo off
chcp 65001 > nul
setlocal
title Metashape Distributed Cluster Worker Node

echo =======================================================================
echo   AutoMetashape Distributed Cluster - Worker Node Launcher
echo   Cluster Target Server : {target_host}:{target_port}
echo =======================================================================
echo.

:: Configure and validate Shared Network Storage root path (before EnableDelayedExpansion so '!' is preserved)
{root_init}
if "%TARGET_ROOT%"=="" (
    echo [ERROR] Shared network root path cannot be empty.
    pause
    exit /b 1
)

echo [OK] Shared Network Root   : %TARGET_ROOT%

setlocal enabledelayedexpansion
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

"%METASHAPE_EXE%" --worker --host {target_host}:{target_port} --root "%TARGET_ROOT%" --cpu-enable {cpu_flag_val}{gpu_flag}

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
    """Test TCP socket reachability to target cluster host and port (IPv4/IPv6 safe)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (socket.timeout, OSError, ValueError):
        return False


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

    Handles Win32 extended-length UNC prefixes (``\\\\?\\UNC\\server\\share\\...``)
    while rejecting extended local drive paths (``\\\\?\\C:\\...``).

    Args:
        path: Path string to inspect.

    Returns:
        UNC root string with backslashes (e.g. '\\\\server\\share') or empty string if not UNC.
    """
    if not path or not isinstance(path, str):
        return ""
    norm = path.strip().replace("/", "\\")
    if norm.lower().startswith("\\\\?\\unc\\"):
        parts = [p for p in norm[8:].split("\\") if p]
        if len(parts) >= 2:
            return f"\\\\{parts[0]}\\{parts[1]}"
        return ""
    if norm.startswith("\\\\?\\") or norm.startswith("\\\\.\\"):
        return ""
    if norm.startswith(r"\\"):
        parts = [p for p in norm[2:].split("\\") if p]
        if len(parts) >= 2:
            return f"\\\\{parts[0]}\\{parts[1]}"
    return ""
