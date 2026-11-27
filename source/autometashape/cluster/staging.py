# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""High-speed local NVMe/SSD scratch and staging management for cluster worker nodes."""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import socket
import string
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

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
        # only query the physical storage bus type on DRIVE_FIXED volumes.
        if drive_type == _WIN32_DRIVE_FIXED:
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
    if not scratch_dir or not str(scratch_dir).strip():
        return (False, 0.0)
    probe_dir = scratch_dir if os.path.exists(scratch_dir) else os.path.dirname(os.path.abspath(scratch_dir))
    while probe_dir and not os.path.exists(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent

    if not probe_dir or not os.path.exists(probe_dir):
        return (False, 0.0)

    try:
        usage = shutil.disk_usage(probe_dir)
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
    candidate_dirs: list[str] = []

    if sys.platform == "win32":
        sys_drive = os.environ.get("SystemDrive", "C:")[0].upper()
        drive_candidates: list[tuple[float, bool, str]] = []  # (free_gb, is_non_system, path)

        for letter in string.ascii_uppercase:
            drive_root = f"{letter}:\\"
            # Check Win32 GetDriveTypeW + bus type BEFORE os.path.exists() to avoid 15-30s hangs on offline mapped drives
            if not _is_fixed_local_windows_drive(drive_root):
                continue
            if os.path.exists(drive_root):
                try:
                    usage = shutil.disk_usage(drive_root)
                    free_gb = usage.free / (1024**3)
                    is_non_system = letter != sys_drive
                    target_folder = rf"{letter}:\AutoMetashape_Scratch"
                    drive_candidates.append((free_gb, is_non_system, target_folder))
                except Exception:
                    continue

        if drive_candidates:
            valid_non_system = sorted(
                [c for c in drive_candidates if c[1] and c[0] >= min_free_gb], key=lambda x: x[0], reverse=True
            )
            valid_system = sorted(
                [c for c in drive_candidates if not c[1] and c[0] >= min_free_gb], key=lambda x: x[0], reverse=True
            )
            under_threshold = sorted(
                [c for c in drive_candidates if c[0] < min_free_gb], key=lambda x: x[0], reverse=True
            )
            candidate_dirs.extend(c[2] for c in (valid_non_system + valid_system + under_threshold))
    else:
        linux_candidates = ["/mnt/scratch", "/var/tmp", "/tmp", os.path.expanduser("~/AutoMetashape_Scratch")]
        ranked_linux: list[tuple[float, str]] = []
        for cand in linux_candidates:
            probe_p = cand if os.path.exists(cand) else os.path.dirname(cand)
            if os.path.exists(probe_p):
                try:
                    u = shutil.disk_usage(probe_p)
                    f_gb = u.free / (1024**3)
                    target_p = (
                        os.path.join(cand, "AutoMetashape_Scratch")
                        if not cand.endswith("AutoMetashape_Scratch")
                        else cand
                    )
                    ranked_linux.append((f_gb, target_p))
                except Exception:
                    continue
        ranked_linux.sort(key=lambda x: x[0], reverse=True)
        candidate_dirs.extend(p for _, p in ranked_linux)

    fallback = os.path.join(tempfile.gettempdir(), "AutoMetashape_Scratch")
    if fallback not in candidate_dirs:
        candidate_dirs.append(fallback)

    # Probe ranked candidates in priority order until one is writable (never drop straight to /tmp on a read-only mount)
    for candidate_dir in candidate_dirs:
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
            continue

    return os.path.abspath(tempfile.gettempdir())


def get_psx_files_dir(psx_path: str) -> str:
    """Returns the associated .files directory for a .psx project path."""
    if psx_path.lower().endswith(".psx"):
        return psx_path[:-4] + ".files"
    return psx_path + ".files"


def _read_primary_chunk_id(project_zip_path: str) -> str | None:
    """Read the primary/active chunk ID from a Metashape ``project.zip`` archive."""
    if not project_zip_path or not os.path.isfile(project_zip_path):
        return None
    try:
        with zipfile.ZipFile(project_zip_path, "r") as zf:
            if "doc.xml" not in zf.namelist():
                return None
            xml_txt = zf.read("doc.xml").decode("utf-8", errors="replace")
    except Exception:
        return None

    try:
        root_elem = ET.fromstring(xml_txt)
        chunks_elem = root_elem.find("chunks")
        if chunks_elem is not None:
            chunk_ids = [
                str(c.attrib.get("id", "")).strip()
                for c in chunks_elem.findall("chunk")
                if str(c.attrib.get("id", "")).strip().isdigit()
            ]
            if chunk_ids:
                active_id = str(chunks_elem.attrib.get("active_id", "")).strip()
                return active_id if active_id in chunk_ids else chunk_ids[0]
    except Exception:
        pass

    chunks = re.findall(r'<chunk\s+id="(\d+)"\s+path="([^"]+)"\s*/>', xml_txt)
    if not chunks:
        return None
    chunk_ids = [cid for cid, _ in chunks]
    active_match = re.search(r'active_id="(\d+)"', xml_txt)
    if active_match and active_match.group(1) in chunk_ids:
        return active_match.group(1)
    return chunk_ids[0]


def reconcile_staged_psx_chunk_id(
    local_stage_dir: str,
    target_network_dir: str,
    project_name: str,
    target_chunk_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Re-index a single-chunk staged ``.psx`` to match the shared stub's chunk ID.

    When Metashape Worker runs a monolithic ``RunScript`` task against a shared ``.psx``
    stub (typically chunk ``0``), its C++ epilogue validates/closes that pre-loaded chunk
    directory after the script exits. If Stage-4 ICP bridging ran ``doc.mergeChunks()``
    and removed chunk ``0`` (leaving e.g. ``1/chunk.zip`` or ``3/chunk.zip``), syncing the
    staged project back deletes ``0/chunk.zip`` and triggers ``io-error: Can't open file:
    .../0/chunk.zip``, causing Metashape Server to retry an already-completed job forever.

    Must be invoked BEFORE deleting ``target_network_dir/<project_name>.files`` so the
    original stub chunk ID can be read accurately.

    Args:
        local_stage_dir: Directory containing the finalized ``.psx`` and ``.files`` folder.
        target_network_dir: Shared network output directory containing the pre-flight stub.
        project_name: Base project filename stem (without ``.psx``).
        target_chunk_id: Optional explicit target chunk ID (used in direct-storage mode when
            the original stub was already overwritten in place).

    Returns:
        Tuple of ``(reconciled, old_chunk_id, target_chunk_id)``.
    """
    if not local_stage_dir or not project_name:
        return False, None, None

    local_files_dir = get_psx_files_dir(os.path.join(local_stage_dir, f"{project_name}.psx"))
    local_pz = os.path.join(local_files_dir, "project.zip")
    if not os.path.isfile(local_pz):
        return False, None, None

    if target_chunk_id is not None and str(target_chunk_id).isdigit():
        target_id = str(target_chunk_id)
    else:
        net_pz = (
            os.path.join(get_psx_files_dir(os.path.join(target_network_dir, f"{project_name}.psx")), "project.zip")
            if target_network_dir
            else ""
        )
        target_id = _read_primary_chunk_id(net_pz) or "0"

    try:
        with zipfile.ZipFile(local_pz, "r") as zf:
            names = zf.namelist()
            if "doc.xml" not in names:
                return False, None, target_id
            xml_txt = zf.read("doc.xml").decode("utf-8", errors="replace")
            other_entries = [(n, zf.read(n)) for n in names if n != "doc.xml"]
    except Exception:
        return False, None, target_id

    try:
        root_elem = ET.fromstring(xml_txt)
        chunks_elem = root_elem.find("chunks")
        chunk_nodes = chunks_elem.findall("chunk") if chunks_elem is not None else []
    except Exception:
        return False, None, target_id

    if not chunk_nodes:
        return False, None, target_id

    for node in chunk_nodes:
        nid = str(node.attrib.get("id", ""))
        npath = str(node.attrib.get("path", "")).replace("\\", "/")
        if nid == target_id and npath == f"{target_id}/chunk.zip":
            return False, target_id, target_id

    active_attr = str(chunks_elem.attrib.get("active_id", "")) if chunks_elem is not None else ""
    target_node = next(
        (n for n in chunk_nodes if active_attr and str(n.attrib.get("id", "")) == active_attr),
        chunk_nodes[0],
    )
    old_id = str(target_node.attrib.get("id", ""))
    old_path = str(target_node.attrib.get("path", "")).replace("\\", "/")
    old_dir = old_path.split("/")[0] if "/" in old_path else ""
    if not old_dir.isdigit() or old_path != f"{old_dir}/chunk.zip":
        return False, old_id, target_id
    if old_id == target_id and old_dir == target_id:
        return False, old_id, target_id

    src_dir = os.path.join(local_files_dir, old_dir)
    dst_dir = os.path.join(local_files_dir, target_id)
    if not os.path.isdir(src_dir):
        return False, old_id, target_id

    target_node.attrib["id"] = target_id
    target_node.attrib["path"] = f"{target_id}/chunk.zip"
    if chunks_elem is not None:
        all_numeric_ids = [int(c.attrib["id"]) for c in chunk_nodes if str(c.attrib.get("id", "")).isdigit()]
        chunks_elem.attrib["next_id"] = str(max(all_numeric_ids, default=0) + 1)
        chunks_elem.attrib["active_id"] = target_id
    xml_body = ET.tostring(root_elem, encoding="unicode").replace(" />", "/>")
    new_xml = f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'

    renamed = False
    dst_bak = f"{dst_dir}.reconcile_bak_{os.getpid()}"
    backed_up_dst = False
    tmp_pz = local_pz + ".reconcile.tmp"
    try:
        if old_dir != target_id:
            if os.path.isdir(dst_bak):
                shutil.rmtree(dst_bak, ignore_errors=True)
            if os.path.isdir(dst_dir):
                os.rename(dst_dir, dst_bak)
                backed_up_dst = True
            os.rename(src_dir, dst_dir)
            renamed = True

        with zipfile.ZipFile(tmp_pz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.xml", new_xml.encode("utf-8"))
            for name, payload in other_entries:
                zf.writestr(name, payload)
        os.replace(tmp_pz, local_pz)
        if backed_up_dst and os.path.isdir(dst_bak):
            shutil.rmtree(dst_bak, ignore_errors=True)
        return True, old_id, target_id
    except Exception:
        if os.path.isfile(tmp_pz):
            try:
                os.remove(tmp_pz)
            except OSError:
                pass
        if renamed and os.path.isdir(dst_dir) and not os.path.exists(src_dir):
            try:
                os.rename(dst_dir, src_dir)
            except OSError:
                pass
        if backed_up_dst and os.path.isdir(dst_bak) and not os.path.exists(dst_dir):
            try:
                os.rename(dst_bak, dst_dir)
            except OSError:
                pass
        return False, old_id, target_id


def _is_disk_full_error(exc: BaseException) -> bool:
    """Return True if ``exc`` (or a wrapped shutil/cause error) represents ENOSPC or Win32 Error 112."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if getattr(cur, "errno", None) == errno.ENOSPC or getattr(cur, "winerror", None) == 112:
            return True
        msg = str(cur)
        if "[WinError 112]" in msg or "[Errno 28]" in msg or "There is not enough space on the disk" in msg:
            return True
        for child_attr in ("__cause__", "__context__"):
            child = getattr(cur, child_attr, None)
            if isinstance(child, BaseException):
                stack.append(child)
    return False


def _calc_staged_payload_bytes(local_stage_dir: str) -> int:
    """Calculate total size of non-transient payload files inside ``local_stage_dir``."""
    total = 0
    for root, _, files in os.walk(local_stage_dir):
        for fname in files:
            low = fname.lower()
            if low == "lock" or low == "model.tmp" or low.endswith(".tmp"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def _is_same_physical_dir(dir_a: str, dir_b: str) -> bool:
    """Return True if ``dir_a`` and ``dir_b`` refer to the same physical directory (including symlinks/junctions)."""
    try:
        if os.path.exists(dir_a) and os.path.exists(dir_b) and os.path.samefile(dir_a, dir_b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(dir_a)) == os.path.normcase(os.path.realpath(dir_b))


def _sync_directory_atomic(s_path: str, d_path: str, uniq_tag: str, ignore_fn) -> None:
    """Copy a staging directory to ``d_path`` via sibling temp-dir swap with safe rollback."""
    tmp_d = f"{d_path}.sync_new_{uniq_tag}"
    old_d = f"{d_path}.sync_old_{uniq_tag}"
    if os.path.exists(tmp_d):
        shutil.rmtree(tmp_d, ignore_errors=True)
    try:
        shutil.copytree(s_path, tmp_d, dirs_exist_ok=True, ignore=ignore_fn)
    except Exception:
        shutil.rmtree(tmp_d, ignore_errors=True)
        raise
    moved_old = False
    try:
        if os.path.isdir(d_path):
            if os.path.exists(old_d):
                shutil.rmtree(old_d, ignore_errors=True)
            os.rename(d_path, old_d)
            moved_old = True
        os.rename(tmp_d, d_path)
        if os.path.isdir(old_d):
            shutil.rmtree(old_d, ignore_errors=True)
    except OSError as e_swap:
        shutil.rmtree(tmp_d, ignore_errors=True)
        if _is_disk_full_error(e_swap) or not moved_old:
            if moved_old and os.path.isdir(old_d) and not os.path.exists(d_path):
                try:
                    os.rename(old_d, d_path)
                except OSError:
                    pass
            raise
        try:
            shutil.copytree(s_path, d_path, dirs_exist_ok=True, ignore=ignore_fn)
        except Exception:
            if moved_old and os.path.isdir(old_d):
                if os.path.exists(d_path):
                    shutil.rmtree(d_path, ignore_errors=True)
                try:
                    os.rename(old_d, d_path)
                except OSError:
                    pass
            raise
        if os.path.isdir(old_d):
            shutil.rmtree(old_d, ignore_errors=True)


def sync_staged_project_to_network(local_stage_dir: str, target_network_dir: str) -> bool:
    """Synchronizes finalized project and deliverables from local NVMe scratch to central network storage.

    Transfers all deliverables, models, textures, reports, and the consolidated ``.psx`` + ``.files``
    archive back to the shared storage root atomically (copying directories to a temporary sibling
    directory before swapping, checking target headroom, and publishing ``.psx`` last so a mid-transfer
    SMB error or Win32 Error 112 never destroys the previous valid checkpoint), then cleans up local scratch.
    """
    if not os.path.isdir(local_stage_dir):
        return False

    if _is_same_physical_dir(local_stage_dir, target_network_dir):
        return True

    os.makedirs(target_network_dir, exist_ok=True)
    raw_host = (os.environ.get("COMPUTERNAME") or "").strip() or socket.gethostname() or "node"
    host_tag = re.sub(r"[^0-9A-Za-z_-]", "_", raw_host)[:24] or "node"
    uniq_tag = f"{host_tag}_{os.getpid()}"

    try:
        # Pre-check target filesystem capacity before overwriting or staging large payloads
        needed_bytes = _calc_staged_payload_bytes(local_stage_dir)
        if needed_bytes > 0:
            try:
                free_bytes = shutil.disk_usage(target_network_dir).free
                if free_bytes < needed_bytes:
                    raise OSError(
                        errno.ENOSPC,
                        f"Insufficient free space on '{target_network_dir}' "
                        f"(need {needed_bytes} B, free {free_bytes} B)",
                    )
            except OSError as e_du:
                if _is_disk_full_error(e_du):
                    raise

        # Pre-clean any lingering local lock files before network transfer
        for root, _, files in os.walk(local_stage_dir):
            for fname in files:
                if fname.lower() == "lock":
                    try:
                        os.remove(os.path.join(root, fname))
                    except Exception:
                        pass

        ignore_transient = shutil.ignore_patterns("lock", "LOCK", "model.tmp", "*.tmp", "*.TMP")
        items = os.listdir(local_stage_dir)
        psx_items = [it for it in items if it.lower().endswith(".psx")]
        other_items = [it for it in items if it not in psx_items]

        # 1. Copy deliverables & .files directories (atomic sibling copy-then-swap)
        for item in other_items:
            s_path = os.path.join(local_stage_dir, item)
            d_path = os.path.join(target_network_dir, item)

            if os.path.isdir(s_path):
                _sync_directory_atomic(s_path, d_path, uniq_tag, ignore_transient)
            else:
                low_item = item.lower()
                if not low_item.endswith(".tmp") and low_item != "lock":
                    tmp_file = f"{d_path}.sync_tmp_{uniq_tag}"
                    try:
                        shutil.copy2(s_path, tmp_file)
                    except Exception:
                        if os.path.isfile(tmp_file):
                            try:
                                os.remove(tmp_file)
                            except OSError:
                                pass
                        raise
                    try:
                        os.replace(tmp_file, d_path)
                    except OSError as e_rep:
                        if os.path.isfile(tmp_file):
                            try:
                                os.remove(tmp_file)
                            except OSError:
                                pass
                        if _is_disk_full_error(e_rep):
                            raise
                        shutil.copy2(s_path, d_path)

        # 2. Publish .psx manifest(s) LAST via atomic temp replace so .psx never points at partial .files
        for item in psx_items:
            s_path = os.path.join(local_stage_dir, item)
            d_path = os.path.join(target_network_dir, item)
            if os.path.isfile(s_path):
                tmp_psx = f"{d_path}.sync_tmp_{uniq_tag}"
                try:
                    shutil.copy2(s_path, tmp_psx)
                    os.replace(tmp_psx, d_path)
                except Exception:
                    if os.path.isfile(tmp_psx):
                        try:
                            os.remove(tmp_psx)
                        except OSError:
                            pass
                    raise

        # Cleanup local staging directory upon verified completion
        try:
            shutil.rmtree(local_stage_dir, ignore_errors=True)
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"[ERROR] Failed to synchronize staged project to network share: {e}", file=sys.stderr)
        return False
