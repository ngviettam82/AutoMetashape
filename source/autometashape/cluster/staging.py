# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""High-speed local NVMe/SSD scratch and staging management for cluster worker nodes."""

from __future__ import annotations

import os
import shutil
import string
import sys
import tempfile


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
        if sys.platform != "win32" and len(candidate_dir) >= 2 and candidate_dir[1] == ":":
            return candidate_dir
        os.makedirs(candidate_dir, exist_ok=True)
        probe_file = os.path.join(candidate_dir, f".write_probe_{os.getpid()}.tmp")
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("probe")
        if os.path.isfile(probe_file):
            os.remove(probe_file)
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
