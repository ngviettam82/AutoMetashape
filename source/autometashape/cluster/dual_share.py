# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Dual Shared Folder (Multi-SSD) Cluster Orchestration.

Enables splitting raw survey photo I/O (Data Source Shared Folder on SSD #1)
from Metashape project checkpoints and deliverables (Result Shared Folder on SSD #2).

In Metashape Network Processing:
- Workers mount `--root` to the Result Shared Folder (SSD #2) so every batch `.psx`
  project file resolves relative to `--root`.
- Raw survey images remain read-only on the Data Source Shared Folder (SSD #1) and
  are read directly over UNC (`//SERVER/SSD1_Data/...`) with zero raw-file duplication.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

from autometashape.cluster.hardware import extract_unc_root


def is_subpath(child: str, parent: str) -> bool:
    """Return True if ``child`` is equal to or nested beneath ``parent``.

    Compares normalized drive/UNC share roots and exact path segments with Windows
    case-folding, eliminating both prefix-collision bugs (``\\\\SRV\\Data2`` vs
    ``\\\\SRV\\Data``) and Windows ``ntpath.commonpath`` UNC root trailing-slash errors.
    """
    if not child or not parent:
        return False
    try:
        abs_child = normalize_share_path(child)
        abs_parent = normalize_share_path(parent)
        if not abs_child or not abs_parent:
            return False
        if sys.platform == "win32":
            abs_child = abs_child.lower()
            abs_parent = abs_parent.lower()
        drv_c, tail_c = os.path.splitdrive(abs_child)
        drv_p, tail_p = os.path.splitdrive(abs_parent)
        if drv_c != drv_p:
            return False
        segs_c = [s for s in tail_c.replace("\\", "/").split("/") if s]
        segs_p = [s for s in tail_p.replace("\\", "/").split("/") if s]
        return len(segs_c) >= len(segs_p) and segs_c[: len(segs_p)] == segs_p
    except ValueError:
        return False


def is_unc_path(path: str) -> bool:
    """Return True if ``path`` is a Windows/POSIX UNC network share path."""
    cleaned = (path or "").strip()
    return cleaned.startswith(r"\\") or cleaned.startswith("//")


def normalize_share_path(path: str) -> str:
    """Normalize a local or UNC share path without collapsing the leading UNC prefix."""
    raw = (path or "").strip()
    if not raw:
        return ""
    if is_unc_path(raw):
        body = raw.lstrip("\\/").replace("/", "\\").rstrip("\\")
        return f"\\\\{body}" if body else ""
    return os.path.normpath(os.path.abspath(raw))


def resolve_result_network_root(result_share: str, explicit_root: str = "") -> str:
    """Resolve the Metashape cluster ``--root`` strictly from the result share.

    Never derives ``--root`` from ``source_share``, ensuring `.psx` projects and
    cluster batch paths stay rooted on the Result SSD share.
    """
    if explicit_root and explicit_root.strip():
        return normalize_share_path(explicit_root)
    unc_root = extract_unc_root(result_share)
    if unc_root:
        return unc_root
    return normalize_share_path(result_share)


def map_source_to_result_dir(
    source_project_dir: str,
    source_share: str,
    result_share: str,
    project_name: Optional[str] = None,
) -> str:
    """Map a project directory under ``source_share`` to its mirrored folder under ``result_share``.

    - If ``source_project_dir`` is a nested subfolder beneath ``source_share`` (e.g.
      ``\\\\SRV\\SSD1\\BatchA\\Site_01`` under ``\\\\SRV\\SSD1``), mirrors the relative
      path under ``result_share`` (``\\\\SRV\\SSD2\\BatchA\\Site_01``).
    - If ``source_project_dir`` equals ``source_share`` (single-project root), uses
      ``result_share\\<project_name>`` unless ``result_share`` already ends with ``project_name``.
    """
    norm_src_proj = normalize_share_path(source_project_dir)
    norm_src_root = normalize_share_path(source_share)
    norm_res_root = normalize_share_path(result_share)

    if norm_src_root and is_subpath(norm_src_proj, norm_src_root):
        rel = os.path.relpath(norm_src_proj, norm_src_root)
        if rel not in (".", ""):
            return os.path.normpath(os.path.join(norm_res_root, rel))

    inferred_name = project_name or os.path.basename(norm_src_proj.rstrip("\\/"))
    if inferred_name and os.path.basename(norm_res_root.rstrip("\\/")).lower() == inferred_name.lower():
        return norm_res_root
    return os.path.normpath(os.path.join(norm_res_root, inferred_name)) if inferred_name else norm_res_root


def validate_dual_share_topology(
    source_share: str,
    result_share: str,
    result_network_root: str = "",
    min_headroom_gb: float = 50.0,
    require_unc: bool = False,
) -> Dict[str, Any]:
    """Pre-flight validation for a Dual Shared Folder (Data Source + Result) topology.

    Checks:
    1. ``source_share`` exists and is readable (strictly read-only check; never requires
       write access or disk headroom on the source photo SSD).
    2. ``result_share`` exists (or can be created), is writable, and satisfies
       ``min_headroom_gb`` (Win32 Error 112 defense on the Result SSD).
    3. ``result_network_root`` contains ``result_share`` so Metashape batch relative paths
       always resolve cleanly against worker ``--root``.
    4. Emits warnings (or errors if ``require_unc=True``) when local drive letters are used
       in a distributed cluster where remote workers require UNC paths.
    """
    errors: List[str] = []
    warnings: List[str] = []

    norm_source = normalize_share_path(source_share)
    norm_result = normalize_share_path(result_share)
    resolved_net_root = resolve_result_network_root(norm_result, result_network_root)

    if not norm_source:
        errors.append("Data source shared folder (source_share) cannot be empty.")
    elif not os.path.isdir(norm_source):
        errors.append(f"Data source shared folder is unreachable or not a directory: {norm_source}")
    else:
        try:
            os.listdir(norm_source)
        except Exception as exc:
            errors.append(f"Data source shared folder is not readable ({norm_source}): {exc}")

    result_free_gb = 0.0
    result_writable = False
    if not norm_result:
        errors.append("Result shared folder (result_share) cannot be empty.")
    else:
        try:
            os.makedirs(norm_result, exist_ok=True)
        except Exception as exc:
            errors.append(f"Cannot create or access result shared folder ({norm_result}): {exc}")

        if os.path.isdir(norm_result):
            probe_fd = None
            probe_path = ""
            try:
                probe_fd, probe_path = tempfile.mkstemp(prefix=".am_write_probe_", dir=norm_result)
                os.close(probe_fd)
                probe_fd = None
                os.remove(probe_path)
                result_writable = True
            except Exception as exc:
                if probe_fd is not None:
                    try:
                        os.close(probe_fd)
                    except Exception:
                        pass
                if probe_path and os.path.exists(probe_path):
                    try:
                        os.remove(probe_path)
                    except Exception:
                        pass
                errors.append(f"Result shared folder is not writable ({norm_result}): {exc}")

            try:
                usage = shutil.disk_usage(norm_result)
                result_free_gb = round(usage.free / (1024**3), 2)
                if min_headroom_gb > 0 and result_free_gb < min_headroom_gb:
                    errors.append(
                        f"Insufficient headroom on result share ({norm_result}): "
                        f"{result_free_gb:.1f} GB free < {min_headroom_gb:.1f} GB threshold."
                    )
            except Exception as exc:
                warnings.append(f"Could not measure free space on result share ({norm_result}): {exc}")

    if resolved_net_root and norm_result and not is_subpath(norm_result, resolved_net_root):
        errors.append(
            f"Result folder '{norm_result}' is not located under Metashape worker root '{resolved_net_root}'."
        )

    if norm_source and not is_unc_path(norm_source):
        msg = (
            f"Data source folder '{norm_source}' is a local path, not a UNC share (\\\\SERVER\\Share). "
            "Remote cluster workers will only be able to read photos if they share the same path."
        )
        if require_unc:
            errors.append(msg)
        else:
            warnings.append(msg)

    if norm_result and not is_unc_path(norm_result):
        msg = (
            f"Result folder '{norm_result}' is a local path, not a UNC share (\\\\SERVER\\Share). "
            "Remote cluster workers require a UNC path matching worker --root."
        )
        if require_unc:
            errors.append(msg)
        else:
            warnings.append(msg)

    return {
        "valid": len(errors) == 0,
        "source_share": norm_source,
        "result_share": norm_result,
        "result_network_root": resolved_net_root,
        "result_free_gb": result_free_gb,
        "result_writable": result_writable,
        "errors": errors,
        "warnings": warnings,
    }


def _dir_contains_images(directory: str) -> bool:
    """Return True as soon as any supported survey image is found under ``directory``."""
    from autometashape.core.telemetry import SUPPORTED_IMAGE_EXTS

    try:
        for _root, _, files in os.walk(directory):
            for fname in files:
                if fname.lower().endswith(SUPPORTED_IMAGE_EXTS):
                    return True
    except Exception:
        return False
    return False


def discover_dual_share_projects(
    source_share: str,
    result_share: str,
    preset: str = "pro",
    expected_assets: Optional[List[str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
    result_network_root: str = "",
) -> List[Any]:
    """Discover discrete survey projects under ``source_share`` and map outputs to ``result_share``.

    Supports both:
    - Multi-project folders (each immediate image-bearing subfolder under ``source_share``
      becomes a queued job mirrored onto ``result_share\\<Subfolder>``).
    - Single-project folders (if ``source_share`` directly contains survey photos, queues
      one project mapped onto ``result_share\\<ProjectName>``).

    Every job has ``extra_args["network_path"]`` pinned to the resolved ``result_network_root``
    (SSD #2), while ``job.images_dir`` retains the full path on ``source_share`` (SSD #1).
    """
    from autometashape.cluster.supervisor import QueueJob

    norm_source = normalize_share_path(source_share)
    norm_result = normalize_share_path(result_share)
    if not norm_source or not os.path.isdir(norm_source):
        raise ValueError(f"Data source shared folder does not exist or is not a directory: {source_share}")

    resolved_net_root = resolve_result_network_root(norm_result, result_network_root)
    base_extra = dict(extra_args or {})
    if resolved_net_root:
        base_extra["network_path"] = resolved_net_root
    base_extra.setdefault("source_share", norm_source)
    base_extra.setdefault("result_share", norm_result)

    from autometashape.core.telemetry import SUPPORTED_IMAGE_EXTS

    excluded_names = {"model", "models", "output", "outputs", "deliverables", "temp", "tmp", "__pycache__"}
    excluded_prefixes = ("model", "output", "deliverables", "temp", "result")
    jobs: List[QueueJob] = []

    try:
        entries = sorted(os.listdir(norm_source))
    except Exception as exc:
        raise ValueError(f"Failed to list data source shared folder '{norm_source}': {exc}") from exc

    subdirs = []
    for entry in entries:
        full_p = os.path.join(norm_source, entry)
        if os.path.isdir(full_p) and not entry.startswith((".", "$")):
            low_name = entry.lower()
            if low_name not in excluded_names and not low_name.startswith(excluded_prefixes):
                subdirs.append((entry, full_p))

    for folder_name, folder_path in subdirs:
        if _dir_contains_images(folder_path):
            out_dir = map_source_to_result_dir(
                source_project_dir=folder_path,
                source_share=norm_source,
                result_share=norm_result,
                project_name=folder_name,
            )
            jobs.append(
                QueueJob(
                    images_dir=folder_path,
                    output_dir=out_dir,
                    project_name=folder_name,
                    preset=preset,
                    expected_assets=expected_assets,
                    extra_args=dict(base_extra),
                )
            )

    if not jobs:
        root_has_images = any(
            os.path.isfile(os.path.join(norm_source, f)) and f.lower().endswith(SUPPORTED_IMAGE_EXTS) for f in entries
        )
        if root_has_images:
            project_name = os.path.basename(norm_source.rstrip("\\/"))
            out_dir = map_source_to_result_dir(
                source_project_dir=norm_source,
                source_share=norm_source,
                result_share=norm_result,
                project_name=project_name,
            )
            jobs.append(
                QueueJob(
                    images_dir=norm_source,
                    output_dir=out_dir,
                    project_name=project_name,
                    preset=preset,
                    expected_assets=expected_assets,
                    extra_args=dict(base_extra),
                )
            )

    return jobs


def run_dual_share_cluster_pipeline(
    source_share: str,
    result_share: str,
    server_host: str = "127.0.0.1",
    server_port: int = 5840,
    preset: str = "pro",
    ortho_resolution: float = 0.05,
    build_point_cloud: bool = False,
    build_dem: bool = False,
    export_obj: bool = True,
    export_ortho: bool = True,
    export_dem: bool = False,
    export_glb: bool = False,
    export_report: bool = False,
    resume: bool = True,
    local_scratch: bool = True,
    concurrent: bool = True,
    min_headroom_gb: float = 50.0,
    stop_on_failure: bool = False,
    result_network_root: str = "",
    expected_assets: Optional[List[str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
    execute_fn: Optional[Any] = None,
    poll_interval: float = 2.0,
) -> Dict[str, Any]:
    """Execute end-to-end distributed photogrammetry across two separate shared folders (Multi-SSD).

    Reads raw imagery directly from ``source_share`` (SSD #1, read-only) and writes all
    Metashape `.psx` checkpoints and production deliverables to ``result_share`` (SSD #2,
    acting as Metashape cluster ``--root``).
    """
    from autometashape.cluster.supervisor import ClusterSupervisor

    validation = validate_dual_share_topology(
        source_share=source_share,
        result_share=result_share,
        result_network_root=result_network_root,
        min_headroom_gb=min_headroom_gb,
        require_unc=False,
    )
    for w in validation["warnings"]:
        print(f"  [Dual-Share Warning] {w}", flush=True)
    if not validation["valid"]:
        raise ValueError("Dual-share topology validation failed: " + "; ".join(validation["errors"]))

    if ":" in str(server_host):
        host_part, port_part = str(server_host).rsplit(":", 1)
        server_host = host_part
        try:
            server_port = int(port_part)
        except ValueError:
            pass

    merged_extra: Dict[str, Any] = {
        "network_enable": True,
        "network_host": server_host,
        "network_port": int(server_port),
        "network_independent_workers": True,
        "network_path": validation["result_network_root"],
        "ortho_resolution": ortho_resolution,
        "build_point_cloud": build_point_cloud,
        "build_dem": build_dem,
        "export_obj": export_obj,
        "export_ortho": export_ortho,
        "export_dem": export_dem,
        "export_glb": export_glb,
        "export_report": export_report,
        "resume": resume,
        "local_scratch": local_scratch,
        "local_scratch_staging": local_scratch,
    }
    if extra_args:
        merged_extra.update(extra_args)

    jobs = discover_dual_share_projects(
        source_share=validation["source_share"],
        result_share=validation["result_share"],
        preset=preset,
        expected_assets=expected_assets,
        extra_args=merged_extra,
        result_network_root=validation["result_network_root"],
    )

    if not jobs:
        return {
            "total_jobs": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "jobs": [],
            "results": [],
            "topology": validation,
        }

    supervisor = ClusterSupervisor(
        min_headroom_gb=min_headroom_gb,
        stop_on_failure=stop_on_failure,
        concurrent=concurrent,
    )
    supervisor.queue.extend(jobs)

    if concurrent:
        summary = supervisor.run_concurrent_queue(execute_fn=execute_fn, poll_interval=poll_interval)
    else:
        summary = supervisor.run_queue(execute_fn=execute_fn, concurrent=False)
    summary["topology"] = validation
    return summary
