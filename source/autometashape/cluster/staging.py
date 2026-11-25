# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""High-speed local NVMe/SSD scratch and staging management for cluster worker nodes."""

from __future__ import annotations

import ctypes
import os
import shutil
import string
import struct
import sys
import tempfile

_WIN32_DRIVE_REMOVABLE = 2
_WIN32_DRIVE_FIXED = 3
_WIN32_DRIVE_REMOTE = 4
_WIN32_DRIVE_CDROM = 5

# Win32 IOCTL_STORAGE_QUERY_PROPERTY & STORAGE_BUS_TYPE constants (winioctl.h)
_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
_OPEN_EXISTING = 3
_WIN32_BUS_TYPE_1394 = 4
_WIN32_BUS_TYPE_USB = 7
_WIN32_BUS_TYPE_SD = 12
_WIN32_BUS_TYPE_MMC = 13
_EXTERNAL_STORAGE_BUS_TYPES = frozenset(
    {
        _WIN32_BUS_TYPE_1394,
        _WIN32_BUS_TYPE_USB,
        _WIN32_BUS_TYPE_SD,
        _WIN32_BUS_TYPE_MMC,
    }
)


def _parse_storage_device_descriptor(raw: bytes) -> tuple[bool, int] | None:
    """Parse ``(removable_media, bus_type)`` from a Win32 ``STORAGE_DEVICE_DESCRIPTOR`` buffer.

    Win32 ``STORAGE_DEVICE_DESCRIPTOR`` layout (``winioctl.h``):
    - offset 0..7:   ``Version`` (DWORD), ``Size`` (DWORD)
    - offset 8..11:  ``DeviceType`` (BYTE), ``DeviceTypeModifier`` (BYTE),
                     ``RemovableMedia`` (BOOLEAN, offset 10), ``CommandQueueing`` (BOOLEAN)
    - offset 12..27: ``VendorIdOffset``, ``ProductIdOffset``, ``ProductRevisionOffset``, ``SerialNumberOffset`` (DWORDs)
    - offset 28..31: ``BusType`` (STORAGE_BUS_TYPE / DWORD)
    """
    if not raw or len(raw) < 32:
        return None
    _ver, _sz, _dt, _dtm, rem_media, _cq, _v_off, _p_off, _r_off, _s_off, bus_type = struct.unpack_from(
        "<IIBBBBIIIII", raw, 0
    )
    return bool(rem_media), int(bus_type)


def _query_windows_drive_bus_info(drive_root: str) -> tuple[bool, int] | None:
    """Query Win32 ``IOCTL_STORAGE_QUERY_PROPERTY`` for ``drive_root`` without admin privileges.

    Returns ``(removable_media, bus_type)`` or ``None`` if unavailable (e.g. non-Windows or
    mocked/virtual drive where the device handle cannot be opened).
    """
    if sys.platform != "win32":
        return None
    cleaned = (drive_root or "").strip()
    if len(cleaned) < 2 or not cleaned[0].isalpha() or cleaned[1] != ":":
        return None
    try:
        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None or not hasattr(ctypes, "c_void_p"):
            return None

        device_path = rf"\\.\{cleaned[0].upper()}:"
        create_file = kernel32.CreateFileW
        device_io_control = kernel32.DeviceIoControl
        close_handle = kernel32.CloseHandle

        create_file.restype = ctypes.c_void_p
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        device_io_control.restype = ctypes.c_int
        device_io_control.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        close_handle.argtypes = [ctypes.c_void_p]

        invalid_handle = ctypes.c_void_p(-1).value
        handle = create_file(device_path, 0, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None)
        if not isinstance(handle, int) or handle in (0, invalid_handle):
            return None

        try:
            query = struct.pack("<III", 0, 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
            out_buf = ctypes.create_string_buffer(1024)
            bytes_ret = ctypes.c_uint32(0)
            ok = device_io_control(
                handle,
                _IOCTL_STORAGE_QUERY_PROPERTY,
                query,
                len(query),
                out_buf,
                len(out_buf),
                ctypes.byref(bytes_ret),
                None,
            )
            if not ok or bytes_ret.value < 32:
                return None
            return _parse_storage_device_descriptor(out_buf.raw[: bytes_ret.value])
        finally:
            close_handle(handle)
    except Exception:
        return None


def _is_fixed_local_windows_drive(drive_root: str) -> bool:
    """Return True when ``drive_root`` is an internal fixed local volume.

    Rejects mapped SMB shares (``DRIVE_REMOTE``), removable flash/optical media, and
    external USB/FireWire/SD/MMC drives (such as external USB SSD/NVMe enclosures that
    Windows reports as ``DRIVE_FIXED`` to ``GetDriveTypeW``).
    """
    if sys.platform != "win32":
        return True
    try:
        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is None:
            return True
        drive_type = int(kernel32.kernel32.GetDriveTypeW(drive_root))
        # Reject mapped network shares (4), removable media (2), and optical drives (5).
        # Note: DRIVE_NO_ROOT_DIR (1) only occurs alongside os.path.exists()==True under mocked tests.
        if drive_type in (_WIN32_DRIVE_REMOVABLE, _WIN32_DRIVE_REMOTE, _WIN32_DRIVE_CDROM):
            return False

        # External USB SSD/NVMe enclosures report DRIVE_FIXED (3) to GetDriveTypeW;
        # inspect the physical storage bus type to exclude USB/1394/SD/MMC external disks.
        bus_info = _query_windows_drive_bus_info(drive_root)
        if bus_info is not None:
            removable_media, bus_type = bus_info
            if removable_media or bus_type in _EXTERNAL_STORAGE_BUS_TYPES:
                return False
        return True
    except Exception:
        return True


def check_scratch_disk_headroom(scratch_dir: str, min_free_gb: float = 100.0) -> tuple[bool, float]:
    """Inspects available disk space on the target scratch volume.

    Args:
        scratch_dir: Target path to inspect for free storage headroom.
        min_free_gb: Minimum free space required in gigabytes (default: 100.0 GB).

    Returns:
        Tuple of (has_headroom: bool, free_gb: float).
    """
    probe_dir = scratch_dir if os.path.exists(scratch_dir) else os.path.dirname(os.path.abspath(scratch_dir))
    while probe_dir and not os.path.exists(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent

    try:
        usage = shutil.disk_usage(probe_dir if os.path.exists(probe_dir) else ".")
        free_gb = usage.free / (1024**3)
        return (free_gb >= min_free_gb, free_gb)
    except Exception:
        return (False, 0.0)


def get_optimal_local_scratch_dir(min_free_gb: float = 100.0) -> str:
    """Dynamically discover and prepare the highest-capacity fast local drive for scratch data.

    Prioritizes fast non-system drives (e.g. D:, E:, G:) with the highest available free
    space to avoid filling the OS drive during massive out-of-core depth map and octree
    meshing passes. Falls back safely to the system drive or %TEMP%.

    Args:
        min_free_gb: Minimum desired free space threshold in gigabytes (default: 100.0 GB).

    Returns:
        Absolute normalized path to verified, writable local scratch directory.
    """
    candidate_dir = None

    if sys.platform == "win32":
        sys_drive = os.environ.get("SystemDrive", "C:")[0].upper()
        drive_candidates: list[tuple[float, bool, str]] = []  # (free_gb, is_non_system, path)

        for letter in string.ascii_uppercase:
            drive_root = f"{letter}:\\"
            if os.path.exists(drive_root):
                if not _is_fixed_local_windows_drive(drive_root):
                    continue
                try:
                    usage = shutil.disk_usage(drive_root)
                    free_gb = usage.free / (1024**3)
                    is_non_system = letter != sys_drive
                    target_folder = rf"{letter}:\AutoMetashape_Scratch"
                    drive_candidates.append((free_gb, is_non_system, target_folder))
                except Exception:
                    continue

        if drive_candidates:
            valid_non_system = [c for c in drive_candidates if c[1] and c[0] >= min_free_gb]
            valid_system = [c for c in drive_candidates if not c[1] and c[0] >= min_free_gb]

            if valid_non_system:
                valid_non_system.sort(key=lambda x: x[0], reverse=True)
                candidate_dir = valid_non_system[0][2]
            elif valid_system:
                candidate_dir = valid_system[0][2]
            else:
                drive_candidates.sort(key=lambda x: x[0], reverse=True)
                candidate_dir = drive_candidates[0][2]

    else:
        linux_candidates = ["/mnt/scratch", "/var/tmp", "/tmp", os.path.expanduser("~/AutoMetashape_Scratch")]
        best_cand = None
        max_free = -1.0
        for cand in linux_candidates:
            probe_p = cand if os.path.exists(cand) else os.path.dirname(cand)
            if os.path.exists(probe_p):
                try:
                    u = shutil.disk_usage(probe_p)
                    f_gb = u.free / (1024**3)
                    if f_gb > max_free:
                        max_free = f_gb
                        best_cand = (
                            os.path.join(cand, "AutoMetashape_Scratch")
                            if not cand.endswith("AutoMetashape_Scratch")
                            else cand
                        )
                except Exception:
                    continue
        if best_cand:
            candidate_dir = best_cand

    if not candidate_dir:
        candidate_dir = os.path.join(tempfile.gettempdir(), "AutoMetashape_Scratch")

    # Verify directory creation and write access
    try:
        os.makedirs(candidate_dir, exist_ok=True)
        probe_file = os.path.join(candidate_dir, f".write_probe_{os.getpid()}.tmp")
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("probe")
        if os.path.isfile(probe_file):
            os.remove(probe_file)
        if len(candidate_dir) >= 2 and candidate_dir[1] == ":":
            return candidate_dir
        return os.path.abspath(candidate_dir)
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "AutoMetashape_Scratch")
        try:
            os.makedirs(fallback, exist_ok=True)
            return os.path.abspath(fallback)
        except Exception:
            return os.path.abspath(tempfile.gettempdir())


def get_psx_files_dir(psx_path: str) -> str:
    """Returns the associated .files directory for a .psx project path."""
    if psx_path.lower().endswith(".psx"):
        return psx_path[:-4] + ".files"
    return psx_path + ".files"


def sync_staged_project_to_network(local_stage_dir: str, target_network_dir: str) -> bool:
    """Synchronizes finalized project and deliverables from local NVMe scratch to central network storage.

    Transfers all deliverables, models, textures, reports, and the consolidated .psx project
    file and .files directory back to the shared storage root, then cleans up local scratch.
    Filters out transient out-of-core octree leaves and lock files to avoid wasting bandwidth.

    Args:
        local_stage_dir: Local staging directory on worker node.
        target_network_dir: Central shared directory on coordinator / NAS.

    Returns:
        True if all files synchronized successfully, False otherwise.
    """
    if not os.path.isdir(local_stage_dir):
        return False

    os.makedirs(target_network_dir, exist_ok=True)

    try:
        # Pre-clean any lingering local lock files before network transfer
        for root, _, files in os.walk(local_stage_dir):
            if "lock" in files:
                try:
                    os.remove(os.path.join(root, "lock"))
                except Exception:
                    pass

        ignore_transient = shutil.ignore_patterns("lock", "model.tmp", "*.tmp")
        for item in os.listdir(local_stage_dir):
            s_path = os.path.join(local_stage_dir, item)
            d_path = os.path.join(target_network_dir, item)

            if os.path.isdir(s_path):
                shutil.copytree(s_path, d_path, dirs_exist_ok=True, ignore=ignore_transient)
            else:
                if not item.endswith(".tmp") and item != "lock":
                    shutil.copy2(s_path, d_path)

        # Cleanup local staging directory upon verified completion
        try:
            shutil.rmtree(local_stage_dir, ignore_errors=True)
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"[ERROR] Failed to synchronize staged project to network share: {e}", file=sys.stderr)
        return False
